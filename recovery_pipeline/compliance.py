"""
compliance.py — stopping rules and escalation limits for RecoverAI.

Razorpay's Track 3 bar explicitly asks for "compliant escalation" and
"stopping rules" — this is what prevents the agent from becoming spam.
Rules enforced here, BEFORE any action_mcp dispatch happens:

  1. MAX_ATTEMPTS — a customer gets at most 3 recovery nudges per transaction.
     Beyond that, it's handed off (flagged for human follow-up), not auto-nudged.
  2. COOLDOWN — at least 4 hours must pass between nudges to the same customer,
     regardless of how many different transactions they have failing.
  3. OPT_OUT — a customer who has opted out of recovery messages (e.g. replied
     STOP) is never contacted again, no exceptions.
  4. NO_INCENTIVE_ESCALATION — discount_percent is capped at 20%. Agents
     cannot self-approve unlimited discounts — this is the "bounded" part
     of "explainable, bounded and gated" money actions.

State is kept in-memory here for the demo (dict keyed by customer_id). In
production this would be Redis-backed (same Redis instance already used for
the job queue) so it survives restarts and is shared across worker processes.
"""

from datetime import datetime, timedelta, timezone

MAX_ATTEMPTS_PER_TRANSACTION = 3
COOLDOWN_HOURS = 4
MAX_DISCOUNT_PERCENT = 20

# In-memory stores — swap for Redis in production (see docstring above).
_attempt_counts: dict[str, int] = {}          # transaction_id -> count
_last_contact: dict[str, datetime] = {}        # customer_id -> last contacted timestamp
_opted_out: set[str] = set()                   # customer_ids who opted out


def opt_out(customer_id: str) -> None:
    """Register a customer as opted-out. Called from a webhook when a
    customer replies STOP/unsubscribe to a recovery message."""
    _opted_out.add(customer_id)


def check(transaction_id: str, customer_id: str) -> tuple[bool, str]:
    """Check whether a recovery action is allowed right now.

    Returns:
        (allowed: bool, reason: str) — reason is empty string if allowed,
        otherwise names which rule blocked it (for the audit log).
    """
    if customer_id in _opted_out:
        return False, "customer_opted_out"

    attempts = _attempt_counts.get(transaction_id, 0)
    if attempts >= MAX_ATTEMPTS_PER_TRANSACTION:
        return False, "max_attempts_reached"

    last = _last_contact.get(customer_id)
    if last is not None:
        elapsed = datetime.now(timezone.utc) - last
        if elapsed < timedelta(hours=COOLDOWN_HOURS):
            return False, "cooldown_active"

    return True, ""


def cap_discount(discount_percent: int) -> int:
    """Enforce the hard ceiling on any discount an agent proposes."""
    return min(max(discount_percent, 0), MAX_DISCOUNT_PERCENT)


def record_action(transaction_id: str, customer_id: str) -> None:
    """Call this AFTER a recovery action is successfully dispatched, to
    update attempt counts and cooldown timers."""
    _attempt_counts[transaction_id] = _attempt_counts.get(transaction_id, 0) + 1
    _last_contact[customer_id] = datetime.now(timezone.utc)


def reset():
    """Clear all in-memory state — used between batch runs/tests."""
    _attempt_counts.clear()
    _last_contact.clear()
    _opted_out.clear()
