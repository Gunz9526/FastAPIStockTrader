"""Bounded research audit. Never promotes models or evaluates the sealed holdout."""

import argparse
import json
import time
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd

from stock_trader.clock import utcnow
from stock_trader.config import Settings
from stock_trader.data import read_history
from stock_trader.features import FEATURES, feature_frame, labels
from stock_trader.modeling import FAMILIES, STRATEGIES, checksum, make_regressor, save_models
from stock_trader.research import fit_model, predict_available, prepare_replay, simulate
from stock_trader.risk import RiskState, entry_plan


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))


def model_configuration(model):
    return {
        k: str(v) if isinstance(v, float) and not np.isfinite(v) else v for k, v in model.get_params().items()
    }


def aligned_labels(frame, horizon, settings):
    """Unit gross return conditional on entry; unknown required future bars stay unlabeled."""
    output = pd.Series(np.nan, index=frame.index, dtype=float)
    close_times = frame.groupby("date")["timestamp"].max() + pd.Timedelta(minutes=5)
    risk = RiskState(
        settings.capital_limit,
        settings.capital_limit,
        settings.capital_limit,
        settings.capital_limit,
        0,
        settings.capital_limit,
        0,
    )
    for (_, day), group in frame.groupby(["symbol", "date"], sort=False):
        if group["symbol"].iloc[0] in settings.benchmark_symbols:
            continue
        rows = list(
            group[
                ["timestamp", "symbol", "open", "high", "low", "close", "atr", "feature_valid"]
            ].itertuples()
        )
        locations = {r.timestamp: i for i, r in enumerate(rows)}
        closing = close_times[day] - pd.Timedelta(minutes=10)
        for i, row in enumerate(rows):
            if not row.feature_valid or row.timestamp >= close_times[day] - pd.Timedelta(minutes=30):
                continue
            entry_time = row.timestamp + pd.Timedelta(minutes=5)
            deadline = min(entry_time + pd.Timedelta(minutes=horizon * 5), closing)
            end = locations.get(deadline)
            if end is None or end - i != int((deadline - row.timestamp).total_seconds() // 300):
                continue
            plan = entry_plan(
                symbol=row.symbol,
                bid=row.close * 0.9996,
                ask=row.close * 1.0004,
                atr=row.atr,
                state=risk,
                settings=settings,
                expected_return=1.0,
            )
            if plan is None:
                continue
            opening = rows[i + 1]
            limit, stop = float(plan["limit_price"]), float(plan["stop_loss"]["stop_price"])
            take = float(plan["take_profit"]["limit_price"])
            if opening.open > limit:
                continue
            entry = min(limit, opening.open * 1.0005)
            if entry <= stop:
                continue
            exit_price = None
            for future in rows[i + 1 : end + 1]:
                if future.timestamp >= deadline:
                    exit_price = future.open * 0.9995
                elif future.low <= stop:
                    exit_price = min(stop, future.open) * 0.9995
                elif future.high >= take:
                    exit_price = take
                if exit_price is not None:
                    break
            if exit_price is not None:
                output.loc[row.Index] = exit_price / entry - 1
    return output


def summarize_trades(trades, capital):
    groups = {}
    for dimension in ("reason", "symbol"):
        groups[dimension] = {}
        for trade in trades:
            group = groups[dimension].setdefault(
                trade[dimension], {"trades": 0, "gross_pnl": 0.0, "explicit_cost": 0.0, "net_pnl": 0.0}
            )
            group["trades"] += 1
            for field in ("gross_pnl", "explicit_cost", "net_pnl"):
                group[field] += trade[field]
    return {
        "gross_return_fixed_trades": sum(t["gross_pnl"] for t in trades) / capital,
        "explicit_cost_return": sum(t["explicit_cost"] for t in trades) / capital,
        "net_return_from_ledger": sum(t["net_pnl"] for t in trades) / capital,
        "groups": groups,
    }


def candidate(frame, fold_frames, predictions, threshold, horizon, family, phase, settings, replays, out):
    trades = []
    execution = "legacy" if phase == "legacy" else "timed_open"
    metrics = simulate(
        frame,
        predictions,
        threshold=threshold,
        horizon=horizon,
        settings=settings,
        replay=replays[0],
        execution=execution,
        trades=trades,
    )
    folds = [
        asdict(
            simulate(
                f,
                predictions,
                threshold=threshold,
                horizon=horizon,
                settings=settings,
                replay=r,
                execution=execution,
            )
        )
        for f, r in zip(fold_frames, replays[1:], strict=True)
    ]
    ledger = summarize_trades(trades, settings.capital_limit)
    if not np.isclose(metrics.net_return, ledger["net_return_from_ledger"], atol=1e-10):
        raise ValueError("portfolio and trade ledger disagree")
    filename = f"{phase}-{family}-{horizon}-{threshold:.4f}.json"
    write_json(out / "trades" / filename, trades)
    return {
        "phase": phase,
        "family": family,
        "horizon": horizon,
        "threshold": threshold,
        "metrics": asdict(metrics),
        "folds": folds,
        "ledger": ledger,
        "trade_file": filename,
        "eligible": metrics.trades >= 60
        and metrics.net_return > 0
        and metrics.max_drawdown <= 0.08
        and metrics.profit_factor >= 1.1
        and sum(f["net_return"] > 0 for f in folds) >= 2,
        "score": metrics.net_return - 0.5 * metrics.max_drawdown,
    }


def run_study(settings, reference_path, feature_cache=None):
    reference = json.loads(reference_path.read_text())
    started = time.monotonic()
    out = settings.report_dir / f"study-{utcnow().strftime('%Y%m%dT%H%M%SZ')}"
    out.mkdir(parents=True)
    for name in ("models", "predictions", "trades"):
        (out / name).mkdir()

    def progress(stage, **fields):
        value = {"stage": stage, "updated_at": utcnow().isoformat(), **fields}
        write_json(out / "progress.json", value)
        print(json.dumps(value, ensure_ascii=False), flush=True)

    progress("read_data")
    bars = read_history(settings)
    session = bars["timestamp"].dt.tz_convert("America/New_York").dt.strftime("%Y-%m-%d")
    dates = sorted(session.unique())
    test_index = int(len(dates) * 0.85)
    if (
        dates[test_index] != reference["test_start"]
        or dates[0] != reference["data_start"]
        or dates[-1] != reference["data_end"]
    ):
        raise ValueError("reference date boundaries changed; register a new research contract")
    validation_start = int(len(dates) * 0.55)
    splits = np.linspace(validation_start, test_index, 4, dtype=int)
    total_rows = len(bars)
    bars = bars.loc[session < reference["test_start"]]
    progress("features", rows=len(bars), sealed_test_start=reference["test_start"])
    if feature_cache:
        frame = pd.read_parquet(feature_cache)
        local = bars["timestamp"].dt.tz_convert("America/New_York")
        regular = (local.dt.hour * 60 + local.dt.minute - 570).between(0, 385)
        keys = (
            bars.loc[regular, ["timestamp", "symbol"]]
            .sort_values(["timestamp", "symbol"])
            .reset_index(drop=True)
        )
        if not frame[["timestamp", "symbol"]].equals(keys) or not set(FEATURES).issubset(frame.columns):
            raise ValueError("feature cache does not match development input")
        if frame["date"].max() >= reference["test_start"]:
            raise ValueError("sealed holdout in feature cache")
    else:
        frame = feature_frame(bars, settings.benchmark_symbols, include_invalid=True)
    frame.to_parquet(out / "development-features.parquet", index=False)
    universe = ~frame["symbol"].isin(settings.benchmark_symbols)
    quality = {
        s: {
            "bars": len(g),
            "valid_features": int(g["feature_valid"].sum()),
            "decision_rows": int((g["feature_valid"] & g["timestamp"].dt.minute.mod(15).eq(10)).sum()),
        }
        for s, g in frame.groupby("symbol")
    }
    report = {
        "created_at": utcnow().isoformat(),
        "reference": reference_path.name,
        "holdout_start": reference["test_start"],
        "holdout_evaluated": False,
        "promoted": False,
        "input_rows": total_rows,
        "development_rows": len(frame),
        "sessions": len(dates),
        "features": FEATURES,
        "data_quality": quality,
        "fits": [],
        "candidates": [],
        "model_configs": {
            f: model_configuration(make_regressor(f, settings.training_threads)) for f in FAMILIES
        },
        "feature_cache": {"source": str(feature_cache), "sha256": checksum(feature_cache)}
        if feature_cache
        else None,
        "data_sha256": {
            str(p.relative_to(settings.data_dir)): checksum(p)
            for p in sorted((settings.data_dir / "bars").glob("*/*.parquet"))
        },
        "settings": settings.public_contract(),
        "code_sha256": {
            p.name: checksum(p)
            for p in [
                Path(__file__),
                Path(__file__).with_name("research.py"),
                Path(__file__).with_name("features.py"),
                Path(__file__).with_name("modeling.py"),
            ]
        },
        "folds": [
            {
                "fold": i + 1,
                "train_start": dates[0],
                "train_end": dates[a - 1],
                "validation_start": dates[a],
                "validation_end": dates[b - 1],
            }
            for i, (a, b) in enumerate(zip(splits[:-1], splits[1:], strict=True))
        ],
    }
    write_json(out / "report.json", report)
    validation = frame.loc[frame["date"].isin(dates[validation_start:test_index])]
    fold_frames = [
        validation.loc[validation["date"].isin(dates[a:b])]
        for a, b in zip(splits[:-1], splits[1:], strict=True)
    ]
    progress("prepare_replay")
    replays = [prepare_replay(f) for f in (validation, *fold_frames)]
    for horizon in (6, 12, 24):
        for target_kind in ("endpoint", "aligned"):
            progress("labels", target=target_kind, horizon_minutes=horizon * 5)
            target = (
                labels(frame, horizon)
                if target_kind == "endpoint"
                else aligned_labels(frame, horizon, settings)
            )
            oof = {f: pd.Series(np.nan, index=frame.index) for f in FAMILIES}
            for fold, (a, _b) in enumerate(zip(splits[:-1], splits[1:], strict=True), start=1):
                train = frame.loc[frame["date"] < dates[a]]
                valid_frame = fold_frames[fold - 1]
                eligible = (
                    target.loc[train.index].notna() & train["feature_valid"] & universe.loc[train.index]
                )
                eligible_validation = (
                    target.loc[valid_frame.index].notna()
                    & valid_frame["feature_valid"]
                    & universe.loc[valid_frame.index]
                )
                for family in FAMILIES:
                    progress(
                        "fit",
                        target=target_kind,
                        horizon_minutes=horizon * 5,
                        fold=fold,
                        family=family,
                        raw_rows=len(train),
                        fitted_rows=int(eligible.sum()),
                    )
                    details = {
                        "target": target_kind,
                        "family": family,
                        "horizon_minutes": horizon * 5,
                        "fold": fold,
                        "raw_training_rows": len(train),
                        "fitted_rows": int(eligible.sum()),
                        "eligible_validation_labels": int(eligible_validation.sum()),
                        **report["folds"][fold - 1],
                    }
                    if eligible.sum() < 5000:
                        details["state"] = "insufficient_training_rows"
                    else:
                        fit_start = time.monotonic()
                        model = fit_model(train, target.loc[train.index], settings, family)
                        oof[family].loc[valid_frame.index] = predict_available(model, valid_frame)
                        directory = out / "models" / f"{target_kind}-{horizon}-{fold}-{family}"
                        directory.mkdir()
                        details["members"] = save_models({family: model}, directory)
                        details["state"] = "fitted"
                        details["seconds"] = time.monotonic() - fit_start
                        del model
                    report["fits"].append(details)
            oof["ensemble"] = (oof["lgbm"] + oof["xgb"] + oof["catboost"]) / 3
            saved = validation[["timestamp", "symbol", "feature_valid", "date"]].copy()
            saved["label"] = target.loc[validation.index]
            for family in STRATEGIES:
                saved[family] = oof[family].loc[validation.index]
            saved.to_parquet(out / "predictions" / f"{target_kind}-{horizon}.parquet", index=False)
            phases = ("legacy", "timing_corrected") if target_kind == "endpoint" else ("aligned",)
            for phase in phases:
                for family in STRATEGIES:
                    for threshold in (0.003, 0.0045, 0.006):
                        progress(
                            "evaluate",
                            phase=phase,
                            family=family,
                            horizon_minutes=horizon * 5,
                            threshold=threshold,
                            completed=len(report["candidates"]),
                            total=108,
                        )
                        c = candidate(
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
                        if phase == "legacy":
                            original = next(
                                x
                                for x in reference["candidates"]
                                if x["family"] == family
                                and x["horizon"] == horizon
                                and x["threshold"] == threshold
                            )
                            if any(
                                not np.isclose(c["metrics"][k], original["metrics"][k], atol=1e-10)
                                for k in ("net_return", "max_drawdown", "trades", "profit_factor")
                            ):
                                raise ValueError("legacy reproduction mismatch")
                        report["candidates"].append(c)
                write_json(out / "report.json", report)
    report["elapsed_seconds"] = time.monotonic() - started
    report["completed_at"] = utcnow().isoformat()
    write_json(out / "report.json", report)
    progress(
        "complete",
        candidates=len(report["candidates"]),
        eligible=sum(c["eligible"] for c in report["candidates"]),
        elapsed_seconds=report["elapsed_seconds"],
    )
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--feature-cache", type=Path)
    args = parser.parse_args()
    print(run_study(Settings(), args.reference, args.feature_cache), flush=True)


if __name__ == "__main__":
    main()
