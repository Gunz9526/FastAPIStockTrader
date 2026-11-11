"""ADR-0055: one-shot, GET-only posted account reconciliation with immutable output."""

import argparse
import asyncio
import json
from pathlib import Path

from stock_trader.account_ledger import AccountLedger, read_activities
from stock_trader.broker import Alpaca
from stock_trader.clock import utcnow
from stock_trader.config import Settings
from stock_trader.etf_shadow import read_sealed, seal


def account_fields(row):
    return {k: row[k] for k in ("status", "cash", "equity", "trading_blocked", "account_blocked")}


def quantities(rows):
    if len({r["symbol"] for r in rows}) != len(rows):
        raise ValueError("duplicate broker positions")
    return {r["symbol"]: r["qty"] for r in rows}


async def main(output):
    if output.exists():
        cached = read_sealed(output)
        print(json.dumps({"cache_hit": True, "new_requests": 0, "result": cached["reconciliation"]}))
        return
    broker = Alpaca(Settings())
    try:
        before = account_fields(await broker.account())
        positions = quantities(await broker.positions())
        activities, pages = await read_activities(broker)
        orders = await broker.open_orders()
        after_positions = quantities(await broker.positions())
        after = account_fields(await broker.account())
    finally:
        await broker.close()
    stable = before == after and positions == after_positions and not orders
    ledger = AccountLedger("100000")
    reason = None
    try:
        ledger.apply(activities)
        result = ledger.reconcile(cash=after["cash"], positions=after_positions)
    except (ValueError, KeyError) as exc:
        reason = type(exc).__name__
        result = {"matched": False, "strategy_ownership_verified": False}
    result["snapshot_stable"] = stable
    result["audit_passed"] = stable and result["matched"] and reason is None
    report = {
        "decision": "ADR-0055",
        "checked_at": utcnow().isoformat(),
        "opening_cash_basis": "100000; prior independently observed empty Paper account",
        "account": after,
        "positions": after_positions,
        "open_orders": len(orders),
        "activity_count": len(activities),
        "read_requests": pages + 5,
        "reconciliation": result,
        "error_type": reason,
        "ledger": ledger.snapshot(),
        "activities": activities,
        "orders_submitted": 0,
        "actual_dividend_observed": any(r["activity_type"] == "DIV" for r in activities),
    }
    seal(output, report)
    print(
        json.dumps(
            {
                k: report[k]
                for k in (
                    "checked_at",
                    "activity_count",
                    "read_requests",
                    "reconciliation",
                    "actual_dividend_observed",
                )
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(main(args.output))
