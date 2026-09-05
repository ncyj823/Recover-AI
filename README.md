# RecoverAI

**Multi-agent payment-recovery system for Razorpay** — automatically diagnoses failed transactions, selects the optimal recovery channel, and sends compliant recovery nudges with bounded discount offers.

> Built for the **Razorpay Buildathon 2026**

🔗 **Live demo:** [https://recover-ai-svnk.onrender.com](https://recover-ai-svnk.onrender.com)
🎥 **Demo video:** https://drive.google.com/file/d/1hS3M1uC9-KgEl0OUUqeM7E5puaZSYMSh/view?usp=sharing

---

## The Problem

Every day, thousands of payments fail — timeouts, insufficient funds, bank declines, UPI glitches. Most merchants have no smart, immediate response to this. A customer who hits a failed payment often just leaves. That's silent, invisible revenue loss, happening at scale, with no automated system trying to win the customer back in the moment it matters most.

## What RecoverAI Does

RecoverAI listens for failed-payment events in real time. The moment a payment fails:

1. **Diagnose** — an AI pipeline analyzes the failure (reason code, payment method, customer history) to understand *why* it failed and what's likely to work.
2. **Plan** — it generates a recovery plan: the best channel (WhatsApp/SMS/email/push), message, and optionally a bounded discount to incentivize a retry.
3. **Act** — it executes the plan autonomously — sending the recovery nudge, generating a retry link, or escalating to a human when the agent shouldn't act alone.

All of this happens asynchronously, in seconds, without blocking the payment gateway/bank that sent the original webhook.

## Architecture

```
Failed-payment webhook (bank/gateway)
        │
        ▼
FastAPI endpoint  ──── acks in < 1s, never blocks the caller
        │
        ▼
Redis Queue (RQ)  ──── dedup + async job queue
        │
        ▼
Background Worker ──── runs the recovery pipeline (findings → plan → action)
        │
        ▼
Recovery Pipeline (multi-agent, LLM-driven)
        │
        ▼
Action MCP Server ──── eligibility check → retry link → send message / escalate
```

**Why async?** The same reasoning that applies to any payment-adjacent webhook: the caller (bank/gateway) shouldn't wait 6–10 seconds for multiple LLM calls to finish. The webhook acknowledges immediately; the real work happens in the background and is queryable via a job-status endpoint.

### Deployment note (Render free tier)

Render's free tier includes Web Services and Redis, but not a separate Background Worker service. To keep this fully deployable on free infrastructure, the RQ worker runs **in-process**, on a background thread inside the same FastAPI process (`RUN_WORKER_IN_PROCESS=true`). This required deliberately overriding RQ's default signal-handling behavior (which assumes it owns the main thread) with a thread-safe worker subclass. In a production deployment with paid infrastructure, this same code can be split back into a separate webhook process and worker process with no logic changes — only the entrypoint changes.

## Tech Stack

- **API layer:** FastAPI (Python)
- **Queue:** Redis + RQ (Redis Queue)
- **Recovery pipeline:** Multi-agent LLM pipeline (diagnosis → planning → action)
- **Action layer:** [Model Context Protocol (MCP)](https://modelcontextprotocol.io) server exposing recovery actions as discoverable, self-describing tools
- **Deployment:** Render (Web Service + Redis)

## Agent Extensibility via MCP

RecoverAI's core recovery actions are exposed as an MCP server (`action_mcp/server.py`), not hard-wired into a single pipeline. This means any MCP-compatible agent — our built-in LangGraph orchestrator, Claude Desktop, or a third-party system — can discover and call these tools without reading our source code.

| Tool | Read/Write | Description |
|------|-----------|-------------|
| `recovery_check_eligibility` | Read-only | Check stopping rules (max attempts, cooldown, opt-out) before generating any recovery action. |
| `recovery_generate_retry_link` | Write | Generate a Razorpay-style payment retry link with an optional discount (hard-capped at 20%). |
| `recovery_send_message` | Write (destructive) | Send a recovery nudge via WhatsApp, SMS, email, or push notification. Enforces compliance internally. |
| `recovery_escalate_to_human` | Write (destructive, idempotent) | Mark a transaction for manual human follow-up when the agent can't or shouldn't resolve it autonomously. |

Compliance (stopping rules, discount caps) is enforced **inside** the MCP tools, server-side — so a calling agent cannot bypass safety limits even if it tries to.

**Inspect the MCP server directly:**
```bash
cd action_mcp
npx @modelcontextprotocol/inspector python server.py
```

**Connect it to Claude Desktop** — add to `claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "recoverai": {
      "command": "python",
      "args": ["<full-path-to>/action_mcp/server.py"]
    }
  }
}
```

> **Scope note:** this proves RecoverAI's actions are discoverable and callable by any MCP-compatible agent — not that the system can rewrite its own architecture at runtime. Extensibility is at the tool-consumption level: new tools follow the existing pattern and are immediately visible to any connected agent.

## API Reference

### `POST /events/payment-failed`
Receives a failed-payment event and queues a recovery job.

```json
{
  "transaction_id": "TXN123",
  "customer_id": "CUST123",
  "amount": 1500,
  "payment_method": "UPI",
  "failure_reason_code": "TIMEOUT",
  "merchant_category": "general",
  "customer_history": {}
}
```
Returns `{"status": "queued", "job_id": "...", "transaction_id": "..."}` in under a second.

### `GET /jobs/{job_id}`
Check the status and result of a queued recovery job.

```json
{
  "job_id": "...",
  "status": "finished",
  "created_at": "...",
  "ended_at": "...",
  "result": {
    "findings": [ ... ],
    "recovery_plan": { ... },
    "action_result": { ... }
  }
}
```

### `GET /health`
Liveness/readiness check — confirms Redis connectivity and whether the in-process worker is active.

## Running Locally

```bash
git clone <repo-url>
cd Recover-AI
pip install -r requirements.txt
cp .env.example .env   # fill in REDIS_HOST, REDIS_PASSWORD, LLM API keys, etc.
uvicorn worker_service.main:app --reload
```

Redis must be running locally (or point `REDIS_HOST`/`REDIS_PORT`/`REDIS_PASSWORD` at a hosted instance).

## What's Live Right Now

The full pipeline — webhook → queue → async AI recovery pipeline → job-status tracking — is deployed and verified end-to-end at:

**https://recover-ai-svnk.onrender.com**

---

Built for the Razorpay Buildathon 2026.
