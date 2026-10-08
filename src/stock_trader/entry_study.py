"""ADR-0019: train on existing entry-eligible setups, without changing trading gates."""

import argparse
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd

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
from stock_trader.research import prepare_replay
from stock_trader.study import aligned_labels, candidate, write_json


def training_mask(frame, target, train_end, benchmarks):
    return (
        frame["date"].le(train_end)
        & frame["feature_valid"]
        & ~frame["symbol"].isin(benchmarks)
        & target.notna()
        & frame["ma_distance"].gt(0)
        & frame["relative_strength"].gt(0)
    )


def run_entry_study(settings, source):
    started = time.monotonic()
    reference = json.loads((source / "report.json").read_text())
    if (
        reference["holdout_evaluated"]
        or reference["promoted"]
        or reference["settings"] != settings.public_contract()
        or reference["data_contract"] != "provider-native-iex-five-minute-v1"
    ):
        raise ValueError("registered completed native study required")
    frame = pd.read_parquet(source / "development-context.parquet")
    if (
        frame["date"].max() >= reference["holdout_start"]
        or len(frame) != reference["rows"]["development"]
        or frame.duplicated(["timestamp", "symbol"]).any()
    ):
        raise ValueError("cache keys or sealed boundary mismatch")
    out = settings.report_dir / f"entry-study-{utcnow().strftime('%Y%m%dT%H%M%SZ')}"
    out.mkdir()
    for folder in ("source", "models", "predictions", "trades"):
        (out / folder).mkdir()
    code = {}
    for name in (
        "entry_study.py",
        "study.py",
        "research.py",
        "modeling.py",
        "risk.py",
        "features.py",
        "config.py",
    ):
        path = Path(__file__).with_name(name)
        (out / "source" / name).write_bytes(path.read_bytes())
        code[name] = checksum(path)

    def progress(stage, **fields):
        value = {"stage": stage, "updated_at": utcnow().isoformat(), **fields}
        write_json(out / "progress.json", value)
        print(json.dumps(value), flush=True)

    validation = frame.loc[frame["date"] >= reference["folds"][0]["validation_start"]]
    fold_frames = [
        validation.loc[validation["date"].between(f["validation_start"], f["validation_end"])]
        for f in reference["folds"]
    ]
    progress("prepare_replay")
    replays = [prepare_replay(f) for f in (validation, *fold_frames)]
    progress("aligned_labels", horizon_minutes=120)
    target = aligned_labels(frame, 24, settings)
    report = {
        "created_at": utcnow().isoformat(),
        "source": str(source),
        "holdout_start": reference["holdout_start"],
        "holdout_evaluated": False,
        "promoted": False,
        "settings": settings.public_contract(),
        "features": reference["features"],
        "folds": reference["folds"],
        "fits": [],
        "candidates": [],
        "reference_candidates": [c for c in reference["candidates"] if c["horizon"] == 24],
        "code_sha256": code,
        "input_sha256": {
            p.name: checksum(p) for p in (source / "report.json", source / "development-context.parquet")
        },
    }
    for phase, columns in reference["features"].items():
        phase = "entry_" + phase
        oof = {f: pd.Series(np.nan, index=validation.index) for f in FAMILIES}
        for fold, spec in enumerate(reference["folds"], start=1):
            eligible = training_mask(frame, target, spec["train_end"], settings.benchmark_symbols)
            unfiltered = (
                frame["date"].le(spec["train_end"])
                & frame["feature_valid"]
                & ~frame["symbol"].isin(settings.benchmark_symbols)
                & target.notna()
            )
            valid = fold_frames[fold - 1]
            usable = valid["feature_valid"]
            for family in FAMILIES:
                progress("fit", phase=phase, family=family, fold=fold, training_rows=int(eligible.sum()))
                if eligible.sum() < 5000:
                    raise ValueError("insufficient entry-eligible training rows")
                model = make_regressor(family, settings.training_threads)
                model.fit(frame.loc[eligible, columns], target.loc[eligible])
                oof[family].loc[valid.index[usable]] = predict_regressor(model, valid.loc[usable, columns])
                directory = out / "models" / f"{phase}-{fold}-{family}"
                directory.mkdir()
                report["fits"].append(
                    {
                        "phase": phase,
                        "family": family,
                        "fold": fold,
                        "training_rows": int(eligible.sum()),
                        "unfiltered_training_rows": int(unfiltered.sum()),
                        "validation_rows": int(usable.sum()),
                        "members": save_models({family: model}, directory),
                    }
                )
                del model
        oof["ensemble"] = (oof["lgbm"] + oof["xgb"] + oof["catboost"]) / 3
        saved = validation[["timestamp", "symbol", "date", "feature_valid"]].copy()
        saved["label"] = target.loc[validation.index]
        for family in STRATEGIES:
            saved[family] = oof[family]
        saved.to_parquet(out / "predictions" / f"{phase}.parquet", index=False)
        for family in STRATEGIES:
            for threshold in (0.003, 0.0045, 0.006):
                progress(
                    "evaluate",
                    phase=phase,
                    family=family,
                    threshold=threshold,
                    completed=len(report["candidates"]),
                    total=24,
                )
                report["candidates"].append(
                    candidate(
                        validation,
                        fold_frames,
                        oof[family],
                        threshold,
                        24,
                        family,
                        phase,
                        settings,
                        replays,
                        out,
                    )
                )
        write_json(out / "report.json", report)
    report["completed_at"] = utcnow().isoformat()
    report["elapsed_seconds"] = time.monotonic() - started
    write_json(out / "report.json", report)
    progress(
        "complete",
        fits=len(report["fits"]),
        candidates=len(report["candidates"]),
        eligible=sum(c["eligible"] for c in report["candidates"]),
    )
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    run_entry_study(Settings(), parser.parse_args().source)


if __name__ == "__main__":
    main()
