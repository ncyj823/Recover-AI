"""
worker_service/main.py — FastAPI webhook receiver + live dashboard for
RecoverAI's payment recovery pipeline.

DEPLOYMENT NOTE: This process ALSO runs the RQ worker in a background
thread (started on FastAPI startup — see `_start_worker_thread` below).
This is a deliberate simplification for free-tier deployment: Render's
free plan includes Web Services and Redis, but not Background Workers.
Rather than pay for a second service just for the demo, we run both
roles in one process. In a real production deployment at scale, you'd
split these back into separate webhook and worker processes (as the
docker-compose.yml in this repo still does for local development) so
webhook traffic and job processing can scale independently.

The GET / route serves a small live dashboard: submit a simulated failed
payment, watch RecoverAI diagnose it, pick a channel, decide on an
offer, and dispatch — all in real time, backed by the same LangGraph
pipeline used in the CLI batch tool.
"""

import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "recovery_pipeline"))

import redis
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from rq import Queue, Worker

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

from worker import run_recovery_job
# pyrefly: ignore [missing-import]
import audit
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    if RUN_WORKER_IN_PROCESS:
        thread = threading.Thread(target=_start_worker_thread, daemon=True)
        thread.start()
        print("[webhook] Started in-process RQ worker thread")
    yield

app = FastAPI(title="RecoverAI", lifespan=lifespan)

redis_conn = redis.Redis(
    host=os.environ.get("REDIS_HOST", "localhost"),
    port=int(os.environ.get("REDIS_PORT", 6379)),
    password=os.environ.get("REDIS_PASSWORD") or None,
)
recovery_queue = Queue("recoveries", connection=redis_conn)

RUN_WORKER_IN_PROCESS = os.environ.get("RUN_WORKER_IN_PROCESS", "false").lower() == "true"


def _start_worker_thread():
    worker_conn = redis.Redis(
        host=os.environ.get("REDIS_HOST", "localhost"),
        port=int(os.environ.get("REDIS_PORT", 6379)),
        password=os.environ.get("REDIS_PASSWORD") or None,
    )
    queue = Queue("recoveries", connection=worker_conn)
    worker = Worker([queue], connection=worker_conn)
    worker.work(with_scheduler=False)


class FailedPaymentEvent(BaseModel):
    transaction_id: str
    customer_id: str
    amount: float
    payment_method: str
    failure_reason_code: str
    merchant_category: str = "general"
    customer_history: dict = {}


@app.get("/health")
async def health():
    try:
        redis_conn.ping()
        redis_ok = True
    except Exception:
        redis_ok = False
    return {"status": "ok", "redis": redis_ok, "worker_in_process": RUN_WORKER_IN_PROCESS}


@app.post("/events/payment-failed")
async def payment_failed(event: FailedPaymentEvent):
    dedup_key = f"recoverai:queued:{event.transaction_id}"
    if redis_conn.get(dedup_key):
        return {"status": "duplicate", "transaction_id": event.transaction_id}

    redis_conn.setex(dedup_key, 600, "queued")

    job = recovery_queue.enqueue(
        run_recovery_job,
        event.model_dump(),
        job_timeout=120,
    )

    print(f"[webhook] Queued recovery job {job.id}: {event.transaction_id}")

    return {
        "status": "queued",
        "job_id": job.id,
        "transaction_id": event.transaction_id,
    }


@app.get("/jobs/{job_id}")
async def job_status(job_id: str):
    from rq.job import Job
    try:
        job = Job.fetch(job_id, connection=redis_conn)
        return {
            "job_id": job_id,
            "status": job.get_status(),
            "created_at": str(job.created_at),
            "ended_at": str(job.ended_at) if job.ended_at else None,
            "result": job.result,
        }
    except Exception:
        raise HTTPException(status_code=404, detail="Job not found")


@app.get("/audit/recent")
async def audit_recent(limit: int = 20):
    """Return the most recent audit trail entries — powers the dashboard's
    live activity feed, and doubles as a lightweight way for anyone to
    inspect agent reasoning without shelling into the container."""
    entries = audit.read_all()
    return {"entries": entries[-limit:][::-1]}


DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>RecoverAI — Agentic Payment Recovery</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root {
    --bg: #0b0f14; --panel: #131a22; --border: #223041; --text: #e6edf3;
    --muted: #8b98a5; --accent: #4f9dff; --good: #3fb950; --warn: #d29922;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; font-family: -apple-system, "Segoe UI", Roboto, sans-serif;
    background: var(--bg); color: var(--text); padding: 32px 16px;
  }
  .wrap { max-width: 880px; margin: 0 auto; }
  h1 { font-size: 24px; margin-bottom: 4px; }
  .sub { color: var(--muted); margin-bottom: 28px; font-size: 14px; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
  @media (max-width: 720px) { .grid { grid-template-columns: 1fr; } }
  .panel {
    background: var(--panel); border: 1px solid var(--border);
    border-radius: 10px; padding: 20px;
  }
  .panel h2 { font-size: 15px; margin: 0 0 14px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; }
  label { display: block; font-size: 13px; color: var(--muted); margin: 12px 0 4px; }
  input, select {
    width: 100%; padding: 9px 10px; background: #0e141b; border: 1px solid var(--border);
    border-radius: 6px; color: var(--text); font-size: 14px;
  }
  button {
    margin-top: 18px; width: 100%; padding: 11px; background: var(--accent);
    color: #041322; border: none; border-radius: 6px; font-weight: 600; font-size: 14px;
    cursor: pointer;
  }
  button:disabled { opacity: 0.5; cursor: not-allowed; }
  .result { margin-top: 18px; font-size: 13px; }
  .row { display: flex; justify-content: space-between; padding: 6px 0; border-bottom: 1px solid var(--border); }
  .row .k { color: var(--muted); }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 11px; font-weight: 600; }
  .badge.sent { background: rgba(63,185,80,0.15); color: var(--good); }
  .badge.skipped { background: rgba(210,153,34,0.15); color: var(--warn); }
  .badge.pending { background: rgba(139,152,165,0.15); color: var(--muted); }
  .feed-item { border-bottom: 1px solid var(--border); padding: 10px 0; font-size: 12.5px; }
  .feed-item .tag { color: var(--accent); font-weight: 600; }
  .feed-item .txn { color: var(--muted); }
  .empty { color: var(--muted); font-size: 13px; }
  footer { text-align: center; margin-top: 32px; color: var(--muted); font-size: 12px; }
  footer a { color: var(--accent); }
</style>
</head>
<body>
<div class="wrap">
  <h1>RecoverAI</h1>
  <div class="sub">Multi-agent payment recovery — diagnose, choose a channel, decide on an offer, and dispatch, live.</div>

  <div class="grid">
    <div class="panel">
      <h2>Simulate a failed payment</h2>
      <form id="txnForm">
        <label>Transaction ID</label>
        <input id="transaction_id" required>

        <label>Customer ID</label>
        <input id="customer_id" required>

        <label>Amount (INR)</label>
        <input id="amount" type="number" step="0.01" value="1499" required>

        <label>Payment Method</label>
        <select id="payment_method">
          <option>UPI</option>
          <option>Credit Card</option>
          <option>Debit Card</option>
          <option>Net Banking</option>
        </select>

        <label>Failure Reason</label>
        <select id="failure_reason_code">
          <option value="TIMEOUT">Network Timeout</option>
          <option value="INSUFFICIENT_FUNDS">Insufficient Funds</option>
          <option value="BANK_DECLINE">Bank Decline</option>
          <option value="OTP_FAILURE">OTP Failure</option>
          <option value="GATEWAY_ERROR">Gateway Error</option>
        </select>

        <button type="submit" id="submitBtn">Run Recovery Agent</button>
      </form>

      <div class="result" id="result"></div>
    </div>

    <div class="panel">
      <h2>Recent activity</h2>
      <div id="feed" class="empty">No activity yet — run a recovery to see the audit trail here.</div>
    </div>
  </div>

  <footer>Built for Razorpay Buildathon — Track 3: AI Revenue Recovery · <a href="/docs" target="_blank">API docs</a></footer>
</div>

<script>
function randId(prefix) {
  return prefix + Math.random().toString(36).slice(2, 8).toUpperCase();
}
document.getElementById('transaction_id').value = randId('TXN-');
document.getElementById('customer_id').value = randId('CUST-');

async function loadFeed() {
  try {
    const res = await fetch('/audit/recent?limit=12');
    const data = await res.json();
    const feed = document.getElementById('feed');
    if (!data.entries || data.entries.length === 0) {
      feed.className = 'empty';
      feed.textContent = 'No activity yet — run a recovery to see the audit trail here.';
      return;
    }
    feed.className = '';
    feed.innerHTML = data.entries.map(e => {
      let content = e.summary || e.reason;
      if (!content) {
        const jsonStr = JSON.stringify(e);
        content = jsonStr.length > 120 ? jsonStr.slice(0, 120) + '...' : jsonStr;
        // Or better, let's pretty-print the JSON and make it scrollable
        content = `<pre style="margin: 6px 0 0; padding: 8px; background: rgba(0,0,0,0.2); border-radius: 4px; overflow-x: auto; font-size: 11px; color: var(--muted);">${JSON.stringify(e, null, 2)}</pre>`;
      }
      return `
      <div class="feed-item">
        <span class="tag">${e.event}</span> · <span class="txn">${e.transaction_id || ''}</span><br>
        ${content}
      </div>
      `;
    }).join('');
  } catch (e) { /* silent */ }
}
loadFeed();
setInterval(loadFeed, 4000);

document.getElementById('txnForm').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const btn = document.getElementById('submitBtn');
  const resultEl = document.getElementById('result');
  btn.disabled = true;
  btn.textContent = 'Queuing...';
  resultEl.innerHTML = '<span class="badge pending">queued</span>';

  const payload = {
    transaction_id: document.getElementById('transaction_id').value,
    customer_id: document.getElementById('customer_id').value,
    amount: parseFloat(document.getElementById('amount').value),
    payment_method: document.getElementById('payment_method').value,
    failure_reason_code: document.getElementById('failure_reason_code').value,
  };

  try {
    const res = await fetch('/events/payment-failed', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    
    if (data.status === 'duplicate') {
      btn.disabled = false;
      btn.textContent = 'Run Recovery Agent';
      resultEl.innerHTML = '<div class="empty">Error: This transaction ID was already processed. Please change it to try again.</div>';
      return;
    }
    
    if (!data.job_id) {
      btn.disabled = false;
      btn.textContent = 'Run Recovery Agent';
      resultEl.innerHTML = '<div class="empty">Error: Failed to queue job.</div>';
      return;
    }

    btn.textContent = 'Processing...';

    let attempts = 0;
    const poll = setInterval(async () => {
      attempts++;
      const jr = await fetch('/jobs/' + data.job_id);
      const jd = await jr.json();
      if (jd.status === 'finished' || jd.status === 'failed' || attempts > 20) {
        clearInterval(poll);
        btn.disabled = false;
        btn.textContent = 'Run Recovery Agent';
        renderResult(jd.result);
        loadFeed();
      }
    }, 1000);
  } catch (e) {
    btn.disabled = false;
    btn.textContent = 'Run Recovery Agent';
    resultEl.innerHTML = '<div class="empty">Error: ' + e.message + '</div>';
  }
});

function renderResult(r) {
  const resultEl = document.getElementById('result');
  if (r && r.action_result) { r = r.action_result; }
  if (!r) { resultEl.innerHTML = '<div class="empty">No result yet.</div>'; return; }
  const badgeClass = r.status === 'sent' ? 'sent' : (r.status === 'skipped' ? 'skipped' : 'pending');
  let rows = `<div class="row"><span class="k">Status</span><span class="badge ${badgeClass}">${r.status || 'unknown'}</span></div>`;
  if (r.channel) rows += `<div class="row"><span class="k">Channel</span><span>${r.channel}</span></div>`;
  if (r.root_cause) rows += `<div class="row"><span class="k">Root cause</span><span>${r.root_cause}</span></div>`;
  if (r.discount_percent !== undefined) rows += `<div class="row"><span class="k">Discount</span><span>${r.discount_percent}%</span></div>`;
  if (r.amount_at_risk) rows += `<div class="row"><span class="k">Amount at risk</span><span>₹${r.amount_at_risk}</span></div>`;
  if (r.reason) rows += `<div class="row"><span class="k">Reason</span><span>${r.reason}</span></div>`;
  resultEl.innerHTML = rows;
}
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return DASHBOARD_HTML
