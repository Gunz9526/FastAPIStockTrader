"""Independent decimal audit of saved SPY fills, entitlements, payments and NAV."""

import argparse
import json
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

import pandas as pd

from stock_trader.clock import utcnow
from stock_trader.etf_cash_study import CONTRACT
from stock_trader.modeling import checksum
from stock_trader.research_cache import completed_collection
from stock_trader.study import write_json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--actions", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--guard", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source = completed_collection(args.actions, CONTRACT)
    report = json.loads((args.report / "report.json").read_text())
    if source is None or not report["audit"]["raw_replay_permitted"] or report["promoted"]:
        raise ValueError("unvalidated cash study")
    for path, expected in report["input_sha256"].items():
        if checksum(Path(path)) != expected:
            raise ValueError("changed study input")
    for name, expected in report["output_sha256"].items():
        if checksum(args.report / name) != expected:
            raise ValueError("changed study output")
    guard_sha = checksum(args.guard)
    if guard_sha != "d4b630dbf77191269fa4da5b6e5925779ce75d752978f2265aeca6d975eb1129":
        raise ValueError("consumed final guard changed")
    raw = pd.read_parquet(args.data / "raw.parquet")
    prices = (
        raw.loc[raw.symbol.eq("SPY") & raw.date.between("2018-01-01", "2025-12-31")]
        .set_index("date")
        .sort_index()
    )
    actions = json.loads((args.report / "normalized-spy-actions.json").read_text())
    by_date = {a["ex_date"]: a for a in actions}
    results = []
    for cost in (20, 40):
        book = json.loads((args.report / f"spy-raw-{cost}bps-ledger.json").read_text())
        curve = pd.read_csv(args.report / f"spy-raw-{cost}bps-equity.csv", dtype={"date": str}).set_index(
            "date"
        )
        metric = next(m for m in report["evaluations"] if m["cost_per_side_bps"] == cost)
        grouped = defaultdict(list)
        for event in book["journal"]:
            grouped[event["date"]].append(event)
        cash, shares, owed, paid_ids = Decimal(100000), Decimal(0), {}, set()
        max_error, total_cost, previous_month = 0.0, Decimal(0), None
        for day, row in prices.iterrows():
            before = shares
            expected = by_date.get(day)
            events = grouped[day]
            entitlements = [e for e in events if e["type"] == "entitlement"]
            if bool(entitlements) != bool(expected) or len(entitlements) > 1:
                raise ValueError("missing or duplicate dividend entitlement")
            if expected:
                entitlement = before * Decimal(expected["rate"])
                entry = entitlements[0]
                if (
                    entry["id"] != expected["id"]
                    or Decimal(entry["amount"]) != entitlement
                    or Decimal(entry["quantity"]) != before
                ):
                    raise ValueError("wrong ex-date share ownership")
                owed[expected["id"]] = (entitlement, expected["pay_date"])
            due = {identity for identity, (_, payable) in owed.items() if payable <= day}
            payments = [e for e in events if e["type"] == "dividend_payment"]
            if {e["id"] for e in payments} != due or len(payments) != len(due):
                raise ValueError("early, late or duplicate payment")
            for event in payments:
                identity = event["id"]
                if identity in paid_ids or Decimal(event["amount"]) != owed[identity][0]:
                    raise ValueError("payment mismatch")
                cash += owed.pop(identity)[0]
                paid_ids.add(identity)
            for event in (e for e in events if e["type"] == "fill"):
                quantity, price, fee = (Decimal(event[k]) for k in ("quantity", "price", "fee"))
                if day[:7] == previous_month or event["side"] != "buy" or price != Decimal(str(row.open)):
                    raise ValueError("incorrect monthly opening fill")
                if fee != quantity * price * Decimal(cost) / 10000 or quantity * price < 1:
                    raise ValueError("incorrect fee or minimum notional")
                cash -= quantity * price + fee
                shares += quantity
                total_cost += fee
            if cash < 0:
                raise ValueError("cash borrowing")
            equity = cash + shares * Decimal(str(row.close)) + sum((x[0] for x in owed.values()), Decimal(0))
            if day == prices.index[-1]:
                exit_cost = shares * Decimal(str(row.close)) * Decimal(cost) / 10000
                if exit_cost != Decimal(metric["estimated_exit_cost"]):
                    raise ValueError("terminal cost mismatch")
                equity -= exit_cost
                total_cost += exit_cost
            max_error = max(max_error, abs(float(equity) - curve.loc[day, "equity"]))
            if (
                abs(float(cash) - float(curve.loc[day, "cash"])) > 1e-8
                or abs(float(shares) - float(curve.loc[day, "shares"])) > 1e-8
            ):
                raise ValueError("cash or quantity path mismatch")
            previous_month = day[:7]
        if max_error > 1e-7 or Decimal(book["cash"]) != cash or Decimal(book["positions"]["SPY"]) != shares:
            raise ValueError("final ledger mismatch")
        if total_cost != Decimal(metric["explicit_costs"]):
            raise ValueError("total cost mismatch")
        final_owed = {
            identity: (Decimal(e["amount"]), e["pay_date"]) for identity, e in book["receivables"].items()
        }
        if final_owed != owed or len(prices) != 2011:
            raise ValueError("receivable or session mismatch")
        results.append(
            {
                "cost_bps": cost,
                "sessions": len(prices),
                "paid_dividends": len(paid_ids),
                "pending_dividends": len(owed),
                "max_equity_error_usd": max_error,
                "cash_never_negative": True,
                "all_fills_after_cash_available": True,
            }
        )
    write_json(
        args.output,
        {
            "checked_at": utcnow().isoformat(),
            "results": results,
            "input_output_hashes_verified": True,
            "guard_sha256": guard_sha,
            "audit_source_sha256": checksum(Path(__file__)),
            "report_sha256": checksum(args.report / "report.json"),
            "api_requests": 0,
            "orders": 0,
        },
    )
    print(json.dumps({"audits": len(results), "verified": True, "results": results}))


if __name__ == "__main__":
    main()
