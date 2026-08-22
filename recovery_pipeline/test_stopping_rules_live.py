"""
test_stopping_rules_live.py — manually verify compliance stopping rules
trigger correctly when the SAME transaction/customer is processed multiple
times within one process (mirrors how batch.py or a real worker would
behave over time, unlike separate `python pipeline.py` CLI calls which
each get fresh in-memory state).

Run:
    python recovery_pipeline/test_stopping_rules_live.py
"""

import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "action_mcp"))

from pipeline import run_recovery
import compliance

TEST_TXN = {
    "transaction_id": "TXN_DUP_TEST",
    "customer_id": "CUST_DUP_TEST",
    "amount": 500.0,
    "payment_method": "UPI",
    "failure_reason_code": "TIMEOUT",
    "merchant_category": "e-commerce",
    "customer_history": {"past_orders": 2, "past_recovery_response_rate": 0.5},
}


async def main():
    compliance.reset()
    print(f"Rules: MAX_ATTEMPTS={compliance.MAX_ATTEMPTS_PER_TRANSACTION}, "
          f"COOLDOWN_HOURS={compliance.COOLDOWN_HOURS}\n")

    for i in range(1, 5):
        print(f"\n{'#'*20} ATTEMPT {i} (same process) {'#'*20}")
        final_state = await run_recovery(TEST_TXN)
        result = final_state.get("action_result", {})
        print(f">>> ATTEMPT {i} RESULT: {result.get('status')} "
              f"(reason={result.get('reason', 'n/a')})")

    # Bonus: verify cooldown blocks a DIFFERENT transaction, same customer
    print(f"\n{'#'*20} DIFFERENT TXN, SAME CUSTOMER (cooldown check) {'#'*20}")
    other_txn = {**TEST_TXN, "transaction_id": "TXN_OTHER"}
    final_state = await run_recovery(other_txn)
    result = final_state.get("action_result", {})
    print(f">>> RESULT: {result.get('status')} (reason={result.get('reason', 'n/a')})")


if __name__ == "__main__":
    asyncio.run(main())
