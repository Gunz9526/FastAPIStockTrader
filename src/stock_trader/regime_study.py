"""ADR-0016: one fixed comparison of lagged session context, without holdout access."""

import argparse
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd

from stock_trader.clock import utcnow
from stock_trader.config import Settings
from stock_trader.features import FEATURES, feature_frame
from stock_trader.modeling import (
    FAMILIES,
    STRATEGIES,
    checksum,
    make_regressor,
    predict_regressor,
    save_models,
)
from stock_trader.research import prepare_replay
from stock_trader.study import aligned_labels, candidate, model_configuration, write_json

CONTEXT_FEATURES = [
    "prev_session_return",
    "prev_5_session_return",
    "prev_20_session_volatility",
    "prev_20_session_coverage",
    "observed_open_gap",
    "market_prev_session_return",
    "market_prev_5_session_return",
    "market_prev_20_session_volatility",
    "relative_prev_5_session_return",
]


def context_frame(frame, benchmarks):
    """All daily close statistics lagged one session; current gap uses first observed open only."""
    if frame.duplicated(["timestamp", "symbol"]).any():
        raise ValueError("duplicate bar keys")
    output = frame.copy()
    dates = sorted(frame["date"].unique())
    # Coverage is relative to the last observed bucket across the universe, not a certified calendar close.
    slots = (frame.groupby("date")["minute_of_session"].max() + 5) / 5
    summaries = []
    for symbol, group in frame.sort_values("timestamp").groupby("symbol", sort=False):
        daily = group.groupby("date").agg(
            observed_open=("open", "first"), observed_close=("close", "last"), bars=("close", "size")
        )
        daily = daily.reindex(dates)
        close = daily["observed_close"]
        returns = close.pct_change(fill_method=None)
        daily["prev_session_return"] = returns.shift(1)
        # A five-session return cannot bridge an unobserved session.
        complete_five = close.notna().shift(1).rolling(6).sum().eq(6)
        daily["prev_5_session_return"] = (close.shift(1) / close.shift(6) - 1).where(complete_five)
        daily["prev_20_session_volatility"] = returns.shift(1).rolling(20, min_periods=20).std()
        daily["prev_20_session_coverage"] = (
            (daily["bars"] / slots).shift(1).rolling(20, min_periods=20).mean()
        )
        daily["observed_open_gap"] = daily["observed_open"] / close.shift(1) - 1
        daily["symbol"] = symbol
        summaries.append(daily.reset_index())
    daily = pd.concat(summaries, ignore_index=True)
    available = ["prev_session_return", "prev_5_session_return", "prev_20_session_volatility"]
    benchmark = daily.loc[daily["symbol"].isin(benchmarks)]
    grouped = benchmark.groupby("date")[available]
    market = (
        grouped.mean()
        .where(grouped.count().eq(len(benchmarks)))
        .rename(columns={c: "market_" + c for c in available})
    )
    daily = daily.join(market, on="date")
    daily["relative_prev_5_session_return"] = (
        daily["prev_5_session_return"] - daily["market_prev_5_session_return"]
    )
    joined = output[["symbol", "date"]].merge(
        daily[["symbol", "date", *CONTEXT_FEATURES]],
        on=["symbol", "date"],
        how="left",
        validate="many_to_one",
    )
    for column in CONTEXT_FEATURES:
        output[column] = joined[column].to_numpy()
    output["context_valid"] = np.isfinite(output[CONTEXT_FEATURES].to_numpy(dtype=float)).all(axis=1)
    output["feature_valid"] &= output["context_valid"]
    return output, daily


def native_frame(settings, directory, strict, holdout):
    manifest = json.loads((directory / "manifest.json").read_text())
    collection = json.loads((directory / "collection.json").read_text())
    if (
        manifest["feed"],
        manifest["timeframe"],
        manifest["adjustment"],
        manifest["symbols"],
        manifest["holdout_start"],
    ) != ("iex", "5Min", "raw", list(settings.symbols), holdout) or collection["contract"] != manifest:
        raise ValueError("native research contract mismatch")
    hashes = {}
    parts = []
    for filename, expected in collection["file_sha256"].items():
        path = directory / filename
        if not path.resolve().is_relative_to(directory.resolve()) or checksum(path) != expected:
            raise ValueError("native input checksum mismatch")
        hashes[str(path)] = expected
        parts.append(pd.read_parquet(path))
    if not parts:
        raise ValueError("empty native input")
    bars = pd.concat(parts, ignore_index=True).sort_values(["timestamp", "symbol"]).reset_index(drop=True)
    if bars.duplicated(["timestamp", "symbol"]).any():
        raise ValueError("duplicate native bars")
    dates = bars["timestamp"].dt.tz_convert("America/New_York").dt.strftime("%Y-%m-%d")
    if dates.max() >= holdout or set(dates) != set(strict["date"]):
        raise ValueError("native date boundaries differ from sealed development")
    native = feature_frame(bars, settings.benchmark_symbols, include_invalid=True)
    columns = ["timestamp", "symbol", "open", "high", "low", "close", "volume"]
    common = strict[columns].merge(
        native[columns], on=["timestamp", "symbol"], validate="one_to_one", suffixes=("_strict", "_native")
    )
    if common.empty:
        raise ValueError("native and strict data have no common bars")
    quality = {
        "strict_rows": len(strict),
        "native_rows": len(native),
        "common_rows": len(common),
        "added_rows": len(native) - len(common),
        "strict_only_rows": len(strict) - len(common),
        "strict_feature_valid": int(strict["feature_valid"].sum()),
        "native_feature_valid": int(native["feature_valid"].sum()),
        "common_field_differences": {},
    }
    for field in columns[2:]:
        previous, current = common[field + "_strict"], common[field + "_native"]
        quality["common_field_differences"][field] = {
            "changed": int((~np.isclose(previous, current, atol=1e-8, rtol=1e-8)).sum()),
            "max_abs_difference": float((previous - current).abs().max()),
        }
    for name in ("manifest.json", "calendar.json", "collection.json"):
        hashes[str(directory / name)] = checksum(directory / name)
    return native, quality, hashes


def run_regime_study(settings, source, native_dir=None):
    started = time.monotonic()
    reference = json.loads((source / "report.json").read_text())
    if (
        reference["holdout_evaluated"]
        or reference["promoted"]
        or reference["settings"] != settings.public_contract()
    ):
        raise ValueError("registered sealed study/settings required")
    raw = pd.read_parquet(source / "development-features.parquet")
    if raw["date"].max() >= reference["holdout_start"]:
        raise ValueError("sealed holdout in input")
    input_hashes = {
        p.name: checksum(p) for p in (source / "report.json", source / "development-features.parquet")
    }
    quality = None
    if native_dir:
        raw, quality, native_hashes = native_frame(settings, native_dir, raw, reference["holdout_start"])
        input_hashes.update(native_hashes)
    prefix = "native-study" if native_dir else "regime-study"
    out = settings.report_dir / f"{prefix}-{utcnow().strftime('%Y%m%dT%H%M%SZ')}"
    out.mkdir()
    for folder in ("source", "models", "predictions", "trades"):
        (out / folder).mkdir()
    code = {}
    for name in (
        "regime_study.py",
        "study.py",
        "research.py",
        "modeling.py",
        "features.py",
        "risk.py",
        "config.py",
        "native_data.py",
    ):
        path = Path(__file__).with_name(name)
        (out / "source" / name).write_bytes(path.read_bytes())
        code[name] = checksum(path)

    def progress(stage, **fields):
        value = {"stage": stage, "updated_at": utcnow().isoformat(), **fields}
        write_json(out / "progress.json", value)
        print(json.dumps(value, ensure_ascii=False), flush=True)

    progress("context_features")
    frame, daily = context_frame(raw, settings.benchmark_symbols)
    frame.to_parquet(out / "development-context.parquet", index=False)
    daily.to_parquet(out / "observed-session-context.parquet", index=False)
    validation = frame.loc[frame["date"] >= reference["folds"][0]["validation_start"]]
    fold_frames = [
        validation.loc[validation["date"].between(f["validation_start"], f["validation_end"])]
        for f in reference["folds"]
    ]
    report = {
        "created_at": utcnow().isoformat(),
        "source": str(source),
        "native_data_dir": str(native_dir) if native_dir else None,
        "data_contract": "provider-native-iex-five-minute-v1" if native_dir else "strict-five-minutes-v1",
        "data_quality_comparison": quality,
        "holdout_start": reference["holdout_start"],
        "holdout_evaluated": False,
        "promoted": False,
        "folds": reference["folds"],
        "features": {"matched_baseline": FEATURES, "session_context": [*FEATURES, *CONTEXT_FEATURES]},
        "fits": [],
        "candidates": [],
        "code_sha256": code,
        "input_sha256": input_hashes,
        "settings": settings.public_contract(),
        "model_configs": {
            f: model_configuration(make_regressor(f, settings.training_threads)) for f in FAMILIES
        },
        "rows": {
            "development": len(frame),
            "original_feature_valid": int(raw["feature_valid"].sum()),
            "matched_feature_valid": int(frame["feature_valid"].sum()),
            "validation_original_valid": int(raw.loc[validation.index, "feature_valid"].sum()),
            "validation_matched_valid": int(validation["feature_valid"].sum()),
        },
    }
    progress("prepare_replay")
    replays = [prepare_replay(f) for f in (validation, *fold_frames)]
    universe = ~frame["symbol"].isin(settings.benchmark_symbols)
    for horizon in (12, 24):
        progress("aligned_labels", horizon_minutes=horizon * 5)
        target = aligned_labels(frame, horizon, settings)
        for phase, columns in report["features"].items():
            oof = {f: pd.Series(np.nan, index=validation.index) for f in FAMILIES}
            for fold, spec in enumerate(reference["folds"], start=1):
                training = frame["date"] <= spec["train_end"]
                eligible = training & frame["feature_valid"] & universe & target.notna()
                v = fold_frames[fold - 1]
                usable = v["feature_valid"]
                for family in FAMILIES:
                    progress(
                        "fit",
                        phase=phase,
                        horizon_minutes=horizon * 5,
                        fold=fold,
                        family=family,
                        rows=int(eligible.sum()),
                    )
                    details = {
                        "phase": phase,
                        "family": family,
                        "horizon": horizon,
                        "fold": fold,
                        "training_rows": int(eligible.sum()),
                        "validation_rows": int(usable.sum()),
                        "feature_count": len(columns),
                    }
                    if eligible.sum() < 5000:
                        details["state"] = "insufficient_training_rows"
                    else:
                        fitting = time.monotonic()
                        model = make_regressor(family, settings.training_threads)
                        model.fit(frame.loc[eligible, columns], target.loc[eligible])
                        if usable.any():
                            oof[family].loc[v.index[usable]] = predict_regressor(
                                model, v.loc[usable, columns]
                            )
                        directory = out / "models" / f"{phase}-{horizon}-{fold}-{family}"
                        directory.mkdir()
                        details["members"] = save_models({family: model}, directory)
                        details["state"] = "fitted"
                        details["seconds"] = time.monotonic() - fitting
                        del model
                    report["fits"].append(details)
            oof["ensemble"] = (oof["lgbm"] + oof["xgb"] + oof["catboost"]) / 3
            saved = validation[["timestamp", "symbol", "date", "feature_valid"]].copy()
            saved["label"] = target.loc[validation.index]
            for family in STRATEGIES:
                saved[family] = oof[family]
            saved.to_parquet(out / "predictions" / f"{phase}-{horizon}.parquet", index=False)
            for family in STRATEGIES:
                for threshold in (0.003, 0.0045, 0.006):
                    progress(
                        "evaluate",
                        phase=phase,
                        horizon_minutes=horizon * 5,
                        family=family,
                        threshold=threshold,
                        completed=len(report["candidates"]),
                        total=48,
                    )
                    report["candidates"].append(
                        candidate(
                            validation,
                            fold_frames,
                            oof[family],
                            threshold,
                            horizon,
                            family,
                            phase,
                            settings,
                            replays,
                            out,
                        )
                    )
            write_json(out / "report.json", report)
    report["elapsed_seconds"] = time.monotonic() - started
    report["completed_at"] = utcnow().isoformat()
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
    parser.add_argument("--native-data", type=Path)
    args = parser.parse_args()
    run_regime_study(Settings(), args.source, args.native_data)


if __name__ == "__main__":
    main()
