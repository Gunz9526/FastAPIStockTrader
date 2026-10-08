"""ADR-0023/24: frozen candidates only, development cost stress, no promotion."""

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
from stock_trader.study import write_json
from stock_trader.swing_study import (
    load_inputs,
    prepare_swing,
    simulate_swing,
    trend_predictions,
)


def validate_prediction_keys(saved, frame, holdout):
    validation = frame.loc[frame["date"] >= "2025-11-07"]
    keys = ["date", "symbol", "feature_valid"]
    if (
        saved["date"].max() >= holdout
        or not saved[keys].reset_index(drop=True).equals(validation[keys].reset_index(drop=True))
        or saved.duplicated(["date", "symbol"]).any()
    ):
        raise ValueError("OOF prediction keys or sealed boundary mismatch")
    result = pd.Series(np.nan, index=frame.index)
    values = saved["xgb"].to_numpy()
    if not np.isfinite(values[saved["feature_valid"]]).all():
        raise ValueError("invalid frozen OOF predictions")
    result.loc[validation.index] = values
    return result


def run_followup(settings, source):
    started = time.monotonic()
    reference = json.loads((source / "report.json").read_text())
    if (
        not reference.get("completed_at")
        or len(reference["candidates"]) != 36
        or reference["promoted"]
        or reference["holdout_evaluated"]
        or reference["entry_reference"] != "opening_quote"
        or reference["settings"] != settings.public_contract()
    ):
        raise ValueError("completed ADR-0022 development reference required")
    model_candidate = next(
        c
        for c in reference["candidates"]
        if (c["family"], c["holding_days"], c["threshold"]) == ("xgb", 2, 0.005)
    )
    rule_candidate = next(c for c in reference["baselines"] if c["holding_days"] == 10)
    if (
        not model_candidate["eligible"]
        or not rule_candidate["eligible"]
        or rule_candidate["score"] <= model_candidate["score"]
    ):
        raise ValueError("frozen ADR-0024 choice differs from source")
    out = settings.report_dir / f"swing-followup-{utcnow().strftime('%Y%m%dT%H%M%SZ')}"
    out.mkdir()
    (out / "source").mkdir()
    (out / "trades").mkdir()
    hashes = {}
    for name, expected in reference["code_sha256"].items():
        if checksum(source / "source" / name) != expected:
            raise ValueError("source snapshot checksum mismatch")
    for fit in reference["fits"]:
        directory = source / "models" / f"{fit['holding_days']}-{fit['fold']}-{fit['family']}"
        for member in fit["members"]:
            if checksum(directory / member["file"]) != member["sha256"]:
                raise ValueError("native model checksum mismatch")
    for name in (
        "swing_followup.py",
        "swing_study.py",
        "risk.py",
        "config.py",
        "modeling.py",
        "research.py",
        "study.py",
    ):
        path = Path(__file__).with_name(name)
        (out / "source" / name).write_bytes(path.read_bytes())
        hashes[name] = checksum(path)
    frame, bars, dates, openings, development, inputs, _ = load_inputs(settings, Path(reference["source"]))
    if inputs != reference["input_sha256"]:
        raise ValueError("development inputs changed after selection")
    replay = prepare_swing(bars, dates, openings)
    saved = pd.read_parquet(source / "predictions" / "2.parquet")
    xgb = validate_prediction_keys(saved, frame, reference["holdout_start"])
    rule = trend_predictions(frame).where(frame["date"].ge(reference["folds"][0]["validation_start"]))
    intervals = [(f["validation_start"], f["validation_end"]) for f in development["folds"]]
    report = {
        "created_at": utcnow().isoformat(),
        "decision": "ADR-0024",
        "source": str(source),
        "holdout_evaluated": False,
        "promoted": False,
        "chosen": "trend_rule_10",
        "source_report_sha256": checksum(source / "report.json"),
        "prediction_sha256": checksum(source / "predictions" / "2.parquet"),
        "input_sha256": inputs,
        "code_sha256": hashes,
        "evaluations": [],
    }
    for family, horizon, predictions, candidate in (
        ("xgb", 2, xgb, model_candidate),
        ("trend_rule", 10, rule, rule_candidate),
    ):
        for cost in (20, 30):
            print(
                json.dumps({"stage": "stress", "family": family, "holding_days": horizon, "cost_bps": cost}),
                flush=True,
            )
            scoped = settings.model_copy(update={"round_trip_cost_bps": cost})
            ledger = []
            metrics = simulate_swing(
                frame, predictions, horizon, 0.005, scoped, replay, intervals, ledger, "opening_quote"
            )
            filename = f"{family}-{horizon}-{cost}.json"
            write_json(out / "trades" / filename, ledger)
            if cost == 20:
                original = json.loads((source / "trades" / candidate["trade_file"]).read_text())
                if ledger != original or any(
                    not np.isclose(getattr(metrics, k), candidate["metrics"][k], atol=1e-12)
                    for k in ("net_return", "max_drawdown", "trades", "profit_factor")
                ):
                    raise ValueError("fixed candidate no longer reproduces")
            report["evaluations"].append(
                {
                    "family": family,
                    "holding_days": horizon,
                    "cost_bps": cost,
                    "metrics": asdict(metrics),
                    "trade_file": filename,
                }
            )
    selected_stress = next(
        e for e in report["evaluations"] if e["family"] == "trend_rule" and e["cost_bps"] == 30
    )
    report["development_stress_passed"] = selected_stress["metrics"]["net_return"] > 0
    report["completed_at"] = utcnow().isoformat()
    report["elapsed_seconds"] = time.monotonic() - started
    write_json(out / "report.json", report)
    print(
        json.dumps({"stage": "complete", "development_stress_passed": report["development_stress_passed"]}),
        flush=True,
    )
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    run_followup(Settings(), parser.parse_args().source)


if __name__ == "__main__":
    main()
