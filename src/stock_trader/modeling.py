"""Common CPU prediction and native artifact contract for comparable model candidates."""

import hashlib
from pathlib import Path

import lightgbm as lgb
import numpy as np
import xgboost as xgb
from catboost import CatBoostRegressor

from stock_trader.features import FEATURES

FAMILIES = ("lgbm", "xgb", "catboost")
STRATEGIES = (*FAMILIES, "ensemble")


def make_regressor(family: str, threads: int):
    if family == "lgbm":
        return lgb.LGBMRegressor(
            n_estimators=200,
            learning_rate=0.03,
            num_leaves=15,
            max_depth=5,
            min_child_samples=200,
            reg_lambda=10,
            n_jobs=threads,
            verbosity=-1,
            random_state=42,
            deterministic=True,
            force_col_wise=True,
        )
    if family == "xgb":
        return xgb.XGBRegressor(
            n_estimators=200,
            learning_rate=0.03,
            max_depth=5,
            min_child_weight=50,
            reg_lambda=10,
            max_bin=255,
            tree_method="hist",
            device="cpu",
            n_jobs=threads,
            objective="reg:squarederror",
            random_state=42,
            verbosity=0,
        )
    if family == "catboost":
        return CatBoostRegressor(
            iterations=200,
            learning_rate=0.03,
            depth=5,
            l2_leaf_reg=10,
            loss_function="RMSE",
            task_type="CPU",
            thread_count=threads,
            random_seed=42,
            verbose=False,
            allow_writing_files=False,
            has_time=True,
        )
    raise ValueError("unknown model family")


def checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def predict_regressor(model, frame):
    if isinstance(model, CatBoostRegressor):
        return model.predict(frame, thread_count=model.get_param("thread_count"))
    return model.predict(frame)


def save_models(models: dict, directory: Path) -> list[dict]:
    members = []
    for family, model in models.items():
        if family == "lgbm":
            path = directory / "lgbm.txt"
            model.booster_.save_model(str(path))
        elif family == "xgb":
            path = directory / "xgb.ubj"
            model.save_model(path)
        elif family == "catboost":
            path = directory / "catboost.cbm"
            model.save_model(str(path), format="cbm")
        else:
            raise ValueError("unsupported model family")
        members.append({"family": family, "file": path.name, "sha256": checksum(path)})
    return members


class NativeBundle:
    def __init__(self, directory: Path, members: list[dict], weights: list[float], threads: int):
        if (
            not members
            or len(weights) != len(members)
            or len(set(m["family"] for m in members)) != len(members)
        ):
            raise ValueError("invalid model bundle")
        if not all(np.isfinite(w) and w > 0 for w in weights) or not np.isclose(sum(weights), 1):
            raise ValueError("invalid ensemble weights")
        self.models = []
        self.weights = weights
        self.threads = threads
        for member in members:
            filename = member["file"]
            if Path(filename).name != filename:
                raise ValueError("invalid model member path")
            path = directory / filename
            if path.is_symlink() or checksum(path) != member["sha256"]:
                raise ValueError("model checksum mismatch")
            family = member["family"]
            if family == "lgbm":
                model = lgb.Booster(model_file=str(path))
                names = model.feature_name()
            elif family == "xgb":
                model = xgb.Booster(params={"nthread": threads, "device": "cpu"})
                model.load_model(path)
                names = model.feature_names
            elif family == "catboost":
                model = CatBoostRegressor(thread_count=threads, task_type="CPU")
                model.load_model(str(path), format="cbm")
                names = model.feature_names_
            else:
                raise ValueError("unknown model family")
            if names != FEATURES:
                raise ValueError("model feature names mismatch")
            self.models.append((family, model))

    def predict(self, frame):
        x = frame[FEATURES]
        results = []
        for family, model in self.models:
            if family == "lgbm":
                values = model.predict(x, num_threads=self.threads)
            elif family == "xgb":
                values = model.predict(xgb.DMatrix(x, nthread=self.threads))
            else:
                values = model.predict(x, thread_count=self.threads)
            results.append(np.asarray(values, dtype=float))
        output = np.average(np.stack(results), weights=self.weights, axis=0)
        if output.shape != (len(frame),) or not np.isfinite(output).all():
            raise ValueError("nonfinite or invalid predictions")
        return output
