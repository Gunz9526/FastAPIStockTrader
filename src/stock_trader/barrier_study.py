"""ADR-0027: remove scheduled exits, preserve targets/entries/risk, development only."""

import argparse
import json
import time
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd

from stock_trader.clock import utcnow
from stock_trader.config import Settings
from stock_trader.modeling import checksum
from stock_trader.study import write_json
from stock_trader.swing_study import (
    load_inputs,
    prepare_swing,
    simulate_swing,
    swing_eligible,
    trend_predictions,
)


def ledger_summary(trades, capital):
    groups = defaultdict(list)
    symbols = defaultdict(list)
    durations = []
    for trade in trades:
        groups[trade["reason"]].append(trade)
        symbols[trade["symbol"]].append(trade)
        durations.append(
            (pd.Timestamp(trade["exit_time"]) - pd.Timestamp(trade["entry_time"])).total_seconds() / 86400
        )

    def amounts(items):
        return {
            "trades": len(items),
            "gross_pnl": sum(t["gross_pnl"] for t in items),
            "explicit_cost": sum(t["explicit_cost"] for t in items),
            "net_pnl": sum(t["net_pnl"] for t in items),
        }

    gross = sum(t["gross_pnl"] for t in trades)
    return {
        "gross_return_completed": gross / capital,
        "explicit_cost_return_completed": sum(t["explicit_cost"] for t in trades) / capital,
        "net_return_completed": sum(t["net_pnl"] for t in trades) / capital,
        "gross_profit_factor_completed": sum(max(t["gross_pnl"], 0) for t in trades)
        / max(sum(max(-t["gross_pnl"], 0) for t in trades), 1e-8),
        "reasons": {k: amounts(v) for k, v in sorted(groups.items())},
        "symbols": {k: amounts(v) for k, v in sorted(symbols.items())},
        "median_holding_calendar_days": float(np.median(durations)) if durations else 0,
        "max_holding_calendar_days": max(durations, default=0),
    }


def rejection_counts(candidates):
    """Overlapping reasons, never pretend they identify an underlying cause."""
    return {
        "candidates": len(candidates),
        "eligible": sum(c["eligible"] for c in candidates),
        "nonpositive_net": sum(c["metrics"]["net_return"] <= 0 for c in candidates),
        "fewer_than_60_completed_trades": sum(c["metrics"]["trades"] < 60 for c in candidates),
        "profit_factor_below_1_1": sum(c["metrics"]["profit_factor"] < 1.1 for c in candidates),
        "drawdown_above_8_percent": sum(c["metrics"]["max_drawdown"] > 0.08 for c in candidates),
        "fewer_than_two_profitable_folds": sum(
            sum(f["net_return"] > 0 for f in c["folds"]) < 2 for c in candidates
        ),
        "reasons_overlap": True,
    }


def block_sensitivity(daily_returns, block_length, repetitions=10000, seed=42):
    values = np.asarray(daily_returns, dtype=float)
    if values.ndim != 1 or not len(values) or not np.isfinite(values).all() or (values <= -1).any():
        raise ValueError("valid observed daily returns required")
    if block_length < 1 or repetitions < 1:
        raise ValueError("positive bootstrap dimensions required")
    rng = np.random.default_rng(seed)
    count = int(np.ceil(len(values) / block_length))
    starts = rng.integers(0, len(values), size=(repetitions, count))
    indices = (starts[..., None] + np.arange(block_length)) % len(values)
    indices = indices.reshape(repetitions, -1)[:, : len(values)]
    simulated = np.expm1(np.log1p(values)[indices].sum(axis=1))
    percentiles = np.percentile(simulated, [2.5, 50, 97.5])
    return {
        "block_sessions": block_length,
        "observed_sessions": len(values),
        "blocks_per_sample": count,
        "repetitions": repetitions,
        "seed": seed,
        "percentile_2_5": float(percentiles[0]),
        "percentile_50": float(percentiles[1]),
        "percentile_97_5": float(percentiles[2]),
        "positive_resample_fraction": float((simulated > 0).mean()),
        "is_future_profit_probability": False,
    }


def run_sensitivity(settings, source):
    reference = json.loads((source / "report.json").read_text())
    if reference["holdout_evaluated"] or reference["promoted"] or reference["decision"] != "ADR-0027":
        raise ValueError("registered development barrier report required")
    out = settings.report_dir / f"barrier-sensitivity-{utcnow().strftime('%Y%m%dT%H%M%SZ')}"
    out.mkdir()
    (out / "source").mkdir()
    code = {}
    for name in ("barrier_study.py", "modeling.py", "config.py"):
        path = Path(__file__).with_name(name)
        (out / "source" / name).write_bytes(path.read_bytes())
        code[name] = checksum(path)
    report = {
        "created_at": utcnow().isoformat(),
        "decision": "ADR-0028",
        "source": str(source),
        "source_report_sha256": checksum(source / "report.json"),
        "code_sha256": code,
        "holdout_evaluated": False,
        "promoted": False,
        "new_price_data_read": False,
        "evaluations": [],
    }
    for evaluation in reference["evaluations"]:
        returns = evaluation["metrics"]["daily_returns"]
        actual = float(np.expm1(np.log1p(returns).sum()))
        if not np.isclose(actual, evaluation["metrics"]["net_return"], atol=1e-10):
            raise ValueError("daily marked returns disagree with terminal NAV")
        for block in (20, 40):
            report["evaluations"].append(
                {
                    "exit_policy": evaluation["exit_policy"],
                    "cost_bps": evaluation["cost_bps"],
                    "observed_net_return": actual,
                    **block_sensitivity(returns, block),
                }
            )
    write_json(out / "report.json", report)
    print(json.dumps({"stage": "sensitivity_complete", "report": str(out)}), flush=True)
    return out


def run_failure_diagnosis(settings, source, final_source):
    """Explain an existing failure from frozen ledgers; never replay its price data."""
    reference = json.loads((source / "report.json").read_text())
    final = json.loads((final_source / "report.json").read_text())
    if (
        not final["holdout_evaluated"]
        or final["promoted"]
        or final["passed"]
        or final["candidate"] != "trend_rule_10"
        or final["test_end"] != "2026-10-01"
    ):
        raise ValueError("frozen failed final report required for explanation only")
    baseline = next(c for c in reference["baselines"] if c["holding_days"] == 10)
    evaluation = next(e for e in final["evaluations"] if e["cost_bps"] == 20)
    development_path = source / "trades" / baseline["trade_file"]
    final_path = final_source / evaluation["trade_file"]
    inputs = [source / "report.json", final_source / "report.json", development_path, final_path]
    report = {
        "created_at": utcnow().isoformat(),
        "decision": "ADR-0027",
        "purpose": "existing failure explanation only; not strategy selection or reevaluation",
        "new_price_data_read": False,
        "new_simulations": 0,
        "promoted": False,
        "input_sha256": {str(p): checksum(p) for p in inputs},
        "development": ledger_summary(json.loads(development_path.read_text()), settings.capital_limit),
        "failed_final": ledger_summary(json.loads(final_path.read_text()), settings.capital_limit),
        "ml_validation_rejections": rejection_counts(reference["candidates"]),
        "observed_primary_problem": "positive development expectancy was not replicated in an independent period; final gross PnL is negative before explicit fee",
        "causal_root_not_identified": True,
    }
    out = settings.report_dir / f"failure-diagnosis-{utcnow().strftime('%Y%m%dT%H%M%SZ')}"
    out.mkdir()
    (out / "source").mkdir()
    report["code_sha256"] = {}
    for name in ("barrier_study.py", "modeling.py", "config.py"):
        path = Path(__file__).with_name(name)
        (out / "source" / name).write_bytes(path.read_bytes())
        report["code_sha256"][name] = checksum(path)
    write_json(out / "report.json", report)
    print(
        json.dumps(
            {
                "stage": "diagnosis_complete",
                "report": str(out),
                "failed_final_gross": report["failed_final"]["gross_return_completed"],
                "failed_final_fee": report["failed_final"]["explicit_cost_return_completed"],
            }
        ),
        flush=True,
    )
    return out


def run_barrier_study(settings, source):
    start = time.monotonic()
    reference = json.loads((source / "report.json").read_text())
    if (
        not reference.get("completed_at")
        or reference["holdout_evaluated"]
        or reference["promoted"]
        or reference["entry_reference"] != "opening_quote"
        or reference["settings"] != settings.public_contract()
    ):
        raise ValueError("completed sealed development reference required")
    baseline = next(c for c in reference["baselines"] if c["holding_days"] == 10)
    frame, bars, dates, openings, development, hashes, _ = load_inputs(settings, Path(reference["source"]))
    if frame["date"].max() >= "2026-06-16" or hashes != reference["input_sha256"]:
        raise ValueError("sealed development inputs changed")
    replay = prepare_swing(bars, dates, openings)
    intervals = [(f["validation_start"], f["validation_end"]) for f in development["folds"]]
    scores = trend_predictions(frame).where(frame["date"].ge(intervals[0][0]))
    out = settings.report_dir / f"barrier-study-{utcnow().strftime('%Y%m%dT%H%M%SZ')}"
    out.mkdir()
    (out / "source").mkdir()
    (out / "trades").mkdir()
    code = {}
    for name in (
        "barrier_study.py",
        "swing_study.py",
        "risk.py",
        "research.py",
        "study.py",
        "modeling.py",
        "config.py",
    ):
        path = Path(__file__).with_name(name)
        (out / "source" / name).write_bytes(path.read_bytes())
        code[name] = checksum(path)
    report = {
        "created_at": utcnow().isoformat(),
        "decision": "ADR-0027",
        "source": str(source),
        "holdout_start": "2026-06-16",
        "holdout_evaluated": False,
        "promoted": False,
        "new_fits": 0,
        "settings": settings.public_contract(),
        "code_sha256": code,
        "input_sha256": hashes,
        "source_report_sha256": checksum(source / "report.json"),
        "entry_window_horizon_for_comparison_only": 10,
        "open_positions_are_censored_not_completed_trades": True,
        "evaluations": [],
    }
    for policy in ("timed", "barrier"):
        for cost in (20, 30):
            print(json.dumps({"stage": "evaluate", "policy": policy, "cost_bps": cost}), flush=True)
            scoped = settings.model_copy(update={"round_trip_cost_bps": cost})
            diagnostics, ledger = {}, []
            metrics = simulate_swing(
                frame,
                scores,
                10,
                0.005,
                scoped,
                replay,
                intervals,
                ledger,
                "opening_quote",
                policy,
                diagnostics,
            )
            folds = []
            for interval in intervals:
                detail, trades = {}, []
                folded = simulate_swing(
                    frame,
                    scores,
                    10,
                    0.005,
                    scoped,
                    replay,
                    [interval],
                    trades,
                    "opening_quote",
                    policy,
                    detail,
                )
                folds.append({"metrics": asdict(folded), "diagnostics": detail})
            if policy == "timed" and cost == 20:
                original = json.loads((source / "trades" / baseline["trade_file"]).read_text())
                if original != ledger or any(
                    not np.isclose(getattr(metrics, k), baseline["metrics"][k], atol=1e-12)
                    for k in ("net_return", "max_drawdown", "trades", "profit_factor")
                ):
                    raise ValueError("original timed baseline no longer reproduces")
            filename = f"{policy}-{cost}.json"
            write_json(out / "trades" / filename, ledger)
            eligible = swing_eligible(metrics, [type(metrics)(**f["metrics"]) for f in folds])
            report["evaluations"].append(
                {
                    "exit_policy": policy,
                    "cost_bps": cost,
                    "metrics": asdict(metrics),
                    "diagnostics": diagnostics,
                    "ledger": ledger_summary(ledger, settings.capital_limit),
                    "folds": folds,
                    "trade_file": filename,
                    "development_criteria_met": eligible,
                    "independent_validation_required": True,
                }
            )
    report["completed_at"] = utcnow().isoformat()
    report["elapsed_seconds"] = time.monotonic() - start
    write_json(out / "report.json", report)
    print(
        json.dumps({"stage": "complete", "report": str(out), "evaluations": len(report["evaluations"])}),
        flush=True,
    )
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--diagnose-final", type=Path)
    parser.add_argument("--sensitivity", action="store_true")
    args = parser.parse_args()
    if args.diagnose_final and args.sensitivity:
        raise ValueError("one diagnosis mode per run")
    if args.diagnose_final:
        run_failure_diagnosis(Settings(), args.source, args.diagnose_final)
    elif args.sensitivity:
        run_sensitivity(Settings(), args.source)
    else:
        run_barrier_study(Settings(), args.source)


if __name__ == "__main__":
    main()
