"""
Shared state definition for the RecoverAI pipeline.

Every node in the LangGraph graph reads from and writes to this state.
Same pattern as Reviewly's PRReviewState — one TypedDict, Annotated[list,
operator.add] on the field that parallel agents write to, so LangGraph
MERGES their outputs instead of one overwriting another.
"""

import operator
from typing import Annotated, Optional
from typing_extensions import TypedDict


class RecoveryState(TypedDict):
    # ── Input fields (set once by fetch_transaction_context node) ──────
    transaction_id: str
    customer_id: str
    amount: float
    payment_method: str          # e.g. "UPI", "Credit Card", "Net Banking"
    failure_reason_code: str     # raw code from gateway, e.g. "TIMEOUT", "INSUFFICIENT_FUNDS"
    merchant_category: str
    customer_history: dict       # past order count, past recovery response rate, etc.

    # ── Output fields (written by parallel agent nodes) ─────────────────
    # Annotated with operator.add so parallel writes MERGE, not overwrite.
    findings: Annotated[list, operator.add]

    # ── Final output (set by aggregator, consumed by execute_recovery) ──
    recovery_plan: Optional[dict]   # {"channel": ..., "message": ..., "offer": ..., "send_at": ...}
    action_result: Optional[dict]   # result of executing the plan via action_mcp
