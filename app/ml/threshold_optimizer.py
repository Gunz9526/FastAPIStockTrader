"""Adaptive Threshold Optimizer for per-regime θ and confidence tuning.

Optimizes two independent thresholds for each market regime:

1. **Classification Threshold (θ)** — daily return cutoff for labeling
   UP / NEUTRAL / DOWN.  Searched via Optuna over ``[0.002, 0.015]``.
2. **Confidence Threshold** — minimum ``max(prob)`` required for the model
   to act on a prediction.  Searched over ``[0.30, 0.70]``.

Both searches maximise composite metrics using ``TimeSeriesSplit`` CV and
``CatBoostClassifier`` (fast: iterations=100, depth=4).

CPU-only.  No GPU back-ends.

Phase H.5+: Adaptive Threshold Optimization
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sklearn.metrics import accuracy_score, f1_score
from sklearn.model_selection import TimeSeriesSplit

# ---------------------------------------------------------------------------
# Lazy imports — surface Korean-language errors early
# ---------------------------------------------------------------------------
try:
    import optuna
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "optuna 패키지가 설치되어 있지 않습니다. "
        "`pip install optuna` 를 실행하세요."
    ) from exc

try:
    from catboost import CatBoostClassifier
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "catboost 패키지가 설치되어 있지 않습니다. "
        "`pip install catboost` 를 실행하세요."
    ) from exc

# Suppress Optuna's verbose trial logs
optuna.logging.set_verbosity(optuna.logging.WARNING)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------
_THETA_RANGE: tuple[float, float] = (0.002, 0.015)
_CONFIDENCE_RANGE: tuple[float, float] = (0.30, 0.70)
_DEFAULT_REGIMES: list[str] = [
    "bull_trending",
    "bear_trending",
    "sideways_volatile",
    "sideways_calm",
]
_CLASS_NAMES: list[str] = ["DOWN", "NEUTRAL", "UP"]
_THRESHOLDS_FILENAME: str = "adaptive_thresholds.json"

# Default confidence thresholds when no optimization data is available
_DEFAULT_CONFIDENCE_BY_REGIME: dict[str, float] = {
    "bull_trending": 0.45,
    "bear_trending": 0.55,
    "sideways_volatile": 0.60,
    "sideways_calm": 0.40,
}

# Global default θ (mirrors training.py CLASSIFICATION_THRESHOLD)
_DEFAULT_THETA: float = 0.005


# ---------------------------------------------------------------------------
# Data container
# ---------------------------------------------------------------------------

@dataclass
class ThresholdResult:
    """Results from threshold optimization for a single regime.

    Attributes:
        regime: Market regime label (e.g. ``"bull_trending"``).
        optimal_theta: Best classification threshold found by Optuna.
        optimal_confidence: Best confidence threshold found by Optuna.
            ``0.0`` if confidence optimization was skipped.
        theta_composite_score: Composite score achieved at ``optimal_theta``.
        confidence_trade_score: Trade score achieved at ``optimal_confidence``.
            ``0.0`` if confidence optimization was skipped.
        n_samples: Number of samples used for θ optimization.
        theta_search_range: ``(low, high)`` of the θ search space.
        confidence_search_range: ``(low, high)`` of the confidence search space.
        class_distribution: ``{0: n_down, 1: n_neutral, 2: n_up}`` at
            ``optimal_theta``.
    """

    regime: str
    optimal_theta: float
    optimal_confidence: float
    theta_composite_score: float
    confidence_trade_score: float
    n_samples: int
    theta_search_range: tuple[float, float]
    confidence_search_range: tuple[float, float]
    class_distribution: dict[int, int]


# ---------------------------------------------------------------------------
# Local composite score (mirrors training.py — do NOT import to avoid
# circular dependency)
# ---------------------------------------------------------------------------

def _composite_score(
    y_true: np.ndarray,
    y_pred: np.ndarray,
) -> tuple[float, float, float, float, float]:
    """Calculate composite objective score for ternary classification.

    Formula (same as ``app.tasks.training._calculate_composite_score``):

    .. math::

        C = 0.30 \\cdot \\text{accuracy}
          + 0.30 \\cdot \\text{f1\\_weighted}
          + 0.15 \\cdot \\text{class\\_balance}
          + 0.25 \\cdot \\text{min\\_class\\_recall}

    Args:
        y_true: Ground-truth labels (0 / 1 / 2).
        y_pred: Predicted labels (0 / 1 / 2).

    Returns:
        Tuple of ``(composite, accuracy, f1_weighted, class_balance,
        min_class_recall)``.
    """
    acc = float(accuracy_score(y_true, y_pred))
    f1 = float(f1_score(y_true, y_pred, average="weighted", zero_division=0))

    unique_preds = np.unique(y_pred)
    class_balance = len(unique_preds) / len(_CLASS_NAMES)  # 0.33 … 1.0

    per_class_recalls: list[float] = []
    for cls in range(len(_CLASS_NAMES)):
        mask = y_true == cls
        recall = float((y_pred[mask] == cls).sum()) / float(mask.sum()) if mask.sum() > 0 else 0.0
        per_class_recalls.append(recall)

    min_class_recall = min(per_class_recalls)

    composite = (
        0.30 * acc
        + 0.30 * f1
        + 0.15 * class_balance
        + 0.25 * min_class_recall
    )
    return composite, acc, f1, class_balance, min_class_recall


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _label_returns(returns: np.ndarray, theta: float) -> np.ndarray:
    """Assign ternary labels based on return threshold θ.

    Args:
        returns: Raw daily return array.
        theta: Classification threshold (positive float).

    Returns:
        Integer array with 0 (DOWN), 1 (NEUTRAL), or 2 (UP).
    """
    return np.where(returns > theta, 2, np.where(returns < -theta, 0, 1))


def _class_distribution(y: np.ndarray) -> dict[int, int]:
    """Count samples per class.

    Args:
        y: Label array (0 / 1 / 2).

    Returns:
        ``{0: count, 1: count, 2: count}`` mapping.
    """
    dist: dict[int, int] = {}
    for cls in range(len(_CLASS_NAMES)):
        dist[cls] = int((y == cls).sum())
    return dist


def _min_class_fraction(y: np.ndarray) -> float:
    """Return the smallest class fraction in *y*.

    Args:
        y: Label array.

    Returns:
        Fraction of the rarest class (0.0 … 1.0).
    """
    n = len(y)
    if n == 0:
        return 0.0
    counts = np.bincount(y.astype(int), minlength=len(_CLASS_NAMES))
    return float(counts.min()) / n


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------

class AdaptiveThresholdOptimizer:
    """Per-regime optimizer for classification and confidence thresholds.

    Stateless — all mutable data flows through method parameters.  Safe to
    instantiate multiple times or share across threads (no internal state
    is mutated after ``__init__``).

    Example::

        optimizer = AdaptiveThresholdOptimizer(output_dir="model_artifacts")
        theta_result = optimizer.optimize_classification_threshold(
            returns=daily_returns,
            X=features_df,
            regime="bull_trending",
        )
    """

    def __init__(self, output_dir: str = "model_artifacts") -> None:
        """Initialise optimizer.

        Args:
            output_dir: Directory for saving / loading threshold JSON.
        """
        self.output_dir = Path(output_dir)

    # ------------------------------------------------------------------
    # θ optimisation
    # ------------------------------------------------------------------

    def optimize_classification_threshold(
        self,
        returns: np.ndarray,
        X: pd.DataFrame,
        regime: str,
        class_weights: dict[int, float] | None = None,
        n_trials: int = 100,
    ) -> dict:
        """Find the best classification threshold θ for a single regime.

        Uses Optuna to search ``_THETA_RANGE`` and evaluates each candidate
        via 3-fold ``TimeSeriesSplit`` cross-validation with a fast
        ``CatBoostClassifier`` (iterations=100, depth=4).

        Args:
            returns: Raw daily returns array (float, same length as *X*).
            X: Feature matrix (already scaled).
            regime: Regime label (e.g. ``"bull_trending"``).
            class_weights: Optional ``{0: w, 1: w, 2: w}`` class weight
                overrides for CatBoost training.
            n_trials: Number of Optuna trials (default 100).

        Returns:
            Dict with keys ``theta``, ``composite_score``,
            ``class_distribution``.
        """
        logger.info(
            "[%s] θ 최적화 시작 — 범위 [%.4f, %.4f], %d trials",
            regime, _THETA_RANGE[0], _THETA_RANGE[1], n_trials,
        )

        # Build CatBoost class_weights list (ordered by class index)
        cw_list: list[float] | None = None
        if class_weights is not None:
            cw_list = [class_weights.get(i, 1.0) for i in range(len(_CLASS_NAMES))]

        def _objective(trial: optuna.Trial) -> float:
            theta = trial.suggest_float("theta", _THETA_RANGE[0], _THETA_RANGE[1])
            y = _label_returns(returns, theta)

            # Penalise extreme imbalance (any class < 5%)
            if _min_class_fraction(y) < 0.05:
                return 0.0

            tscv = TimeSeriesSplit(n_splits=3)
            fold_scores: list[float] = []

            for train_idx, val_idx in tscv.split(X):
                X_train = X.iloc[train_idx]
                y_train = y[train_idx]
                X_val = X.iloc[val_idx]
                y_val = y[val_idx]

                params: dict[str, Any] = {
                    "iterations": 100,
                    "depth": 4,
                    "learning_rate": 0.1,
                    "verbose": False,
                    "allow_writing_files": False,
                    "loss_function": "MultiClass",
                    "random_seed": 42,
                    "thread_count": -1,
                }
                if cw_list is not None:
                    params["class_weights"] = cw_list

                model = CatBoostClassifier(**params)
                model.fit(X_train, y_train)

                preds = model.predict(X_val).astype(int).ravel()
                score, _, _, _, _ = _composite_score(y_val, preds)
                fold_scores.append(score)

            return float(np.mean(fold_scores))

        sampler = optuna.samplers.TPESampler(seed=42)
        pruner = optuna.pruners.MedianPruner()
        study = optuna.create_study(
            direction="maximize",
            sampler=sampler,
            pruner=pruner,
            study_name=f"theta_{regime}",
        )
        study.optimize(_objective, n_trials=n_trials, show_progress_bar=False)

        best_theta: float = study.best_params["theta"]
        best_score: float = study.best_value
        best_y = _label_returns(returns, best_theta)
        dist = _class_distribution(best_y)

        logger.info(
            "[%s] θ 최적화 완료 — θ=%.5f, composite=%.4f, 분포=%s",
            regime, best_theta, best_score, dist,
        )

        return {
            "theta": best_theta,
            "composite_score": best_score,
            "class_distribution": dist,
        }

    # ------------------------------------------------------------------
    # Confidence optimisation
    # ------------------------------------------------------------------

    def optimize_confidence_threshold(
        self,
        model: Any,
        X: pd.DataFrame,
        y: np.ndarray,
        regime: str,
        n_trials: int = 80,
    ) -> dict:
        """Find the best confidence threshold for acting on predictions.

        The *model* must expose ``predict_proba(X) -> (n, 3)`` and
        ``predict(X) -> (n,)`` interfaces.

        Trade score formula:

        .. math::

            \\text{trade\\_score} = \\text{acted\\_accuracy}
            \\times \\sqrt{\\text{coverage}}

        If ``coverage < 0.05`` the score is penalised by ``× 0.1``.

        Args:
            model: Trained ensemble model with ``predict`` /
                ``predict_proba``.
            X: Feature matrix (already scaled).
            y: True labels (0 / 1 / 2).
            regime: Regime label.
            n_trials: Number of Optuna trials (default 80).

        Returns:
            Dict with keys ``confidence``, ``trade_score``,
            ``acted_accuracy``, ``coverage``.
        """
        logger.info(
            "[%s] 신뢰도 임계값 최적화 시작 — 범위 [%.2f, %.2f], %d trials",
            regime, _CONFIDENCE_RANGE[0], _CONFIDENCE_RANGE[1], n_trials,
        )

        # Pre-compute probabilities and predictions once (immutable during search)
        proba: np.ndarray = np.asarray(model.predict_proba(X))  # (n, 3)
        pred_classes: np.ndarray = np.asarray(model.predict(X)).astype(int).ravel()
        max_probs: np.ndarray = proba.max(axis=1)

        y_arr = np.asarray(y).astype(int).ravel()

        def _objective(trial: optuna.Trial) -> float:
            conf = trial.suggest_float(
                "confidence", _CONFIDENCE_RANGE[0], _CONFIDENCE_RANGE[1],
            )

            # Act mask: BUY (pred==2) or SELL (pred==0) with confidence ≥ threshold
            acted_mask = (
                ((pred_classes == 2) | (pred_classes == 0))
                & (max_probs >= conf)
            )
            n_acted = int(acted_mask.sum())
            n_total = len(y_arr)

            if n_acted == 0:
                return 0.0

            coverage = n_acted / n_total
            acted_accuracy = float(
                accuracy_score(y_arr[acted_mask], pred_classes[acted_mask]),
            )
            trade_score = acted_accuracy * math.sqrt(coverage)

            # Penalise very low coverage
            if coverage < 0.05:
                trade_score *= 0.1

            return trade_score

        sampler = optuna.samplers.TPESampler(seed=123)
        study = optuna.create_study(
            direction="maximize",
            sampler=sampler,
            study_name=f"confidence_{regime}",
        )
        study.optimize(_objective, n_trials=n_trials, show_progress_bar=False)

        best_conf: float = study.best_params["confidence"]

        # Re-evaluate best to capture all metrics
        best_acted_mask = (
            ((pred_classes == 2) | (pred_classes == 0))
            & (max_probs >= best_conf)
        )
        n_acted = int(best_acted_mask.sum())
        n_total = len(y_arr)
        coverage = n_acted / n_total if n_total > 0 else 0.0
        if n_acted > 0:
            acted_accuracy = float(
                accuracy_score(y_arr[best_acted_mask], pred_classes[best_acted_mask]),
            )
        else:
            acted_accuracy = 0.0
        trade_score = acted_accuracy * math.sqrt(coverage) if coverage > 0 else 0.0
        if coverage < 0.05:
            trade_score *= 0.1

        logger.info(
            "[%s] 신뢰도 최적화 완료 — threshold=%.3f, trade_score=%.4f, "
            "accuracy=%.3f, coverage=%.3f",
            regime, best_conf, trade_score, acted_accuracy, coverage,
        )

        return {
            "confidence": best_conf,
            "trade_score": trade_score,
            "acted_accuracy": acted_accuracy,
            "coverage": coverage,
        }

    # ------------------------------------------------------------------
    # Multi-regime orchestration
    # ------------------------------------------------------------------

    def optimize_all_regimes(
        self,
        data_by_regime: dict[str, tuple[pd.DataFrame, np.ndarray, np.ndarray]],
        models_by_regime: dict[str, Any] | None = None,
        class_weights_by_regime: dict[str, dict[int, float]] | None = None,
    ) -> dict[str, ThresholdResult]:
        """Run θ and confidence optimisation for every supplied regime.

        Args:
            data_by_regime: ``{regime: (X_scaled, y_labels, raw_returns)}``
                mapping.  ``y_labels`` are used only for confidence
                optimisation; for θ optimisation the labels are re-derived
                from ``raw_returns``.
            models_by_regime: ``{regime: trained_model}`` for confidence
                optimisation.  Regimes without a model skip confidence
                tuning.
            class_weights_by_regime: ``{regime: {0: w, 1: w, 2: w}}``
                class weight overrides per regime.

        Returns:
            ``{regime: ThresholdResult}`` mapping.
        """
        if models_by_regime is None:
            models_by_regime = {}
        if class_weights_by_regime is None:
            class_weights_by_regime = {}

        results: dict[str, ThresholdResult] = {}

        for regime, (X, y_labels, raw_returns) in data_by_regime.items():
            logger.info("레짐 '%s' 최적화 시작 (샘플 수: %d)", regime, len(X))

            # --- θ optimisation ---
            cw = class_weights_by_regime.get(regime)
            theta_res = self.optimize_classification_threshold(
                returns=raw_returns,
                X=X,
                regime=regime,
                class_weights=cw,
            )

            # --- Confidence optimisation (if model available) ---
            conf_res: dict = {}
            if regime in models_by_regime:
                conf_res = self.optimize_confidence_threshold(
                    model=models_by_regime[regime],
                    X=X,
                    y=y_labels,
                    regime=regime,
                )

            result = ThresholdResult(
                regime=regime,
                optimal_theta=theta_res["theta"],
                optimal_confidence=conf_res.get("confidence", 0.0),
                theta_composite_score=theta_res["composite_score"],
                confidence_trade_score=conf_res.get("trade_score", 0.0),
                n_samples=len(X),
                theta_search_range=_THETA_RANGE,
                confidence_search_range=_CONFIDENCE_RANGE,
                class_distribution=theta_res["class_distribution"],
            )
            results[regime] = result
            logger.info(
                "레짐 '%s' 최적화 완료 — θ=%.5f, confidence=%.3f",
                regime, result.optimal_theta, result.optimal_confidence,
            )

        return results

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save_thresholds(
        self,
        results: dict[str, ThresholdResult],
        path: str | Path | None = None,
    ) -> Path:
        """Serialise optimisation results to JSON.

        Args:
            results: ``{regime: ThresholdResult}`` mapping from
                ``optimize_all_regimes``.
            path: Override output file path.  Defaults to
                ``<output_dir>/adaptive_thresholds.json``.

        Returns:
            Resolved path of the written file.
        """
        out_path = Path(path) if path else self.output_dir / _THRESHOLDS_FILENAME
        out_path.parent.mkdir(parents=True, exist_ok=True)

        regimes_payload: dict[str, dict] = {}
        for regime, res in results.items():
            regimes_payload[regime] = {
                "theta": res.optimal_theta,
                "confidence": res.optimal_confidence,
                "theta_composite_score": res.theta_composite_score,
                "confidence_trade_score": res.confidence_trade_score,
                "n_samples": res.n_samples,
                "class_distribution": {
                    str(k): v for k, v in res.class_distribution.items()
                },
            }

        payload: dict[str, Any] = {
            "optimization_date": datetime.now(_dt.UTC).isoformat(),
            "regimes": regimes_payload,
            "defaults": {
                "theta": _DEFAULT_THETA,
                "confidence_by_regime": _DEFAULT_CONFIDENCE_BY_REGIME,
            },
        }

        with open(out_path, "w", encoding="utf-8") as fp:
            json.dump(payload, fp, indent=2, ensure_ascii=False)

        logger.info("임계값 저장 완료 — %s", out_path)
        return out_path

    def load_thresholds(
        self,
        path: str | Path | None = None,
    ) -> dict[str, ThresholdResult]:
        """Load previously saved threshold results from JSON.

        Args:
            path: Override input file path.  Defaults to
                ``<output_dir>/adaptive_thresholds.json``.

        Returns:
            ``{regime: ThresholdResult}`` mapping.

        Raises:
            FileNotFoundError: If the JSON file does not exist.
        """
        in_path = Path(path) if path else self.output_dir / _THRESHOLDS_FILENAME

        if not in_path.exists():
            raise FileNotFoundError(
                f"임계값 파일을 찾을 수 없습니다: {in_path}"
            )

        with open(in_path, encoding="utf-8") as fp:
            data = json.load(fp)

        results: dict[str, ThresholdResult] = {}
        for regime, info in data.get("regimes", {}).items():
            # Convert string keys back to int for class_distribution
            raw_dist = info.get("class_distribution", {})
            dist = {int(k): v for k, v in raw_dist.items()}

            results[regime] = ThresholdResult(
                regime=regime,
                optimal_theta=info["theta"],
                optimal_confidence=info.get("confidence", 0.0),
                theta_composite_score=info.get("theta_composite_score", 0.0),
                confidence_trade_score=info.get("confidence_trade_score", 0.0),
                n_samples=info.get("n_samples", 0),
                theta_search_range=_THETA_RANGE,
                confidence_search_range=_CONFIDENCE_RANGE,
                class_distribution=dist,
            )

        logger.info("임계값 로드 완료 — %d개 레짐", len(results))
        return results


# ---------------------------------------------------------------------------
# Standalone convenience entry-point
# ---------------------------------------------------------------------------

def run_threshold_optimization(
    output_dir: str = "model_artifacts",
) -> dict[str, ThresholdResult]:
    """Convenience entry-point for Celery tasks.

    Creates an ``AdaptiveThresholdOptimizer``, runs all-regime optimisation,
    and persists results to ``<output_dir>/adaptive_thresholds.json``.

    .. note::

        Requires data to be passed separately (similar to
        ``run_shap_analysis``).  This stub performs a no-op optimisation
        when called without data — callers must supply data via the
        optimizer instance directly.

    Args:
        output_dir: Directory containing model artifacts and where the
            threshold JSON is saved.

    Returns:
        ``{regime: ThresholdResult}`` mapping.  Empty dict if no data is
        provided.
    """
    optimizer = AdaptiveThresholdOptimizer(output_dir=output_dir)

    logger.info(
        "적응형 임계값 최적화 시작 — output_dir=%s", output_dir,
    )

    # Attempt to load existing thresholds as a fallback
    try:
        existing = optimizer.load_thresholds()
        logger.info(
            "기존 임계값 파일 로드됨 — %d개 레짐", len(existing),
        )
        return existing
    except FileNotFoundError:
        logger.warning(
            "기존 임계값 파일 없음 — 데이터를 전달하여 "
            "optimize_all_regimes()를 호출하세요."
        )
        return {}
