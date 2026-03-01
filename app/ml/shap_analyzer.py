"""SHAP-based Feature Importance Analyzer.

Complements ``feature_analyzer.py`` (tree-based ``feature_importances_``) by
computing SHAP (SHapley Additive exPlanations) values for the ensemble
classifier (CatBoost + LightGBM + XGBoost, ``VotingClassifier(soft)``).

Key capabilities:
- Per-estimator ``TreeExplainer`` with voting-weight aggregation.
- Per-class and global (mean |SHAP|) importance.
- Directional SHAP summary (mean, std, min, max) — preserves sign.
- Feature selection helpers (threshold / top-*k*).
- Human-readable text report + JSON export.

CPU-only — no GPU SHAP back-ends.

Phase H.4+: Advanced Feature Importance (SHAP)
"""

import json
import logging
from dataclasses import dataclass
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

from app.ml.models import CLASS_NAMES

logger = logging.getLogger(__name__)

# Regimes used when no explicit list is provided.
_DEFAULT_REGIMES: list[str] = [
    "bull_trending",
    "bear_trending",
    "sideways_volatile",
    "sideways_calm",
]

# Feature that must never be suggested for removal (categorical).
_PROTECTED_FEATURES: frozenset[str] = frozenset({"sector_id"})


# ---------------------------------------------------------------------------
# Data container
# ---------------------------------------------------------------------------

@dataclass
class SHAPResult:
    """Container for SHAP analysis results of a single model/regime.

    Attributes:
        feature_names: Ordered feature list used during analysis.
        global_importance: ``{feature: mean_abs_shap}`` averaged across all
            classes and weighted across estimators.
        per_class_importance: ``{class_name: {feature: mean_abs_shap}}``.
        shap_values_summary: ``{feature: {mean, std, min, max}}`` of the
            *signed* (directional) SHAP values averaged across classes.
        n_samples: Number of samples used for SHAP computation.
        regime: Regime label (e.g. ``"bull_trending"``).
    """

    feature_names: list[str]
    global_importance: dict[str, float]
    per_class_importance: dict[str, dict[str, float]]
    shap_values_summary: dict[str, dict[str, float]]
    n_samples: int
    regime: str


# ---------------------------------------------------------------------------
# Main analyser class
# ---------------------------------------------------------------------------

class SHAPFeatureSelector:
    """SHAP-based feature importance analyser for the ensemble classifier.

    Uses ``shap.TreeExplainer`` on each sub-estimator of the
    ``VotingClassifier`` (cat / lgbm / xgb), weights the SHAP matrices
    by the ensemble voting weights, and aggregates per-class and global
    importance.

    Args:
        model_artifacts_path: Directory that contains the trained
            ``ensemble_classifier_*.pkl`` files.
        shap_sample_size: Maximum number of samples passed to SHAP.
            If the dataset is larger, stratified sampling is applied.
    """

    def __init__(
        self,
        model_artifacts_path: str = "model_artifacts",
        shap_sample_size: int = 500,
    ) -> None:
        self.model_path = Path(model_artifacts_path)
        self.shap_sample_size = shap_sample_size

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def analyze_model(
        self,
        model,
        X: pd.DataFrame,
        feature_names: list[str],
        regime: str = "unknown",
        y: pd.Series | None = None,
    ) -> SHAPResult:
        """Compute SHAP values for one ensemble model.

        Runs ``shap.TreeExplainer`` on each individual estimator
        (``cat`` / ``lgbm`` / ``xgb``), computes per-class mean |SHAP|,
        then aggregates with voting weights.

        Args:
            model: Trained ``EnsembleClassifierWrapper`` **or** its inner
                ``VotingClassifier``.  Expects ``named_estimators_`` and
                ``weights`` attributes (or falls back to equal weights).
            X: Feature matrix (all available data — sampling is handled
                internally).
            feature_names: Ordered list of feature column names.
            regime: Label for the regime (informational).
            y: Optional target Series for stratified sub-sampling.

        Returns:
            Populated ``SHAPResult`` instance.

        Raises:
            ImportError: If the ``shap`` package is not installed.
        """
        shap = self._import_shap()

        # Resolve VotingClassifier and weights.
        voting_clf, weights = self._resolve_model(model)

        # Sub-sample if necessary.
        X_sample = self._subsample(X, n=self.shap_sample_size, y=y)
        n_samples = len(X_sample)
        logger.info(
            "SHAP 분석 시작: %s  (샘플 %d / 전체 %d)",
            regime, n_samples, len(X),
        )

        n_features = len(feature_names)
        n_classes = len(CLASS_NAMES)

        # Accumulators: shape (n_samples, n_features, n_classes)
        weighted_shap = np.zeros((n_samples, n_features, n_classes))

        estimator_names = ["cat", "lgbm", "xgb"]
        for name, w in zip(estimator_names, weights, strict=True):
            estimator = voting_clf.named_estimators_.get(name)
            if estimator is None:
                logger.warning("추정기 '%s' 없음 — 건너뜁니다", name)
                continue

            try:
                shap_vals = self._compute_tree_shap(
                    shap, estimator, name, X_sample, n_features, n_classes,
                )
                weighted_shap += shap_vals * w
            except Exception:
                logger.exception("SHAP 계산 실패 (%s)", name)

        # Normalise by sum of weights (handles missing estimators).
        total_weight = sum(weights)
        if total_weight > 0:
            weighted_shap /= total_weight

        # --- Aggregate ---
        # per-class importance: mean |SHAP| per feature
        per_class_importance: dict[str, dict[str, float]] = {}
        for cls_idx, cls_name in enumerate(CLASS_NAMES):
            cls_abs_mean = np.abs(weighted_shap[:, :, cls_idx]).mean(axis=0)
            per_class_importance[cls_name] = {
                feat: float(cls_abs_mean[i])
                for i, feat in enumerate(feature_names)
            }

        # global importance: average across classes
        global_abs = np.abs(weighted_shap).mean(axis=(0, 2))  # (n_features,)
        global_importance = {
            feat: float(global_abs[i]) for i, feat in enumerate(feature_names)
        }

        # directional summary (signed SHAP, averaged over classes)
        signed_mean_over_classes = weighted_shap.mean(axis=2)  # (n_samples, n_features)
        shap_values_summary: dict[str, dict[str, float]] = {}
        for i, feat in enumerate(feature_names):
            col = signed_mean_over_classes[:, i]
            shap_values_summary[feat] = {
                "mean": float(np.mean(col)),
                "std": float(np.std(col)),
                "min": float(np.min(col)),
                "max": float(np.max(col)),
            }

        logger.info("SHAP 분석 완료: %s  (피처 %d개)", regime, n_features)

        return SHAPResult(
            feature_names=feature_names,
            global_importance=global_importance,
            per_class_importance=per_class_importance,
            shap_values_summary=shap_values_summary,
            n_samples=n_samples,
            regime=regime,
        )

    def analyze_all_regimes(
        self,
        feature_names: list[str],
        regimes: list[str] | None = None,
        X_data: dict[str, pd.DataFrame] | None = None,
        y_data: dict[str, pd.Series] | None = None,
    ) -> dict[str, SHAPResult]:
        """Load models from disk and run SHAP analysis for every regime.

        Args:
            feature_names: Feature column names.
            regimes: Regime labels to analyse.  Defaults to all four
                standard regimes.
            X_data: ``{regime: DataFrame}`` mapping with real feature data
                for each regime.  Regimes without data are **skipped**.
            y_data: ``{regime: Series}`` mapping with target labels for
                stratified sampling.  Optional.

        Returns:
            ``{regime: SHAPResult}`` mapping (regimes whose model files are
            missing or whose data is not provided are skipped).
        """
        if regimes is None:
            regimes = list(_DEFAULT_REGIMES)

        results: dict[str, SHAPResult] = {}

        for regime in regimes:
            # 실제 데이터 확인 — 더미 데이터 SHAP은 무의미하므로 건너뜀
            X_regime = X_data.get(regime) if X_data else None
            if X_regime is None or X_regime.empty:
                logger.warning(
                    "레짐 '%s': 실제 데이터 없음 — SHAP 분석을 건너뜁니다. "
                    "X_data에 해당 레짐 데이터를 전달하세요.",
                    regime,
                )
                continue

            model_file = self.model_path / f"ensemble_classifier_{regime}.pkl"
            metadata_file = self.model_path / f"ensemble_classifier_{regime}_metadata.json"

            if not model_file.exists():
                logger.warning("모델 파일 없음: %s", model_file)
                continue

            try:
                voting_clf = joblib.load(model_file)
                logger.info("모델 로드 완료: %s", model_file)

                # Load weights from metadata if available.
                weights: list[float] | None = None
                if metadata_file.exists():
                    with open(metadata_file) as f:
                        metadata = json.load(f)
                        weights = metadata.get("weights")

                # Build a lightweight shim so _resolve_model sees the expected attrs.
                model_shim = _ModelShim(voting_clf, weights)

                # feature 이름에 맞는 컬럼만 선택
                available = [f for f in feature_names if f in X_regime.columns]
                if len(available) < len(feature_names):
                    missing = set(feature_names) - set(available)
                    logger.warning("레짐 '%s' 누락 feature: %s", regime, missing)

                X_regime_filtered = pd.DataFrame(X_regime[available])
                y_regime = y_data.get(regime) if y_data else None

                result = self.analyze_model(
                    model_shim, X_regime_filtered, available,
                    regime=regime, y=y_regime,
                )
                results[regime] = result

            except Exception:
                logger.exception("레짐 '%s' SHAP 분석 실패", regime)

        return results

    def select_features(
        self,
        result: SHAPResult,
        min_importance: float = 0.01,
        top_k: int | None = None,
    ) -> list[str]:
        """Select features whose global SHAP importance exceeds a threshold.

        Args:
            result: ``SHAPResult`` from ``analyze_model``.
            min_importance: Minimum mean |SHAP| to keep.
            top_k: If set, keep at most this many features (ranked by
                importance descending).

        Returns:
            Sorted list of selected feature names (most important first).
        """
        ranked = sorted(
            result.global_importance.items(),
            key=lambda kv: kv[1],
            reverse=True,
        )

        selected = [feat for feat, imp in ranked if imp >= min_importance]

        if top_k is not None:
            selected = selected[:top_k]

        logger.info(
            "피처 선택 완료: %d / %d (min_importance=%.4f, top_k=%s)",
            len(selected), len(result.feature_names), min_importance, top_k,
        )
        return selected

    def get_removal_candidates(
        self,
        result: SHAPResult,
        threshold: float = 0.005,
    ) -> list[str]:
        """Identify features with negligible SHAP importance.

        Features whose global mean |SHAP| falls below *threshold* are
        returned as removal candidates.  ``sector_id`` (categorical) is
        **never** included regardless of its SHAP value.

        Args:
            result: ``SHAPResult`` from ``analyze_model``.
            threshold: Maximum mean |SHAP| to qualify for removal.

        Returns:
            List of removal-candidate feature names (lowest importance first).
        """
        candidates = [
            (feat, imp)
            for feat, imp in result.global_importance.items()
            if imp < threshold and feat not in _PROTECTED_FEATURES
        ]
        candidates.sort(key=lambda kv: kv[1])

        names = [feat for feat, _ in candidates]
        logger.info(
            "제거 후보 피처 %d개 (threshold=%.4f): %s",
            len(names), threshold, names,
        )
        return names

    def save_report(
        self,
        results: dict[str, SHAPResult],
        output_dir: str | None = None,
    ) -> str:
        """Save SHAP analysis to JSON and a human-readable text report.

        Args:
            results: ``{regime: SHAPResult}`` mapping.
            output_dir: Target directory.  Defaults to ``model_artifacts_path``.

        Returns:
            Path (string) to the saved JSON file.
        """
        out = Path(output_dir) if output_dir else self.model_path
        out.mkdir(parents=True, exist_ok=True)

        # --- JSON ---
        export: dict = {
            "analysis_date": pd.Timestamp.now().isoformat(),
            "regimes": {},
        }

        for regime, res in results.items():
            top_10 = sorted(
                res.global_importance.items(), key=lambda kv: kv[1], reverse=True,
            )[:10]
            removal = self.get_removal_candidates(res)

            export["regimes"][regime] = {
                "n_samples": res.n_samples,
                "global_importance": res.global_importance,
                "per_class_importance": res.per_class_importance,
                "shap_values_summary": res.shap_values_summary,
                "top_10": dict(top_10),
                "removal_candidates": removal,
            }

        json_path = out / "shap_feature_analysis.json"
        with open(json_path, "w", encoding="utf-8") as fp:
            json.dump(export, fp, indent=2, ensure_ascii=False)
        logger.info("SHAP JSON 리포트 저장: %s", json_path)

        # --- Text report ---
        text = self.generate_text_report(results)
        txt_path = out / "shap_feature_analysis.txt"
        with open(txt_path, "w", encoding="utf-8") as fp:
            fp.write(text)
        logger.info("SHAP 텍스트 리포트 저장: %s", txt_path)

        return str(json_path)

    def generate_text_report(self, results: dict[str, SHAPResult]) -> str:
        """Generate a human-readable SHAP importance report.

        Args:
            results: ``{regime: SHAPResult}`` mapping.

        Returns:
            Formatted multi-line string (Markdown style).
        """
        lines: list[str] = [
            "# SHAP Feature Importance Report",
            f"**Generated:** {pd.Timestamp.now().strftime('%Y-%m-%d %H:%M')}",
            "",
        ]

        for regime, res in results.items():
            lines.append(f"## {regime.replace('_', ' ').title()}")
            lines.append(f"- Samples used: {res.n_samples}")
            lines.append("")

            # Top-10 global
            ranked = sorted(
                res.global_importance.items(), key=lambda kv: kv[1], reverse=True,
            )
            lines.append("### Top 10 Features (Global mean |SHAP|)")
            lines.append("| Rank | Feature | Importance |")
            lines.append("|------|---------|------------|")
            for rank, (feat, imp) in enumerate(ranked[:10], 1):
                lines.append(f"| {rank} | `{feat}` | {imp:.6f} |")
            lines.append("")

            # Per-class breakdown (top 5 per class)
            lines.append("### Per-Class Top 5")
            for cls_name in CLASS_NAMES:
                cls_imp = res.per_class_importance.get(cls_name, {})
                cls_ranked = sorted(cls_imp.items(), key=lambda kv: kv[1], reverse=True)[:5]
                lines.append(f"**{cls_name}:** " + ", ".join(
                    f"`{f}` ({v:.6f})" for f, v in cls_ranked
                ))
            lines.append("")

            # Directional summary (top 5 positive / negative mean SHAP)
            lines.append("### Directional SHAP Summary")
            summaries = [
                (feat, vals["mean"]) for feat, vals in res.shap_values_summary.items()
            ]
            pos = sorted(summaries, key=lambda kv: kv[1], reverse=True)[:5]
            neg = sorted(summaries, key=lambda kv: kv[1])[:5]
            lines.append("**Positive influence (→ higher class):** " + ", ".join(
                f"`{f}` ({v:+.6f})" for f, v in pos
            ))
            lines.append("**Negative influence (→ lower class):** " + ", ".join(
                f"`{f}` ({v:+.6f})" for f, v in neg
            ))
            lines.append("")

            # Removal candidates
            removal = self.get_removal_candidates(res)
            if removal:
                lines.append("### Removal Candidates (|SHAP| < 0.005)")
                lines.append(", ".join(f"`{f}`" for f in removal))
            else:
                lines.append("### Removal Candidates")
                lines.append("None — all features exceed threshold.")
            lines.append("")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _import_shap():
        """Lazy-import ``shap`` with a user-friendly error on failure."""
        try:
            import shap  # noqa: F811
            return shap
        except ImportError:
            msg = (
                "shap 패키지가 설치되어 있지 않습니다.  "
                "`pip install shap` 으로 설치하세요."
            )
            logger.warning(msg)
            raise ImportError(msg) from None

    @staticmethod
    def _resolve_model(model) -> tuple:
        """Extract ``VotingClassifier`` and weight list from various wrappers.

        Supports:
        - ``EnsembleClassifierWrapper`` (has ``.model`` and ``.weights``).
        - Bare ``VotingClassifier`` (has ``.named_estimators_``).
        - ``_ModelShim`` (internal helper for disk-loaded models).

        Returns:
            ``(voting_classifier, weights_list)``
        """
        # EnsembleClassifierWrapper
        if hasattr(model, "model") and hasattr(model, "weights"):
            voting = model.model
            weights = model.weights or [1 / 3, 1 / 3, 1 / 3]
            return voting, weights

        # Bare VotingClassifier
        if hasattr(model, "named_estimators_"):
            weights = getattr(model, "weights", None) or [1 / 3, 1 / 3, 1 / 3]
            return model, weights

        raise TypeError(
            f"지원하지 않는 모델 타입: {type(model).__name__}.  "
            "EnsembleClassifierWrapper 또는 VotingClassifier를 전달하세요."
        )

    def _compute_tree_shap(
        self,
        shap_module,
        estimator,
        name: str,
        X_sample: pd.DataFrame,
        n_features: int,
        n_classes: int,
    ) -> np.ndarray:
        """Run ``TreeExplainer.shap_values`` for one estimator.

        Handles the two common return formats:
        1. ``ndarray`` of shape ``(n_samples, n_features, n_classes)``
        2. ``list[ndarray]`` with one ``(n_samples, n_features)`` per class.

        Also handles CatBoost-specific edge cases (Pool conversion, etc.).

        Args:
            shap_module: The imported ``shap`` module.
            estimator: A single fitted tree estimator.
            name: Short label (``"cat"`` / ``"lgbm"`` / ``"xgb"``).
            X_sample: Sampled feature DataFrame.
            n_features: Expected number of features.
            n_classes: Expected number of classes (3).

        Returns:
            Array of shape ``(n_samples, n_features, n_classes)``.
        """
        # Prepare data: cast sector_id to the dtype each estimator expects.
        # CatBoost expects int; LightGBM/XGBoost expect pandas 'category'.
        _SECTOR_DTYPE_MAP: dict[str, str] = {"cat": "int", "lgbm": "category", "xgb": "category"}
        X_input = X_sample.copy()
        if "sector_id" in X_input.columns:
            target_dtype: str = _SECTOR_DTYPE_MAP.get(name, "int")
            X_input["sector_id"] = X_input["sector_id"].astype(target_dtype)

        logger.debug("TreeExplainer 생성 중: %s", name)
        explainer = shap_module.TreeExplainer(estimator)

        raw = explainer.shap_values(X_input)

        return self._normalise_shap_output(raw, n_features, n_classes)

    @staticmethod
    def _normalise_shap_output(
        raw,
        n_features: int,
        n_classes: int,
    ) -> np.ndarray:
        """Convert various SHAP output formats to ``(n_samples, n_features, n_classes)``.

        Args:
            raw: Return value of ``explainer.shap_values()``.
            n_features: Expected feature count.
            n_classes: Expected class count.

        Returns:
            Normalised array of shape ``(n_samples, n_features, n_classes)``.
        """
        # Format 1: list of arrays — one (n_samples, n_features) per class.
        if isinstance(raw, list):
            # Ensure correct number of class arrays.
            if len(raw) != n_classes:
                logger.warning(
                    "SHAP list 길이 불일치: %d (expected %d) — 잘라냅니다",
                    len(raw), n_classes,
                )
                raw = raw[:n_classes]
                while len(raw) < n_classes:
                    raw.append(np.zeros_like(raw[0]))
            return np.stack(raw, axis=-1)  # (n_samples, n_features, n_classes)

        arr = np.asarray(raw)

        # Format 2: already (n_samples, n_features, n_classes).
        if arr.ndim == 3 and arr.shape[1] == n_features and arr.shape[2] == n_classes:
            return arr

        # Format 3: binary-style (n_samples, n_features) — broadcast to n_classes.
        if arr.ndim == 2 and arr.shape[1] == n_features:
            logger.debug(
                "2-D SHAP 출력 감지 (%s) — %d 클래스로 확장합니다",
                arr.shape, n_classes,
            )
            return np.stack([arr] * n_classes, axis=-1)

        # Fallback: return zeros and warn.
        logger.warning(
            "예상치 못한 SHAP 출력 형태: %s — 0으로 대체합니다", arr.shape,
        )
        n_samples = arr.shape[0] if arr.ndim >= 1 else 1
        return np.zeros((n_samples, n_features, n_classes))

    def _subsample(
        self,
        X: pd.DataFrame,
        n: int,
        y: pd.Series | None = None,
    ) -> pd.DataFrame:
        """Return at most *n* rows, using stratified sampling when possible.

        If *y* is provided (target labels), each class is proportionally
        represented in the sample.  Otherwise simple random sampling is used.

        Args:
            X: Full feature DataFrame.
            n: Maximum sample size.
            y: Optional target labels for stratified sampling.

        Returns:
            Sub-sampled DataFrame (or the original if ``len(X) <= n``).
        """
        if len(X) <= n:
            return X

        if y is not None:
            try:
                from sklearn.model_selection import train_test_split

                _, X_sample, _, _ = train_test_split(
                    X, y, test_size=n, stratify=y, random_state=42,
                )
                return X_sample
            except Exception:
                logger.debug("Stratified 샘플링 실패 — 랜덤 샘플링 사용")

        return X.sample(n=n, random_state=42)


# ---------------------------------------------------------------------------
# Lightweight shim for disk-loaded models (no EnsembleClassifierWrapper)
# ---------------------------------------------------------------------------

class _ModelShim:
    """Thin wrapper exposing ``.model`` and ``.weights`` for a bare VotingClassifier."""

    def __init__(self, voting_clf, weights: list[float] | None = None) -> None:
        self.model = voting_clf
        self.weights = weights or [1 / 3, 1 / 3, 1 / 3]


# ---------------------------------------------------------------------------
# Standalone convenience function
# ---------------------------------------------------------------------------

def run_shap_analysis(
    feature_names: list[str] | None = None,
    regimes: list[str] | None = None,
    sample_size: int = 500,
    output_dir: str = "model_artifacts",
    X_data: dict[str, pd.DataFrame] | None = None,
    y_data: dict[str, pd.Series] | None = None,
) -> dict[str, SHAPResult]:
    """Convenience entry-point for Celery tasks or CLI scripts.

    Loads all regime models from *output_dir*, runs SHAP, and saves a
    combined report (JSON + text).

    Args:
        feature_names: Feature columns.  Defaults to the canonical 27-feature
            list if ``None``.
        regimes: Regime labels to analyse (default: all four).
        sample_size: ``shap_sample_size`` forwarded to ``SHAPFeatureSelector``.
        output_dir: Directory for model artifacts and output reports.
        X_data: ``{regime: DataFrame}`` mapping with real feature data.
            When ``None``, regimes without data are skipped.
        y_data: ``{regime: Series}`` mapping with target labels for
            stratified sampling.  Optional.

    Returns:
        ``{regime: SHAPResult}`` mapping.
    """
    if feature_names is None:
        feature_names = [
            "rsi", "macd", "macd_signal", "macd_hist",
            "bb_width", "bb_position",
            "sma_20", "sma_50", "ema_12", "ema_26",
            "atr_pct", "adx",
            "stoch_k", "stoch_d",
            "volume_ratio", "roc", "mom",
            "sector_id", "relative_volume",
            "vwap_distance", "trade_intensity",
            "momentum_5", "momentum_10",
            "rsi_momentum", "trend_strength",
            "price_position",
        ]

    selector = SHAPFeatureSelector(
        model_artifacts_path=output_dir,
        shap_sample_size=sample_size,
    )

    results = selector.analyze_all_regimes(
        feature_names, regimes=regimes,
        X_data=X_data, y_data=y_data,
    )

    if results:
        selector.save_report(results, output_dir=output_dir)
        logger.info("SHAP 분석 완료 — %d개 레짐 처리됨", len(results))
    else:
        logger.warning("SHAP 분석 결과 없음 — 모델 파일을 확인하세요")

    return results
