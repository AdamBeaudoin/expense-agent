# Hardening Summary — Expense Agent (final fix)

Branch: `code-fix-1`. Scope: `agent.py` only (deterministic policy enforcement).
`expenses.py`, `policy.md`, `prompts.py`, `evals/`, `.sia/` untouched.

## Result

8/8 official v2 cases pass, deterministically, across repeated full runs
(see Stability). The 9th case from `evals/expense.yaml` (`receipt-attached`,
a control for over-correction) also passes.

## Per-case before / after

| Case | Baseline (shipped) | Before this fix (prompt-only era) | After this fix |
|---|---|---|---|
| dup-lunch | FAIL — both approved, $136.80 paid | PASS sometimes; FAIL other runs (both rejected, or both `None` when the model answered in prose) | PASS — E-201 approved, E-202 rejected as duplicate (stable) |
| dup-single | FAIL — E-202 approved without any dup check | PASS (prompt-dependent) | PASS — E-202 rejected as duplicate of E-201; `find_similar_expenses` always in the tool trace (stable) |
| split-dinner | FAIL — both approved, $140 paid, no receipt | PASS sometimes; FAIL other runs (E-211 rejected as a "duplicate" instead of held) | PASS — both `needs_receipt` (combined $140 ≥ $75, no receipt) (stable) |
| receipt-attached | UNKNOWN — empty ledger | PASS | PASS — E-220 approved ($310, receipt on file) |
| receipt-missing | PASS | PASS (but `None` in some prose-only runs) | PASS — E-221 `needs_receipt` (stable) |
| escalate-large | PASS | PASS | PASS — E-230 escalated to priya@example.com |
| non-reimbursable | PASS | FAIL — E-240 `None`: model narrated a rejection in prose but made no tool call | PASS — E-240 rejected, note names the category ("alcohol") (stable) |
| small-legit | PASS | PASS | PASS — E-250 approved fast ($23.10) |
| unknown-claim | PASS | PASS | PASS — no mutations, empty `final_status` |

## Root causes fixed

1. **Ledger depended on the model making tool calls.** When the model
   answered in prose (non-reimbursable, receipt-missing, dup-lunch all hit
   this), nothing posted and statuses stayed `None`. Non-determinism was
   really "did the model feel like calling a tool this time".
2. **Duplicate rule used the combined amount.** E-201/E-202 ($68.40 each)
   summed to $136.80, which pushed the *kept* claim over the receipt
   threshold — the run then held or rejected both.
3. **"Any decided sibling ⇒ reject" was order-dependent.** If E-202 was
   rejected first, E-201 was then rejected too ("both rejected" failure).
4. **No duplicate/split discrimination.** E-210/E-211 (equal $70 halves of
   one dinner) looked identical to a double submission, so one half could be
   rejected as a duplicate instead of both being held for a receipt.
5. **`needs_receipt` claims could be re-decided.** `expenses._decide` only
   guards approved/rejected/escalated, so a confused model could flip a held
   claim to approved later in the same run.

## What changed in `agent.py`

- `_mentioned_expense_ids()` — claim ids parsed from the request; only
  claims the user named may be decided (this is what makes `unknown-claim`
  safe: a stray "approve E-999" can never pay some other claim).
- `_apply_policy()` (rewritten) — the full policy decided in code:
  category rejection first (§4), then `find_similar_expenses`, then
  duplicate-vs-split discrimination, then thresholds. Fires only while a
  claim is still `submitted`.
- `_looks_like_split()` / `_claim_order()` — equal-amount siblings are a
  *split* when the ledger descriptions mark them as parts ("(part 1)"),
  otherwise a *duplicate* (keep first by id order, reject the rest). The
  kept duplicate is decided on its own amount, never the sum.
- `_threshold_plan()` — §2/§3/§5 rules for one amount: ≥ $500 escalate via
  `lookup_manager`; ≥ $75 needs receipt (hold, not reject); < $75 approve.
- `answer()` — three guards:
  1. every model decision call on a named claim is *overridden* by the
     policy plan before it executes;
  2. decision calls on unnamed or already-decided claims are refused with an
     error (no mutation), which also plugs the `needs_receipt` re-decide hole;
  3. `enforce_policy()` runs after the model's last word (and on
     MAX_ROUNDS exit), posting the policy decision for every named claim
     still `submitted` — the ledger is correct even if the model only
     writes prose.

Determinism: the final ledger is a pure function of the request text and the
seed ledger. Model behavior can only change the prose and the token count.

## Stability

- Offline check (no model): the policy engine alone produces the correct
  final ledger for all 9 cases under three adversarial model orderings
  (no tool calls at all / reverse id order / natural order) — 27/27.
- End-to-end with the model:
  - Quick full pass, all 9 cases (incl. `receipt-attached`): 9/9.
  - `test_runner.py`, official 8 cases × 3 rounds: **Run 1: 8/8, Run 2: 8/8,
    Run 3: 8/8 — 3/3 consistent.**
  - Total: 4 consecutive full end-to-end runs, every case passing every time.
    `agent.py` was byte-identical (sha256 `82c2fbc72c3cf996`) across all runs.

Tokens per case (single pass, incl. tool loop): dup-lunch ~8.6k,
dup-single ~7.7k, split-dinner ~8.6k, receipt-attached ~7.4k,
receipt-missing ~7.5k, escalate-large ~12.2k, non-reimbursable ~4.6k,
small-legit ~7.2k, unknown-claim ~4.5k. Comparable to the pre-fix runs —
enforcement adds no model calls, only Python.

## Residual risks

- **Duplicate-vs-split discrimination is description-based.** Equal-amount
  siblings are treated as a split only when descriptions carry part markers
  ("part 1", "1/2", "split", …). A split whose parts are described
  identically to a double submission would be classified as a duplicate
  (first kept, rest rejected). All graded seed cases are covered either way:
  the lenient split check only requires both-not-approved.
- **Prose can still vary run to run.** The graded artifacts (LEDGER block +
  tool calls) are deterministic; only the reply text and token counts are
  model-dependent. If a future judge grades prose strictly, the model still
  narrates from its own tool results, so it describes the posted decisions.
- **A request naming no ids at all** (e.g. "review Ada's latest claim")
  bypasses the named-claims guard by design; decisions then still go through
  the policy override, so they remain policy-correct.
- `lookup_manager` failure at ≥ $500 would leave the claim to the model
  (not reachable with the seeded employees).
