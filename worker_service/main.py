"""
worker_service/main.py — FastAPI webhook receiver for failed-payment events.

Same core design decision as Reviewly's webhook: the caller (payment gateway
webhook, or in our demo, a batch script replaying the Kaggle dataset) needs a
fast ack. All heavy work (3 parallel LLM calls + dispatch) is queued to Redis
and handled by worker.py in a separate process.

Run locally:
    uvicorn main:app --host 0.0.0.0 --port 8000 --reload

Trigger a recovery:
    curl -X POST localhost:8000/events/payment-failed -H "Content-Type: application/json" \
      -d '{"transaction_id":"TXN1","customer_id":"CUST1","amount":1499,"payment_method":"UPI","failure_reason_code":"TIMEOUT"}'
"""

import os
import sys

import redis
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from rq import Queue

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

from worker import run_recovery_job

app = FastAPI(title="RecoverAI Webhook Service")

redis_conn = redis.Redis(
    host=os.environ.get("REDIS_HOST", "localhost"),
    port=6379,
    decode_responses=True,
)
recovery_queue = Queue("recoveries", connection=redis_conn)


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
    return {"status": "ok", "redis": redis_ok}


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
