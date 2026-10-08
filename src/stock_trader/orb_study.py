"""ADR-0041 bounded rule research. No training, broker, or model registration."""

import argparse
import json
import time
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd

from stock_trader.clock import utcnow
from stock_trader.config import Settings
from stock_trader.modeling import checksum
from stock_trader.research import prepare_replay, simulate
from stock_trader.study import write_json
from stock_trader.swing_study import load_inputs

CONTRACT = {
    "decision": "ADR-0041",
    "rules": ["orb_rvol", "orb_control"],
    "opening_minutes": 5,
    "relative_volume_lookback_sessions": 14,
    "relative_volume_threshold": 1.0,
    "signal_cutoff_minutes_from_open": 90,
    "prior_daily_atr_multiplier": 0.10,
    "minimum_stop_fraction": 0.003,
    "quote_spread_bps": 20.0,
    "costs_bps": [20.0, 30.0],
    "signal_semantics": "completed-close breakout, first qualifying signal per symbol/session",
    "priority": "opening relative volume descending, symbol ascending",
    "prediction_semantics": "gross target distance only; NOT an expected return estimate",
    "exit": "2R target / initial stop / calendar close minus 10 minutes",
    "development_start": "2024-10-03",
    "development_end": "2026-06-15",
    "holdout_evaluated": False,
}


def rule_frame(daily, normalized_bars, dates, openings, settings):
    """Only prior daily ATR and completed opening/signal bars can affect a signal."""
    frame = normalized_bars.sort_values(["symbol", "timestamp"]).copy()
    for column in ("open", "high", "low", "close"):
        frame[column] /= frame["factor"]
    opening = frame.loc[frame["timestamp"].eq(frame["date"].map(openings))].copy()
    opening["normalized_volume"] = opening["volume"] / opening["factor"]
    opening = opening.rename(
        columns={"high": "opening_high", "open": "opening_open", "close": "opening_close"}
    )
    past = daily.sort_values(["symbol", "date"])[["date", "symbol", "normalized_atr", "factor"]].copy()
    if past.duplicated(["date", "symbol"]).any():
        raise ValueError("duplicate daily features")
    if any(list(g["date"]) != dates for _, g in past.groupby("symbol", sort=False)):
        raise ValueError("daily session continuity required")
    past["prior_atr"] = past.groupby("symbol")["normalized_atr"].shift(1) / past["factor"]
    past = past.merge(
        opening[["date", "symbol", "opening_high", "opening_open", "opening_close", "normalized_volume"]],
        on=["date", "symbol"],
        how="left",
        validate="one_to_one",
    )
    past = past.sort_values(["symbol", "date"])
    past["opening_rvol"] = past["normalized_volume"] / past.groupby("symbol")["normalized_volume"].transform(
        lambda v: v.shift(1).rolling(14, min_periods=14).mean()
    )
    frame = frame.merge(
        past.drop(columns=["normalized_atr", "factor", "normalized_volume"]),
        on=["date", "symbol"],
        validate="many_to_one",
    ).sort_values(["symbol", "timestamp"])
    grouped = frame.groupby(["symbol", "date"], sort=False)
    previous_close = grouped["close"].shift(1)
    consecutive = frame["timestamp"].sub(grouped["timestamp"].shift(1)).eq(pd.Timedelta(minutes=5))
    elapsed = frame["timestamp"].sub(frame["date"].map(openings)) / pd.Timedelta(minutes=1)
    qualifies = (
        consecutive
        & previous_close.le(frame["opening_high"])
        & frame["close"].gt(frame["opening_high"])
        & frame["opening_close"].gt(frame["opening_open"])
        & elapsed.ge(5)
        & elapsed.lt(90)
        & frame["prior_atr"].gt(0)
        & np.isfinite(frame["opening_rvol"])
        & ~frame["symbol"].isin(settings.benchmark_symbols)
    )
    first = qualifies & qualifies.groupby([frame["symbol"], frame["date"]]).cumsum().eq(1)
    frame["orb_control"] = first
    frame["orb_rvol"] = first & frame["opening_rvol"].ge(1.0)
    # entry_plan applies its existing minimum distance and 2R sizing; this is a rule risk scale.
    frame["atr"] = 0.1 * frame["prior_atr"] / settings.stop_atr
    distance = np.maximum(0.1 * frame["prior_atr"], frame["close"] * 0.003)
    frame["gross_target_potential"] = settings.reward_risk * distance / frame["close"]
    # Legacy model gates are bypassed only when explicit rule masks are provided to simulate.
    frame["ma_distance"] = np.nan
    frame["relative_strength"] = np.nan
    return frame.sort_values(["timestamp", "symbol"]).reset_index(drop=True)


def ledger_quality(ledger):
    overnight = sum(
        pd.Timestamp(t["entry_time"]).tz_convert("America/New_York").date()
        != pd.Timestamp(t["exit_time"]).tz_convert("America/New_York").date()
        for t in ledger
    )
    missing = sum(t["missing_path_bars"] > 0 for t in ledger)
    substituted = sum(t["reason"] in ("sparse_close", "final_close") for t in ledger)
    return {
        "overnight_trades": overnight,
        "trades_with_missing_path": missing,
        "substituted_exits": substituted,
    }


def rejection_reasons(metrics, folds, stressed, quality):
    checks = {
        "fewer_than_60_closed": metrics.trades >= 60,
        "nonpositive_net": metrics.net_return > 0,
        "drawdown_above_8pct": metrics.max_drawdown <= 0.08,
        "profit_factor_below_1_1": metrics.profit_factor >= 1.1,
        "fewer_than_two_positive_folds": sum(f["net_return"] > 0 for f in folds) >= 2,
        "nonpositive_30bps_stress": stressed.net_return > 0,
        "unverifiable_execution_path": not any(quality.values()),
    }
    return [name for name, passes in checks.items() if not passes]


def run(settings, source, output):
    if settings.round_trip_cost_bps != 20 or settings.reward_risk != 2:
        raise ValueError("unregistered base cost or reward risk")
    if output.exists():
        raise ValueError("research output must be new")
    output.mkdir(parents=True)
    started = time.monotonic()
    daily, bars, dates, openings, reference, hashes, input_quality = load_inputs(settings, source)
    if dates[0] != CONTRACT["development_start"] or dates[-1] != CONTRACT["development_end"]:
        raise ValueError("unregistered development interval")
    frame = rule_frame(daily, bars, dates, openings, settings)
    calendar = json.loads(
        (settings.data_dir / "research/native-iex-5m-development/calendar.json").read_text()
    )
    closings = {
        c["date"]: pd.Timestamp(c["date"] + " " + c["close"], tz="America/New_York").tz_convert("UTC")
        for c in calendar
    }
    expected_folds = [
        ("2025-11-07", "2026-01-21"),
        ("2026-01-22", "2026-04-02"),
        ("2026-04-06", "2026-06-15"),
    ]
    folds = reference["folds"]
    if [(f["validation_start"], f["validation_end"]) for f in folds] != expected_folds:
        raise ValueError("unregistered validation folds")
    validation = frame.loc[frame["date"].between(expected_folds[0][0], expected_folds[-1][1])]
    if validation["date"].nunique() != 150:
        raise ValueError("validation must contain 150 sessions")
    replay = prepare_replay(validation)
    replay.day_end = pd.Series(closings)
    fold_inputs = []
    for f in folds:
        subset = validation.loc[validation["date"].between(f["validation_start"], f["validation_end"])]
        prepared = prepare_replay(subset)
        prepared.day_end = pd.Series(closings)
        fold_inputs.append((subset, prepared))
    code_files = (
        "orb_study.py",
        "research.py",
        "risk.py",
        "swing_study.py",
        "config.py",
        "study.py",
        "modeling.py",
    )
    code_hashes = {name: checksum(Path(__file__).with_name(name)) for name in code_files}
    (output / "code").mkdir()
    for name in code_files:
        (output / "code" / name).write_bytes(Path(__file__).with_name(name).read_bytes())
    report = {
        "created_at": utcnow().isoformat(),
        "contract": CONTRACT,
        "settings": settings.public_contract(),
        "input_sha256": hashes,
        "code_sha256": code_hashes,
        "input_quality": input_quality,
        "folds": folds,
        "development_rows": len(frame),
        "validation_rows": len(validation),
        "holdout_evaluated": False,
        "promoted": False,
        "fits": 0,
        "api_requests": 0,
        "candidates": [],
    }
    write_json(output / "registration.json", report)
    for rule in CONTRACT["rules"]:
        print(json.dumps({"stage": "replay", "rule": rule}), flush=True)
        ledgers, metrics, fold_metrics = {}, {}, {}
        for cost in CONTRACT["costs_bps"]:
            configured = settings.model_copy(update={"round_trip_cost_bps": cost})
            ledger = []

            def replay_one(subset, prepared, trades=None, *, configured=configured, rule=rule):
                return simulate(
                    subset,
                    subset["gross_target_potential"],
                    threshold=0,
                    horizon=100_000,
                    settings=configured,
                    replay=prepared,
                    execution="timed_open",
                    trades=trades,
                    rule_signals=subset[rule],
                    rule_priority=subset["opening_rvol"],
                    quote_spread_bps=20,
                )

            result = replay_one(validation, replay, ledger)
            if not np.isclose(sum(t["net_pnl"] for t in ledger) / settings.capital_limit, result.net_return):
                raise ValueError("trade ledger and capital return disagree")
            for t in ledger:
                t["gross_target_potential"] = t.pop("prediction")
            filename = f"{rule}-{int(cost)}bps-trades.json"
            write_json(output / filename, ledger)
            ledgers[cost], metrics[cost] = ledger, result
            fold_metrics[cost] = [asdict(replay_one(subset, prepared)) for subset, prepared in fold_inputs]
        quality = ledger_quality(ledgers[20])
        stress_quality = ledger_quality(ledgers[30])
        combined_quality = {k: max(quality[k], stress_quality[k]) for k in quality}
        rejected = rejection_reasons(metrics[20], fold_metrics[20], metrics[30], combined_quality)
        report["candidates"].append(
            {
                "rule": rule,
                "signals": int(validation[rule].sum()),
                "metrics": {str(int(k)): asdict(v) for k, v in metrics.items()},
                "fold_metrics": {str(int(k)): v for k, v in fold_metrics.items()},
                "ledger_quality": {"20": quality, "30": stress_quality},
                "gross_pnl_20bps": sum(t["gross_pnl"] for t in ledgers[20]),
                "explicit_cost_20bps": sum(t["explicit_cost"] for t in ledgers[20]),
                "exit_reasons": pd.Series([t["reason"] for t in ledgers[20]], dtype=str)
                .value_counts()
                .to_dict(),
                "development_eligible": not rejected,
                "rejection_reasons": rejected,
            }
        )
        write_json(output / "report.json", report)
    report["elapsed_seconds"] = time.monotonic() - started
    report["completed_at"] = utcnow().isoformat()
    report["output_sha256"] = {p.name: checksum(p) for p in output.glob("*-trades.json")}
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
