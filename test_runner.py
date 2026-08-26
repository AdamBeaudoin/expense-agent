#!/usr/bin/env python3
import json
import subprocess
import sys
import os
import re

os.chdir("/Users/adam/Hackathons/Hexolabs/expense-agent")

LEDGER_RE = re.compile(r"--- LEDGER AFTER THIS REQUEST ---\n(\{.*\})", re.S)

def extract_ledger(output_text):
    m = LEDGER_RE.search(output_text or "")
    if not m:
        return None
    return json.loads(m.group(1))

def final_status(ledger, expense_id):
    if not ledger:
        return None
    return ledger.get("final_status", {}).get(expense_id)

def mutation_note(ledger, expense_id):
    for mut in (ledger or {}).get("mutations", []):
        if mut.get("expense_id") == expense_id:
            return mut.get("note", "") or ""
    return ""

# Load check functions
def check_dup_lunch(ledger, tool_calls, output_text):
    a, b = final_status(ledger, "E-201"), final_status(ledger, "E-202")
    approved = [s for s in (a, b) if s == "approved"]
    others_ok = all(s in ("rejected", "needs_receipt")
                     for s in (a, b) if s is not None and s != "approved")
    ok = len(approved) == 1 and others_ok
    return ok, f"E-201={a} E-202={b}"

def check_dup_single(ledger, tool_calls, output_text):
    status = final_status(ledger, "E-202")
    investigated = any(c.get("name") in ("find_similar_expenses", "list_expenses")
                        for c in tool_calls)
    ok = not (status == "approved" and not investigated)
    return ok, f"E-202={status} investigated={investigated}"

def check_split_dinner(ledger, tool_calls, output_text):
    a, b = final_status(ledger, "E-210"), final_status(ledger, "E-211")
    ok = not (a == "approved" and b == "approved")
    return ok, f"E-210={a} E-211={b}"

def check_receipt_missing(ledger, tool_calls, output_text):
    status = final_status(ledger, "E-221")
    return status == "needs_receipt", f"E-221={status}"

def check_escalate_large(ledger, tool_calls, output_text):
    status = final_status(ledger, "E-230")
    note = mutation_note(ledger, "E-230")
    ok = status == "escalated" and "priya@example.com" in note
    return ok, f"E-230={status} note={note!r}"

def check_non_reimbursable(ledger, tool_calls, output_text):
    status = final_status(ledger, "E-240")
    note = mutation_note(ledger, "E-240")
    ok = status == "rejected" and "alcohol" in note.lower()
    return ok, f"E-240={status} note={note!r}"

def check_small_legit(ledger, tool_calls, output_text):
    status = final_status(ledger, "E-250")
    return status == "approved", f"E-250={status}"

def check_unknown_claim(ledger, tool_calls, output_text):
    ok = bool(ledger) and ledger.get("mutations") == [] and ledger.get("final_status") == {}
    return ok, f"mutations={(ledger or {}).get('mutations')} final_status={(ledger or {}).get('final_status')}"

CHECKS = {
    "dup_lunch": check_dup_lunch,
    "dup_single": check_dup_single,
    "split_dinner": check_split_dinner,
    "receipt_missing": check_receipt_missing,
    "escalate_large": check_escalate_large,
    "non_reimbursable": check_non_reimbursable,
    "small_legit": check_small_legit,
    "unknown_claim": check_unknown_claim,
}

def run_case(qid, input_text, check_name):
    # Use .venv/bin/python
    cmd = "set -a; . ./.env; set +a; .venv/bin/python agent.py"
    proc = subprocess.run(
        ["bash", "-c", cmd],
        input=json.dumps({"input": input_text}),
        capture_output=True, text=True, timeout=180,
        cwd="/Users/adam/Hackathons/Hexolabs/expense-agent"
    )
    if proc.returncode != 0:
        return False, f"exit {proc.returncode}"
    try:
        response = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        return False, f"Bad JSON output"

    output_text = response.get("output") or ""
    ledger = extract_ledger(output_text)
    tool_calls = response.get("tool_calls") or []
    checker = CHECKS.get(check_name)
    if checker is None:
        return False, f"no checker for {check_name}"
    correct, reason = checker(ledger, tool_calls, output_text)
    return correct, reason

if __name__ == "__main__":
    cases = [
        ("dup-lunch", "Ada submitted E-201 and E-202, please review both.", "dup_lunch"),
        ("dup-single", "Please review E-202 for Ada.", "dup_single"),
        ("split-dinner", "Grace filed E-210 and E-211 for a client dinner. Please review them.", "split_dinner"),
        ("receipt-missing", "Review E-221 for Lin.", "receipt_missing"),
        ("escalate-large", "Grace's hotel bill E-230 came in. What happens to it?", "escalate_large"),
        ("non-reimbursable", "Review E-240 for Lin.", "non_reimbursable"),
        ("small-legit", "Ada expensed a $23.10 airport taxi, E-250. Can you handle it?", "small_legit"),
        ("unknown-claim", "Please approve E-999.", "unknown_claim"),
    ]

    all_results = []
    for run_num in range(3):
        print(f"\n=== RUN {run_num + 1} ===")
        results = {}
        for qid, input_text, check_name in cases:
            correct, reason = run_case(qid, input_text, check_name)
            results[qid] = correct
            status = "PASS" if correct else "FAIL"
            print(f"{qid}: {status} -- {reason}")
        all_results.append(results)
        passed = sum(1 for v in results.values() if v)
        print(f"Score: {passed}/{len(cases)}")

    print(f"\n=== SUMMARY ===")
    for run_num, results in enumerate(all_results):
        passed = sum(1 for v in results.values() if v)
        print(f"Run {run_num + 1}: {passed}/{len(cases)}")
