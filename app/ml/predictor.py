import logging
import os
import threading

import pandas as pd

from app.ml.models import CLASS_NAMES, EnsembleClassifierWrapper, EnsembleWrapper
from app.services.regime import MarketRegime

logger = logging.getLogger(__name__)

# Resolve model artifacts path: env var > relative default
_MODEL_ARTIFACTS_PATH: str = os.getenv("MODEL_SAVE_PATH", "model_artifacts")


class PredictorService:
    """Regime-aware predictor service using ensemble classifier models.

    Thread-safe singleton that caches one ``EnsembleClassifierWrapper`` per
    ``MarketRegime``.  All mutable access to ``_models`` is protected by an
    ``RLock`` so that concurrent Celery workers / API threads never see a
    partially-loaded model dict.

    Supports ternary classification:
        - 0 = DOWN
        - 1 = NEUTRAL
        - 2 = UP
    """

    _instance: "PredictorService | None" = None
    _lock: threading.RLock = threading.RLock()
    _models: dict[MarketRegime, EnsembleClassifierWrapper | EnsembleWrapper] = {}
    _base_path: str = _MODEL_ARTIFACTS_PATH
    _classifier_map: dict[MarketRegime, str] = {
        MarketRegime.BULL_TRENDING: "ensemble_classifier_bull_trending.pkl",
        MarketRegime.BEAR_TRENDING: "ensemble_classifier_bear_trending.pkl",
        MarketRegime.SIDEWAYS_VOLATILE: "ensemble_classifier_sideways_volatile.pkl",
        MarketRegime.SIDEWAYS_CALM: "ensemble_classifier_sideways_calm.pkl",
    }
    # Legacy regressor model map (fallback)
    _model_map: dict[MarketRegime, str] = {
        MarketRegime.BULL_TRENDING: "ensemble_model_bull_trending.pkl",
        MarketRegime.BEAR_TRENDING: "ensemble_model_bear_trending.pkl",
        MarketRegime.SIDEWAYS_VOLATILE: "ensemble_model_sideways_volatile.pkl",
        MarketRegime.SIDEWAYS_CALM: "ensemble_model_sideways_calm.pkl",
    }

    def __new__(cls) -> "PredictorService":
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(PredictorService, cls).__new__(cls)
                cls._instance._initialize()
        return cls._instance

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_models_from_disk(self) -> dict[MarketRegime, EnsembleClassifierWrapper | EnsembleWrapper]:
        """Load all regime models from disk into a *new* dict.

        Prefers classifier models (``EnsembleClassifierWrapper``).  Falls back
        to legacy regressor models (``EnsembleWrapper``) if classifier not found.

        Returns:
            A dict mapping ``MarketRegime`` to loaded model wrapper.
        """
        new_models: dict[MarketRegime, EnsembleClassifierWrapper | EnsembleWrapper] = {}

        for regime in MarketRegime:
            # Try classifier first
            clf_filename = self._classifier_map[regime]
            clf_path = os.path.join(self._base_path, clf_filename)

            if os.path.exists(clf_path):
                try:
                    model = EnsembleClassifierWrapper()
                    model.load(clf_path)
                    new_models[regime] = model
                    logger.info("✓ Loaded classifier %s from %s", regime.value, clf_filename)
                    continue
                except Exception as e:
                    logger.error("Failed to load classifier %s: %s", regime.value, e)

            # Fallback to legacy regressor
            reg_filename = self._model_map[regime]
            reg_path = os.path.join(self._base_path, reg_filename)
            try:
                if os.path.exists(reg_path):
                    model = EnsembleWrapper()
                    model.load(reg_path)
                    new_models[regime] = model
                    logger.warning("⚠ Using legacy regressor for %s (no classifier found)", regime.value)
                else:
                    logger.warning("✗ No model found for %s", regime.value)
            except Exception as e:
                logger.error("Failed to load %s model: %s", regime.value, e)

        # Fallback: generic model for every regime
        if not new_models:
            for name in ("ensemble_classifier.pkl", "ensemble_model.pkl"):
                generic_path = os.path.join(self._base_path, name)
                if os.path.exists(generic_path):
                    if "classifier" in name:
                        model = EnsembleClassifierWrapper()
                    else:
                        model = EnsembleWrapper()
                    model.load(generic_path)
                    for regime in MarketRegime:
                        new_models[regime] = model
                    logger.warning("⚠ Using generic %s for all regimes", name)
                    break

            if not new_models:
                logger.error("No models found (classifier or regressor). Train models first.")

        return new_models

    def _initialize(self) -> None:
        """Initialize all regime-specific models (called once on first instantiation)."""
        loaded = self._load_models_from_disk()
        with self._lock:
            self._models = loaded

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_model(
        self, regime: MarketRegime = MarketRegime.SIDEWAYS_CALM
    ) -> EnsembleClassifierWrapper | EnsembleWrapper | None:
        """Get the model for a specific market regime.

        Args:
            regime: The market regime to retrieve the model for.

        Returns:
            The model wrapper for *regime*, or ``None`` if not loaded.
        """
        with self._lock:
            return self._models.get(regime)

    def reload_models(self) -> None:
        """Atomically reload all regime models from disk.

        Models are first loaded into a temporary dict **outside** the lock so
        that concurrent ``predict_next`` calls continue to use the previous
        (valid) model set.  The swap is performed under the lock.
        """
        logger.info("Reloading regime models from disk...")
        new_models = self._load_models_from_disk()
        with self._lock:
            self._models = new_models
        logger.info("Reloaded %d models", len(new_models))

    def predict_class(
        self,
        features: pd.DataFrame,
        regime: MarketRegime = MarketRegime.SIDEWAYS_CALM,
    ) -> tuple[int, float, dict[str, float]]:
        """Predict class using the regime-aware ensemble classifier.

        This is the primary prediction method for the ternary classification
        system (0=DOWN, 1=NEUTRAL, 2=UP).

        Args:
            features: Feature DataFrame (single row or multiple) with column
                names matching the trained model's feature set.
            regime: Current market regime.

        Returns:
            Tuple of (predicted_class, confidence, probabilities).
            - predicted_class: 0=DOWN, 1=NEUTRAL, 2=UP
            - confidence: probability of the predicted class (0.0 to 1.0)
            - probabilities: dict mapping class names to probabilities
            Returns (1, 0.33, {"DOWN": 0.33, "NEUTRAL": 0.34, "UP": 0.33})
            on error (neutral prediction).
        """
        neutral = (1, 0.33, {"DOWN": 0.33, "NEUTRAL": 0.34, "UP": 0.33})

        with self._lock:
            model = self._models.get(regime)

        if model is None:
            logger.warning("No model for regime %s, returning neutral", regime.value)
            return neutral

        try:
            # Check if model supports classification
            if not isinstance(model, EnsembleClassifierWrapper):
                # Legacy regressor: convert to class via threshold
                raw_pred = self._predict_regression(model, features)
                if raw_pred > 0.001:
                    return (2, 0.6, {"DOWN": 0.15, "NEUTRAL": 0.25, "UP": 0.6})
                elif raw_pred < -0.001:
                    return (0, 0.6, {"DOWN": 0.6, "NEUTRAL": 0.25, "UP": 0.15})
                return neutral

            # Ensure DataFrame format
            if not isinstance(features, pd.DataFrame):
                logger.warning("Features not DataFrame, converting...")
                return neutral

            probas = model.predict_proba(features)
            predicted_class = int(probas.argmax(axis=1)[0])
            confidence = float(probas[0, predicted_class])

            class_names = CLASS_NAMES  # ["DOWN", "NEUTRAL", "UP"]
            probabilities = {
                name: float(probas[0, i]) for i, name in enumerate(class_names)
            }

            logger.debug(
                "Classification: class=%s, confidence=%.2f, probs=%s (regime=%s)",
                class_names[predicted_class],
                confidence,
                {k: f"{v:.3f}" for k, v in probabilities.items()},
                regime.value,
            )

            return predicted_class, confidence, probabilities

        except Exception as e:
            logger.error(
                "Classification error for regime %s: %s", regime.value, e, exc_info=True
            )
            return neutral

    def _predict_regression(self, model: EnsembleWrapper, features: pd.DataFrame) -> float:
        """Helper to get raw float prediction from legacy regressor model."""
        try:
            prediction = model.predict(features)
            if isinstance(prediction, pd.Series):
                return float(prediction.iloc[0])
            return float(prediction[0])
        except Exception:
            return 0.0

    def predict_next(
        self,
        features: pd.DataFrame,
        regime: MarketRegime = MarketRegime.SIDEWAYS_CALM,
    ) -> float:
        """Predict next value using the regime-aware ensemble model.

        Args:
            features: Feature DataFrame (single row or multiple) with column
                names matching the trained model's feature set.
            regime: Current market regime.

        Returns:
            Prediction value between 0.0 and 1.0.  Returns 0.5 (neutral) on
            error or when no model is available.
        """
        # Grab a reference under lock — subsequent work is lock-free.
        with self._lock:
            model = self._models.get(regime)

        if model is None:
            logger.warning("No model for regime %s, returning neutral", regime.value)
            return 0.5

        try:
            # Ensure DataFrame format with column names (XGBoost requirement)
            if not isinstance(features, pd.DataFrame):
                logger.warning("Features not DataFrame, converting...")
                feature_names = (
                    model.model.feature_names_in_
                    if hasattr(model.model, "feature_names_in_")
                    else None
                )
                if feature_names is not None:
                    features = pd.DataFrame(features, columns=feature_names)
                else:
                    logger.error(
                        "Cannot convert to DataFrame: no feature names available"
                    )
                    return 0.5

            logger.debug(
                "Features type: %s, columns: %s, shape: %s",
                type(features),
                list(features.columns) if isinstance(features, pd.DataFrame) else "N/A",
                features.shape,
            )

            prediction = model.predict(features)
            if isinstance(prediction, pd.Series):
                return float(prediction.iloc[0])
            return float(prediction[0])
        except Exception as e:
            logger.error(
                "Prediction error for regime %s: %s", regime.value, e, exc_info=True
            )
            return 0.5

    def retrain(
        self,
        X: pd.DataFrame,
        y: pd.Series,
        regime: MarketRegime = MarketRegime.SIDEWAYS_CALM,
        weights: list | None = None,
        model_params: dict | None = None,
    ) -> bool:
        """Retrain a regime-specific classifier with optional weights and hyperparameters.

        Creates an ``EnsembleClassifierWrapper`` (ternary: DOWN/NEUTRAL/UP),
        trains it, saves to disk, and hot-swaps the in-memory model.

        Args:
            X: Feature DataFrame (already scaled).
            y: Target Series with ternary labels (0=DOWN, 1=NEUTRAL, 2=UP).
            regime: Target regime for this model.
            weights: Optional list ``[cat, lgbm, xgb]`` weights.
            model_params: Optional dict ``{'catboost': …, 'lgbm': …, 'xgboost': …}``.

        Returns:
            ``True`` on success, ``False`` on failure.
        """
        try:
            model = EnsembleClassifierWrapper(
                weights=weights, model_params=model_params
            )
            model.train(X, y)

            filename = self._classifier_map.get(
                regime, f"ensemble_classifier_{regime.value}.pkl"
            )
            model_path = os.path.join(self._base_path, filename)
            os.makedirs(os.path.dirname(model_path) or ".", mode=0o777, exist_ok=True)
            model.save(model_path)

            with self._lock:
                self._models[regime] = model
            logger.info("Classifier retrained for regime %s", regime.value)
            return True
        except Exception as e:
            logger.error(
                "Retraining failed for %s: %s", regime.value, e, exc_info=True
            )
            return False

    def get_model_info(self) -> dict[str, dict]:
        """Get metadata for every loaded regime model.

        Returns:
            A dict keyed by regime value (``str``) whose values are the
            ``EnsembleWrapper.metadata`` dicts (weights, training date, etc.).
            Returns an empty dict when no models are loaded.
        """
        with self._lock:
            models_snapshot = dict(self._models)

        info: dict[str, dict] = {}
        for regime, model in models_snapshot.items():
            meta = getattr(model, "metadata", {})
            info[regime.value] = {
                "loaded": True,
                "metadata": meta if isinstance(meta, dict) else {},
            }
        return info
