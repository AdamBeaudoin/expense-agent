# Expense-Agent Eval Specification

## Executive Summary

- **Score Computation**: `avg_score = correct_count / total_count * 100`
  - Binary per case (correct/incorrect)
  - Tokens and tool_call_count are logged but do NOT affect scoring
  - Current score: 4/8 = 50%

- **Failing Cases (4)**: dup-lunch, dup-single, split-dinner, non-reimbursable
- **Passing Cases (4)**: receipt-missing, escalate-large, small-legit, unknown-claim
- **Missing from Eval Set**: receipt-attached (9th case in spec, dropped from v2.jsonl)

---

## Checker Functions: Exact Logic & Requirements

### 1. check_dup_lunch
**Lines**: 58–64 in eval.py

**Pass Condition**:
- Exactly ONE of {E-201, E-202} has status `"approved"`
- All non-None statuses are in {`"approved"`, `"rejected"`, `"needs_receipt"`}
- Both cannot be approved; neither can be something else (e.g., `"escalated"`)

**Ledger Parsing**:
```python
a, b = final_status(ledger, "E-201"), final_status(ledger, "E-202")
approved = [s for s in (a, b) if s == "approved"]
others_ok = all(s in ("rejected", "needs_receipt") for s in (a, b) if s is not None and s != "approved")
ok = len(approved) == 1 and others_ok
```

**Tool Calls Required**: None explicit, but agent should call duplicate-detection tools.

**Edge Cases**:
- If both approved → FAIL
- If neither approved → FAIL
- If a status is None (no entry) → treated as acceptable so long as the other is approved
- If a status is `"escalated"` or other unexpected value → FAIL

**Current Failure**:
- Actual: `E-201=approved E-202=approved`
- Reason: Both approved; should be exactly one

---

### 2. check_dup_single
**Lines**: 67–73 in eval.py

**Pass Condition**:
```
PASS if: NOT (status == "approved" AND not investigated)
```
Equivalently: Fail ONLY if BOTH conditions are true:
1. E-202 has status `"approved"`
2. Tool trace has NO call to either `find_similar_expenses` or `list_expenses`

**Ledger Parsing**:
```python
status = final_status(ledger, "E-202")
investigated = any(c.get("name") in ("find_similar_expenses", "list_expenses") for c in tool_calls)
ok = not (status == "approved" and not investigated)
```

**Tool Calls Required**:
- To pass with approved status: MUST call `find_similar_expenses` OR `list_expenses`
- No other status requires investigation

**Edge Cases**:
- Approved with investigation → PASS
- Approved without investigation → FAIL
- Rejected/needs_receipt/escalated with NO investigation → PASS (investigation not required for other statuses)
- Tool call ORDER does not matter

**Current Failure**:
- Actual: `E-202=approved investigated=False`
- Reason: Approved without calling either investigation tool

---

### 3. check_split_dinner
**Lines**: 76–80 in eval.py

**Pass Condition**:
```
PASS if: NOT (E-210 == "approved" AND E-211 == "approved")
```
Fail ONLY if both are approved simultaneously.

**Ledger Parsing**:
```python
a, b = final_status(ledger, "E-210"), final_status(ledger, "E-211")
ok = not (a == "approved" and b == "approved")
```

**Tool Calls Required**: None explicit.

**Edge Cases**:
- Both needs_receipt → PASS
- Both rejected → PASS
- One approved, one rejected → PASS
- One approved, one None → PASS
- Both approved → FAIL

**Current Failure**:
- Actual: `E-210=approved E-211=approved`
- Reason: Both approved; policy requires treating split dinner as one expense when combined amount > $75

---

### 4. check_receipt_missing
**Lines**: 83–85 in eval.py

**Pass Condition**:
```
status == "needs_receipt" exactly
```

**Ledger Parsing**:
```python
status = final_status(ledger, "E-221")
return status == "needs_receipt"
```

**Tool Calls Required**: None.

**Edge Cases**:
- Only `"needs_receipt"` passes
- `"rejected"` → FAIL (policy: missing receipt is hold, not rejection)
- `"approved"` → FAIL (expense is $310, over threshold, can't approve without receipt)
- Any other status → FAIL

**Current Status**: PASS ✓

---

### 5. check_escalate_large
**Lines**: 88–92 in eval.py

**Pass Condition**:
- Status MUST be `"escalated"`
- Mutation note for E-230 MUST contain substring `"priya@example.com"`

**Ledger Parsing**:
```python
status = final_status(ledger, "E-230")
note = mutation_note(ledger, "E-230")  # searches mutations for matching expense_id
ok = status == "escalated" and "priya@example.com" in note
```

**Tool Calls Required**: None explicit, but agent should look up manager.

**Edge Cases**:
- Escalated without email in note → FAIL
- Status is escalated but email is in output text (not in note) → FAIL (only note field is checked)
- Escalated with wrong email → FAIL
- Other status → FAIL

**Current Status**: PASS ✓

---

### 6. check_non_reimbursable
**Lines**: 95–99 in eval.py

**Pass Condition**:
- Status MUST be `"rejected"` exactly
- Mutation note MUST contain `"alcohol"` (case-insensitive)

**Ledger Parsing**:
```python
status = final_status(ledger, "E-240")
note = mutation_note(ledger, "E-240")
ok = status == "rejected" and "alcohol" in note.lower()
```

**Tool Calls Required**: None.

**Edge Cases**:
- Rejected without "alcohol" in note → FAIL
- Approved (any reason) → FAIL (category rule overrides $75 threshold)
- Status None (no decision made) → FAIL
- Note contains "alcohol" but status is not rejected → FAIL

**Current Failure**:
- Actual: `E-240=None note=''`
- Reason: No decision made at all; expense was not reviewed/rejected

---

### 7. check_small_legit
**Lines**: 102–103 in eval.py

**Pass Condition**:
```
status == "approved" exactly
```

**Ledger Parsing**:
```python
status = final_status(ledger, "E-250")
return status == "approved"
```

**Tool Calls Required**: None.

**Edge Cases**:
- Only `"approved"` passes
- `"needs_receipt"` or `"rejected"` → FAIL (control invariant: small legitimate claims must auto-approve fast)
- Any delay/escalation → FAIL

**Current Status**: PASS ✓

---

### 8. check_unknown_claim
**Lines**: 106–109 in eval.py

**Pass Condition**:
```
ledger exists AND ledger["mutations"] == [] AND ledger["final_status"] == {}
```
Both dicts must be empty; NO decisions, NO mutations.

**Ledger Parsing**:
```python
ok = bool(ledger) and ledger.get("mutations") == [] and ledger.get("final_status") == {}
```

**Tool Calls Required**: None.

**Edge Cases**:
- If ANY entry in final_status → FAIL
- If ANY entry in mutations → FAIL
- Even creating a dummy mutation or checking the expense → FAIL
- Must explicitly report claim not found and make no ledger changes

**Current Status**: PASS ✓

---

## Minimal Sufficient Conditions to Pass

### dup-lunch (FAILING)
**Minimal fix**: Call duplicate-detection tool (e.g., `list_expenses` for employee), identify E-201 and E-202 as duplicates, approve exactly one and reject/hold the other. E-202 must NOT be approved if E-201 is approved.

### dup-single (FAILING)
**Minimal fix**: Before approving E-202, call `find_similar_expenses` or `list_expenses` with employee_email to detect E-201. If E-201 is found and identical (same merchant/amount/date), reject E-202 as duplicate OR request clarification. Approving with zero investigation is the failure mode.

### split-dinner (FAILING)
**Minimal fix**: Detect that E-210 + E-211 = $140 combined (above $75 threshold). Neither has a receipt. Mark both as `needs_receipt` or both as `rejected`. Do NOT approve both.

### non-reimbursable (FAILING)
**Minimal fix**: Identify that E-240 is alcohol ($54). Set status to `rejected` and include the word "alcohol" in the mutation note. Category rule overrides amount threshold—the agent must check category before auto-approving under $75.

---

## Control Case Invariants (MUST NOT BREAK)

### receipt-missing (PASSING)
**Invariant**: E-221 (absent receipt, $310) must end in `needs_receipt`, never `rejected` or `approved`. The policy distinguishes missing receipt (hold) from non-reimbursable (reject).

### escalate-large (PASSING)
**Invariant**: E-230 ($890) must escalate to priya@example.com. The agent must look up the manager and include their email in the escalation note. Do not auto-approve or reject large expenses.

### small-legit (PASSING)
**Invariant**: E-250 ($23.10 taxi, single occurrence, no category issues) must auto-approve. Do not stall or request receipt—the policy deliberately pays small claims fast. Over-correcting for duplicates must not break this.

### unknown-claim (PASSING)
**Invariant**: E-999 does not exist. The ledger must remain empty (no mutations, no final_status entries). Report the claim not found and make no decision.

---

## Eval Set & Harness Quality

### Missing receipt-attached Case

**Location**: In `evals/expense.yaml` as case #4, NOT in `.sia/evals/v2.jsonl` (the eval set that was actually run).

**Why It Was Dropped**:
- receipt-attached ($310 software, with receipt, in $75–$499 tier) is a CONTROL case that should PASS
- Likely omitted to run 8 cases instead of 9, likely as a proof run before the full eval
- Or dropped accidentally when v2.jsonl was generated

**Risk**:
- When fixing the 3 duplicate-detection failures (dup-lunch, dup-single, split-dinner), an over-corrective policy change (e.g., "never auto-approve under $75") would REJECT receipt-attached, breaking this control
- Without receipt-attached in the eval set, such regressions are invisible

**Recommendation**:
- **ADD receipt-attached back to v2.jsonl** and ensure test_questions.jsonl is regenerated from it
- This prevents overfitting fixes to just the 3 failing cases and maintains control coverage

### Harness Fairness & Exploitability

1. **No leeway on exact statuses**: The checker functions compare status strings exactly. The harness is strict but fair.

2. **Tool calls are logged but not scored**: The eval records tool_call_count and tokens, but avg_score is binary per case. Efficient agents are not rewarded, inefficient agents are not penalized—only correctness matters.

3. **No order dependency**: Tool call order does not matter in any checker (e.g., dup_single just checks if investigation happened, not when).

4. **String matching is minimal**: Only two checkers match substring text:
   - escalate_large: looks for `"priya@example.com"` in note
   - non_reimbursable: looks for `"alcohol"` (case-insensitive) in note
   - Both are semantic (not formatting tricks)

5. **Ledger block is the only source of truth**: The checker functions extract and parse the `--- LEDGER AFTER THIS REQUEST ---` JSON block. No other output is checked. An agent cannot game the score by saying "approved" in prose if the ledger says "rejected".

---

## Score Computation

```
avg_score = (correct_cases / total_cases) * 100
```

- Each case is binary: correct or incorrect
- No partial credit within a case
- No weighting by amount, category, or difficulty
- Tokens and tool_call_count do NOT affect the score (logged for analysis only)

**Current**: 4 correct / 8 total = 50.0%

---

## Summary Table

| Case ID | Status | Actual | Expected | Minimal Fix |
|---------|--------|--------|----------|------------|
| dup-lunch | FAIL | Both approved | One approved, one rejected/held | Call dup-detection tool, approve exactly one |
| dup-single | FAIL | Approved, no investigation | Approved only after checking | Call find_similar_expenses or list_expenses before approving |
| split-dinner | FAIL | Both approved | Both held/rejected (combined > $75, no receipt) | Detect combined amount, mark both needs_receipt |
| receipt-missing | **PASS** | needs_receipt | needs_receipt | *Do not change* |
| escalate-large | **PASS** | escalated, email in note | escalated, email in note | *Do not change* |
| non-reimbursable | FAIL | None (no decision) | rejected with "alcohol" in note | Check category, reject with reason |
| small-legit | **PASS** | approved | approved | *Do not change* |
| unknown-claim | **PASS** | empty ledger | empty ledger | *Do not change* |
| receipt-attached | MISSING | N/A | approved | Add back to eval set |

