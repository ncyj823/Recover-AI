"""
The three parallel recovery agents for RecoverAI.

Each agent is a LangGraph node — an async function that:
  1. Reads what it needs from RecoveryState
  2. Calls Groq (via langchain-groq) with a focused, role-specific prompt
  3. Parses the LLM's JSON response
  4. Returns {"findings": [its_finding]} to be MERGED into shared state

Why three separate agents instead of one big "figure out recovery" prompt?
- Focus: a diagnosis-only prompt reasons better about WHY a payment failed
  than a prompt also trying to pick channel + offer at the same time
- Parallelism: all 3 run simultaneously — cuts latency vs. running sequentially
- Debuggability: if the offer agent is being too generous with discounts,
  you tune THAT prompt without touching diagnosis or channel logic
- This is the same "single responsibility per agent" design as Reviewly,
  just re-pointed at a different domain — which is the whole pitch for
  reusing this architecture in the buildathon.
"""

import json
import os
import re
from langchain_groq import ChatGroq
from langchain_core.messages import SystemMessage, HumanMessage
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from groq import APIConnectionError, APITimeoutError, RateLimitError

from state import RecoveryState
from logging_config import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Shared LLM client — same as Reviewly, temperature=0 for consistent output.
# ---------------------------------------------------------------------------

def _get_llm() -> ChatGroq:
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError("GROQ_API_KEY not set. Add it to your .env file.")
    return ChatGroq(
        model="openai/gpt-oss-120b",
        temperature=0,
        groq_api_key=api_key,
    )


def _parse_json_response(raw: str, agent_name: str) -> dict:
    """Safely extract JSON from an LLM response (strips markdown fences)."""
    cleaned = re.sub(r"```(?:json)?\s*|\s*```", "", raw).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        return {
            "agent": agent_name,
            "confidence": "error",
            "summary": f"Agent failed to produce valid JSON: {str(e)}",
        }


# Retry policy shared by all 3 agents: retries transient network/rate-limit
# errors (NOT auth or validation errors, which won't fix themselves) up to
# 3 times with exponential backoff (2s, 4s, 8s). This is what turns a
# single dropped connection into a recovered call instead of a crashed
# batch run — important since batch.py processes many transactions in one
# long-running process where a momentary network hiccup shouldn't kill
# everything after it.
llm_retry = retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type((APIConnectionError, APITimeoutError, RateLimitError)),
    reraise=True,
)


def _txn_context(state: RecoveryState) -> str:
    """Build a compact text block of the transaction for prompting."""
    return (
        f"Transaction ID: {state['transaction_id']}\n"
        f"Amount: ₹{state['amount']}\n"
        f"Payment Method: {state['payment_method']}\n"
        f"Failure Reason Code: {state['failure_reason_code']}\n"
        f"Merchant Category: {state['merchant_category']}\n"
        f"Customer History: {json.dumps(state.get('customer_history', {}))}"
    )


# ---------------------------------------------------------------------------
# Agent 1: Failure Diagnosis
# ---------------------------------------------------------------------------

DIAGNOSIS_SYSTEM_PROMPT = """You are a payment failure diagnosis specialist.
Given raw transaction failure data, determine the MOST LIKELY root cause and
whether it is recoverable at all.

Classify into one of: "bank_decline", "insufficient_funds", "network_timeout",
"user_abandoned", "otp_failure", "gateway_error", "unknown".

Respond ONLY with a JSON object in this exact schema (no markdown, no explanation):
{
  "agent": "diagnosis",
  "root_cause": "one of the categories above",
  "recoverable": true | false,
  "confidence": "high" | "medium" | "low",
  "summary": "one sentence explanation of why this classification was chosen"
}

If the failure is due to fraud suspicion or hard decline, set recoverable to false.
"""


async def diagnosis_agent(state: RecoveryState) -> dict:
    """Diagnoses why the payment failed — runs in parallel with channel + offer agents."""
    llm = _get_llm()
    messages = [
        SystemMessage(content=DIAGNOSIS_SYSTEM_PROMPT),
        HumanMessage(content=f"Diagnose this failed transaction:\n\n{_txn_context(state)}"),
    ]
    try:
        response = await llm_retry(llm.ainvoke)(messages)
        finding = _parse_json_response(response.content, "diagnosis")
    except (APIConnectionError, APITimeoutError, RateLimitError) as e:
        # After 3 retries, still failing — don't crash the whole batch over
        # one transaction. Mark it low-confidence/unrecoverable so aggregate()
        # skips it safely, and this failure is visible in the audit trail.
        logger.error("diagnosis_agent failed after retries for %s: %s", state['transaction_id'], e)
        finding = {"agent": "diagnosis", "root_cause": "unknown", "recoverable": False,
                   "confidence": "error", "summary": f"LLM call failed after retries: {e}"}
    return {"findings": [finding]}


# ---------------------------------------------------------------------------
# Agent 2: Channel Selection
# ---------------------------------------------------------------------------

CHANNEL_SYSTEM_PROMPT = """You are a customer engagement channel strategist for
payment recovery. Given transaction and customer history data, pick the BEST
channel and timing to reach this customer.

Choose channel from: "whatsapp", "sms", "email", "push_notification".
Choose send_delay_minutes: how long to wait before sending (0 for immediate).

Respond ONLY with a JSON object in this exact schema (no markdown, no explanation):
{
  "agent": "channel",
  "channel": "one of the channels above",
  "send_delay_minutes": integer,
  "confidence": "high" | "medium" | "low",
  "summary": "one sentence explanation of why this channel/timing was chosen"
}

Guidance: high-value or repeat customers often respond better to WhatsApp/push
(immediate, personal); low engagement history customers may need SMS first.
Network timeouts often deserve an immediate nudge; insufficient funds usually
needs delay (hours) to let balance refresh.
"""


async def channel_agent(state: RecoveryState) -> dict:
    """Picks the best outreach channel and timing — runs in parallel."""
    llm = _get_llm()
    messages = [
        SystemMessage(content=CHANNEL_SYSTEM_PROMPT),
        HumanMessage(content=f"Pick a channel for this failed transaction:\n\n{_txn_context(state)}"),
    ]
    try:
        response = await llm_retry(llm.ainvoke)(messages)
        finding = _parse_json_response(response.content, "channel")
    except (APIConnectionError, APITimeoutError, RateLimitError) as e:
        # Safe fallback — SMS is the most universally deliverable channel,
        # so defaulting here doesn't block recovery, it just picks a
        # conservative default when the agent itself couldn't be reached.
        logger.error("channel_agent failed after retries for %s: %s", state['transaction_id'], e)
        finding = {"agent": "channel", "channel": "sms", "send_delay_minutes": 0,
                   "confidence": "error", "summary": f"LLM call failed after retries, defaulted to SMS: {e}"}
    return {"findings": [finding]}


# ---------------------------------------------------------------------------
# Agent 3: Offer Strategy
# ---------------------------------------------------------------------------

OFFER_SYSTEM_PROMPT = """You are a revenue-conscious offer strategist for
payment recovery. Given transaction and customer data, decide whether an
incentive is needed to recover this payment, and if so, how much.

Be conservative — most recoveries need NO discount, just a working retry link.
Only recommend a discount for high-value customers at risk of churn, or when
the failure reason suggests price hesitation (e.g. cart abandonment patterns).

Respond ONLY with a JSON object in this exact schema (no markdown, no explanation):
{
  "agent": "offer",
  "needs_incentive": true | false,
  "discount_percent": integer (0 if no incentive),
  "confidence": "high" | "medium" | "low",
  "summary": "one sentence explanation of the offer decision"
}
"""


async def offer_agent(state: RecoveryState) -> dict:
    """Decides whether an incentive is needed to recover the payment — runs in parallel."""
    llm = _get_llm()
    messages = [
        SystemMessage(content=OFFER_SYSTEM_PROMPT),
        HumanMessage(content=f"Decide on offer strategy for this failed transaction:\n\n{_txn_context(state)}"),
    ]
    try:
        response = await llm_retry(llm.ainvoke)(messages)
        finding = _parse_json_response(response.content, "offer")
    except (APIConnectionError, APITimeoutError, RateLimitError) as e:
        # Safe fallback — no discount is the conservative default; we'd
        # rather under-offer than have a broken agent call over-discount.
        logger.error("offer_agent failed after retries for %s: %s", state['transaction_id'], e)
        finding = {"agent": "offer", "needs_incentive": False, "discount_percent": 0,
                   "confidence": "error", "summary": f"LLM call failed after retries, defaulted to no discount: {e}"}
    return {"findings": [finding]}
