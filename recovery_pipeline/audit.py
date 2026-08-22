"""
audit.py — structured audit trail for RecoverAI.

Why this exists:
Razorpay's Track 3 bar explicitly requires "compliant escalation, stopping
rules, and an audit trail" for any workflow that touches money. This module
is the single place every money-relevant decision gets recorded — which
customer was contacted, why, through which channel, whether a discount was
offered, and what the outcome was. Every entry is one JSON line, so the log
is both human-readable (tail -f) and machine-parseable (for the batch report
and for any compliance review later).

This is deliberately a plain append-only JSONL file, not a database, so it
stays trivially inspectable for the demo/pitch. Swapping to a real audit
store (Postgres, or a write-once S3 bucket) later means changing only
`_write()` — nothing else in the codebase needs to know.
"""

import json
import os
from datetime import datetime, timezone

AUDIT_LOG_PATH = os.path.join(os.path.dirname(__file__), "audit_log.jsonl")


def _write(event: dict) -> None:
    event["logged_at"] = datetime.now(timezone.utc).isoformat()
    with open(AUDIT_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")


def log_diagnosis(transaction_id: str, finding: dict) -> None:
    _write({"event": "diagnosis", "transaction_id": transaction_id, **finding})


def log_channel_decision(transaction_id: str, finding: dict) -> None:
    _write({"event": "channel_decision", "transaction_id": transaction_id, **finding})


def log_offer_decision(transaction_id: str, finding: dict) -> None:
    _write({"event": "offer_decision", "transaction_id": transaction_id, **finding})


def log_recovery_plan(transaction_id: str, plan: dict) -> None:
    _write({"event": "recovery_plan", "transaction_id": transaction_id, **plan})


def log_action_taken(transaction_id: str, customer_id: str, result: dict) -> None:
    _write({
        "event": "action_taken",
        "transaction_id": transaction_id,
        "customer_id": customer_id,
        **result,
    })


def log_skipped(transaction_id: str, reason: str) -> None:
    _write({"event": "skipped", "transaction_id": transaction_id, "reason": reason})


def log_stopping_rule_triggered(transaction_id: str, customer_id: str, rule: str) -> None:
    """Logged when a compliance/stopping rule blocks an action — e.g. max
    attempts reached, cooldown active, or customer opted out."""
    _write({
        "event": "stopping_rule_triggered",
        "transaction_id": transaction_id,
        "customer_id": customer_id,
        "rule": rule,
    })


def read_all() -> list[dict]:
    """Read the full audit log — used by the batch report to compute metrics."""
    if not os.path.exists(AUDIT_LOG_PATH):
        return []
    entries = []
    with open(AUDIT_LOG_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries
