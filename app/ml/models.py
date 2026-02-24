import logging
import os
from abc import ABC, abstractmethod
from typing import Any

import joblib
import numpy as np
import pandas as pd
from catboost import CatBoostClassifier, CatBoostRegressor
from lightgbm import LGBMClassifier, LGBMRegressor
from sklearn.base import BaseEstimator, ClassifierMixin, RegressorMixin
from sklearn.ensemble import VotingClassifier, VotingRegressor
from xgboost import XGBClassifier, XGBRegressor, DMatrix

logger = logging.getLogger(__name__)

class CompatibleCatBoostRegressor(CatBoostRegressor, BaseEstimator, RegressorMixin):
    """
    Wrapper for CatBoostRegressor to be compatible with scikit-learn 1.6+ Tags API.
    """
    def __sklearn_tags__(self):
        tags = super().__sklearn_tags__()
        tags.estimator_type = "regressor"
        tags.classifier_tags = None
        tags.regressor_tags = {}
        tags.transformer_tags = None
        return tags

class ModelWrapper(ABC):
    @abstractmethod
    def train(self, X: pd.DataFrame, y: pd.Series, params: dict[str, Any] = None):
        pass

    @abstractmethod
    def predict(self, X: pd.DataFrame) -> np.ndarray:
        pass

    @abstractmethod
    def save(self, path: str):
        pass

    @abstractmethod
    def load(self, path: str):
        pass

class CatBoostWrapper(ModelWrapper):
    def __init__(self, **custom_params):
        self.model = None
        self.custom_params = custom_params

    def train(self, X: pd.DataFrame, y: pd.Series, params: dict[str, Any] = None):
        """Train with data-adaptive complexity adjustment."""
        n_samples = len(X)

        # Use custom params (from Optuna) or auto-adjust
        if params is not None:
            final_params = params
        elif self.custom_params:
            final_params = self.custom_params
        else:
            # Auto-adjust based on data size
            if n_samples < 1000:
                final_params = {
                    'iterations': 100,
                    'depth': 3,
                    'learning_rate': 0.1,
                    'l2_leaf_reg': 5,
                    'verbose': False,
                    'allow_writing_files': False
                }
            elif n_samples < 3000:
                final_params = {
                    'iterations': 200,
                    'depth': 6,
                    'learning_rate': 0.05,
                    'l2_leaf_reg': 3,
                    'verbose': False,
                    'allow_writing_files': False
                }
            else:
                final_params = {
                    'iterations': 300,
                    'depth': 8,
                    'learning_rate': 0.03,
                    'l2_leaf_reg': 2,
                    'verbose': False,
                    'allow_writing_files': False
                }

        # Use CompatibleCatBoostRegressor for future proofing, though strictly needed for Ensemble
        self.model = CompatibleCatBoostRegressor(**final_params)
        self.model.fit(X, y)

    def predict(self, X: pd.DataFrame) -> np.ndarray:
        return self.model.predict(X)

    def save(self, path: str):
        self.model.save_model(path)

    def load(self, path: str):
        self.model = CompatibleCatBoostRegressor()
        self.model.load_model(path)

class LGBMWrapper(ModelWrapper):
    def __init__(self, **params):
        self.model = None
        self.params = params

    def train(self, X: pd.DataFrame, y: pd.Series, params: dict[str, Any] = None):
        """Train with data-adaptive complexity adjustment."""
        n_samples = len(X)

        # Use init params, allow override
        final_params = self.params.copy()
        if params:
            final_params.update(params)

        # Auto-adjust based on data size if no params provided
        if not final_params:
            if n_samples < 1000:
                final_params = {
                    'n_estimators': 50,
                    'max_depth': 3,
                    'num_leaves': 7,  # 2^3 - 1
                    'min_data_in_leaf': 10,
                    'learning_rate': 0.1,
                    'verbose': -1
                }
            elif n_samples < 3000:
                final_params = {
                    'n_estimators': 100,
                    'max_depth': 5,
                    'num_leaves': 31,
                    'min_data_in_leaf': 20,
                    'learning_rate': 0.05,
                    'verbose': -1
                }
            else:
                final_params = {
                    'n_estimators': 150,
                    'max_depth': 6,
                    'num_leaves': 63,
                    'min_data_in_leaf': 30,
                    'learning_rate': 0.03,
                    'verbose': -1
                }
        else:
            # Ensure verbose is set
            if 'verbose' not in final_params:
                final_params['verbose'] = -1

            # Data-adaptive safety checks for tuned params
            if n_samples < 1000:
                # For small datasets, relax constraints
                final_params['min_data_in_leaf'] = min(final_params.get('min_data_in_leaf', 20), 10)
                final_params['num_leaves'] = min(final_params.get('num_leaves', 31), 15)
                final_params['max_depth'] = min(final_params.get('max_depth', 6), 4)

        self.model = LGBMRegressor(**final_params)
        self.model.fit(X, y)

    def predict(self, X: pd.DataFrame) -> np.ndarray:
        return self.model.predict(X)

    def save(self, path: str):
        joblib.dump(self.model, path)

    def load(self, path: str):
        self.model = joblib.load(path)

class XGBoostWrapper(ModelWrapper):
    def __init__(self, **params):
        self.model = None
        self.params = params

    def train(self, X: pd.DataFrame, y: pd.Series, params: dict[str, Any] = None):
        final_params = self.params.copy()
        if params:
            final_params.update(params)

        if not final_params:
            final_params = {'n_estimators': 100, 'max_depth': 6, 'learning_rate': 0.1}

        self.model = XGBRegressor(**final_params)
        self.model.fit(X, y)

    def predict(self, X: pd.DataFrame) -> np.ndarray:
        return self.model.predict(X)

    def save(self, path: str):
        self.model.save_model(path)

    def load(self, path: str):
        self.model = XGBRegressor()
        self.model.load_model(path)

class EnsembleWrapper(ModelWrapper):
    def __init__(self, weights: list[float] = None, model_params: dict[str, dict] = None):
        self.model = None
        self.weights = weights  # [cat, lgbm, xgb]
        self.model_params = model_params or {} # {'catboost': ..., 'lgbm': ..., 'xgboost': ...}
        self.metadata = {}

    def train(self, X: pd.DataFrame, y: pd.Series, params: dict[str, Any] = None):
        # Extract params for each model
        cat_p = self.model_params.get('catboost', {})
        lgbm_p = self.model_params.get('lgbm', {})
        xgb_p = self.model_params.get('xgboost', {})

        # Ensure verbosity control for all models
        # Note: CatBoost only allows ONE of: verbose, logging_level, verbose_eval, silent
        cat_p.setdefault('verbose', False)
        cat_p.setdefault('allow_writing_files', False)
        lgbm_p.setdefault('verbose', -1)
        xgb_p.setdefault('verbosity', 0)

        estimators = [
            ('cat', CompatibleCatBoostRegressor(**cat_p)),
            ('lgbm', LGBMRegressor(**lgbm_p)),
            ('xgb', XGBRegressor(**xgb_p))
        ]

        # Use weights if provided, otherwise equal weighting
        if self.weights:
            self.model = VotingRegressor(estimators, weights=self.weights)
            logger.info(f"Training weighted ensemble with weights: {self.weights}")
        else:
            self.model = VotingRegressor(estimators)
            logger.info("Training equal-weighted ensemble")

        self.model.fit(X, y)

    def predict(self, X: pd.DataFrame) -> np.ndarray:
        """
        Predict using ensemble with DataFrame support.
        
        VotingRegressor internally converts DataFrame to numpy array,
        which loses feature names required by XGBoost. 
        We manually call each estimator to preserve DataFrame format.
        """
        if self.model is None:
            raise ValueError("Model not trained or loaded")
        
        # Get XGBoost estimator's expected feature names
        xgb_estimator = self.model.named_estimators_['xgb']

        # Check if model has feature_names_in_ attribute (trained with DataFrame)
        if not hasattr(xgb_estimator, 'feature_names_in_'):
            logger.debug(
                "XGBoost model does not have feature_names_in_. "
                "Model may have been trained with numpy array or metadata not loaded."
            )
            # If no feature names stored, use input columns as-is
            expected_features = [str(c) for c in X.columns] if isinstance(X, pd.DataFrame) else None
        else:
            # CRITICAL: Convert numpy strings to Python str to avoid np.str_ column names
            expected_features = [str(f) for f in xgb_estimator.feature_names_in_]
            logger.info(f"XGBoost expects features: {expected_features}")

        # Ensure X is DataFrame with correct column names and order
        if not isinstance(X, pd.DataFrame):
            logger.warning("Input is not DataFrame, converting with expected feature names")
            if expected_features:
                X = pd.DataFrame(X, columns=expected_features)
            else:
                raise ValueError("Cannot convert numpy to DataFrame without feature names")
        else:
            # Make a copy to avoid modifying original
            X = X.copy()
            input_cols = list(X.columns)

            if expected_features and input_cols != expected_features:
                # Check for missing columns
                missing = set(expected_features) - set(input_cols)
                extra = set(input_cols) - set(expected_features)

                if missing:
                    logger.error(f"Missing features in input: {missing}")
                    raise ValueError(f"Missing features: {missing}")
                if extra:
                    logger.warning(f"Extra features in input (will be ignored): {extra}")

                # Reorder columns to match training order
                logger.info(f"Reordering columns from {input_cols} to {expected_features}")
                X = X.reindex(columns=expected_features)

        logger.debug(
            f"Predict input: type={type(X)}, columns={list(X.columns)}, shape={X.shape}"
        )

        # Get predictions from each estimator directly (preserving DataFrame)
        predictions = []
        feature_names = list(X.columns)
        
        for name, estimator in self.model.named_estimators_.items():
            if name == 'xgb':
                # XGBoost requires explicit DMatrix with feature_names
                # sklearn wrapper loses feature names during DataFrame->DMatrix conversion
                dmatrix = DMatrix(X.values, feature_names=feature_names)
                pred = estimator.get_booster().predict(dmatrix)
            else:
                # CatBoost and LightGBM handle DataFrame directly
                pred = estimator.predict(X)

            predictions.append(pred)

        # Apply weights (or equal weighting)
        predictions = np.array(predictions)
        weights = np.array(self.weights) if self.weights else np.ones(len(predictions)) / len(predictions)

        # Weighted average
        return np.average(predictions, axis=0, weights=weights)

    def save(self, path: str):
        # Save both model and metadata
        joblib.dump(self.model, path)

        # Save metadata (weights, training date, feature_names)
        metadata_path = path.replace('.pkl', '_metadata.json')
        import json
        from datetime import datetime

        # Extract feature names from XGBoost estimator
        feature_names = None
        if self.model and hasattr(self.model, 'named_estimators_'):
            xgb_est = self.model.named_estimators_.get('xgb')
            if xgb_est and hasattr(xgb_est, 'feature_names_in_'):
                feature_names = [str(f) for f in xgb_est.feature_names_in_]

        self.metadata = {
            'weights': self.weights if self.weights else [1/3, 1/3, 1/3],
            'training_date': datetime.now().isoformat(),
            'model_names': ['catboost', 'lgbm', 'xgboost'],
            'feature_names': feature_names
        }

        with open(metadata_path, 'w') as f:
            json.dump(self.metadata, f, indent=2)
        logger.info(f"Metadata saved to {metadata_path}")

    def load(self, path: str):
        self.model = joblib.load(path)

        # Load metadata if exists
        metadata_path = path.replace('.pkl', '_metadata.json')
        if os.path.exists(metadata_path):
            import json
            with open(metadata_path) as f:
                self.metadata = json.load(f)
                self.weights = self.metadata.get('weights')

                # Restore feature_names_in_ to XGBoost estimator
                feature_names = self.metadata.get('feature_names')
                if feature_names and hasattr(self.model, 'named_estimators_'):
                    xgb_est = self.model.named_estimators_.get('xgb')
                    if xgb_est:
                        xgb_est.feature_names_in_ = np.array(feature_names)
                        logger.debug(f"Restored feature_names_in_ to XGBoost: {feature_names[:3]}...")


# ============================================================
# Classification Models (Ternary: UP / NEUTRAL / DOWN)
# ============================================================

DEFAULT_CLASS_WEIGHTS: dict[int, float] = {0: 1.3, 1: 1.0, 2: 1.3}
"""Default class weights: emphasize UP(2)/DOWN(0), fair NEUTRAL(1) representation."""

CLASS_NAMES: list[str] = ["DOWN", "NEUTRAL", "UP"]
"""Label mapping — 0=DOWN, 1=NEUTRAL, 2=UP."""


class CompatibleCatBoostClassifier(CatBoostClassifier, BaseEstimator, ClassifierMixin):
    """Wrapper for CatBoostClassifier compatible with scikit-learn 1.6+ Tags API."""

    def __sklearn_tags__(self):
        tags = super().__sklearn_tags__()
        tags.estimator_type = "classifier"
        tags.classifier_tags = {}
        tags.regressor_tags = None
        tags.transformer_tags = None
        return tags


# Column name used for categorical sector feature across all classifiers
SECTOR_FEATURE_NAME: str = "sector_id"
"""The feature column treated as native categorical by CatBoost/LightGBM."""


def _detect_cat_feature_indices(X: pd.DataFrame) -> list[int]:
    """Detect indices of categorical features (sector_id) in feature matrix.

    Args:
        X: Feature DataFrame.

    Returns:
        List of column indices for categorical features.
    """
    cols = list(X.columns)
    indices = [i for i, c in enumerate(cols) if c == SECTOR_FEATURE_NAME]
    return indices


def _prepare_categorical_for_predict(X: pd.DataFrame, model_type: str) -> pd.DataFrame:
    """Prepare categorical features for prediction to match training dtype.

    Each ML framework requires a specific dtype for categorical features:

    - **CatBoost**: ``int`` (Ordered Target Statistics encoding).
    - **LightGBM**: ``category`` pandas dtype.
    - **XGBoost**: ``category`` pandas dtype (``enable_categorical=True``).

    Args:
        X: Input feature DataFrame.
        model_type: One of ``'catboost'``, ``'lgbm'``, ``'xgb'``.

    Returns:
        DataFrame with correct categorical dtypes.
    """
    if not isinstance(X, pd.DataFrame) or SECTOR_FEATURE_NAME not in X.columns:
        return X
    X = X.copy()
    if model_type == "catboost":
        X[SECTOR_FEATURE_NAME] = X[SECTOR_FEATURE_NAME].astype(int)
    else:  # lgbm, xgb
        X[SECTOR_FEATURE_NAME] = X[SECTOR_FEATURE_NAME].astype("category")
    return X


class CatBoostClassifierWrapper(ModelWrapper):
    """CatBoost ternary classifier with data-adaptive complexity.

    Automatically detects ``sector_id`` column and treats it as a native
    categorical feature using CatBoost's Ordered Target Statistics encoding.

    Attributes:
        model: Trained CatBoostClassifier instance.
        custom_params: User-supplied hyperparameters.
    """

    def __init__(self, **custom_params: Any) -> None:
        self.model: CompatibleCatBoostClassifier | None = None
        self.custom_params = custom_params

    def train(
        self,
        X: pd.DataFrame,
        y: pd.Series,
        params: dict[str, Any] | None = None,
    ) -> None:
        """Train CatBoost classifier with data-adaptive complexity.

        Detects ``sector_id`` in *X* and passes it as ``cat_features`` to
        CatBoost so that it uses **Ordered Target Statistics** encoding
        instead of treating the integer as an ordinal number.

        Args:
            X: Feature matrix.
            y: Target labels (0=DOWN, 1=NEUTRAL, 2=UP).
            params: Optional hyperparameter overrides.
        """
        n_samples = len(X)

        if params is not None:
            final_params = params.copy()
        elif self.custom_params:
            final_params = self.custom_params.copy()
        else:
            if n_samples < 1000:
                final_params = {
                    "iterations": 100,
                    "depth": 3,
                    "learning_rate": 0.1,
                    "l2_leaf_reg": 5,
                }
            elif n_samples < 3000:
                final_params = {
                    "iterations": 200,
                    "depth": 6,
                    "learning_rate": 0.05,
                    "l2_leaf_reg": 3,
                }
            else:
                final_params = {
                    "iterations": 300,
                    "depth": 8,
                    "learning_rate": 0.03,
                    "l2_leaf_reg": 2,
                }

        # CatBoost uses class_weights as a list ordered by class index
        if "class_weights" not in final_params:
            final_params["class_weights"] = [
                DEFAULT_CLASS_WEIGHTS[i] for i in range(len(CLASS_NAMES))
            ]

        final_params.setdefault("verbose", False)
        final_params.setdefault("allow_writing_files", False)
        final_params.setdefault("loss_function", "MultiClass")

        self.model = CompatibleCatBoostClassifier(**final_params)

        # Detect sector_id for native categorical encoding
        cat_indices = _detect_cat_feature_indices(X)
        if cat_indices:
            # CatBoost requires int columns for cat_features to be cast properly
            X = X.copy()
            X[SECTOR_FEATURE_NAME] = X[SECTOR_FEATURE_NAME].astype(int)
            self.model.fit(X, y, cat_features=cat_indices)
            logger.info(
                "CatBoostClassifier trained on %d samples with cat_features=%s",
                n_samples, cat_indices,
            )
        else:
            self.model.fit(X, y)
            logger.info("CatBoostClassifier trained on %d samples (no cat features)", n_samples)

    def predict(self, X: pd.DataFrame) -> np.ndarray:
        """Return class predictions (0/1/2).

        Args:
            X: Feature matrix.

        Returns:
            Array of predicted class labels.
        """
        if self.model is None:
            raise ValueError("Model not trained or loaded")
        X = _prepare_categorical_for_predict(X, "catboost")
        return self.model.predict(X).astype(int).ravel()

    def predict_proba(self, X: pd.DataFrame) -> np.ndarray:
        """Return class probabilities with shape (n_samples, 3).

        Args:
            X: Feature matrix.

        Returns:
            Probability array of shape (n_samples, 3).
        """
        if self.model is None:
            raise ValueError("Model not trained or loaded")
        X = _prepare_categorical_for_predict(X, "catboost")
        return self.model.predict_proba(X)

    def save(self, path: str) -> None:
        """Save model to disk.

        Args:
            path: File path for saved model.
        """
        if self.model is None:
            raise ValueError("No model to save")
        self.model.save_model(path)
        logger.info("CatBoostClassifier saved to %s", path)

    def load(self, path: str) -> None:
        """Load model from disk.

        Args:
            path: File path of saved model.
        """
        self.model = CompatibleCatBoostClassifier()
        self.model.load_model(path)
        logger.info("CatBoostClassifier loaded from %s", path)


class LGBMClassifierWrapper(ModelWrapper):
    """LightGBM ternary classifier with data-adaptive complexity.

    Attributes:
        model: Trained LGBMClassifier instance.
        params: User-supplied hyperparameters.
    """

    def __init__(self, **params: Any) -> None:
        self.model: LGBMClassifier | None = None
        self.params = params

    def train(
        self,
        X: pd.DataFrame,
        y: pd.Series,
        params: dict[str, Any] | None = None,
    ) -> None:
        """Train LightGBM classifier with data-adaptive complexity.

        Args:
            X: Feature matrix.
            y: Target labels (0=DOWN, 1=NEUTRAL, 2=UP).
            params: Optional hyperparameter overrides.
        """
        n_samples = len(X)

        final_params = self.params.copy()
        if params:
            final_params.update(params)

        if not final_params:
            if n_samples < 1000:
                final_params = {
                    "n_estimators": 50,
                    "max_depth": 3,
                    "num_leaves": 7,
                    "min_data_in_leaf": 10,
                    "learning_rate": 0.1,
                    "verbose": -1,
                }
            elif n_samples < 3000:
                final_params = {
                    "n_estimators": 100,
                    "max_depth": 5,
                    "num_leaves": 31,
                    "min_data_in_leaf": 20,
                    "learning_rate": 0.05,
                    "verbose": -1,
                }
            else:
                final_params = {
                    "n_estimators": 150,
                    "max_depth": 6,
                    "num_leaves": 63,
                    "min_data_in_leaf": 30,
                    "learning_rate": 0.03,
                    "verbose": -1,
                }
        else:
            if "verbose" not in final_params:
                final_params["verbose"] = -1

            # Data-adaptive safety checks for tuned params
            if n_samples < 1000:
                final_params["min_data_in_leaf"] = min(
                    final_params.get("min_data_in_leaf", 20), 10
                )
                final_params["num_leaves"] = min(
                    final_params.get("num_leaves", 31), 15
                )
                final_params["max_depth"] = min(
                    final_params.get("max_depth", 6), 4
                )

        # LGBM uses class_weight dict {class_label: weight}
        if "class_weight" not in final_params:
            final_params["class_weight"] = DEFAULT_CLASS_WEIGHTS.copy()

        final_params.setdefault("objective", "multiclass")
        final_params.setdefault("num_class", len(CLASS_NAMES))

        self.model = LGBMClassifier(**final_params)

        # Detect sector_id for native categorical encoding
        cat_feature_names: list[str] | str = "auto"
        if isinstance(X, pd.DataFrame) and SECTOR_FEATURE_NAME in X.columns:
            cat_feature_names = [SECTOR_FEATURE_NAME]
            # LightGBM requires categorical columns to be 'category' dtype
            X = X.copy()
            X[SECTOR_FEATURE_NAME] = X[SECTOR_FEATURE_NAME].astype("category")
            logger.info(
                "LGBMClassifier: sector_id set as native categorical feature",
            )

        self.model.fit(X, y, categorical_feature=cat_feature_names)
        logger.info("LGBMClassifier trained on %d samples", n_samples)

    def predict(self, X: pd.DataFrame) -> np.ndarray:
        """Return class predictions (0/1/2).

        Args:
            X: Feature matrix.

        Returns:
            Array of predicted class labels.
        """
        if self.model is None:
            raise ValueError("Model not trained or loaded")
        X = _prepare_categorical_for_predict(X, "lgbm")
        return self.model.predict(X).astype(int).ravel()

    def predict_proba(self, X: pd.DataFrame) -> np.ndarray:
        """Return class probabilities with shape (n_samples, 3).

        Args:
            X: Feature matrix.

        Returns:
            Probability array of shape (n_samples, 3).
        """
        if self.model is None:
            raise ValueError("Model not trained or loaded")
        X = _prepare_categorical_for_predict(X, "lgbm")
        return self.model.predict_proba(X)

    def save(self, path: str) -> None:
        """Save model to disk via joblib.

        Args:
            path: File path for saved model.
        """
        if self.model is None:
            raise ValueError("No model to save")
        joblib.dump(self.model, path)
        logger.info("LGBMClassifier saved to %s", path)

    def load(self, path: str) -> None:
        """Load model from disk via joblib.

        Args:
            path: File path of saved model.
        """
        self.model = joblib.load(path)
        logger.info("LGBMClassifier loaded from %s", path)


class XGBoostClassifierWrapper(ModelWrapper):
    """XGBoost ternary classifier with data-adaptive complexity.

    Attributes:
        model: Trained XGBClassifier instance.
        params: User-supplied hyperparameters.
    """

    def __init__(self, **params: Any) -> None:
        self.model: XGBClassifier | None = None
        self.params = params

    def train(
        self,
        X: pd.DataFrame,
        y: pd.Series,
        params: dict[str, Any] | None = None,
    ) -> None:
        """Train XGBoost classifier with sample_weight for class balancing.

        Args:
            X: Feature matrix.
            y: Target labels (0=DOWN, 1=NEUTRAL, 2=UP).
            params: Optional hyperparameter overrides.
        """
        n_samples = len(X)

        final_params = self.params.copy()
        if params:
            final_params.update(params)

        if not final_params:
            final_params = {
                "n_estimators": 100,
                "max_depth": 6,
                "learning_rate": 0.1,
            }

        # Force multiclass softmax
        final_params["objective"] = "multi:softprob"
        final_params["num_class"] = len(CLASS_NAMES)
        final_params.setdefault("verbosity", 0)
        final_params.setdefault("eval_metric", "mlogloss")

        # XGBoost doesn't have class_weight — compute sample_weight
        class_weights = final_params.pop("class_weight", DEFAULT_CLASS_WEIGHTS)
        sample_weight = np.array([class_weights.get(int(label), 1.0) for label in y])

        # XGBoost: enable_categorical for tree-based categorical handling
        # Note: XGBoost 2.0+ supports enable_categorical with 'category' dtype
        if isinstance(X, pd.DataFrame) and SECTOR_FEATURE_NAME in X.columns:
            final_params["enable_categorical"] = True
            X = X.copy()
            X[SECTOR_FEATURE_NAME] = X[SECTOR_FEATURE_NAME].astype("category")
            logger.info(
                "XGBClassifier: sector_id set as native categorical feature",
            )

        self.model = XGBClassifier(**final_params)
        self.model.fit(X, y, sample_weight=sample_weight)
        logger.info("XGBClassifier trained on %d samples", n_samples)

    def predict(self, X: pd.DataFrame) -> np.ndarray:
        """Return class predictions (0/1/2).

        Args:
            X: Feature matrix.

        Returns:
            Array of predicted class labels.
        """
        if self.model is None:
            raise ValueError("Model not trained or loaded")
        X = _prepare_categorical_for_predict(X, "xgb")
        return self.model.predict(X).astype(int).ravel()

    def predict_proba(self, X: pd.DataFrame) -> np.ndarray:
        """Return class probabilities with shape (n_samples, 3).

        Args:
            X: Feature matrix.

        Returns:
            Probability array of shape (n_samples, 3).
        """
        if self.model is None:
            raise ValueError("Model not trained or loaded")
        X = _prepare_categorical_for_predict(X, "xgb")
        return self.model.predict_proba(X)

    def save(self, path: str) -> None:
        """Save model to disk.

        Args:
            path: File path for saved model.
        """
        if self.model is None:
            raise ValueError("No model to save")
        self.model.save_model(path)
        logger.info("XGBClassifier saved to %s", path)

    def load(self, path: str) -> None:
        """Load model from disk.

        Args:
            path: File path of saved model.
        """
        self.model = XGBClassifier()
        self.model.load_model(path)
        logger.info("XGBClassifier loaded from %s", path)


class EnsembleClassifierWrapper(ModelWrapper):
    """Soft-voting ensemble of CatBoost + LGBM + XGBoost classifiers.

    Uses ``VotingClassifier(voting='soft')`` so ``predict_proba`` is the
    weighted average of individual classifiers' probabilities.

    Attributes:
        model: Trained VotingClassifier instance.
        weights: Per-estimator weights [cat, lgbm, xgb].
        model_params: Per-model hyperparameters.
        metadata: Serialised metadata dict.
    """

    def __init__(
        self,
        weights: list[float] | None = None,
        model_params: dict[str, dict] | None = None,
    ) -> None:
        self.model: VotingClassifier | None = None
        self.weights = weights  # [cat, lgbm, xgb]
        self.model_params = model_params or {}
        self.metadata: dict[str, Any] = {}

    def train(
        self,
        X: pd.DataFrame,
        y: pd.Series,
        params: dict[str, Any] | None = None,
    ) -> None:
        """Train soft-voting ensemble with class-weight support.

        Args:
            X: Feature matrix.
            y: Target labels (0=DOWN, 1=NEUTRAL, 2=UP).
            params: Optional global overrides (unused — per-model params via
                ``model_params``).
        """
        cat_p = self.model_params.get("catboost", {}).copy()
        lgbm_p = self.model_params.get("lgbm", {}).copy()
        xgb_p = self.model_params.get("xgboost", {}).copy()

        # --- CatBoost defaults ---
        cat_p.setdefault("verbose", False)
        cat_p.setdefault("allow_writing_files", False)
        cat_p.setdefault("loss_function", "MultiClass")
        if "class_weights" not in cat_p:
            cat_p["class_weights"] = [
                DEFAULT_CLASS_WEIGHTS[i] for i in range(len(CLASS_NAMES))
            ]

        # --- LGBM defaults ---
        lgbm_p.setdefault("verbose", -1)
        lgbm_p.setdefault("objective", "multiclass")
        lgbm_p.setdefault("num_class", len(CLASS_NAMES))
        if "class_weight" not in lgbm_p:
            lgbm_p["class_weight"] = DEFAULT_CLASS_WEIGHTS.copy()

        # --- XGBoost defaults ---
        xgb_p.setdefault("verbosity", 0)
        xgb_p["objective"] = "multi:softprob"
        xgb_p["num_class"] = len(CLASS_NAMES)
        xgb_p.setdefault("eval_metric", "mlogloss")
        xgb_p.setdefault("enable_categorical", True)

        # XGBoost sample_weight
        xgb_class_weights = xgb_p.pop("class_weight", DEFAULT_CLASS_WEIGHTS)
        sample_weight = np.array(
            [xgb_class_weights.get(int(label), 1.0) for label in y]
        )

        estimators = [
            ("cat", CompatibleCatBoostClassifier(**cat_p)),
            ("lgbm", LGBMClassifier(**lgbm_p)),
            ("xgb", XGBClassifier(**xgb_p)),
        ]

        if self.weights:
            self.model = VotingClassifier(
                estimators, voting="soft", weights=self.weights
            )
            logger.info(
                "Training weighted classifier ensemble with weights: %s",
                self.weights,
            )
        else:
            self.model = VotingClassifier(estimators, voting="soft")
            logger.info("Training equal-weighted classifier ensemble")

        # Prepare categorical features for native encoding
        cat_indices = _detect_cat_feature_indices(X)
        if cat_indices and isinstance(X, pd.DataFrame) and SECTOR_FEATURE_NAME in X.columns:
            X = X.copy()
            # CatBoost needs int, LightGBM needs category, XGBoost needs category
            # We cast to int first (CatBoost is called first by VotingClassifier)
            # Note: VotingClassifier.fit() calls each estimator sequentially,
            # so we train manually to handle different dtype needs.
            logger.info(
                "Training ensemble with native categorical: %s (index=%s)",
                SECTOR_FEATURE_NAME, cat_indices,
            )
            self._train_with_categorical(X, y, sample_weight, cat_indices)
        else:
            # VotingClassifier.fit accepts sample_weight and forwards it
            self.model.fit(X, y, sample_weight=sample_weight)

        logger.info(
            "EnsembleClassifier trained on %d samples, %d features",
            len(X),
            X.shape[1],
        )

    def _train_with_categorical(
        self,
        X: pd.DataFrame,
        y: np.ndarray,
        sample_weight: np.ndarray | None,
        cat_indices: list[int],
    ) -> None:
        """Train VotingClassifier estimators individually with native categorical support.

        sklearn VotingClassifier.fit() does not forward ``cat_features`` to
        CatBoost or ``categorical_feature`` to LightGBM.  This method trains
        each sub-estimator manually with the correct categorical parameters,
        then patches the VotingClassifier so that predict/predict_proba work
        normally.

        Args:
            X: Feature DataFrame (will be copied and dtype-cast per estimator).
            y: Target labels (0/1/2).
            sample_weight: Per-sample weights, or None.
            cat_indices: Column indices of categorical features (sector_id).
        """
        # We need to train each estimator individually because each framework
        # requires different dtype/parameter handling for categoricals.
        fitted_estimators: list = []

        for name, estimator in self.model.estimators:
            X_est = X.copy()

            if name == "cat":
                # CatBoost: sector_id as int, pass cat_features indices
                X_est[SECTOR_FEATURE_NAME] = X_est[SECTOR_FEATURE_NAME].astype(int)
                estimator.fit(
                    X_est, y,
                    sample_weight=sample_weight,
                    cat_features=cat_indices,
                )
                logger.info("CatBoost trained with cat_features=%s", cat_indices)

            elif name == "lgbm":
                # LightGBM: sector_id as category dtype
                X_est[SECTOR_FEATURE_NAME] = X_est[SECTOR_FEATURE_NAME].astype("category")
                estimator.fit(
                    X_est, y,
                    sample_weight=sample_weight,
                    categorical_feature=[SECTOR_FEATURE_NAME],
                )
                logger.info("LightGBM trained with categorical_feature=[%s]", SECTOR_FEATURE_NAME)

            elif name == "xgb":
                # XGBoost: sector_id as category dtype, enable_categorical already in params
                X_est[SECTOR_FEATURE_NAME] = X_est[SECTOR_FEATURE_NAME].astype("category")
                estimator.fit(X_est, y, sample_weight=sample_weight)
                logger.info("XGBoost trained with enable_categorical + category dtype")

            else:
                # Fallback: unknown estimator — train normally
                estimator.fit(X_est, y, sample_weight=sample_weight)

            fitted_estimators.append(estimator)

        # Patch VotingClassifier internals so predict/predict_proba work
        self.model.estimators_ = fitted_estimators
        self.model.named_estimators_ = {
            name: est for (name, _), est in zip(self.model.estimators, fitted_estimators)
        }
        # Set le_ and classes_ so VotingClassifier considers itself fitted
        from sklearn.preprocessing import LabelEncoder
        le = LabelEncoder().fit(y)
        self.model.le_ = le
        self.model.classes_ = le.classes_

    def predict(self, X: pd.DataFrame) -> np.ndarray:
        """Return class predictions (0=DOWN, 1=NEUTRAL, 2=UP).

        Handles the XGBoost DMatrix feature-name issue by calling each
        estimator individually, same approach as ``EnsembleWrapper.predict``.

        Args:
            X: Feature matrix.

        Returns:
            Array of predicted class labels with shape (n_samples,).
        """
        proba = self.predict_proba(X)
        return np.argmax(proba, axis=1).astype(int)

    def predict_proba(self, X: pd.DataFrame) -> np.ndarray:
        """Return class probabilities with shape (n_samples, 3).

        Manually aggregates per-estimator ``predict_proba`` to avoid the
        VotingClassifier numpy conversion that strips XGBoost feature names.

        Args:
            X: Feature matrix.

        Returns:
            Weighted average probability array of shape (n_samples, 3).
        """
        if self.model is None:
            raise ValueError("Model not trained or loaded")

        X = self._align_features(X)

        # Collect probabilities from each estimator
        probas: list[np.ndarray] = []

        # Model type mapping for categorical feature dtype preparation
        _estimator_model_type: dict[str, str] = {
            "cat": "catboost",
            "lgbm": "lgbm",
            "xgb": "xgb",
        }

        for name, estimator in self.model.named_estimators_.items():
            try:
                model_type = _estimator_model_type.get(name, "lgbm")
                X_est = _prepare_categorical_for_predict(X, model_type)
                if name == "xgb":
                    feature_names = [str(c) for c in X_est.columns]
                    dmatrix = DMatrix(
                        X_est, feature_names=feature_names,
                        enable_categorical=True,
                    )
                    raw = estimator.get_booster().predict(dmatrix)
                    # multi:softprob returns flat array — reshape to (n, 3)
                    proba = raw.reshape(len(X), len(CLASS_NAMES))
                else:
                    proba = estimator.predict_proba(X_est)
                probas.append(proba)
            except Exception:
                logger.exception("predict_proba failed for estimator '%s'", name)
                raise

        stacked = np.array(probas)  # (n_estimators, n_samples, n_classes)
        weights = (
            np.array(self.weights)
            if self.weights
            else np.ones(len(probas)) / len(probas)
        )
        # Weighted average across estimators
        averaged = np.tensordot(weights, stacked, axes=([0], [0]))
        # Normalise rows to sum to 1 (safety)
        row_sums = averaged.sum(axis=1, keepdims=True)
        row_sums[row_sums == 0] = 1.0
        return averaged / row_sums

    def save(self, path: str) -> None:
        """Save ensemble model and metadata to disk.

        Metadata includes weights, class names, feature names, and training
        date.

        Args:
            path: File path for the pickle file (``*.pkl``).
        """
        import json
        from datetime import datetime

        if self.model is None:
            raise ValueError("No model to save")

        joblib.dump(self.model, path)

        # Extract feature names from XGBoost estimator
        feature_names: list[str] | None = None
        if hasattr(self.model, "named_estimators_"):
            xgb_est = self.model.named_estimators_.get("xgb")
            if xgb_est and hasattr(xgb_est, "feature_names_in_"):
                feature_names = [str(f) for f in xgb_est.feature_names_in_]

        self.metadata = {
            "weights": self.weights if self.weights else [1 / 3, 1 / 3, 1 / 3],
            "training_date": datetime.now().isoformat(),
            "model_names": ["catboost", "lgbm", "xgboost"],
            "feature_names": feature_names,
            "class_names": CLASS_NAMES,
        }

        metadata_path = path.replace(".pkl", "_metadata.json")
        with open(metadata_path, "w") as f:
            json.dump(self.metadata, f, indent=2)
        logger.info("Classifier metadata saved to %s", metadata_path)

    def load(self, path: str) -> None:
        """Load ensemble model and metadata from disk.

        Restores ``feature_names_in_`` on the XGBoost sub-estimator so that
        subsequent ``predict`` / ``predict_proba`` calls work correctly.

        Args:
            path: File path of the pickle file (``*.pkl``).
        """
        import json

        self.model = joblib.load(path)

        metadata_path = path.replace(".pkl", "_metadata.json")
        if os.path.exists(metadata_path):
            with open(metadata_path) as f:
                self.metadata = json.load(f)
            self.weights = self.metadata.get("weights")

            feature_names = self.metadata.get("feature_names")
            if feature_names and hasattr(self.model, "named_estimators_"):
                xgb_est = self.model.named_estimators_.get("xgb")
                if xgb_est:
                    xgb_est.feature_names_in_ = np.array(feature_names)
                    logger.debug(
                        "Restored feature_names_in_ to XGBClassifier: %s...",
                        feature_names[:3],
                    )
        logger.info("EnsembleClassifier loaded from %s", path)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _align_features(self, X: pd.DataFrame) -> pd.DataFrame:
        """Ensure input DataFrame matches the training feature order.

        Args:
            X: Raw input feature matrix.

        Returns:
            DataFrame with columns reordered/validated to match training.

        Raises:
            ValueError: If required features are missing.
        """
        xgb_estimator = self.model.named_estimators_["xgb"]

        if not hasattr(xgb_estimator, "feature_names_in_"):
            return X

        expected_features = [str(f) for f in xgb_estimator.feature_names_in_]

        if not isinstance(X, pd.DataFrame):
            return pd.DataFrame(X, columns=expected_features)

        X = X.copy()
        input_cols = list(X.columns)

        if input_cols != expected_features:
            missing = set(expected_features) - set(input_cols)
            extra = set(input_cols) - set(expected_features)

            if missing:
                logger.error("Missing features in input: %s", missing)
                raise ValueError(f"Missing features: {missing}")
            if extra:
                logger.warning("Extra features (ignored): %s", extra)

            X = X.reindex(columns=expected_features)

        return X

