"""ADR-0042: one long-only reversal rule, frozen exits, development only."""

import argparse
import json
import time
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd

from stock_trader.barrier_study import ledger_summary
from stock_trader.clock import utcnow
from stock_trader.config import Settings
from stock_trader.modeling import checksum
from stock_trader.orb_study import rejection_reasons
from stock_trader.study import write_json
from stock_trader.swing_study import load_inputs, prepare_swing, simulate_swing

CONTRACT = {
    "decision": "ADR-0042",
    "rule": "reversal_5d",
    "selection": "valid trade universe relative_ret_5 percentile <= 0.20; ret_5<0; relative_ret_5<0; ma_60>0",
    "priority": "relative_ret_5 ascending, symbol ascending",
    "entry": "next session opening quote, previous completed daily signal",
    "exit": "2 daily ATR stop / 2R target / account risk; no scheduled holding period",
    "costs_bps": [20.0, 30.0],
    "quote_spread_bps": 20.0,
    "future_path_filter": False,
    "development_start": "2024-10-03",
    "development_end": "2026-06-15",
    "holdout_evaluated": False,
    "score_semantics": "relative weakness ranking, NOT expected return",
    "potential_semantics": "gross barrier distance, NOT expected return",
}


def rule_signals(frame, settings):
    valid = frame.feature_valid & ~frame.symbol.isin(settings.benchmark_symbols)
    ranks = frame.relative_ret_5.where(valid).groupby(frame.date).rank(method="average", pct=True)
    eligible = valid & ranks.le(0.20) & frame.ret_5.lt(0) & frame.relative_ret_5.lt(0) & frame.ma_60.gt(0)
    priority = (-frame.relative_ret_5).where(eligible)
    distance = np.maximum(settings.stop_atr * frame.normalized_atr, frame.normalized_close * 0.003)
    potential = (settings.reward_risk * distance / frame.normalized_close).where(eligible)
    return potential, priority


def path_quality(ledger, diagnostics, bars, calendar, evaluation_end):
    """Audit AFTER simulation: missing future prices never select an entry."""
    observed = {s: set(g.timestamp) for s, g in bars.groupby("symbol", sort=False)}
    windows = [
        (t["symbol"], pd.Timestamp(t["entry_time"]), pd.Timestamp(t["exit_bar_start"])) for t in ledger
    ] + [(t["symbol"], pd.Timestamp(t["entry_time"]), evaluation_end) for t in diagnostics["open_positions"]]
    missing_windows, missing_bars = 0, 0
    sessions = []
    for c in calendar:
        opening = pd.Timestamp(c["date"] + " " + c["open"], tz="America/New_York").tz_convert("UTC")
        closing = pd.Timestamp(c["date"] + " " + c["close"], tz="America/New_York").tz_convert("UTC")
        sessions.append((opening, closing))
    for symbol, entry, last in windows:
        count = 0
        for opening, closing in sessions:
            start, end = max(entry, opening), min(last, closing - pd.Timedelta(minutes=5))
            if start <= end:
                count += sum(t not in observed[symbol] for t in pd.date_range(start, end, freq="5min"))
        missing_bars += count
        missing_windows += count > 0
    return {"positions_with_missing_path": missing_windows, "missing_path_bars": missing_bars}


def run(settings, source, output):
    if settings.round_trip_cost_bps != 20 or settings.stop_atr != 2 or settings.reward_risk != 2:
        raise ValueError("unregistered cost or barriers")
    if output.exists():
        raise ValueError("research output must be new")
    output.mkdir(parents=True)
    started = time.monotonic()
    frame, bars, dates, openings, reference, inputs, input_quality = load_inputs(settings, source)
    if dates[0] != CONTRACT["development_start"] or dates[-1] != CONTRACT["development_end"]:
        raise ValueError("unregistered development interval")
    intervals = [(f["validation_start"], f["validation_end"]) for f in reference["folds"]]
    if intervals != [
        ("2025-11-07", "2026-01-21"),
        ("2026-01-22", "2026-04-02"),
        ("2026-04-06", "2026-06-15"),
    ]:
        raise ValueError("unregistered validation intervals")
    potential, priority = rule_signals(frame, settings)
    potential = potential.where(frame.date.ge(intervals[0][0]))
    priority = priority.where(frame.date.ge(intervals[0][0]))
    replay = prepare_swing(bars, dates, openings)
    calendar = json.loads(
        (settings.data_dir / "research/native-iex-5m-development/calendar.json").read_text()
    )
    code_files = (
        "reversal_study.py",
        "swing_study.py",
        "risk.py",
        "orb_study.py",
        "research.py",
        "barrier_study.py",
        "config.py",
        "study.py",
        "modeling.py",
    )
    (output / "code").mkdir()
    code_hashes = {}
    for name in code_files:
        path = Path(__file__).with_name(name)
        (output / "code" / name).write_bytes(path.read_bytes())
        code_hashes[name] = checksum(path)
    report = {
        "created_at": utcnow().isoformat(),
        "contract": CONTRACT,
        "settings": settings.public_contract(),
        "input_sha256": inputs,
        "code_sha256": code_hashes,
        "input_quality": input_quality,
        "folds": reference["folds"],
        "holdout_evaluated": False,
        "promoted": False,
        "fits": 0,
        "api_requests": 0,
        "signals": int(potential.notna().sum()),
        "evaluations": [],
    }
    write_json(output / "registration.json", report)
    metrics, fold_metrics, qualities = {}, {}, {}
    for cost in CONTRACT["costs_bps"]:
        print(json.dumps({"stage": "reversal_replay", "cost_bps": cost}), flush=True)
        configured = settings.model_copy(update={"round_trip_cost_bps": cost})

        def replay_one(active_intervals, *, configured=configured):
            trades, diagnostics = [], {}
            result = simulate_swing(
                frame,
                potential,
                0,
                0,
                configured,
                replay,
                active_intervals,
                trades,
                "opening_quote",
                "barrier",
                diagnostics,
                rule_priority=priority,
                future_path_filter=False,
                quote_spread_bps=20,
            )
            last = active_intervals[-1][1]
            close = next(c["close"] for c in calendar if c["date"] == last)
            end = pd.Timestamp(last + " " + close, tz="America/New_York").tz_convert("UTC") - pd.Timedelta(
                minutes=5
            )
            quality = path_quality(trades, diagnostics, bars, calendar, end)
            return result, trades, diagnostics, quality

        result, trades, diagnostics, quality = replay_one(intervals)
        for trade in trades:
            trade["gross_target_potential"] = trade.pop("prediction")
        write_json(output / f"reversal_5d-{int(cost)}bps-trades.json", trades)
        write_json(output / f"reversal_5d-{int(cost)}bps-open.json", diagnostics)
        folds = []
        for interval in intervals:
            f, _, d, q = replay_one([interval])
            folds.append({**asdict(f), "open_positions": len(d["open_positions"]), "path_quality": q})
        metrics[cost], fold_metrics[cost] = result, folds
        qualities[cost] = quality
        report["evaluations"].append(
            {
                "cost_bps": cost,
                "metrics": asdict(result),
                "fold_metrics": folds,
                "diagnostics": diagnostics,
                "path_quality": quality,
                "ledger_summary": ledger_summary(trades, settings.capital_limit),
            }
        )
        write_json(output / "report.json", report)
    merged_quality = {k: max(q[k] for q in qualities.values()) for k in qualities[20]}
    # Fold path uncertainty also blocks approval; it never changes entry selection.
    merged_quality["folds_with_missing_path"] = sum(
        bool(f["path_quality"]["positions_with_missing_path"])
        for folds in fold_metrics.values()
        for f in folds
    )
    report["rejection_reasons"] = rejection_reasons(
        metrics[20], fold_metrics[20], metrics[30], merged_quality
    )
    report["development_eligible"] = not report["rejection_reasons"]
    report["elapsed_seconds"] = time.monotonic() - started
    report["completed_at"] = utcnow().isoformat()
    report["output_sha256"] = {p.name: checksum(p) for p in output.glob("reversal_5d-*.json")}
    write_json(output / "report.json", report)
    print(json.dumps({"stage": "complete", "elapsed_seconds": report["elapsed_seconds"]}), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run(Settings(), args.source, args.output)


if __name__ == "__main__":
    main()
