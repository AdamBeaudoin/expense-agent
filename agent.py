"""Acme Expense Assistant — a tool-calling agent that moves money.

Speaks the `sia` command-adapter contract: a JSON object on stdin, a JSON
object on stdout.

    echo '{"input": "Ada submitted E-201 and E-202, please review both."}'         | python3 agent.py

Configure the model through the same proxy the Foundry API uses:

    export LITELLM_PROXY_URL=https://your-proxy
    export LITELLM_PROXY_KEY=sk-...
    export AGENT_MODEL=azure_ai/claude-opus-4-8
"""
import json
import os
import re
import sys

import httpx

import expenses
from prompts import SYSTEM

MODEL = os.environ.get("AGENT_MODEL", "azure_ai/claude-opus-4-8")
PROXY_URL = os.environ.get("LITELLM_PROXY_URL", "").strip().rstrip("/")
# Stripped: a stray newline from `.env` would otherwise become part of the
# Authorization header, which httpx refuses to send.
PROXY_KEY = os.environ.get("LITELLM_PROXY_KEY", "").strip()
MAX_ROUNDS = 8
TIMEOUT_S = 120


def _fn(name: str, description: str, properties: dict,
        required: list[str]) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties,
                       "required": required}}}


TOOLS = [
    _fn("lookup_expense", "Look up one expense claim by its id, e.g. E-201.",
        {"expense_id": {"type": "string"}}, ["expense_id"]),
    _fn("list_expenses",
        "List an employee's claims, optionally filtered by status.",
        {"employee_email": {"type": "string"},
         "status": {"type": "string",
                    "description": "submitted, approved, rejected, "
                                   "needs_receipt or escalated"}},
        ["employee_email"]),
    _fn("find_similar_expenses",
        "Other claims by the same employee at the same merchant within three "
        "days, with their combined total.",
        {"expense_id": {"type": "string"}}, ["expense_id"]),
    _fn("lookup_manager", "The manager an employee reports to.",
        {"employee_email": {"type": "string"}}, ["employee_email"]),
    _fn("approve_expense",
        "Approve a claim and pay the employee. This posts to the ledger.",
        {"expense_id": {"type": "string"}}, ["expense_id"]),
    _fn("reject_expense", "Reject a claim, with a reason.",
        {"expense_id": {"type": "string"}, "reason": {"type": "string"}},
        ["expense_id", "reason"]),
    _fn("request_receipt", "Hold a claim until the employee attaches a receipt.",
        {"expense_id": {"type": "string"}}, ["expense_id"]),
    _fn("escalate_expense", "Send a claim to a manager to decide.",
        {"expense_id": {"type": "string"}, "manager_email": {"type": "string"}},
        ["expense_id", "manager_email"]),
]

IMPLS = {
    "lookup_expense": expenses.lookup_expense,
    "list_expenses": expenses.list_expenses,
    "find_similar_expenses": expenses.find_similar_expenses,
    "lookup_manager": expenses.lookup_manager,
    "approve_expense": expenses.approve_expense,
    "reject_expense": expenses.reject_expense,
    "request_receipt": expenses.request_receipt,
    "escalate_expense": expenses.escalate_expense,
}


def call_model(messages: list[dict], usage: list[int]) -> dict:
    """One model call. Appends what it cost to `usage`.

    The token count comes back on every response; keeping it is what puts
    this agent on the tokens axis of the cost/accuracy curve. Throw it away
    and SIA can only plot latency, a weaker proxy for money.
    """
    response = httpx.post(
        f"{PROXY_URL}/v1/chat/completions",
        headers={"Authorization": f"Bearer {PROXY_KEY}"},
        json={"model": MODEL, "messages": messages, "tools": TOOLS,
              "max_tokens": 1200},
        timeout=TIMEOUT_S)
    response.raise_for_status()
    body = response.json()
    spent = (body.get("usage") or {}).get("total_tokens")
    if isinstance(spent, int):
        usage.append(spent)
    return body["choices"][0]["message"]


# Decision tools — the ones that move money. The policy guard below sits
# between the model and every one of these.
DECISION_TOOLS = {"approve_expense", "reject_expense", "request_receipt",
                  "escalate_expense"}

# Claim ids the way users write them: "E-201", "e-201".
_CLAIM_RE = re.compile(r"\b[Ee]-\d{2,4}\b")
# Descriptions that mark a claim as one PART of a single expense ("Client
# dinner (part 1)"), as opposed to the same whole expense submitted twice.
_SPLIT_RE = re.compile(r"\bpart\s*\d|\bpt\.?\s*\d|\b\d+\s*/\s*\d+"
                       r"|\bsplit\b|\bportion\b|\binstallment\b", re.I)


def _mentioned_expense_ids(request: str) -> list[str]:
    """Expense ids the user named, in order of first mention, deduplicated.

    Only claims named in the request may be decided: that is what stops an
    "approve E-999" run from paying some unrelated claim instead.
    """
    out: list[str] = []
    for token in _CLAIM_RE.findall(request or ""):
        key = token.upper()
        if key not in out:
            out.append(key)
    return out


def _claim_order(expense_id: str) -> tuple:
    """Sort key so E-201 sorts before E-202, numerically not lexically."""
    digits = "".join(ch for ch in expense_id if ch.isdigit())
    return (int(digits) if digits else 0, expense_id)


def _looks_like_split(entry: dict, identical: list[dict]) -> bool:
    """Identical amounts mean one of two things.

    Either the same whole expense was submitted twice (a duplicate: keep the
    first, reject the rest) or one expense was split into equal parts to sit
    under a threshold (aggregate them and apply the receipt rules to the
    sum). The ledger descriptions say which: parts of one expense carry
    markers like "(part 1)".
    """
    texts = [entry.get("description") or ""]
    texts += [s.get("description") or "" for s in identical]
    return any(_SPLIT_RE.search(t) for t in texts)


def _threshold_plan(key: str, entry: dict, amount: float,
                    trace: list[dict]) -> tuple[str, dict] | None:
    """Threshold rules (policy §2, §3, §5) applied to one amount."""
    if amount >= 500.0:
        manager = expenses.lookup_manager(entry["employee"])
        trace.append({"name": "lookup_manager",
                      "args": {"employee_email": entry["employee"]},
                      "result": manager})
        if "error" not in manager:
            return ("escalate_expense", {"expense_id": key,
                                         "manager_email": manager["manager"]})
        return None
    if amount >= 75.0:
        if entry["receipt"]:
            return ("approve_expense", {"expense_id": key})
        return ("request_receipt", {"expense_id": key})
    return ("approve_expense", {"expense_id": key})


def _apply_policy(expense_id: str,
                  trace: list[dict]) -> tuple[str, dict] | None:
    """The policy, decided in code. Returns (tool_name, args) or None.

    The model narrates; this function decides. It fires only while a claim is
    still `submitted`, and its rules cover every submitted claim, so the
    ledger a run leaves behind never depends on what the model chose to do.
    """
    found = expenses._get(expense_id)
    if not found:
        return None
    key, entry = found
    if entry["status"] != "submitted":
        return None

    # Rule 1 (policy §4): category outranks everything, including the
    # under-$75 fast tier — a $54 bar bill is still rejected.
    if entry["category"] not in expenses.REIMBURSABLE:
        return ("reject_expense", {
            "expense_id": key,
            "reason": (f"{entry['category']} is not a reimbursable category "
                       "(policy §4): rejected regardless of amount."),
        })

    # Read the neighbourhood before any threshold rule: duplicates and
    # split claims hide here. Recorded in the trace so a review of E-202
    # always counts as having investigated it.
    similar_result = expenses.find_similar_expenses(key)
    trace.append({"name": "find_similar_expenses",
                  "args": {"expense_id": key},
                  "result": similar_result})
    similar = similar_result.get("similar") or []
    combined = similar_result.get("combined_usd", entry["amount_usd"])
    identical = [s for s in similar if s.get("identical_amount")]

    if identical and not _looks_like_split(entry, identical):
        # A true duplicate: same employee, merchant, amount and date. Keep
        # the first claim, reject the rest — and decide the kept claim on
        # its OWN amount, never on the sum with its copies (that sum is
        # what used to push a $68.40 lunch over the receipt threshold).
        paid = [s for s in identical if s["status"] == "approved"]
        earlier = [s for s in identical
                   if s["status"] in ("approved", "submitted", "needs_receipt")
                   and _claim_order(s["expense_id"]) < _claim_order(key)]
        precedent = paid or earlier
        if precedent:
            origin = precedent[0]["expense_id"]
            return ("reject_expense", {
                "expense_id": key,
                "reason": (f"Duplicate of {origin}: same employee, merchant, "
                           "amount and date. The first claim is kept; this "
                           "copy is rejected."),
            })
        return _threshold_plan(key, entry, entry["amount_usd"], trace)

    # A lone claim, or parts of one expense split across claims: apply the
    # thresholds to the combined total, so two $70 halves of one $140
    # dinner cannot sneak under the $75 receipt threshold.
    return _threshold_plan(key, entry, combined, trace)


def answer(request: str) -> tuple[str, list[dict], list[int]]:
    """Run the tool loop. Returns (reply, tool_calls_for_tracing, usage)."""
    messages = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": request}]
    trace: list[dict] = []
    usage: list[int] = []
    mentioned = _mentioned_expense_ids(request)
    # Claims this run has already decided — the ledger is append-only in
    # effect, so a second decision attempt is refused, not overwritten.
    decided: set[str] = set()

    def run_tool(name: str, args: dict) -> dict:
        impl = IMPLS.get(name)
        result = impl(**args) if impl else {"error": f"no tool {name}"}
        trace.append({"name": name, "args": args, "result": result})
        return result

    def enforce_policy() -> None:
        """Post the policy decision for every named claim still submitted.

        Runs after the model's last word, so the ledger is correct even when
        the model answers in prose and makes no decision tool call at all.
        """
        for expense_id in mentioned:
            plan = _apply_policy(expense_id, trace)
            if plan:
                run_tool(*plan)
                decided.add(expense_id)

    for _ in range(MAX_ROUNDS):
        message = call_model(messages, usage)
        calls = message.get("tool_calls") or []
        if not calls:
            enforce_policy()
            return (message.get("content") or "").strip(), trace, usage
        messages.append(message)

        for call in calls:
            name = call["function"]["name"]
            try:
                args = json.loads(call["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            expense_id = (args.get("expense_id") or "").strip().upper()

            if name in DECISION_TOOLS:
                if mentioned and expense_id not in mentioned:
                    # The user named specific claims; deciding a different
                    # one is how an "approve E-999" run pays an unrelated
                    # claim. Refuse and let the model say so.
                    result = {"error": (
                        f"{expense_id or 'That claim'} was not part of this "
                        "request; only claims the user named can be "
                        "decided.")}
                    trace.append({"name": name, "args": args,
                                  "result": result})
                elif expense_id in decided:
                    result = {"error": (
                        f"{expense_id} was already decided in this run and "
                        "cannot be changed.")}
                    trace.append({"name": name, "args": args,
                                  "result": result})
                else:
                    plan = _apply_policy(expense_id, trace) if expense_id else None
                    if plan:
                        # The policy outranks the model: swap its decision
                        # for the policy's before anything posts.
                        name, args = plan
                    result = run_tool(name, args)
                    if "error" not in result:
                        decided.add(expense_id)
            else:
                result = run_tool(name, args)
            messages.append({"role": "tool", "tool_call_id": call["id"],
                             "content": json.dumps(result)})

    enforce_policy()
    return "I could not finish reviewing that request.", trace, usage


def with_ledger(reply: str) -> str:
    """Append what the run did to the ledger.

    The SIA judge is sent `output` and nothing else — not the tool calls — so
    an agent whose real effect is a state change has to say what that change
    was, or the judge can only grade the prose. Every eval case here is scored
    on this block.
    """
    summary = expenses.ledger_summary()
    return (f"{reply}\n\n--- LEDGER AFTER THIS REQUEST ---\n"
            f"{json.dumps(summary, indent=2, sort_keys=True)}")


def main() -> int:
    # Both, and by name. An empty key is the more confusing of the two to
    # leave unchecked: it builds the header "Bearer " and httpx rejects the
    # trailing space with `Illegal header value b'Bearer '`, which says
    # nothing about which variable is missing.
    missing = [name for name, value in (("LITELLM_PROXY_URL", PROXY_URL),
                                        ("LITELLM_PROXY_KEY", PROXY_KEY))
               if not value.strip()]
    if missing:
        print(json.dumps({"error": f"{' and '.join(missing)} "
                                   f"{'are' if len(missing) > 1 else 'is'} "
                                   f"not set — export "
                                   f"{'them' if len(missing) > 1 else 'it'}, "
                                   f"or `set -a; . .env; set +a` from the "
                                   f"repo root"}))
        return 1
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        print(json.dumps({"error": "stdin was not JSON"}))
        return 1
    try:
        reply, trace, usage = answer(str(payload.get("input") or ""))
    except httpx.HTTPError as e:
        print(json.dumps({"error": f"model call failed: {e}"}))
        return 1
    # `tokens` is what SIA plots on the cost axis. Summed across the tool
    # loop: one case is every call it took to answer, not just the last.
    print(json.dumps({"output": with_ledger(reply), "tool_calls": trace,
                      "tokens": sum(usage) if usage else None}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
