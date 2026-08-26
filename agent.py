"""Acme Expense Assistant — a tool-calling agent that moves money.

Speaks the `sia` command-adapter contract: a JSON object on stdin, a JSON
object on stdout.

    echo '{"input": "Ada submitted E-201 and E-202, please review both."}' \
        | python3 agent.py

Configure the model through the same proxy the Foundry API uses:

    export LITELLM_PROXY_URL=https://your-proxy
    export LITELLM_PROXY_KEY=sk-...
    export AGENT_MODEL=azure_ai/claude-opus-4-8
"""
import json
import os
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


def answer(request: str) -> tuple[str, list[dict], list[int]]:
    """Run the tool loop. Returns (reply, tool_calls_for_tracing, usage)."""
    messages = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": request}]
    trace: list[dict] = []
    usage: list[int] = []

    for _ in range(MAX_ROUNDS):
        message = call_model(messages, usage)
        calls = message.get("tool_calls") or []
        if not calls:
            return (message.get("content") or "").strip(), trace, usage
        messages.append(message)
        for call in calls:
            name = call["function"]["name"]
            try:
                args = json.loads(call["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            impl = IMPLS.get(name)
            result = impl(**args) if impl else {"error": f"no tool {name}"}
            trace.append({"name": name, "args": args, "result": result})
            messages.append({"role": "tool", "tool_call_id": call["id"],
                             "content": json.dumps(result)})

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
