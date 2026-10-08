"""Audit ADR-0046 artifacts and independently rebuild NAV from saved target weights."""

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import matplotlib
import numpy as np
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from stock_trader.etf_study import DATA_CONTRACT, SYMBOLS, validate_frames
from stock_trader.research_cache import completed_collection


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def audit_run(folder, key, metrics, opens, closes, cost, delay):
    curve = pd.read_csv(folder / f"{key}-equity.csv", index_col="date", parse_dates=True).equity
    events = json.loads((folder / f"{key}-ledger.json").read_text())
    rebalance = {pd.Timestamp(e["date"]): e for e in events if e["type"] == "rebalance"}
    if len(rebalance) != 96 or len(curve) != 2011:
        raise ValueError("incomplete evaluation")
    shares, cash, rebuilt = np.zeros(len(SYMBOLS)), 100_000.0, []
    monthly = closes.groupby(closes.index.to_period("M")).last()
    for date in curve.index:
        if date in rebalance:
            event = rebalance[date]
            month_dates = closes.index[closes.index.to_period("M") == date.to_period("M")]
            if date != month_dates[delay] or event["signal_month"] != str(date.to_period("M") - 1):
                raise ValueError("noncausal execution date")
            signal_month = pd.Period(event["signal_month"], freq="M")
            history = monthly.loc[:signal_month]
            expected = pd.Series(0.0, index=SYMBOLS)
            eligible = [s for s in SYMBOLS[:-1] if history[s].iloc[-1] > history[s].tail(10).mean()]
            if key.startswith("trend_10m"):
                expected.loc[eligible] = 0.2
            elif key.startswith("rotation_2"):
                scores = {
                    s: sum(history[s].iloc[-1] / history[s].iloc[-n - 1] - 1 for n in (3, 6, 12)) / 3
                    for s in eligible
                }
                expected.loc[sorted(eligible, key=lambda s: (-scores[s], s))[:2]] = 0.5
            elif key.startswith("balanced_hold"):
                expected.loc[list(SYMBOLS[:-1])] = 0.2
            elif key.startswith("spy_hold"):
                expected["SPY"] = 1.0
            expected["BIL"] = 1 - expected.drop("BIL").sum()
            weights = pd.Series(event["target_weights"]).reindex(SYMBOLS)
            if not np.allclose(weights, expected, atol=1e-12, rtol=0):
                raise ValueError("saved targets differ from prior-month-only calculation")
            opening = opens.loc[date].to_numpy()
            before = shares * opening
            capital = before.sum() + cash
            after = weights.to_numpy() * event["post_cost_equity"]
            turnover = np.abs(after - before).sum()
            fee = turnover * cost / 10000
            cash = capital - after.sum() - fee
            if cash < -1e-6 or not np.isclose(fee, event["cost"], atol=1e-6, rtol=0):
                raise ValueError("fees or funding differ from independent accounting")
            if not np.isclose(turnover, event["traded_notional"], atol=1e-6, rtol=0):
                raise ValueError("turnover differs from ledger")
            shares = after / opening
        rebuilt.append(float(shares @ closes.loc[date].to_numpy() + cash))
    terminal = float(shares @ closes.loc[curve.index[-1]].to_numpy() * cost / 10000)
    if not np.isclose(terminal, events[-1]["cost"], atol=1e-6, rtol=0):
        raise ValueError("incorrect terminal cost")
    rebuilt[-1] -= terminal
    error = float(np.max(np.abs(curve.to_numpy() - rebuilt)))
    if error > 1e-6 or not np.isclose(curve.iloc[-1] / 100_000 - 1, metrics["net_return"]):
        raise ValueError("NAV disagrees with ledger")
    return {"key": key, "rebalances": len(rebalance), "sessions": len(curve), "max_nav_error_usd": error}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--guard", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    cache = completed_collection(args.data, DATA_CONTRACT)
    report = json.loads((args.report / "report.json").read_text())
    if report["promoted"] or report["holdout_evaluated"] or not report.get("completed_at"):
        raise ValueError("unexpected report status")
    if digest(args.data / "collection.json") != report["data_collection_sha256"]:
        raise ValueError("changed collection")
    if digest(args.report / "source.py") != report["source_sha256"]:
        raise ValueError("changed research source")
    for name, expected in report["output_sha256"].items():
        if digest(args.report / name) != expected:
            raise ValueError("changed research output")
    for name, expected in report["data_sha256"].items():
        if digest(args.data / name) != expected:
            raise ValueError("changed research data")
    guard_hash = digest(args.guard)
    if guard_hash != "d4b630dbf77191269fa4da5b6e5925779ce75d752978f2265aeca6d975eb1129":
        raise ValueError("consumed final guard changed")
    raw, adjusted = (pd.read_parquet(args.data / f"{n}.parquet") for n in ("raw", "all"))
    calendar = json.loads((args.data / "calendar.json").read_text())
    validate_frames(raw, adjusted, calendar)
    if calendar[0]["date"] != "2016-01-04" or calendar[-1]["date"] != "2025-12-31":
        raise ValueError("historical boundaries incomplete")
    opens = adjusted.pivot(index="date", columns="symbol", values="open").reindex(columns=SYMBOLS)
    closes = adjusted.pivot(index="date", columns="symbol", values="close").reindex(columns=SYMBOLS)
    opens.index, closes.index = pd.to_datetime(opens.index), pd.to_datetime(closes.index)
    audits = []
    for evaluation in report["evaluations"]:
        strategy, cost = evaluation["strategy"], evaluation["cost_per_side_bps"]
        audits.append(
            audit_run(args.report, f"{strategy}-{cost}bps", evaluation["metrics"], opens, closes, cost, 0)
        )
    for evaluation in report["delay_sensitivity"]:
        audits.append(
            audit_run(
                args.report, f"{evaluation['strategy']}-delayed", evaluation["metrics"], opens, closes, 20, 1
            )
        )
    args.output.mkdir(parents=True, exist_ok=True)
    fig, axes = plt.subplots(2, 1, figsize=(11, 8), sharex=True, gridspec_kw={"height_ratios": [2, 1]})
    labels = {
        "spy_hold": "SPY hold",
        "balanced_hold": "Equal-weight 5 ETFs",
        "rotation_2": "Top-2 + trend",
        "trend_10m": "Diversified 10-month trend",
        "bills_hold": "BIL hold",
    }
    for strategy, label in labels.items():
        curve = pd.read_csv(
            args.report / f"{strategy}-20bps-equity.csv", index_col="date", parse_dates=True
        ).equity
        axes[0].plot(curve.index, curve / 1000, label=label, linewidth=1.4)
        axes[1].plot(curve.index, (curve / curve.cummax().clip(lower=100_000) - 1) * 100, linewidth=1.1)
    axes[0].set(
        title="ETF historical development study | 2018-2025", ylabel="Portfolio value (USD thousands)"
    )
    axes[0].legend(loc="upper left", fontsize=9)
    axes[1].set(ylabel="Close drawdown (%)")
    for axis in axes:
        axis.grid(alpha=0.25)
    fig.text(
        0.5,
        0.01,
        "Initial USD 100,000 | Adjusted-price proxy | 20 bps per side | Includes estimated exit cost | Not live performance",
        ha="center",
        fontsize=8,
    )
    fig.tight_layout(rect=(0, 0.035, 1, 1))
    fig.savefig(args.output / "comparison.png", dpi=160)
    plt.close(fig)
    result = {
        "checked_at": datetime.now(UTC).isoformat(),
        "collection_sessions": cache["sessions"],
        "rows_per_adjustment": cache["rows_per_adjustment"],
        "all_input_output_hashes_verified": True,
        "consumed_final_guard_sha256": guard_hash,
        "independent_accounting": audits,
        "network_requests": 0,
        "orders": 0,
        "limitations": [
            "provider-adjusted total-return proxy",
            "no raw-share dividend payment ledger",
            "historical development, not independent final",
        ],
        "audit_source_sha256": digest(Path(__file__)),
        "report_sha256": digest(args.report / "report.json"),
        "chart_sha256": digest(args.output / "comparison.png"),
    }
    (args.output / "integrity.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"audits": len(audits), "verified": True, "requests": 0}))


if __name__ == "__main__":
    main()
