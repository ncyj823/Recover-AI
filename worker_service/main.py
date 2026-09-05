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
"""

import os
import sys
import threading

# Explicitly add this file's own directory to sys.path BEFORE importing
# worker.py. This is necessary because how Python resolves `import worker`
# depends on how uvicorn was invoked: running with --reload (as local
# docker-compose does) happens to add this directory to sys.path as a
# side effect of the reloader subprocess, but running the plain module
# path `uvicorn worker_service.main:app` (as Render's production CMD
# does, with no --reload) does NOT. Making this explicit removes that
# invocation-dependent behavior entirely — the import now works the same
# way everywhere.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import redis
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from rq import Queue, Worker
from rq.worker import SimpleWorker

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

from worker import run_recovery_job

app = FastAPI(title="RecoverAI Webhook Service")

redis_conn = redis.Redis(
    host=os.environ.get("REDIS_HOST", "localhost"),
    port=int(os.environ.get("REDIS_PORT", 6379)),
    password=os.environ.get("REDIS_PASSWORD") or None,
    decode_responses=True,
)

rq_redis_conn = redis.Redis(
    host=os.environ.get("REDIS_HOST", "localhost"),
    port=int(os.environ.get("REDIS_PORT", 6379)),
    password=os.environ.get("REDIS_PASSWORD") or None,
    decode_responses=False,
)
recovery_queue = Queue("recoveries", connection=rq_redis_conn)

RUN_WORKER_IN_PROCESS = os.environ.get("RUN_WORKER_IN_PROCESS", "false").lower() == "true"




def _start_worker_thread():
    """Run an RQ worker on a background thread inside this same process."""
    worker_conn = redis.Redis(
        host=os.environ.get("REDIS_HOST", "localhost"),
        port=int(os.environ.get("REDIS_PORT", 6379)),
        password=os.environ.get("REDIS_PASSWORD") or None,
        decode_responses=False,
    )
    queue = Queue("recoveries", connection=worker_conn)
    worker = SimpleWorker([queue], connection=worker_conn)
    worker.work(with_scheduler=False, burst=False)


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
    """Receive a failed-payment event and queue a recovery job."""
    dedup_key = f"recoverai:queued:{event.transaction_id}"
    if redis_conn.get(dedup_key):
        print(f"[webhook] Duplicate delivery ignored: {event.transaction_id}")
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
    """Check the status of a queued recovery job."""
    from rq.job import Job
    try:
        job = Job.fetch(job_id, connection=rq_redis_conn)
        return {
            "job_id": job_id,
            "status": job.get_status(),
            "created_at": str(job.created_at),
            "ended_at": str(job.ended_at) if job.ended_at else None,
            "result": job.result,
        }
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Job not found: {str(e)}")
