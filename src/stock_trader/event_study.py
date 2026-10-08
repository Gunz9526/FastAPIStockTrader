"""ADR-0030: CPU models learn first target-hit returns, sealed development only."""

import argparse
import json
import time
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd

from stock_trader.barrier_study import ledger_summary, rejection_counts
from stock_trader.clock import utcnow
from stock_trader.config import Settings
from stock_trader.modeling import (
    FAMILIES,
    STRATEGIES,
    checksum,
    make_regressor,
    predict_regressor,
    save_models,
)
from stock_trader.risk import RiskState
from stock_trader.study import model_configuration, write_json
from stock_trader.swing_study import (
    DAILY_FEATURES,
    load_inputs,
    normalized_plan,
    observable_window,
    path_exit,
    prepare_swing,
    purged_training,
    simulate_swing,
    swing_eligible,
    trend_predictions,
)


def entry_eligible(frame, settings):
    return (
        frame["feature_valid"]
        & frame["ma_20"].gt(0)
        & frame["ret_20"].gt(0)
        & ~frame["symbol"].isin(settings.benchmark_symbols)
    )


def event_labels(frame, replay, settings):
    """Unknown endings remain NaN; purge on observed event date, never planned time."""
    labels = pd.DataFrame(index=frame.index)
    labels["target"] = np.nan
    labels["event_end"] = ""
    labels["status"] = "not_entry_eligible"
    labels["exit_reason"] = ""
    labels["holding_sessions"] = np.nan
    lookup = {d: i for i, d in enumerate(replay.dates)}
    capital = settings.capital_limit
    state = RiskState(capital, capital, capital, capital, 0, capital, 0)
    for index, signal in frame.loc[entry_eligible(frame, settings)].iterrows():
        day = lookup[signal["date"]]
        if day + 1 >= len(replay.dates):
            labels.loc[index, "status"] = "no_next_session"
            continue
        entry_day = replay.dates[day + 1]
        path = replay.paths.get(signal["symbol"])
        if path is None:
            labels.loc[index, "status"] = "missing_open"
            continue
        start = np.searchsorted(path["time"], replay.openings[entry_day].value)
        if start >= len(path["time"]) or path["time"][start] != replay.openings[entry_day].value:
            labels.loc[index, "status"] = "missing_open"
            continue
        row = path["values"][start]
        plan = normalized_plan(signal, row[4], state, settings, 1.0, row[0])
        if plan is None:
            labels.loc[index, "status"] = "no_plan"
            continue
        if row[0] * 1.0005 > plan["entry"]:
            labels.loc[index, "status"] = "unfilled"
            continue
        values = path["values"][start:]
        hits = np.flatnonzero((values[:, 2] <= plan["stop"]) | (values[:, 1] >= plan["take"]))
        if not len(hits):
            labels.loc[index, "status"] = (
                "censored_end"
                if observable_window(replay, signal["symbol"], day + 1, len(replay.dates) - 1)
                else "unobserved_before_exit"
            )
            continue
        hit = start + hits[0]
        end = path["date"][hit]
        if not observable_window(replay, signal["symbol"], day + 1, lookup[end]):
            labels.loc[index, "status"] = "unobserved_before_exit"
            continue
        price, reason, _ = path_exit(path["values"][hit], plan["stop"], plan["take"])
        labels.loc[index, ["target", "event_end", "status", "exit_reason", "holding_sessions"]] = [
            price / plan["entry"] - 1,
            end,
            "closed",
            reason,
            lookup[end] - day - 1,
        ]
    return labels


def run_event_study(settings, source):
    started = time.monotonic()
    out = settings.report_dir / f"event-study-{utcnow().strftime('%Y%m%dT%H%M%SZ')}"
    out.mkdir()
    for name in ("source", "models", "trades"):
        (out / name).mkdir()
    code = {}
    for path in sorted(Path(__file__).parent.glob("*.py")):
        (out / "source" / path.name).write_bytes(path.read_bytes())
        code[path.name] = checksum(path)

    def progress(stage, **fields):
        value = {"stage": stage, "updated_at": utcnow().isoformat(), **fields}
        write_json(out / "progress.json", value)
        print(json.dumps(value), flush=True)

    progress("inputs")
    frame, bars, dates, openings, reference, hashes, quality = load_inputs(settings, source)
    replay = prepare_swing(bars, dates, openings)
    del bars
    progress("event_labels")
    labels = event_labels(frame, replay, settings)
    frame.join(labels.add_prefix("label_")).to_parquet(out / "development-labels.parquet", index=False)
    eligible_entries = entry_eligible(frame, settings)
    intervals = [(f["validation_start"], f["validation_end"]) for f in reference["folds"]]
    report = {
        "decision": "ADR-0030",
        "created_at": utcnow().isoformat(),
        "source": str(source),
        "holdout_start": reference["holdout_start"],
        "holdout_evaluated": False,
        "promoted": False,
        "settings": settings.public_contract(),
        "features": DAILY_FEATURES,
        "folds": reference["folds"],
        "input_sha256": hashes,
        "code_sha256": code,
        "split_rounding_max_abs_difference": quality,
        "label_contract": "first-target-hit-gross-return-next-rth-opening-quote-v1",
        "entry_cutoff_sessions": 10,
        "holding_time_limit": None,
        "label_status_counts": {str(k): int(v) for k, v in labels["status"].value_counts().items()},
        "completed_label_holding_sessions": {
            "median": float(labels["holding_sessions"].median()),
            "max": float(labels["holding_sessions"].max()),
        },
        "model_configuration": {
            f: model_configuration(make_regressor(f, settings.training_threads)) for f in FAMILIES
        },
        "fits": [],
        "candidates": [],
        "baseline": {},
        "assumptions": [
            "completed-event-only regression can favor shorter holdings; censoring not corrected",
            "labels omit portfolio-dependent risk exits; simulation enforces them",
            "IEX-only opening OHLC execution proxy, no dividends, sparse intraday observations",
            "future execution-data availability restricts sample, not an executable trading filter",
            "existing development validation reused; independent fresh data required",
            "20/30bps modeled fees and 5bps adverse prices are not measured actual costs",
        ],
    }
    oof = {family: pd.Series(np.nan, index=frame.index) for family in FAMILIES}
    for fold, spec in enumerate(reference["folds"], start=1):
        mask = (
            purged_training(
                frame, labels["target"], labels["event_end"], spec["train_end"], settings.benchmark_symbols
            )
            & eligible_entries
        )
        unpurged = eligible_entries & frame["date"].le(spec["train_end"]) & labels["target"].notna()
        validation = eligible_entries & frame["date"].between(
            spec["validation_start"], spec["validation_end"]
        )
        if mask.sum() < 1500:
            raise ValueError("insufficient purged event training rows")
        for family in FAMILIES:
            progress("fit", fold=fold, family=family, training_rows=int(mask.sum()))
            model = make_regressor(family, settings.training_threads)
            model.fit(frame.loc[mask, DAILY_FEATURES], labels.loc[mask, "target"])
            oof[family].loc[validation] = predict_regressor(model, frame.loc[validation, DAILY_FEATURES])
            directory = out / "models" / f"{fold}-{family}"
            directory.mkdir()
            report["fits"].append(
                {
                    "family": family,
                    "fold": fold,
                    "training_rows": int(mask.sum()),
                    "purged_rows": int(unpurged.sum() - mask.sum()),
                    "unfinished_or_unobserved_rows": int(
                        (eligible_entries & frame["date"].le(spec["train_end"]) & ~mask).sum()
                    ),
                    "max_label_end": labels.loc[mask, "event_end"].max(),
                    "training_label_holding_median": float(labels.loc[mask, "holding_sessions"].median()),
                    "validation_rows": int(validation.sum()),
                    "members": save_models({family: model}, directory),
                }
            )
            del model
    oof["ensemble"] = (oof["lgbm"] + oof["xgb"] + oof["catboost"]) / 3
    saved = frame.loc[frame["date"].between(intervals[0][0], intervals[-1][1]), ["date", "symbol"]].copy()
    for family in STRATEGIES:
        saved[family] = oof[family].loc[saved.index]
    saved.to_parquet(out / "oof-predictions.parquet", index=False)

    def evaluate(name, predictions, threshold):
        costs = {}
        fold_metrics = []
        base_metrics = None
        for cost in (20, 30):
            cfg = settings.model_copy(update={"round_trip_cost_bps": cost})
            ledger, diagnostics = [], {}
            metrics = simulate_swing(
                frame,
                predictions,
                10,
                threshold,
                cfg,
                replay,
                intervals,
                ledger,
                "opening_quote",
                "barrier",
                diagnostics,
            )
            filename = f"{name}-{cost}.json"
            write_json(out / "trades" / filename, ledger)
            costs[str(cost)] = {
                "metrics": asdict(metrics),
                "diagnostics": diagnostics,
                "ledger": ledger_summary(ledger, settings.capital_limit),
                "trade_file": filename,
            }
            if cost == 20:
                base_metrics = metrics
                fold_metrics = [
                    simulate_swing(
                        frame,
                        predictions,
                        10,
                        threshold,
                        cfg,
                        replay,
                        [interval],
                        [],
                        "opening_quote",
                        "barrier",
                        {},
                    )
                    for interval in intervals
                ]
        return {
            "costs": costs,
            "folds": [asdict(f) for f in fold_metrics],
            "metrics": costs["20"]["metrics"],
            "eligible": swing_eligible(base_metrics, fold_metrics),
            "stress_positive": costs["30"]["metrics"]["net_return"] > 0,
        }

    progress("baseline")
    scores = trend_predictions(frame).where(frame["date"].ge(intervals[0][0]))
    report["baseline"] = evaluate("trend-rule", scores, 0.005)
    for family in STRATEGIES:
        for threshold in (0.005, 0.010, 0.015):
            progress(
                "evaluate", family=family, threshold=threshold, completed=len(report["candidates"]), total=12
            )
            value = evaluate(f"{family}-{threshold:.3f}", oof[family], threshold)
            value.update(
                {
                    "family": family,
                    "threshold": threshold,
                    "beats_baseline": value["metrics"]["net_return"]
                    > report["baseline"]["metrics"]["net_return"],
                }
            )
            value["development_followup_eligible"] = (
                value["eligible"] and value["stress_positive"] and value["beats_baseline"]
            )
            report["candidates"].append(value)
            write_json(out / "report.json", report)
    report["rejection_counts"] = rejection_counts(report["candidates"])
    report["completed_at"] = utcnow().isoformat()
    report["elapsed_seconds"] = time.monotonic() - started
    report["development_followup_eligible"] = sum(
        c["development_followup_eligible"] for c in report["candidates"]
    )
    write_json(out / "report.json", report)
    progress(
        "complete",
        fits=len(report["fits"]),
        candidates=len(report["candidates"]),
        followup_eligible=report["development_followup_eligible"],
    )
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    run_event_study(Settings(), parser.parse_args().source)


if __name__ == "__main__":
    main()
