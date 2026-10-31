#!/usr/bin/env python3
"""Operator refund script for Paddle Billing (LevelNRead).

Creates a Paddle adjustment (refund) for a transaction. This is the refund
ISSUANCE channel; the refund INTAKE channel is the backend webhook
(adjustment.created -> record + cancel subscription at period end, per the
default policy documented in backend.py).

Usage:
  PADDLE_API_KEY=pdl_... PADDLE_ENVIRONMENT=sandbox \
      python3 scripts/paddle_refund.py txn_01... --reason "Customer request"
  Partial refund (amount in smallest currency unit, e.g. 500 = $5.00):
      python3 scripts/paddle_refund.py txn_01... --amount 500 --reason "..." --yes

Notes:
- Paddle refunds do NOT auto-cancel the subscription. A full refund created
  here triggers the adjustment.created webhook, which schedules the
  subscription to cancel at period end (backend default policy).
- Full refund = every item type "full". Partial = the given amount applied
  to the first item only (multi-item partial splits must be done in the
  Paddle dashboard).
- Never commit API keys into the repo; pass them via environment variables.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def api_base() -> str:
    return "https://api.paddle.com" if os.environ.get("PADDLE_ENVIRONMENT") == "live" else "https://sandbox-api.paddle.com"


def api_request(method: str, path: str, body=None) -> dict:
    key = os.environ.get("PADDLE_API_KEY")
    if not key:
        print("ERROR: PADDLE_API_KEY is not set", file=sys.stderr)
        sys.exit(2)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(api_base() + path, data=data, method=method, headers={
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read().decode("utf-8"))
            print(f"ERROR: Paddle HTTP {e.code}: {json.dumps(err.get('error', {}))}", file=sys.stderr)
        except Exception:
            print(f"ERROR: Paddle HTTP {e.code}", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Refund a Paddle transaction (LevelNRead)")
    parser.add_argument("transaction_id", help="Paddle transaction id, e.g. txn_01...")
    parser.add_argument("--reason", default="Customer request", help="Refund reason shown in Paddle")
    parser.add_argument("--amount", default=None,
                        help="Partial refund amount in smallest currency unit (omit = full refund)")
    parser.add_argument("--yes", action="store_true", help="Skip confirmation prompt")
    args = parser.parse_args()

    txn = api_request("GET", f"/transactions/{args.transaction_id}").get("data") or {}
    items = txn.get("items") or []
    if not items:
        print(f"ERROR: transaction {args.transaction_id} has no items to refund", file=sys.stderr)
        sys.exit(1)
    print(f"Transaction: {txn.get('id')}  status={txn.get('status')}  "
          f"subscription={txn.get('subscription_id')}  customer={txn.get('customer_id')}")
    for it in items:
        price = (it.get("price") or {})
        print(f"  item {it.get('id')} price={price.get('id')} qty={it.get('quantity')}")

    if args.amount is None:
        adj_items = [{"item_id": it["id"], "type": "full"} for it in items]
        desc = f"FULL refund of {len(items)} item(s)"
    else:
        adj_items = [{"item_id": items[0]["id"], "type": "partial", "amount": args.amount}]
        desc = f"PARTIAL refund of {args.amount} on item {items[0]['id']} (first item only)"

    print(f"Action: {desc}  reason: {args.reason}")
    if not args.yes:
        confirm = input("Type YES to create this adjustment: ").strip()
        if confirm != "YES":
            print("Aborted.")
            return

    result = api_request("POST", "/adjustments", {
        "action": "refund",
        "transaction_id": args.transaction_id,
        "reason": args.reason,
        "items": adj_items,
    })
    adj = result.get("data") or {}
    print(f"Created adjustment {adj.get('id')} action={adj.get('action')} "
          f"status={adj.get('status')} amount={adj.get('amount')}")
    print("Note: the backend webhook will pick up adjustment.created and, for a full "
          "approved refund, schedule the subscription to cancel at period end.")


if __name__ == "__main__":
    main()
