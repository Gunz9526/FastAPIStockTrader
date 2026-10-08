"""ADR-0046: bounded monthly ETF allocation research, never submits orders."""

import argparse
import asyncio
import json
from pathlib import Path

import numpy as np
import pandas as pd

from stock_trader.broker import Alpaca
from stock_trader.clock import utcnow
from stock_trader.config import Settings
from stock_trader.data import atomic_parquet, normalized_bars
from stock_trader.modeling import checksum
from stock_trader.research_cache import completed_collection
from stock_trader.study import write_json

RISK_ASSETS = ("SPY", "EFA", "IEF", "DBC", "VNQ")
SYMBOLS = (*RISK_ASSETS, "BIL")
DATA_CONTRACT = {
    "decision": "ADR-0046",
    "feed": "sip",
    "timeframe": "1Day",
    "adjustments": ["raw", "all"],
    "symbols": list(SYMBOLS),
    "start": "2016-01-01",
    "end_exclusive": "2026-01-01",
    "purpose": "historical-development-not-independent-final",
}
STUDY_CONTRACT = {
    "decision": "ADR-0046",
    "data": DATA_CONTRACT,
    "candidates": ["trend_10m", "rotation_2"],
    "benchmarks": ["balanced_hold", "spy_hold", "bills_hold"],
    "evaluation_start": "2018-01-01",
    "evaluation_end": "2025-12-31",
    "trend_months": 10,
    "momentum_months": [3, 6, 12],
    "cost_per_side_bps": [20, 40],
    "execution": "next-month first session open",
    "sensitivity": "one additional session delay, candidates at 20bps only",
    "accounting": "fractional adjusted-price total-return proxy; not raw broker fills",
    "terminal": "estimated liquidation cost at last observed close",
    "leverage_limit": 1.0,
    "risk_gate_close_drawdown": 0.25,
    "holdout_evaluated": False,
    "promoted": False,
}


def validate_frames(raw, adjusted, calendar):
    dates = [c["date"] for c in calendar]
    if dates != sorted(set(dates)) or not dates or dates[0] < "2016-01-01" or dates[-1] >= "2026-01-01":
        raise ValueError("invalid historical calendar boundary")
    for frame in (raw, adjusted):
        if frame.duplicated(["date", "symbol"]).any() or set(frame.symbol) != set(SYMBOLS):
            raise ValueError("duplicate or unexpected ETF keys")
        prices = frame[["open", "high", "low", "close"]].to_numpy(dtype=float)
        if not np.isfinite(prices).all() or (prices <= 0).any():
            raise ValueError("invalid ETF prices")
        if (frame.high < frame.low).any():
            raise ValueError("invalid OHLC range")
        if any(sorted(g.date.tolist()) != dates for _, g in frame.groupby("symbol")):
            raise ValueError("missing ETF calendar session")
    keys = ["date", "symbol", "timestamp"]
    if (
        not raw.sort_values(keys)[keys]
        .reset_index(drop=True)
        .equals(adjusted.sort_values(keys)[keys].reset_index(drop=True))
    ):
        raise ValueError("raw/adjusted ETF keys differ")


async def collect(settings, directory):
    directory.mkdir(parents=True, exist_ok=True)
    manifest = directory / "manifest.json"
    if manifest.exists() and json.loads(manifest.read_text()) != DATA_CONTRACT:
        raise ValueError("ETF collection contract mismatch")
    if (cached := completed_collection(directory, DATA_CONTRACT)) is not None:
        return {"cache_hit": True, "new_requests": 0, "collection": cached}
    write_json(manifest, DATA_CONTRACT)
    broker = Alpaca(settings)
    requests = 0
    try:
        assets = {}
        for symbol in SYMBOLS:
            asset = await broker.asset(symbol)
            requests += 1
            assets[symbol] = {k: asset.get(k) for k in ("status", "tradable", "fractionable", "class")}
            if asset.get("status") != "active" or not asset.get("tradable"):
                raise ValueError("ETF is not currently tradable")
        write_json(directory / "assets.json", assets)
        calendar = await broker.calendar("2016-01-01", "2025-12-31")
        requests += 1
        write_json(directory / "calendar.json", calendar)
        frames = {}
        for adjustment in DATA_CONTRACT["adjustments"]:
            parts, token, seen_tokens = [], None, set()
            while True:
                params = {
                    "symbols": ",".join(SYMBOLS),
                    "start": "2016-01-01T00:00:00Z",
                    "end": "2026-01-01T00:00:00Z",
                    "feed": "sip",
                    "timeframe": "1Day",
                    "adjustment": adjustment,
                    "limit": 10000,
                }
                if token:
                    params["page_token"] = token
                response = await broker.request("GET", "/v2/stocks/bars", data_api=True, params=params)
                requests += 1
                for symbol, rows in response.get("bars", {}).items():
                    if symbol not in SYMBOLS:
                        raise ValueError("unexpected ETF symbol")
                    if rows:
                        parts.append(normalized_bars(symbol, rows))
                token = response.get("next_page_token")
                if not token:
                    break
                if token in seen_tokens:
                    raise ValueError("nonadvancing ETF pagination")
                seen_tokens.add(token)
            if not parts:
                raise ValueError("empty ETF data")
            frame = pd.concat(parts, ignore_index=True).sort_values(["timestamp", "symbol"])
            frame["date"] = frame.timestamp.dt.tz_convert("America/New_York").dt.strftime("%Y-%m-%d")
            if frame.date.min() < "2016-01-01" or frame.date.max() >= "2026-01-01":
                raise ValueError("ETF source crossed registered boundary")
            frames[adjustment] = frame
            atomic_parquet(frame, directory / f"{adjustment}.parquet")
        validate_frames(frames["raw"], frames["all"], calendar)
        result = {
            "contract": DATA_CONTRACT,
            "completed_at": utcnow().isoformat(),
            "requests": requests,
            "sessions": len(calendar),
            "rows_per_adjustment": len(frames["all"]),
            "source_sha256": checksum(Path(__file__)),
            "file_sha256": {
                n: checksum(directory / n)
                for n in ("raw.parquet", "all.parquet", "calendar.json", "assets.json")
            },
        }
        write_json(directory / "collection.json", result)
        return {"cache_hit": False, "new_requests": requests, "collection": result}
    finally:
        await broker.close()


def targets(close, strategy, delay=0):
    if strategy not in (*STUDY_CONTRACT["candidates"], *STUDY_CONTRACT["benchmarks"]) or delay not in (0, 1):
        raise ValueError("unregistered allocation strategy/delay")
    monthly = close.groupby(close.index.to_period("M")).last()
    average = monthly.rolling(10, min_periods=10).mean()
    momentum = sum(monthly.pct_change(n, fill_method=None) for n in (3, 6, 12)) / 3
    result = {}
    for month, row in monthly.iterrows():
        next_dates = close.index[close.index.to_period("M") == month + 1]
        if len(next_dates) <= delay:
            continue
        weights = pd.Series(0.0, index=SYMBOLS)
        if strategy == "trend_10m":
            if average.loc[month, list(RISK_ASSETS)].isna().any():
                continue
            for symbol in RISK_ASSETS:
                weights[symbol] = 0.2 if row[symbol] > average.loc[month, symbol] else 0
        elif strategy == "rotation_2":
            if momentum.loc[month, list(RISK_ASSETS)].isna().any():
                continue
            eligible = [s for s in RISK_ASSETS if row[s] > average.loc[month, s]]
            selected = sorted(eligible, key=lambda s: (-momentum.loc[month, s], s))[:2]
            weights.loc[selected] = 0.5
        elif strategy == "balanced_hold":
            weights.loc[list(RISK_ASSETS)] = 0.2
        elif strategy == "spy_hold":
            weights["SPY"] = 1
        weights["BIL"] = 1 - weights.drop("BIL").sum()
        result[next_dates[delay]] = weights
    return pd.DataFrame.from_dict(result, orient="index", columns=SYMBOLS).sort_index()


def rebalance(values, cash, weights, cost):
    """Solve post-cost equity before allocating so purchases never borrow fees."""
    if (
        not 0 <= cost < 0.1
        or not np.isfinite(weights).all()
        or min(weights) < 0
        or not np.isclose(sum(weights), 1)
    ):
        raise ValueError("invalid allocation or cost")
    equity = sum(values) + cash
    if equity <= 0 or min(values) < 0 or cash < -1e-6:
        raise ValueError("invalid portfolio capital")
    low, high = 0.0, equity
    for _ in range(60):
        middle = (low + high) / 2
        charge = cost * np.abs(weights * middle - values).sum()
        if middle + charge > equity:
            high = middle
        else:
            low = middle
    desired = weights * low
    turnover = np.abs(desired - values).sum()
    fee = turnover * cost
    residual = equity - desired.sum() - fee
    if residual < -1e-6:
        raise ValueError("rebalance borrowed fees")
    return desired, max(residual, 0), fee, turnover


def simulate(opens, closes, schedule, cost_bps, capital=100_000):
    active = closes.loc["2018-01-01":"2025-12-31"]
    if active.empty or not opens.index.equals(closes.index):
        raise ValueError("invalid price interval")
    shares, cash = np.zeros(len(SYMBOLS)), float(capital)
    nav, ledger = [], []
    for date, close in active.iterrows():
        opening = opens.loc[date].to_numpy(dtype=float)
        if date in schedule.index:
            values = shares * opening
            desired, cash, fee, turnover = rebalance(
                values, cash, schedule.loc[date].to_numpy(), cost_bps / 10000
            )
            shares = desired / opening
            ledger.append(
                {
                    "date": date.date().isoformat(),
                    "type": "rebalance",
                    "signal_month": str(date.to_period("M") - 1),
                    "cost": float(fee),
                    "traded_notional": float(turnover),
                    "post_cost_equity": float(desired.sum() + cash),
                    "target_weights": schedule.loc[date].to_dict(),
                }
            )
        nav.append(float(shares @ close.to_numpy() + cash))
    terminal_cost = float(shares @ active.iloc[-1].to_numpy() * cost_bps / 10000)
    nav[-1] -= terminal_cost
    ledger.append(
        {
            "date": active.index[-1].date().isoformat(),
            "type": "estimated_terminal_liquidation",
            "cost": terminal_cost,
            "traded_notional": terminal_cost / (cost_bps / 10000) if cost_bps else 0,
        }
    )
    curve = pd.Series(nav, index=active.index, name="equity")
    returns = curve.pct_change(fill_method=None)
    returns.iloc[0] = curve.iloc[0] / capital - 1
    years = ((curve.index[-1] - curve.index[0]).days + 1) / 365.25
    cagr = float((curve.iloc[-1] / capital) ** (1 / years) - 1)
    drawdown = float((1 - curve / curve.cummax().clip(lower=capital)).max())
    blocks = {
        f"{start}-{end}": float((1 + returns.loc[f"{start}-01-01" : f"{end}-12-31"]).prod() - 1)
        for start, end in ((2018, 2019), (2020, 2022), (2023, 2025))
    }
    return (
        {
            "net_return": float(curve.iloc[-1] / capital - 1),
            "cagr": cagr,
            "max_drawdown_close": drawdown,
            "calmar": cagr / max(drawdown, 1e-9),
            "volatility": float(returns.std() * np.sqrt(252)),
            "blocks": blocks,
            "annual_returns": {
                str(k): float(v) for k, v in (1 + returns).groupby(returns.index.year).prod().sub(1).items()
            },
            "costs": sum(e["cost"] for e in ledger),
            "rebalances": len(ledger) - 1,
            "sessions": len(curve),
            "months": len(curve.index.to_period("M").unique()),
        },
        curve,
        ledger,
    )


def run(directory, output):
    cached = completed_collection(directory, DATA_CONTRACT)
    if cached is None or output.exists():
        raise ValueError("validated completed cache and new output required")
    raw, adjusted = (pd.read_parquet(directory / n) for n in ("raw.parquet", "all.parquet"))
    calendar = json.loads((directory / "calendar.json").read_text())
    validate_frames(raw, adjusted, calendar)
    opens = adjusted.pivot(index="date", columns="symbol", values="open").reindex(columns=SYMBOLS)
    closes = adjusted.pivot(index="date", columns="symbol", values="close").reindex(columns=SYMBOLS)
    opens.index, closes.index = pd.to_datetime(opens.index), pd.to_datetime(closes.index)
    output.mkdir(parents=True)
    (output / "source.py").write_bytes(Path(__file__).read_bytes())
    report = {
        "created_at": utcnow().isoformat(),
        "contract": STUDY_CONTRACT,
        "data_collection_sha256": checksum(directory / "collection.json"),
        "data_sha256": cached["file_sha256"],
        "source_sha256": checksum(Path(__file__)),
        "holdout_evaluated": False,
        "promoted": False,
        "fits": 0,
        "api_requests": 0,
        "evaluations": [],
    }
    write_json(output / "registration.json", report)
    for strategy in (*STUDY_CONTRACT["candidates"], *STUDY_CONTRACT["benchmarks"]):
        for cost in (20, 40):
            schedule = targets(closes, strategy)
            metrics, curve, ledger = simulate(opens, closes, schedule, cost)
            key = f"{strategy}-{cost}bps"
            curve.to_csv(output / f"{key}-equity.csv", index_label="date")
            write_json(output / f"{key}-ledger.json", ledger)
            schedule.to_csv(output / f"{key}-targets.csv", index_label="execution_date")
            report["evaluations"].append(
                {"strategy": strategy, "cost_per_side_bps": cost, "metrics": metrics}
            )
    report["delay_sensitivity"] = []
    for strategy in STUDY_CONTRACT["candidates"]:
        metrics, curve, ledger = simulate(opens, closes, targets(closes, strategy, 1), 20)
        curve.to_csv(output / f"{strategy}-delayed-equity.csv", index_label="date")
        write_json(output / f"{strategy}-delayed-ledger.json", ledger)
        report["delay_sensitivity"].append({"strategy": strategy, "metrics": metrics})
    lookup = {(e["strategy"], e["cost_per_side_bps"]): e["metrics"] for e in report["evaluations"]}
    report["screening"] = []
    for strategy in STUDY_CONTRACT["candidates"]:
        base, stress = lookup[strategy, 20], lookup[strategy, 40]
        checks = {
            "positive_cagr": base["cagr"] > 0,
            "positive_stress_cagr": stress["cagr"] > 0,
            "drawdown_within_25pct": base["max_drawdown_close"] <= 0.25,
            "two_positive_blocks": sum(v > 0 for v in base["blocks"].values()) >= 2,
            "calmar_above_balanced": base["calmar"] > lookup["balanced_hold", 20]["calmar"],
            "full_96_months": base["months"] == 96,
        }
        report["screening"].append(
            {"strategy": strategy, "passes_historical_screen": all(checks.values()), "checks": checks}
        )
    eligible = [c["strategy"] for c in report["screening"] if c["passes_historical_screen"]]
    report["followup_priority"] = max(eligible, key=lambda s: lookup[s, 20]["cagr"]) if eligible else None
    report["completed_at"] = utcnow().isoformat()
    report["output_sha256"] = {p.name: checksum(p) for p in output.iterdir() if p.is_file()}
    write_json(output / "report.json", report)
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("collect", "study"))
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.action == "collect":
        result = asyncio.run(collect(Settings(), args.directory))
        print(
            json.dumps(
                {
                    "cache_hit": result["cache_hit"],
                    "new_requests": result["new_requests"],
                    "sessions": result["collection"]["sessions"],
                }
            )
        )
    else:
        if args.output is None:
            parser.error("--output required for study")
        result = run(args.directory, args.output)
        print(
            json.dumps({"followup_priority": result["followup_priority"], "screening": result["screening"]})
        )


if __name__ == "__main__":
    main()
