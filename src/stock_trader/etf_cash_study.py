"""ADR-0048: source audit and raw-share SPY cash-distribution replay, no network."""

import argparse
import json
from collections import Counter
from datetime import datetime
from decimal import ROUND_FLOOR
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import numpy as np
import pandas as pd

from stock_trader.clock import utcnow
from stock_trader.etf_ledger import SHARE_STEP, CashLedger, number
from stock_trader.etf_study import DATA_CONTRACT, SYMBOLS, validate_frames
from stock_trader.modeling import checksum
from stock_trader.research_cache import completed_collection
from stock_trader.study import write_json

CONTRACT = {
    "decision": "ADR-0048",
    "symbols": list(SYMBOLS),
    "process_start": "2016-01-01",
    "process_end": "2026-05-31",
    "endpoint": "/v1/corporate-actions",
    "types": "all",
    "limit": 1000,
    "ssga_url": "https://www.ssga.com/library-content/products/fund-data/etfs/us/spdr-etf-historical-distributions.xlsx",
    "analysis_symbol": "SPY",
    "analysis_start": "2018-01-01",
    "analysis_end": "2025-12-31",
    "reinvestment": "available dividend cash at next month first session open",
    "cost_per_side_bps": [20, 40],
    "withholding_tax": "not modeled; gross USD distributions",
    "pay_date_availability": "first trading session on/after payable_date",
}


def ssga_spy(path):
    ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with ZipFile(path) as source:
        if "xl/sharedStrings.xml" in source.namelist():
            strings = [
                "".join(t.text or "" for t in node.iterfind(".//s:t", ns))
                for node in ET.fromstring(source.read("xl/sharedStrings.xml")).findall("s:si", ns)
            ]
        else:
            strings = []
        rows = ET.fromstring(source.read("xl/worksheets/sheet1.xml")).findall("s:sheetData/s:row", ns)
    actions, seen = [], set()
    for index, row in enumerate(rows):
        values = {}
        for cell in row:
            column = "".join(c for c in cell.attrib["r"] if c.isalpha())
            value = cell.find("s:v", ns)
            text = value.text if value is not None else ""
            if cell.attrib.get("t") == "s":
                text = strings[int(text)]
            elif cell.attrib.get("t") == "inlineStr":
                text = "".join(t.text or "" for t in cell.iterfind(".//s:t", ns))
            values[column] = text.strip()
        if index == 0:
            required = {"B": "TICKER", "C": "CUSIP", "D": "EX-DATE", "F": "PAYABLE DATE", "G": "DIVIDEND ($)"}
            if any(values.get(k) != v for k, v in required.items()):
                raise ValueError("SSGA schema changed")
            continue
        if values.get("B") != "SPY":
            continue
        ex = datetime.strptime(values["D"], "%m/%d/%Y").date().isoformat()
        if not "2016-01-01" <= ex < "2026-01-01":
            continue
        if values.get("C") != "78462F103" or ex in seen:
            raise ValueError("wrong US security or duplicate distribution")
        seen.add(ex)
        pay = datetime.strptime(values["F"], "%m/%d/%Y").date().isoformat()
        if pay < ex:
            raise ValueError("invalid US distribution payment date")
        rates = [number(values.get(c) or "0") for c in ("G", "H", "I")]
        if min(rates) < 0 or sum(rates) <= 0:
            raise ValueError("invalid distribution")
        actions.append(
            {
                "id": f"ssga-SPY-{ex}",
                "symbol": "SPY",
                "kind": "cash_dividend",
                "ex_date": ex,
                "pay_date": pay,
                "rate": str(sum(rates)),
                "currency": "USD",
                "special": False,
            }
        )
    return sorted(actions, key=lambda a: a["ex_date"])


def seal_sources(directory):
    cached = completed_collection(directory, CONTRACT)
    if cached is not None:
        return cached
    manifest = directory / "manifest.json"
    if manifest.exists() and json.loads(manifest.read_text()) != CONTRACT:
        raise ValueError("corporate source contract changed")
    response = json.loads((directory / "probe.json").read_text())
    if response.get("next_page_token"):
        raise ValueError("incomplete corporate actions pagination")
    ids = set()
    for events in response["corporate_actions"].values():
        for event in events:
            if event["id"] in ids or event["symbol"] not in SYMBOLS:
                raise ValueError("duplicate or unexpected corporate action")
            if not CONTRACT["process_start"] <= event["process_date"] <= CONTRACT["process_end"]:
                raise ValueError("corporate action outside process boundary")
            ids.add(event["id"])
    ssga_spy(directory / "ssga-distributions.xlsx")
    write_json(manifest, CONTRACT)
    result = {
        "contract": CONTRACT,
        "completed_at": utcnow().isoformat(),
        "alpaca_gets": 1,
        "public_file_gets": 1,
        "alpaca_events": len(ids),
        "source_sha256": checksum(Path(__file__)),
        "file_sha256": {n: checksum(directory / n) for n in ("probe.json", "ssga-distributions.xlsx")},
    }
    write_json(directory / "collection.json", result)
    return result


def source_audit(raw, adjusted, actions, response):
    old = response["corporate_actions"].get("cash_dividends", [])
    summary = {}
    for symbol in SYMBOLS:
        records = [a for a in old if a["symbol"] == symbol and "2016-01-01" <= a["ex_date"] < "2026-01-01"]
        summary[symbol] = {
            "count": len(records),
            "missing_pay_date": sum(not a.get("payable_date") for a in records),
            "annual_counts": dict(sorted(Counter(a["ex_date"][:4] for a in records).items())),
        }
    raw = raw.loc[raw.symbol.eq("SPY")].set_index("date").sort_index()
    adjusted = adjusted.loc[adjusted.symbol.eq("SPY")].set_index("date").sort_index()
    ratio = adjusted.close / raw.close
    jumps = ratio / ratio.shift(1) - 1
    # The provider rounds adjusted prices to cents. Compare entire possible
    # factor intervals; a small change inside rounding error is not an event.
    lower = (adjusted.close - 0.005) / raw.close
    upper = (adjusted.close + 0.005) / raw.close
    jump_low, jump_high = lower / upper.shift(1) - 1, upper / lower.shift(1) - 1
    observed = set(jumps.index[(jump_low > 1e-9) | (jump_high < -1e-9)])
    expected = {a["ex_date"] for a in actions}
    previous = raw.close.shift(1)
    comparisons = []
    for event in actions:
        day = event["ex_date"]
        if day not in previous.index or not np.isfinite(previous.loc[day]):
            raise ValueError("missing pre-distribution raw price")
        expected_jump = 1 / (1 - float(event["rate"]) / previous.loc[day]) - 1
        comparisons.append(
            {
                "ex_date": day,
                "actual_factor_jump": float(jumps.loc[day]),
                "expected_factor_jump": expected_jump,
                "absolute_error": abs(float(jumps.loc[day]) - expected_jump),
                "within_rounding_interval": bool(
                    jump_low.loc[day] - 1e-9 <= expected_jump <= jump_high.loc[day] + 1e-9
                ),
            }
        )
    provider_rows = [a for a in old if a["symbol"] == "SPY" and "2016-01-01" <= a["ex_date"] < "2026-01-01"]
    provider = {a["ex_date"]: a for a in provider_rows}
    official = {a["ex_date"]: a for a in actions}
    rate_differences = [
        {"ex_date": day, "alpaca": a["rate"], "ssga": official[day]["rate"]}
        for day, a in sorted(provider.items())
        if day in official and abs(float(a["rate"]) - float(official[day]["rate"])) > 0.000001
    ]
    pay_differences = [
        {"ex_date": day, "alpaca": a["payable_date"], "ssga": official[day]["pay_date"]}
        for day, a in sorted(provider.items())
        if day in official and a.get("payable_date") and a["payable_date"] != official[day]["pay_date"]
    ]
    years = Counter(a["ex_date"][:4] for a in actions)
    same_symbol_other_actions = [
        kind
        for kind, rows in response["corporate_actions"].items()
        if kind != "cash_dividends"
        and any(a["symbol"] == "SPY" and a["ex_date"] < "2026-01-01" for a in rows)
    ]
    checks = {
        "quarterly_coverage": all(years[str(y)] == 4 for y in range(2016, 2026)),
        "adjustment_dates_match": observed == expected,
        "adjustment_amounts_match": all(c["within_rounding_interval"] for c in comparisons),
        "no_other_spy_actions": not same_symbol_other_actions,
    }
    raw_checks = {
        "official_40_unique_quarterly_events": len(actions) == len(expected) == 40
        and all(
            sum(pd.Period(a["ex_date"], freq="Q") == quarter for a in actions) == 1
            for quarter in pd.period_range("2016Q1", "2025Q4", freq="Q")
        ),
        "shared_rates_and_known_pay_dates_match": not rate_differences and not pay_differences,
        "provider_no_extra_or_duplicate_events": set(provider).issubset(expected)
        and len(provider) == len(provider_rows),
        "adjustment_discrepancies_explained_by_provider_omissions": not (observed - expected)
        and all(
            c["within_rounding_interval"] or (c["ex_date"] not in provider and c["ex_date"] not in observed)
            for c in comparisons
        ),
        "no_unsupported_spy_actions": not same_symbol_other_actions,
    }
    return {
        "alpaca_coverage": summary,
        "spy_ssga_rows": len(actions),
        "checks": checks,
        "raw_replay_checks": raw_checks,
        "raw_replay_permitted": all(raw_checks.values()),
        "unexpected_adjustment_dates": sorted(observed - expected),
        "distribution_without_adjustment": sorted(expected - observed),
        "ssga_spy_missing_in_alpaca": sorted(expected - set(provider)),
        "spy_missing_alpaca_pay_dates": [
            day for day, a in sorted(provider.items()) if not a.get("payable_date")
        ],
        "provider_rate_differences": rate_differences,
        "provider_pay_date_differences": pay_differences,
        "adjustment_comparisons": comparisons,
        "coverage_limit": "cross-source consistency, not a point-in-time corporate-action guarantee",
    }


def replay(raw, actions, cost_bps):
    prices = raw.loc[raw.symbol.eq("SPY") & raw.date.between("2018-01-01", "2025-12-31")].sort_values("date")
    ledger = CashLedger("100000")
    by_date = {a["ex_date"]: a for a in actions}
    curves, previous_month = [], None
    for row in prices.itertuples():
        ledger.open_day(row.date, [by_date[row.date]] if row.date in by_date else [])
        month = row.date[:7]
        if month != previous_month:
            opening = number(row.open)
            quantity = (ledger.cash / (opening * (1 + number(cost_bps) / 10000))).quantize(
                SHARE_STEP, rounding=ROUND_FLOOR
            )
            if quantity * opening >= 1:
                ledger.fill(
                    f"buy-{row.date}",
                    "SPY",
                    "buy",
                    quantity,
                    opening,
                    quantity * opening * number(cost_bps) / 10000,
                )
        equity = ledger.equity({"SPY": row.close})
        curves.append(
            {
                "date": row.date,
                "equity": float(equity),
                "cash": str(ledger.cash),
                "shares": str(ledger.positions.get("SPY", 0)),
                "receivable": str(sum((number(e["amount"]) for e in ledger.receivables.values()), number(0))),
            }
        )
        previous_month = month
    last = prices.iloc[-1]
    exit_cost = ledger.positions["SPY"] * number(last.close) * number(cost_bps) / 10000
    curves[-1]["equity"] -= float(exit_cost)
    curve = pd.DataFrame(curves).set_index("date")
    nav = curve.equity
    years = ((pd.Timestamp(nav.index[-1]) - pd.Timestamp(nav.index[0])).days + 1) / 365.25
    payments = [e for e in ledger.journal if e["type"] == "dividend_payment"]
    fills = [e for e in ledger.journal if e["type"] == "fill"]
    return (
        {
            "cost_per_side_bps": cost_bps,
            "net_return": nav.iloc[-1] / 100000 - 1,
            "cagr": (nav.iloc[-1] / 100000) ** (1 / years) - 1,
            "max_drawdown_close": float((1 - nav / nav.cummax().clip(lower=100000)).max()),
            "terminal_equity": float(nav.iloc[-1]),
            "estimated_exit_cost": str(exit_cost),
            "dividend_cash_received": str(sum((number(e["amount"]) for e in payments), number(0))),
            "paid_events": len(payments),
            "buy_fills": len(fills),
            "final_cash": str(ledger.cash),
            "final_receivables": ledger.receivables,
            "min_cash": min(float(e["cash"]) for e in curves),
            "explicit_costs": str(exit_cost + sum((number(e["fee"]) for e in fills), number(0))),
        },
        curve,
        ledger.snapshot(),
    )


def run(data, actions_dir, output):
    if output.exists():
        raise ValueError("new output directory required")
    if completed_collection(data, DATA_CONTRACT) is None:
        raise ValueError("missing original ETF cache")
    source = seal_sources(actions_dir)
    raw, adjusted = (pd.read_parquet(data / f"{mode}.parquet") for mode in ("raw", "all"))
    validate_frames(raw, adjusted, json.loads((data / "calendar.json").read_text()))
    actions = ssga_spy(actions_dir / "ssga-distributions.xlsx")
    audit = source_audit(raw, adjusted, actions, json.loads((actions_dir / "probe.json").read_text()))
    output.mkdir(parents=True)
    report = {
        "created_at": utcnow().isoformat(),
        "contract": CONTRACT,
        "correction_decision": "ADR-0050",
        "raw_execution": {"fractional_places": 9, "minimum_buy_usd": 1, "taxes": "not modeled"},
        "source": source,
        "audit": audit,
        "evaluations": [],
        "api_requests": 0,
        "orders": 0,
        "promoted": False,
        "holdout_evaluated": False,
        "input_sha256": {
            str(p): checksum(p)
            for p in [
                data / "raw.parquet",
                data / "all.parquet",
                data / "calendar.json",
                data / "collection.json",
                actions_dir / "collection.json",
            ]
        },
    }
    write_json(output / "registration.json", report)
    write_json(output / "normalized-spy-actions.json", actions)
    for module in (Path(__file__), Path(__file__).with_name("etf_ledger.py")):
        (output / module.name).write_bytes(module.read_bytes())
    if audit["raw_replay_permitted"]:
        for cost in (20, 40):
            metrics, curve, ledger = replay(raw, actions, cost)
            curve.to_csv(output / f"spy-raw-{cost}bps-equity.csv")
            write_json(output / f"spy-raw-{cost}bps-ledger.json", ledger)
            report["evaluations"].append(metrics)
    report["output_sha256"] = {p.name: checksum(p) for p in output.iterdir() if p.is_file()}
    report["completed_at"] = utcnow().isoformat()
    write_json(output / "report.json", report)
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--actions", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = run(args.data, args.actions, args.output)
    print(json.dumps({"data_checks": result["audit"]["checks"], "evaluations": result["evaluations"]}))


if __name__ == "__main__":
    main()
