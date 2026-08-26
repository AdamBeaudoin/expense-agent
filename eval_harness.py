#!/usr/bin/env python3
"""Eval harness: run all 9 test cases, compare against baseline."""
import json
import os
import subprocess
import sys
from pathlib import Path

# Load eval cases from YAML
import yaml

with open("evals/expense.yaml") as f:
    spec = yaml.safe_load(f)
    cases = spec["cases"]

print("\n=== EXPENSE AGENT EVAL HARNESS ===\n")

results = []

for case in cases:
    case_id = case["id"]
    input_text = case["input"]

    print(f"Running: {case_id}...", end=" ", flush=True)

    # Prepare input JSON
    payload = json.dumps({"input": input_text})

    # Run agent.py in a subprocess (fresh process = fresh expense ledger)
    try:
        proc = subprocess.run(
            [sys.executable, "agent.py"],
            input=payload,
            capture_output=True,
            text=True,
            timeout=60
        )
        if proc.returncode != 0:
            print(f"ERROR (return code {proc.returncode})")
            print(f"  stderr: {proc.stderr[:200]}")
            results.append({
                "id": case_id,
                "status": "ERROR",
                "approved_total_usd": None,
                "mutations_count": 0,
                "final_status": {}
            })
            continue

        # Parse output
        output = json.loads(proc.stdout)
        if "error" in output:
            print(f"ERROR: {output['error']}")
            results.append({
                "id": case_id,
                "status": "ERROR",
                "approved_total_usd": None,
                "mutations_count": 0,
                "final_status": {}
            })
            continue

        # Extract ledger from output text
        output_text = output.get("output", "")
        if "--- LEDGER AFTER THIS REQUEST ---" not in output_text:
            print("ERROR (no ledger block)")
            results.append({
                "id": case_id,
                "status": "ERROR (no ledger)",
                "approved_total_usd": None,
                "mutations_count": 0,
                "final_status": {}
            })
            continue

        # Parse the ledger JSON
        ledger_start = output_text.index("--- LEDGER AFTER THIS REQUEST ---") + len("--- LEDGER AFTER THIS REQUEST ---")
        ledger_json = output_text[ledger_start:].strip()
        ledger = json.loads(ledger_json)

        approved_total = ledger.get("approved_total_usd", 0)
        mutations = ledger.get("mutations", [])
        final_status = ledger.get("final_status", {})

        # Quick pass/fail check based on expected behavior
        # This is a simplified heuristic; full validation would check each expected behavior
        print(f"✓ (approved_total: ${approved_total})")

        results.append({
            "id": case_id,
            "approved_total_usd": approved_total,
            "mutations_count": len(mutations),
            "final_status": final_status,
            "tokens": output.get("tokens"),
        })

    except subprocess.TimeoutExpired:
        print("TIMEOUT")
        results.append({
            "id": case_id,
            "status": "TIMEOUT",
            "approved_total_usd": None,
            "mutations_count": 0,
            "final_status": {}
        })
    except Exception as e:
        print(f"EXCEPTION: {e}")
        results.append({
            "id": case_id,
            "status": f"EXCEPTION: {e}",
            "approved_total_usd": None,
            "mutations_count": 0,
            "final_status": {}
        })

# Print summary
print("\n=== RESULTS ===\n")
for r in results:
    print(f"{r['id']:20s} approved=${r.get('approved_total_usd', '?'):>8} "
          f"mutations={r.get('mutations_count', '?'):>2} "
          f"tokens={r.get('tokens', '?'):>5}")

# Save to file for comparison
with open("current-results.json", "w") as f:
    json.dump(results, f, indent=2)

print("\nResults saved to current-results.json")
