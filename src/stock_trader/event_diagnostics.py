"""ADR-0031: audit existing native models and OOF outcomes without new trials."""

import argparse
import json
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd
import xgboost as xgb
from catboost import CatBoostRegressor

from stock_trader.clock import utcnow
from stock_trader.config import Settings
from stock_trader.event_study import entry_eligible
from stock_trader.modeling import FAMILIES, STRATEGIES, checksum
from stock_trader.study import write_json
from stock_trader.swing_study import DAILY_FEATURES, purged_training


def prediction_summary(frame, prediction):
    predicted = prediction.notna() & np.isfinite(prediction)
    complete = predicted & frame["label_target"].notna()
    p, y = prediction.loc[complete], frame.loc[complete, "label_target"]
    variable = len(y) > 1 and p.std() > 0 and y.std() > 0
    thresholds = {}
    for threshold in (0.005, 0.010, 0.015):
        chosen = predicted & prediction.ge(threshold)
        known = chosen & complete
        thresholds[str(threshold)] = {
            "predicted_signals": int(chosen.sum()),
            "known_completed_events": int(known.sum()),
            "unknown_events": int((chosen & ~complete).sum()),
            "completed_mean_gross_return": float(frame.loc[known, "label_target"].mean())
            if known.any()
            else None,
            "completed_positive_fraction": float(frame.loc[known, "label_target"].gt(0).mean())
            if known.any()
            else None,
            "is_actual_portfolio_trade_count": False,
        }
    return {
        "predicted_signals": int(predicted.sum()),
        "known_completed_events": int(complete.sum()),
        "status_counts": {
            str(k): int(v) for k, v in frame.loc[predicted, "label_status"].value_counts().items()
        },
        "prediction_mean_completed": float(p.mean()) if len(p) else None,
        "actual_gross_mean_completed": float(y.mean()) if len(y) else None,
        "mean_absolute_error_completed": float((p - y).abs().mean()) if len(y) else None,
        "pearson_completed": float(p.corr(y)) if variable else None,
        "spearman_completed": float(p.rank().corr(y.rank())) if variable else None,
        "thresholds": thresholds,
        "censoring_corrected": False,
    }


def native_predictions(family, path, frame, threads):
    if family == "lgbm":
        model = lgb.Booster(model_file=str(path))
        names, rounds = model.feature_name(), model.num_trees()
        values = model.predict(frame, num_threads=threads)
        importance = model.feature_importance(importance_type="gain")
        kind = "gain"
    elif family == "xgb":
        model = xgb.Booster(params={"nthread": threads, "device": "cpu"})
        model.load_model(path)
        names, rounds = model.feature_names, model.num_boosted_rounds()
        values = model.predict(xgb.DMatrix(frame, nthread=threads))
        scores = model.get_score(importance_type="gain")
        importance = np.array([scores.get(name, 0.0) for name in names])
        kind = "gain"
    elif family == "catboost":
        model = CatBoostRegressor(thread_count=threads, task_type="CPU")
        model.load_model(str(path), format="cbm")
        names, rounds = model.feature_names_, model.tree_count_
        values = model.predict(frame, thread_count=threads)
        importance = model.get_feature_importance(thread_count=threads)
        kind = "PredictionValuesChange"
    else:
        raise ValueError("unsupported family")
    if names != DAILY_FEATURES or rounds != 200:
        raise ValueError("native event model feature/round mismatch")
    total = float(np.sum(importance))
    pairs = sorted(zip(names, importance, strict=True), key=lambda item: item[1], reverse=True)
    return np.asarray(values), {
        "rounds": rounds,
        "features": len(names),
        "importance_kind": kind,
        "top_five": [
            {"feature": name, "normalized_importance": float(score / total) if total else 0.0}
            for name, score in pairs[:5]
        ],
        "is_causal_or_cross_family_comparable": False,
    }


def run_diagnostics(settings, source):
    reference = json.loads((source / "report.json").read_text())
    if (
        reference["decision"] != "ADR-0030"
        or reference["holdout_evaluated"]
        or reference["promoted"]
        or not reference.get("completed_at")
    ):
        raise ValueError("completed development event study required")
    out = settings.report_dir / f"event-diagnostics-{utcnow().strftime('%Y%m%dT%H%M%SZ')}"
    out.mkdir()
    (out / "source").mkdir()
    code = {}
    for path in sorted(Path(__file__).parent.glob("*.py")):
        (out / "source" / path.name).write_bytes(path.read_bytes())
        code[path.name] = checksum(path)
    for name, expected in reference["code_sha256"].items():
        if checksum(source / "source" / name) != expected:
            raise ValueError("archived study code checksum mismatch")
    hashes = {
        str(path): checksum(path)
        for path in (
            source / "report.json",
            source / "development-labels.parquet",
            source / "oof-predictions.parquet",
        )
    }
    frame = pd.read_parquet(source / "development-labels.parquet")
    oof = pd.read_parquet(source / "oof-predictions.parquet")
    if (
        frame.duplicated(["date", "symbol"]).any()
        or oof.duplicated(["date", "symbol"]).any()
        or frame["date"].max() >= reference["holdout_start"]
    ):
        raise ValueError("duplicate or sealed diagnostic rows")
    validation = frame.merge(oof, on=["date", "symbol"], how="inner", validate="one_to_one")
    cfg = Settings(_env_file=None, _secrets_dir=None, **reference["settings"])
    diagnostics = {
        "decision": "ADR-0031",
        "created_at": utcnow().isoformat(),
        "source": str(source),
        "holdout_evaluated": False,
        "promoted": False,
        "new_fits": 0,
        "new_replays": 0,
        "external_requests": 0,
        "code_sha256": code,
        "artifact_sha256": hashes,
        "native_models": [],
        "fold_label_availability": [],
        "prediction_summary": {},
        "portfolio_ledgers_verified": 0,
    }
    for fold, spec in enumerate(reference["folds"], start=1):
        available = entry_eligible(frame, cfg) & frame["date"].le(spec["train_end"])
        mask = (
            purged_training(
                frame,
                frame["label_target"],
                frame["label_event_end"],
                spec["train_end"],
                cfg.benchmark_symbols,
            )
            & available
        )
        diagnostics["fold_label_availability"].append(
            {
                "fold": fold,
                "train_end": spec["train_end"],
                "past_entry_eligible_signals": int(available.sum()),
                "known_completed_events": int(mask.sum()),
                "known_completed_fraction": float(mask.sum() / available.sum()),
                "event_ends_after_train_end": int(
                    (
                        available
                        & frame["label_target"].notna()
                        & frame["label_event_end"].gt(spec["train_end"])
                    ).sum()
                ),
                "other_unavailable_labels": int((available & frame["label_target"].isna()).sum()),
            }
        )
        for family in FAMILIES:
            fit = next(f for f in reference["fits"] if f["fold"] == fold and f["family"] == family)
            if (
                fit["training_rows"] != int(mask.sum())
                or fit["max_label_end"] != frame.loc[mask, "label_event_end"].max()
            ):
                raise ValueError("training label cutoff/count mismatch")
            member = fit["members"][0]
            path = source / "models" / f"{fold}-{family}" / member["file"]
            if path.is_symlink() or checksum(path) != member["sha256"]:
                raise ValueError("native event model checksum mismatch")
            valid = (
                validation["date"].between(spec["validation_start"], spec["validation_end"])
                & validation[family].notna()
            )
            predictions, detail = native_predictions(
                family, path, validation.loc[valid, DAILY_FEATURES], settings.training_threads
            )
            np.testing.assert_allclose(predictions, validation.loc[valid, family], rtol=1e-6, atol=1e-8)
            diagnostics["native_models"].append(
                {"family": family, "fold": fold, "validation_rows_reproduced": int(valid.sum()), **detail}
            )
            diagnostics["artifact_sha256"][str(path)] = member["sha256"]
    np.testing.assert_allclose(
        validation["ensemble"],
        validation[list(FAMILIES)].mean(axis=1, skipna=False),
        equal_nan=True,
        rtol=1e-12,
        atol=1e-12,
    )
    for family in STRATEGIES:
        diagnostics["prediction_summary"][family] = prediction_summary(validation, validation[family])
    baseline_reference = json.loads(
        (settings.report_dir / "barrier-study-20261003T163121Z/report.json").read_text()
    )
    for candidate in [reference["baseline"], *reference["candidates"]]:
        for cost in candidate["costs"].values():
            path = source / "trades" / cost["trade_file"]
            trades = json.loads(path.read_text())
            m, d = cost["metrics"], cost["diagnostics"]
            realized = sum(t["net_pnl"] for t in trades)
            if len(trades) != m["trades"] or not np.isclose(realized, d["realized_pnl"], atol=1e-6):
                raise ValueError("event realized ledger mismatch")
            total = (realized + d["unrealized_after_estimated_exit_cost"]) / cfg.capital_limit
            if not np.isclose(total, m["net_return"], atol=1e-12) or not np.isclose(
                np.prod(1 + np.array(m["daily_returns"])) - 1, total, atol=1e-12
            ):
                raise ValueError("event marked capital mismatch")
            diagnostics["artifact_sha256"][str(path)] = checksum(path)
            diagnostics["portfolio_ledgers_verified"] += 1
    # Compare the exact existing barrier baseline metrics, not a newly fitted candidate.
    for old in baseline_reference["evaluations"]:
        if old["exit_policy"] == "barrier":
            current = reference["baseline"]["costs"][str(old["cost_bps"])]["metrics"]
            for name in ("net_return", "max_drawdown", "trades", "profit_factor"):
                if not np.isclose(current[name], old["metrics"][name], atol=1e-12):
                    raise ValueError("registered barrier baseline mismatch")
    consumed = settings.report_dir / "final-test-consumed-0025.json"
    if checksum(consumed) != "d4b630dbf77191269fa4da5b6e5925779ce75d752978f2265aeca6d975eb1129":
        raise ValueError("consumed final protection changed")
    diagnostics["consumed_final_test_sha256"] = checksum(consumed)
    diagnostics["all_integrity_checks_passed"] = True
    write_json(out / "report.json", diagnostics)
    print(
        json.dumps(
            {
                "stage": "complete",
                "report": str(out),
                "native_models_verified": len(diagnostics["native_models"]),
                "ledgers_verified": diagnostics["portfolio_ledgers_verified"],
            }
        ),
        flush=True,
    )
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    run_diagnostics(Settings(), parser.parse_args().source)


if __name__ == "__main__":
    main()
