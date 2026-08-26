# Acme Expense Assistant — SIA Foundry Hackathon Entry

**Team Nights** · Adam Beaudoin & Charlene

A tool-calling AI agent that reviews employee expense claims against a reimbursement policy — built for the **SIA Foundry: Self-Improving Agents** hackathon by Hexo Labs.

## What it does

The agent reads expense claims (amount, merchant, category, receipt status), queries the expense ledger and employee records, and decides: **approve**, **reject**, **hold for receipt**, or **escalate to a manager**. It speaks a JSON command contract — input on stdin, a ledger summary + tool trace on stdout.

It enforces a policy with sharp edges that naively-written agents miss:

- **Duplicate claims** get paid twice → the agent detects identical claims (same employee, merchant, amount, date) and pays exactly once
- **Split claims** sneak under the receipt threshold → the agent aggregates parts and applies the combined amount
- **Non-reimbursable categories** (alcohol, entertainment, personal) get auto-approved as small claims → the agent rejects them regardless of amount
- **Unverifiable claims** get rubber-stamped → the agent refuses to decide claims the user never named

## How SIA improved it

We ran the full SIA Foundry loop on this agent:

| Stage | Score |
|---|---|
| Baseline eval | **50/100** (4/8) |
| SIA failure detection | grouped losses into 4 modes: duplicate approvals, split-claim threshold abuse, missing category rejections, prose-only decisions |
| Deterministic policy engine (this repo) | **100/100** (8/8), stable across repeated runs |

SIA's server-side fix loop stalled on platform 503s, so we took the failure modes SIA identified and engineered a **deterministic policy engine** directly in the agent:

1. **`_apply_policy()`** — category rejection → mandatory similar-expense lookup → duplicate-vs-split discrimination → threshold rules. Runs only while a claim is still `submitted`.
2. **Duplicate handling** — the kept claim is decided on its *own* amount (never the combined sum); first-by-id wins regardless of decision order.
3. **`answer()` guards** — every LLM decision call is overridden by the policy plan; decisions on unnamed or already-decided claims are refused; `enforce_policy()` runs after the model's final message so **prose-only replies still mutate the ledger**.
4. **`_mentioned_expense_ids()`** — only claims named in the request can be decided (unknown claims stay untouched).

**Validation:** 27/27 offline across adversarial LLM orderings (prose-only, reverse-id, natural), then 4 consecutive full end-to-end runs at 8/8 with zero variance in graded artifacts.

## Run it

```bash
# 1. Set up
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python sia-foundry

# 2. Configure (Hexo Labs proxy)
cp .env.example .env   # fill in LITELLM_PROXY_URL / LITELLM_PROXY_KEY / AGENT_MODEL

# 3. Run the agent
echo '{"input": "Ada submitted E-201 and E-202, please review both."}' \
  | set -a; . ./.env; set +a; .venv/bin/python agent.py

# 4. Run the official eval
.venv/bin/sia evals run
```

## Files

- `agent.py` — the agent: tool loop + deterministic policy engine
- `expenses.py` — the expense ledger and tools
- `policy.md` — the reimbursement policy the agent enforces
- `prompts.py` — the system prompt
- `evals/expense.yaml` — the eval case spec
- `test_runner.py` — local harness replicating the official checks
- `hardening-summary.md` — full before/after analysis

## Platform

Project on [sia.hexo.ai](https://sia.hexo.ai) (expense-agent): baseline 50 → **100**, with the full artifact trail — runs, failures, pareto curve.
