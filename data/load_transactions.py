"""
load_transactions.py — loads failed transactions to feed into RecoverAI.

Two modes:
1. Real dataset: drop the Kaggle "UPI Payment Transactions Dataset" CSV
   (https://www.kaggle.com/datasets/devildyno/upi-payment-transactions-dataset)
   at data/upi_transactions.csv. This loader filters to failed transactions
   and maps its columns onto RecoverAI's transaction schema.
2. Mock mode (fallback, no CSV needed): generates realistic synthetic failed
   transactions so the pipeline can be demoed/tested without the dataset.

Run directly to preview what will be fed into the pipeline:
    python load_transactions.py --mode mock --n 5
    python load_transactions.py --mode csv
"""

import argparse
import csv
import os
import random
import uuid

CSV_PATH = os.path.join(os.path.dirname(__file__), "upi_transactions.csv")

FAILURE_CODES = ["TIMEOUT", "INSUFFICIENT_FUNDS", "BANK_DECLINE", "OTP_FAILURE", "GATEWAY_ERROR"]
PAYMENT_METHODS = ["UPI", "Credit Card", "Debit Card", "Net Banking"]
MERCHANT_CATEGORIES = ["e-commerce", "food_delivery", "travel", "subscription", "utilities"]


def load_mock_transactions(n: int = 10) -> list[dict]:
    """Generate n synthetic failed transactions with varied profiles.

    Mix of profiles is intentional — it exercises all three agents
    differently (a repeat high-spender vs. a first-time low-value buyer
    should get different channel/offer decisions from the LLM agents).
    """
    transactions = []
    for i in range(n):
        past_orders = random.choice([0, 1, 2, 5, 12, 20])
        transactions.append({
            "transaction_id": f"TXN{1000 + i}",
            "customer_id": f"CUST{100 + i}",
            "amount": round(random.uniform(150, 5000), 2),
            "payment_method": random.choice(PAYMENT_METHODS),
            "failure_reason_code": random.choice(FAILURE_CODES),
            "merchant_category": random.choice(MERCHANT_CATEGORIES),
            "customer_history": {
                "past_orders": past_orders,
                "past_recovery_response_rate": round(random.uniform(0.1, 0.9), 2) if past_orders else 0.0,
            },
        })
    return transactions


def load_csv_transactions(limit: int | None = None) -> list[dict]:
    """Load and filter failed transactions from the Kaggle CSV, if present.

    NOTE: exact column names depend on the dataset version — check the
    header row after downloading and adjust FIELD_MAP below if needed.
    This is written defensively to handle a few common column-naming
    variants seen across UPI transaction datasets on Kaggle.
    """
    if not os.path.exists(CSV_PATH):
        raise FileNotFoundError(
            f"Dataset not found at {CSV_PATH}. Download it from Kaggle "
            f"(devildyno/upi-payment-transactions-dataset) and place the CSV there, "
            f"or use --mode mock to test without it."
        )

    transactions = []
    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        headers = {h.lower().strip(): h for h in reader.fieldnames or []}

        def col(*candidates):
            for c in candidates:
                if c in headers:
                    return headers[c]
            return None

        status_col = col("status", "transaction_status", "txn_status")
        amount_col = col("amount", "transaction_amount", "txn_amount")
        method_col = col("payment_method", "mode", "transaction_type")
        customer_col = col("customer_id", "user_id", "sender")

        for row in reader:
            status = (row.get(status_col, "") or "").strip().lower() if status_col else ""
            if status not in {"failed", "failure", "declined"}:
                continue

            transactions.append({
                "transaction_id": row.get("transaction_id") or f"TXN{uuid.uuid4().hex[:8]}",
                "customer_id": row.get(customer_col, f"CUST{uuid.uuid4().hex[:6]}") if customer_col else f"CUST{uuid.uuid4().hex[:6]}",
                "amount": float(row.get(amount_col, 0) or 0) if amount_col else 0.0,
                "payment_method": row.get(method_col, "UPI") if method_col else "UPI",
                "failure_reason_code": random.choice(FAILURE_CODES),  # dataset may not have this — infer/randomize
                "merchant_category": random.choice(MERCHANT_CATEGORIES),
                "customer_history": {"past_orders": random.randint(0, 20),
                                      "past_recovery_response_rate": round(random.uniform(0.1, 0.9), 2)},
            })
            if limit and len(transactions) >= limit:
                break

    return transactions


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Preview transactions RecoverAI will process")
    parser.add_argument("--mode", choices=["mock", "csv"], default="mock")
    parser.add_argument("--n", type=int, default=5)
    args = parser.parse_args()

    txns = load_mock_transactions(args.n) if args.mode == "mock" else load_csv_transactions(args.n)
    for t in txns:
        print(t)
