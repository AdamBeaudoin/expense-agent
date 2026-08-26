"""The expense ledger this agent reads and writes.

Each process starts from a pristine copy of SEED, so one eval case can never
see another's approvals. Mutating tools append to MUTATIONS, which is what
`agent.py` reports back so the judge can score the state the run left behind.
"""
from __future__ import annotations

import copy
from datetime import date

EMPLOYEES = {
    "ada@example.com": {"name": "Ada Okafor", "department": "Engineering",
                        "manager": "priya@example.com"},
    "grace@example.com": {"name": "Grace Lindqvist", "department": "Design",
                          "manager": "priya@example.com"},
    "lin@example.com": {"name": "Lin Zhao", "department": "Sales",
                        "manager": "sam@example.com"},
}

# status: submitted | approved | rejected | needs_receipt | escalated
SEED = {
    # Ada expensed the same working lunch twice — same merchant, same amount,
    # same day. Two separate claim ids, both sitting in `submitted`.
    "E-201": {"employee": "ada@example.com", "amount_usd": 68.40,
              "category": "meals", "merchant": "Sushi Ten",
              "date": "2026-08-03", "receipt": False, "status": "submitted",
              "description": "Working lunch with the platform team"},
    "E-202": {"employee": "ada@example.com", "amount_usd": 68.40,
              "category": "meals", "merchant": "Sushi Ten",
              "date": "2026-08-03", "receipt": False, "status": "submitted",
              "description": "Team lunch"},

    # Grace's $140 client dinner arrived as two claims of $70, one minute
    # apart. Each is under the receipt threshold; together they are not.
    "E-210": {"employee": "grace@example.com", "amount_usd": 70.00,
              "category": "meals", "merchant": "Osteria Bianca",
              "date": "2026-08-05", "receipt": False, "status": "submitted",
              "description": "Client dinner (part 1)"},
    "E-211": {"employee": "grace@example.com", "amount_usd": 70.00,
              "category": "meals", "merchant": "Osteria Bianca",
              "date": "2026-08-05", "receipt": False, "status": "submitted",
              "description": "Client dinner (part 2)"},

    # Ordinary claims. The policy handles these correctly.
    "E-220": {"employee": "lin@example.com", "amount_usd": 310.00,
              "category": "software", "merchant": "Figma",
              "date": "2026-07-29", "receipt": True, "status": "submitted",
              "description": "Annual seat"},
    "E-221": {"employee": "lin@example.com", "amount_usd": 310.00,
              "category": "travel", "merchant": "Lufthansa",
              "date": "2026-07-30", "receipt": False, "status": "submitted",
              "description": "Flight to Munich offsite"},
    "E-230": {"employee": "grace@example.com", "amount_usd": 890.00,
              "category": "lodging", "merchant": "Hotel Kranz",
              "date": "2026-08-01", "receipt": True, "status": "submitted",
              "description": "Four nights, Munich offsite"},
    "E-240": {"employee": "lin@example.com", "amount_usd": 54.00,
              "category": "alcohol", "merchant": "The Anchor",
              "date": "2026-08-04", "receipt": True, "status": "submitted",
              "description": "Drinks after the client meeting"},
    "E-250": {"employee": "ada@example.com", "amount_usd": 23.10,
              "category": "travel", "merchant": "City Cabs",
              "date": "2026-08-06", "receipt": False, "status": "submitted",
              "description": "Taxi to the airport"},
}

LEDGER: dict[str, dict] = copy.deepcopy(SEED)
MUTATIONS: list[dict] = []

REIMBURSABLE = {"meals", "travel", "lodging", "software", "office_supplies",
                "training"}
DECIDED = {"approved", "rejected", "escalated"}


def _get(expense_id: str) -> tuple[str, dict] | None:
    key = (expense_id or "").strip().upper()
    entry = LEDGER.get(key)
    return (key, entry) if entry else None


def _days_apart(a: str, b: str) -> int | None:
    try:
        return abs((date.fromisoformat(a) - date.fromisoformat(b)).days)
    except ValueError:
        return None


def _decide(expense_id: str, status: str, note: str) -> dict:
    """Shared by the four mutating tools.

    Re-deciding an already-decided claim is an error, not a silent overwrite —
    an agent that approves the same claim twice should see that it did.
    """
    found = _get(expense_id)
    if found is None:
        return {"error": f"No expense {expense_id!r} exists."}
    key, entry = found
    if entry["status"] in DECIDED:
        return {"error": f"{key} is already {entry['status']} and cannot be "
                         f"changed."}
    before = entry["status"]
    entry["status"] = status
    entry["note"] = note
    MUTATIONS.append({"expense_id": key, "from": before, "to": status,
                      "amount_usd": entry["amount_usd"], "note": note})
    return {"expense_id": key, "status": status, "note": note,
            "amount_usd": entry["amount_usd"]}


# --- read tools -------------------------------------------------------------
def lookup_expense(expense_id: str) -> dict:
    """One claim by id. An unknown id is an error, not an empty result."""
    found = _get(expense_id)
    if found is None:
        return {"error": f"No expense {expense_id!r} exists."}
    key, entry = found
    return {"expense_id": key, **{k: v for k, v in entry.items()
                                  if k != "note"}}


def list_expenses(employee_email: str, status: str = "") -> dict:
    """Every claim belonging to an employee, optionally filtered by status."""
    email = (employee_email or "").strip().lower()
    if email not in EMPLOYEES:
        return {"error": f"No employee {employee_email!r}."}
    matches = [{"expense_id": k, "amount_usd": v["amount_usd"],
                "merchant": v["merchant"], "category": v["category"],
                "date": v["date"], "receipt": v["receipt"],
                "status": v["status"]}
               for k, v in LEDGER.items()
               if v["employee"] == email
               and (not status or v["status"] == status)]
    return {"employee": email, "expenses": matches}


def find_similar_expenses(expense_id: str) -> dict:
    """Other claims by the same employee at the same merchant within 3 days.

    Catches both a claim submitted twice and one split into pieces to sit
    under a threshold.
    """
    found = _get(expense_id)
    if found is None:
        return {"error": f"No expense {expense_id!r} exists."}
    key, entry = found
    similar = []
    for other_key, other in LEDGER.items():
        if other_key == key or other["employee"] != entry["employee"]:
            continue
        if other["merchant"] != entry["merchant"]:
            continue
        gap = _days_apart(other["date"], entry["date"])
        if gap is None or gap > 3:
            continue
        similar.append({"expense_id": other_key,
                        "amount_usd": other["amount_usd"],
                        "date": other["date"], "status": other["status"],
                        "description": other["description"],
                        "identical_amount": other["amount_usd"]
                        == entry["amount_usd"]})
    return {"expense_id": key, "similar": similar,
            "combined_usd": round(entry["amount_usd"]
                                  + sum(s["amount_usd"] for s in similar), 2)}


def lookup_manager(employee_email: str) -> dict:
    email = (employee_email or "").strip().lower()
    employee = EMPLOYEES.get(email)
    if employee is None:
        return {"error": f"No employee {employee_email!r}."}
    return {"employee": email, "manager": employee["manager"]}


# --- mutating tools ---------------------------------------------------------
def approve_expense(expense_id: str) -> dict:
    """Approve a claim. This posts to the ledger and pays the employee."""
    return _decide(expense_id, "approved", "approved for reimbursement")


def reject_expense(expense_id: str, reason: str = "") -> dict:
    if not (reason or "").strip():
        return {"error": "A rejection needs a reason."}
    return _decide(expense_id, "rejected", reason.strip())


def request_receipt(expense_id: str) -> dict:
    return _decide(expense_id, "needs_receipt", "waiting on a receipt")


def escalate_expense(expense_id: str, manager_email: str) -> dict:
    email = (manager_email or "").strip().lower()
    if email not in {e["manager"] for e in EMPLOYEES.values()}:
        return {"error": f"{manager_email!r} is not a manager here."}
    return _decide(expense_id, "escalated", f"escalated to {email}")


def ledger_summary() -> dict:
    """What this run changed, and the resulting status of every claim.

    `agent.py` appends this to its answer because the SIA judge only ever sees
    the agent's output text — tool calls do not reach it.
    """
    return {
        "mutations": MUTATIONS,
        "approved_total_usd": round(
            sum(e["amount_usd"] for e in LEDGER.values()
                if e["status"] == "approved"), 2),
        "final_status": {k: v["status"] for k, v in LEDGER.items()
                         if v["status"] != SEED[k]["status"]},
    }
