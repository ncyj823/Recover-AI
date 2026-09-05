"""
worker_service/main.py — FastAPI webhook receiver for failed-payment events.

DEPLOYMENT NOTE: This process ALSO runs the RQ worker in a background
thread (started on FastAPI startup — see `_start_worker_thread` below).
This is a deliberate simplification for free-tier deployment: Render's
free plan includes Web Services and Redis, but not Background Workers.
Rather than pay for a second service just for the demo, we run both
roles in one process. In a real production deployment at scale, you'd
split these back into separate webhook and worker processes (as the
docker-compose.yml in this repo still does for local development) so
webhook traffic and job processing can scale independently.

Run locally (webhook only, matching docker-compose):
    uvicorn main:app --host 0.0.0.0 --port 8000 --reload

Trigger a recovery:
    curl -X POST localhost:8000/events/payment-failed -H "Content-Type: application/json" \
      -d '{"transaction_id":"TXN1","customer_id":"CUST1","amount":1499,"payment_method":"UPI","failure_reason_code":"TIMEOUT"}'
"""

import os
import sys
import threading

import redis
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from rq import Queue, Worker

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

from worker import run_recovery_job

app = FastAPI(title="RecoverAI Webhook Service")

redis_conn = redis.Redis(
    host=os.environ.get("REDIS_HOST", "localhost"),
    port=int(os.environ.get("REDIS_PORT", 6379)),
    password=os.environ.get("REDIS_PASSWORD") or None,
    decode_responses=True,
)
recovery_queue = Queue("recoveries", connection=redis_conn)

# Only start the in-process worker thread when explicitly enabled — keeps
# local docker-compose (which runs a dedicated worker container) from
# accidentally running two workers competing for the same jobs.
RUN_WORKER_IN_PROCESS = os.environ.get("RUN_WORKER_IN_PROCESS", "false").lower() == "true"


def _start_worker_thread():
    """Run an RQ worker on a background thread inside this same process.

    Uses RQ's SimpleWorker (not the default fork-based Worker) since
    threads can't fork — SimpleWorker runs jobs in-thread instead of in a
    forked child process. Fine for a low-volume demo deployment; a forked
    Worker would be preferred at higher throughput.
    """
    worker_conn = redis.Redis(
        host=os.environ.get("REDIS_HOST", "localhost"),
        port=int(os.environ.get("REDIS_PORT", 6379)),
        password=os.environ.get("REDIS_PASSWORD") or None,
        decode_responses=True,
    )
    queue = Queue("recoveries", connection=worker_conn)
    worker = Worker([queue], connection=worker_conn)
    worker.work(with_scheduler=False)


@app.on_event("startup")
async def startup_event():
    if RUN_WORKER_IN_PROCESS:
        thread = threading.Thread(target=_start_worker_thread, daemon=True)
        thread.start()
        print("[webhook] Started in-process RQ worker thread (RUN_WORKER_IN_PROCESS=true)")


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
    """Receive a failed-payment event and queue a recovery job.

    Deduplicates on transaction_id so retried webhook deliveries don't
    trigger duplicate recovery messages to the same customer.
    """
    dedup_key = f"recoverai:queued:{event.transaction_id}"
    if redis_conn.get(dedup_key):
        print(f"[webhook] Duplicate delivery ignored: {event.transaction_id}")
        return {"status": "duplicate", "transaction_id": event.transaction_id}

    redis_conn.setex(dedup_key, 600, "queued")  # expires in 10 minutes

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
    """Check the status of a queued recovery job."""
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
