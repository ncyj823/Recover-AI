"""
worker.py — the actual job that runs in the background via RQ.

Flow:
    Failed-payment webhook → FastAPI (acks in <1s) → Redis queue → THIS FILE runs

Same reasoning as Reviewly: the webhook caller (gateway/bank) shouldn't wait
6-10s for 3 LLM calls to finish. Ack fast, do the real work async.
"""

import sys
import os
import asyncio

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "action_mcp"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "recovery_pipeline"))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))


def run_recovery_job(transaction: dict):
    """Entry point called by RQ worker for each failed-transaction job.

    RQ calls regular (sync) functions, so we wrap our async pipeline
    with asyncio.run() here.
    """
    txn_id = transaction.get("transaction_id", "unknown")
    print(f"[worker] Starting recovery job: {txn_id}")
    try:
        from pipeline import run_recovery
        asyncio.run(run_recovery(transaction))
        print(f"[worker] [OK] Recovery complete: {txn_id}")
    except Exception as e:
        print(f"[worker] [Error] Recovery failed: {e}")
        raise  # Re-raise so RQ marks job as failed (visible in Redis)
