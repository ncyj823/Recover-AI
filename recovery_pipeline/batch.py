"""
batch.py — run RecoverAI across a batch of failed transactions and report
measured impact.

This is the direct answer to Razorpay's Track 3 bar: "Don't just identify
the problem. Show measured money recovered across a batch." A single-
transaction demo can't show that — this script processes N failed
transactions through the full pipeline and prints/exports a summary report.

NOTE on "recovered": since we don't have a real payment gateway to confirm
actual recovery, this script reports "amount targeted for recovery" (i.e.
sum of transaction amounts where a compliant recovery action was
successfully dispatched) as the demo-mode proxy metric, and clearly labels
it as such. In production this would be replaced by a webhook confirming
the retry link was actually paid.

Run:
    python batch.py --mode mock --n 20
    python batch.py --mode csv
"""

import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "data"))
sys.path.insert(0, os.path.dirname(__file__))

from load_transactions import load_mock_transactions, load_csv_transactions
from pipeline import run_recovery
import audit
import compliance


async def run_batch(transactions: list[dict]) -> dict:
    """Run the pipeline sequentially over a batch (kept sequential, not
    parallel, so compliance cooldown/attempt state stays consistent and
    the demo output is easy to follow line-by-line)."""
    compliance.reset()  # fresh stopping-rule state per batch run

    results = []
    for txn in transactions:
        final_state = await run_recovery(txn)
        results.append({
            "transaction_id": txn["transaction_id"],
            "amount": txn["amount"],
            "action_result": final_state.get("action_result"),
        })

    total_at_risk = sum(t["amount"] for t in transactions)
    targeted = [r for r in results if (r["action_result"] or {}).get("status") not in {"skipped", None}]
    skipped = [r for r in results if (r["action_result"] or {}).get("status") == "skipped"]
    amount_targeted = sum(r["amount"] for r in targeted)

    skip_reasons = {}
    for r in skipped:
        reason = (r["action_result"] or {}).get("reason", "unknown")
        skip_reasons[reason] = skip_reasons.get(reason, 0) + 1

    report = {
        "batch_size": len(transactions),
        "total_amount_at_risk": round(total_at_risk, 2),
        "amount_targeted_for_recovery": round(amount_targeted, 2),
        "recovery_target_rate_percent": round(100 * amount_targeted / total_at_risk, 1) if total_at_risk else 0,
        "transactions_actioned": len(targeted),
        "transactions_skipped": len(skipped),
        "skip_reasons": skip_reasons,
        "note": (
            "'amount_targeted_for_recovery' = sum of transactions where a compliant "
            "recovery action was dispatched. Actual recovery requires a payment-gateway "
            "confirmation webhook, not available in this demo environment."
        ),
    }
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run RecoverAI across a batch and report impact")
    parser.add_argument("--mode", choices=["mock", "csv"], default="mock")
    parser.add_argument("--n", type=int, default=20)
    args = parser.parse_args()

    txns = load_mock_transactions(args.n) if args.mode == "mock" else load_csv_transactions(args.n)

    report = asyncio.run(run_batch(txns))

    print(f"\n{'='*60}")
    print("  RecoverAI Batch Report")
    print(f"{'='*60}")
    print(json.dumps(report, indent=2))
    print(f"{'='*60}")
    print(f"  Full audit trail: {audit.AUDIT_LOG_PATH}")
    print(f"{'='*60}\n")
