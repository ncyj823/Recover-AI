"""
Mock action client for RecoverAI.

In production this would call real WhatsApp Business API, SMS gateway (e.g.
MSG91), email service (e.g. SES), and Razorpay's payment link API to generate
a retry link. Since the buildathon doesn't grant real gateway/API access,
this simulates those calls with realistic latency + structured responses so
the rest of the pipeline (and the demo) works end-to-end.

Swapping this for real APIs later = implement the same function signatures
with real HTTP calls. Nothing else in the pipeline needs to change — this is
the same "MCP tool as a swappable boundary" pattern as Reviewly's github_client.
"""

import asyncio
import random
import uuid
from datetime import datetime, timezone


async def generate_retry_link(transaction_id: str, amount: float, discount_percent: int = 0) -> str:
    """Simulate generating a Razorpay payment retry link, optionally with a discount applied."""
    await asyncio.sleep(0.2)  # simulate network latency
    token = uuid.uuid4().hex[:10]
    final_amount = amount * (1 - discount_percent / 100)
    return f"https://rzp.io/retry/{token}?txn={transaction_id}&amt={final_amount:.2f}"


async def send_whatsapp(customer_id: str, message: str) -> dict:
    """Simulate sending a WhatsApp message via Business API."""
    await asyncio.sleep(random.uniform(0.1, 0.3))
    return {
        "channel": "whatsapp",
        "status": "sent",
        "message_id": f"wa_{uuid.uuid4().hex[:8]}",
        "sent_at": datetime.now(timezone.utc).isoformat(),
        "customer_id": customer_id,
    }


async def send_sms(customer_id: str, message: str) -> dict:
    """Simulate sending an SMS via gateway."""
    await asyncio.sleep(random.uniform(0.1, 0.3))
    return {
        "channel": "sms",
        "status": "sent",
        "message_id": f"sms_{uuid.uuid4().hex[:8]}",
        "sent_at": datetime.now(timezone.utc).isoformat(),
        "customer_id": customer_id,
    }


async def send_email(customer_id: str, subject: str, message: str) -> dict:
    """Simulate sending a recovery email."""
    await asyncio.sleep(random.uniform(0.1, 0.3))
    return {
        "channel": "email",
        "status": "sent",
        "message_id": f"eml_{uuid.uuid4().hex[:8]}",
        "sent_at": datetime.now(timezone.utc).isoformat(),
        "customer_id": customer_id,
    }


async def send_push_notification(customer_id: str, message: str) -> dict:
    """Simulate sending a mobile push notification."""
    await asyncio.sleep(random.uniform(0.1, 0.3))
    return {
        "channel": "push_notification",
        "status": "sent",
        "message_id": f"push_{uuid.uuid4().hex[:8]}",
        "sent_at": datetime.now(timezone.utc).isoformat(),
        "customer_id": customer_id,
    }


CHANNEL_DISPATCH = {
    "whatsapp": send_whatsapp,
    "sms": send_sms,
    "push_notification": send_push_notification,
}


async def dispatch(channel: str, customer_id: str, message: str, subject: str = "") -> dict:
    """Route to the right send_* function based on channel name."""
    if channel == "email":
        return await send_email(customer_id, subject or "Complete your payment", message)
    fn = CHANNEL_DISPATCH.get(channel, send_sms)  # fall back to SMS if unknown channel
    return await fn(customer_id, message)
