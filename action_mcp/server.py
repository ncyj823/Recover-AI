"""
action_mcp/server.py — A custom MCP server exposing RecoverAI's recovery
action capabilities as discoverable, callable tools.

Why expose these as MCP tools instead of just plain Python functions?
Any MCP-compatible agent (Claude, or another orchestrator entirely) can
discover and call these tools without knowing our internal code — this is
what "can this product be read by AI agents and can agents write code on
their own" concretely means. Our own LangGraph pipeline calls action_client.py
directly for speed; this server is the same capabilities exposed for
external agent consumption.

Every tool here is annotated with destructiveHint=True where it sends a
real message or spends discount budget — signalling to any calling agent
that these are NOT safe to call speculatively/repeatedly.

Run locally for testing with:
    python server.py

Or inspect interactively with:
    npx @modelcontextprotocol/inspector python server.py
"""

import json
import sys
import os

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field, ConfigDict
from dotenv import load_dotenv

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "recovery_pipeline"))

from action_client import generate_retry_link, dispatch
import compliance
import audit

load_dotenv()

mcp = FastMCP("recoverai_action_mcp")


# ---------------------------------------------------------------------------
# Tool 1: recovery_check_eligibility (read-only, safe to call freely)
# ---------------------------------------------------------------------------

class CheckEligibilityInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    transaction_id: str = Field(..., description="Transaction being recovered", min_length=1)
    customer_id: str = Field(..., description="Customer being contacted", min_length=1)


@mcp.tool(
    name="recovery_check_eligibility",
    annotations={
        "title": "Check whether a recovery action is compliant to send",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def recovery_check_eligibility(params: CheckEligibilityInput) -> str:
    """Check stopping rules (max attempts, cooldown, opt-out) BEFORE
    generating any recovery action. Always call this first — the send
    tool below will refuse if this would return allowed=false anyway,
    but checking first avoids wasted work.

    Args:
        params (CheckEligibilityInput): transaction_id, customer_id

    Returns:
        str: JSON {"allowed": bool, "reason": str}
    """
    allowed, reason = compliance.check(params.transaction_id, params.customer_id)
    return json.dumps({"allowed": allowed, "reason": reason or "ok"})


# ---------------------------------------------------------------------------
# Tool 2: recovery_generate_retry_link (read-only-ish — creates a link but
# doesn't contact the customer or spend anything until it's used)
# ---------------------------------------------------------------------------

class GenerateRetryLinkInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    transaction_id: str = Field(..., min_length=1)
    amount: float = Field(..., description="Original transaction amount in INR", gt=0)
    discount_percent: int = Field(
        default=0, description="Discount to apply, 0-20. Values above 20 are capped.", ge=0, le=100
    )


@mcp.tool(
    name="recovery_generate_retry_link",
    annotations={
        "title": "Generate a bounded payment retry link",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def recovery_generate_retry_link(params: GenerateRetryLinkInput) -> str:
    """Generate a Razorpay-style payment retry link, optionally with a
    discount applied. The discount is HARD-CAPPED at 20% regardless of what
    is requested — this is the "bounded" guarantee for any money action
    an agent takes through this server.

    Args:
        params (GenerateRetryLinkInput): transaction_id, amount, discount_percent

    Returns:
        str: JSON {"retry_link": str, "final_amount": float, "discount_applied": int}
    """
    capped_discount = compliance.cap_discount(params.discount_percent)
    link = await generate_retry_link(params.transaction_id, params.amount, capped_discount)
    final_amount = params.amount * (1 - capped_discount / 100)
    return json.dumps({
        "retry_link": link,
        "final_amount": round(final_amount, 2),
        "discount_applied": capped_discount,
    })


# ---------------------------------------------------------------------------
# Tool 3: recovery_send_message (DESTRUCTIVE — actually contacts a customer)
# ---------------------------------------------------------------------------

class SendMessageInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    transaction_id: str = Field(..., min_length=1)
    customer_id: str = Field(..., min_length=1)
    channel: str = Field(..., description="'whatsapp', 'sms', 'email', or 'push_notification'")
    message: str = Field(..., min_length=1, max_length=1000)


@mcp.tool(
    name="recovery_send_message",
    annotations={
        "title": "Send a recovery message to a customer",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def recovery_send_message(params: SendMessageInput) -> str:
    """Send a payment-recovery nudge to a customer through the given channel.

    SAFETY NOTE: This tool ENFORCES stopping rules internally — it will
    refuse (and log why) if the customer has opted out, hit the max-attempts
    limit for this transaction, or is within the cooldown window, even if
    the calling agent didn't check eligibility first.

    Args:
        params (SendMessageInput): transaction_id, customer_id, channel, message

    Returns:
        str: JSON confirmation of send, or {"blocked": true, "reason": str}
            if a stopping rule prevented the send.
    """
    allowed, reason = compliance.check(params.transaction_id, params.customer_id)
    if not allowed:
        audit.log_stopping_rule_triggered(params.transaction_id, params.customer_id, reason)
        return json.dumps({"blocked": True, "reason": reason})

    result = await dispatch(params.channel, params.customer_id, params.message)
    compliance.record_action(params.transaction_id, params.customer_id)
    audit.log_action_taken(params.transaction_id, params.customer_id, result)
    return json.dumps(result, default=str)


# ---------------------------------------------------------------------------
# Tool 4: recovery_escalate_to_human (DESTRUCTIVE — writes an audit entry
# marking a transaction for human review when the agent can't resolve it)
# ---------------------------------------------------------------------------

class EscalateToHumanInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    transaction_id: str = Field(..., description="Transaction that needs human attention", min_length=1)
    customer_id: str = Field(..., description="Customer associated with the transaction", min_length=1)
    reason: str = Field(
        ...,
        description="Why the agent is escalating — e.g. 'max retries exhausted', "
                    "'customer dispute detected', 'high-value transaction needs manual review'",
        min_length=1,
        max_length=500,
    )


@mcp.tool(
    name="recovery_escalate_to_human",
    annotations={
        "title": "Escalate a transaction to human review",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def recovery_escalate_to_human(params: EscalateToHumanInput) -> str:
    """Mark a transaction for manual human follow-up when the agent cannot
    or should not resolve it autonomously.

    Use this when:
    - All automated recovery attempts have been exhausted (max_attempts_reached)
    - The customer has disputed the charge or expressed frustration
    - The transaction amount is unusually high and warrants human judgement
    - The failure mode is one the agent doesn't have a playbook for

    This is the "bounded" part of RecoverAI — agents are expected to know
    their limits and hand off rather than keep retrying indefinitely.

    Args:
        params (EscalateToHumanInput): transaction_id, customer_id, reason

    Returns:
        str: JSON {"escalated": true, "transaction_id": str, "reason": str}
    """
    audit.log_escalation(params.transaction_id, params.customer_id, params.reason)
    return json.dumps({
        "escalated": True,
        "transaction_id": params.transaction_id,
        "reason": params.reason,
    })


if __name__ == "__main__":
    # stdio transport — launched as a subprocess by an MCP client.
    mcp.run()
