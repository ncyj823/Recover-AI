"""
RecoverAI's LangGraph agentic recovery pipeline.

Graph structure:
    fetch_transaction_context
              │
    ┌─────────┼─────────┐
    ▼         ▼         ▼        ← parallel fan-out (all 3 run simultaneously)
diagnosis  channel    offer
    └─────────┼─────────┘
              ▼
          aggregate            ← merges 3 findings into one recovery_plan
              │
              ▼
       execute_recovery        ← calls action_mcp to actually send the nudge

Running this file directly:
    python pipeline.py --transaction-id TXN12345
"""

import asyncio
import json
import os
import sys
import argparse

from dotenv import load_dotenv
from langgraph.graph import StateGraph, START, END

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "action_mcp"))

from state import RecoveryState
from agents import diagnosis_agent, channel_agent, offer_agent
from action_client import generate_retry_link, dispatch
import audit
import compliance

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))


# ---------------------------------------------------------------------------
# Node 1: fetch_transaction_context
# In production this would query Razorpay's transaction DB / webhook payload.
# For the buildathon, it reads from the loaded dataset (see data/load_transactions.py).
# ---------------------------------------------------------------------------

async def fetch_transaction_context(state: RecoveryState) -> dict:
    """Placeholder passthrough — real values are already set by run_recovery().
    This node exists so the graph shape mirrors Reviewly (fetch → fan-out)
    and so a future version can plug in a real DB/webhook lookup here
    without touching the rest of the graph."""
    print(f"[fetch] Loaded transaction {state['transaction_id']} "
          f"(INR {state['amount']}, {state['payment_method']}, {state['failure_reason_code']})")
    return {"findings": []}


# ---------------------------------------------------------------------------
# Node 5: aggregate
# Merges diagnosis + channel + offer findings into one actionable recovery_plan.
# ---------------------------------------------------------------------------

def _build_message(state: RecoveryState, offer_finding: dict, retry_link: str) -> str:
    amount = state["amount"]
    if offer_finding.get("needs_incentive"):
        pct = offer_finding.get("discount_percent", 0)
        final = amount * (1 - pct / 100)
        return (f"Your payment of INR {amount:.2f} didn't go through. "
                f"Complete it now for just INR {final:.2f} ({pct}% off): {retry_link}")
    return f"Your payment of INR {amount:.2f} didn't go through. Retry here: {retry_link}"


async def aggregate(state: RecoveryState) -> dict:
    """Merge all 3 agent findings into one structured recovery plan."""
    txn_id = state["transaction_id"]
    findings = state.get("findings", [])
    by_agent = {f.get("agent"): f for f in findings}
    diagnosis = by_agent.get("diagnosis", {})
    channel = by_agent.get("channel", {})
    offer = by_agent.get("offer", {})

    # Audit every agent's decision, independent of what happens downstream.
    audit.log_diagnosis(txn_id, diagnosis)
    audit.log_channel_decision(txn_id, channel)
    audit.log_offer_decision(txn_id, offer)

    print(f"[aggregate] root_cause={diagnosis.get('root_cause')} "
          f"recoverable={diagnosis.get('recoverable')} "
          f"channel={channel.get('channel')} "
          f"needs_incentive={offer.get('needs_incentive')}")

    if diagnosis.get("recoverable") is False:
        reason = diagnosis.get("summary", "not recoverable")
        plan = {"skip": True, "reason": reason}
        audit.log_recovery_plan(txn_id, plan)
        return {"recovery_plan": plan}

    # Compliance check BEFORE generating any retry link or discount —
    # gates the action, doesn't just log it after the fact.
    allowed, block_reason = compliance.check(txn_id, state["customer_id"])
    if not allowed:
        audit.log_stopping_rule_triggered(txn_id, state["customer_id"], block_reason)
        plan = {"skip": True, "reason": f"stopping_rule:{block_reason}"}
        audit.log_recovery_plan(txn_id, plan)
        return {"recovery_plan": plan}

    # Discount is capped here — agents can PROPOSE a discount, but the
    # ceiling is enforced in code, not left to the LLM's judgment.
    discount = compliance.cap_discount(offer.get("discount_percent", 0))

    retry_link = await generate_retry_link(txn_id, state["amount"], discount)
    message = _build_message(state, {**offer, "discount_percent": discount}, retry_link)

    plan = {
        "skip": False,
        "channel": channel.get("channel", "sms"),
        "send_delay_minutes": channel.get("send_delay_minutes", 0),
        "message": message,
        "retry_link": retry_link,
        "discount_percent": discount,
        "root_cause": diagnosis.get("root_cause"),
    }
    audit.log_recovery_plan(txn_id, plan)
    return {"recovery_plan": plan}


# ---------------------------------------------------------------------------
# Node 6: execute_recovery
# Dispatches the recovery action via the (mocked) action_mcp client.
# ---------------------------------------------------------------------------

async def execute_recovery(state: RecoveryState) -> dict:
    """Send the recovery nudge through the chosen channel."""
    txn_id = state["transaction_id"]
    plan = state.get("recovery_plan", {}) or {}

    if plan.get("skip"):
        print(f"[execute] Skipping — {plan.get('reason')}")
        audit.log_skipped(txn_id, plan.get("reason", "unknown"))
        return {"action_result": {"status": "skipped", "reason": plan.get("reason")}}

    delay = plan.get("send_delay_minutes", 0)
    print(f"[execute] Dispatching via {plan['channel']} (delay={delay}min)...")

    # NOTE: real delay would be handled by the Redis/RQ worker scheduling this
    # job for later — see worker_service/worker.py. Here we send immediately
    # for demo purposes.
    result = await dispatch(plan["channel"], state["customer_id"], plan["message"])
    result["amount_at_risk"] = state["amount"]
    result["discount_percent"] = plan.get("discount_percent", 0)
    result["root_cause"] = plan.get("root_cause")

    # Only record the attempt/cooldown AFTER a successful dispatch —
    # a failed send shouldn't burn one of the customer's 3 attempts.
    compliance.record_action(txn_id, state["customer_id"])
    audit.log_action_taken(txn_id, state["customer_id"], result)

    print(f"[execute] [OK] Sent via {result['channel']}: {result['message_id']}")
    return {"action_result": result}


# ---------------------------------------------------------------------------
# Build the graph
# ---------------------------------------------------------------------------

def build_graph() -> StateGraph:
    """Assemble and compile the full recovery pipeline graph."""
    builder = StateGraph(RecoveryState)

    builder.add_node("fetch_transaction_context", fetch_transaction_context)
    builder.add_node("diagnosis_agent", diagnosis_agent)
    builder.add_node("channel_agent", channel_agent)
    builder.add_node("offer_agent", offer_agent)
    builder.add_node("aggregate", aggregate)
    builder.add_node("execute_recovery", execute_recovery)

    builder.add_edge(START, "fetch_transaction_context")

    # Fan-out: fetch → all 3 agents IN PARALLEL
    builder.add_edge("fetch_transaction_context", "diagnosis_agent")
    builder.add_edge("fetch_transaction_context", "channel_agent")
    builder.add_edge("fetch_transaction_context", "offer_agent")

    # Fan-in: all 3 agents → aggregate
    builder.add_edge("diagnosis_agent", "aggregate")
    builder.add_edge("channel_agent", "aggregate")
    builder.add_edge("offer_agent", "aggregate")

    builder.add_edge("aggregate", "execute_recovery")
    builder.add_edge("execute_recovery", END)

    return builder.compile()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

async def run_recovery(transaction: dict):
    """Run the full recovery pipeline against one failed transaction record."""
    graph = build_graph()

    initial_state: RecoveryState = {
        "transaction_id": transaction["transaction_id"],
        "customer_id": transaction["customer_id"],
        "amount": transaction["amount"],
        "payment_method": transaction["payment_method"],
        "failure_reason_code": transaction["failure_reason_code"],
        "merchant_category": transaction.get("merchant_category", "general"),
        "customer_history": transaction.get("customer_history", {}),
        "findings": [],
        "recovery_plan": None,
        "action_result": None,
    }

    print(f"\n{'='*50}")
    print(f"  RecoverAI — recovering {transaction['transaction_id']}")
    print(f"{'='*50}\n")

    start = asyncio.get_event_loop().time()
    final_state = await graph.ainvoke(initial_state)
    elapsed = asyncio.get_event_loop().time() - start

    print(f"\n{'='*50}")
    print(f"  Done in {elapsed:.1f}s")
    print(f"  Result: {json.dumps(final_state.get('action_result'), indent=2)}")
    print(f"{'='*50}\n")

    return final_state


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run RecoverAI on a failed transaction")
    parser.add_argument("--transaction-id", required=True)
    parser.add_argument("--amount", type=float, default=1499.00)
    parser.add_argument("--customer-id", default="CUST001")
    parser.add_argument("--payment-method", default="UPI")
    parser.add_argument("--failure-reason", default="TIMEOUT")
    args = parser.parse_args()

    txn = {
        "transaction_id": args.transaction_id,
        "customer_id": args.customer_id,
        "amount": args.amount,
        "payment_method": args.payment_method,
        "failure_reason_code": args.failure_reason,
        "merchant_category": "e-commerce",
        "customer_history": {"past_orders": 4, "past_recovery_response_rate": 0.6},
    }

    asyncio.run(run_recovery(txn))
