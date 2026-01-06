from abc import ABC, abstractmethod
from typing import Any, Dict, List
import pandas as pd
import numpy as np
import joblib
import logging
import os
from catboost import CatBoostRegressor
from lightgbm import LGBMRegressor
from xgboost import XGBRegressor
from sklearn.ensemble import VotingRegressor

from sklearn.base import BaseEstimator, RegressorMixin


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
    def train(self, X: pd.DataFrame, y: pd.Series, params: Dict[str, Any] = None):
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

    def train(self, X: pd.DataFrame, y: pd.Series, params: Dict[str, Any] = None):
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

    def train(self, X: pd.DataFrame, y: pd.Series, params: Dict[str, Any] = None):
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
    
    def train(self, X: pd.DataFrame, y: pd.Series, params: Dict[str, Any] = None):
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
        joblib.dump(self.model, path)
    
    def load(self, path: str):
        self.model = joblib.load(path)

class EnsembleWrapper(ModelWrapper):
    def __init__(self, weights: List[float] = None, model_params: Dict[str, Dict] = None):
        self.model = None
        self.weights = weights  # [cat, lgbm, xgb]
        self.model_params = model_params or {} # {'catboost': ..., 'lgbm': ..., 'xgboost': ...}
        self.metadata = {}

    def train(self, X: pd.DataFrame, y: pd.Series, params: Dict[str, Any] = None):
        # Extract params for each model
        cat_p = self.model_params.get('catboost', {})
        lgbm_p = self.model_params.get('lgbm', {})
        xgb_p = self.model_params.get('xgboost', {})
        
        # Ensure verbosity control
        if 'verbose' not in cat_p: cat_p['verbose'] = 0
        if 'verbose' not in lgbm_p: lgbm_p['verbose'] = -1
        
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
        return self.model.predict(X)

    def save(self, path: str):
        # Save both model and metadata
        joblib.dump(self.model, path)
        
        # Save metadata (weights, training date)
        metadata_path = path.replace('.pkl', '_metadata.json')
        import json
        from datetime import datetime
        
        self.metadata = {
            'weights': self.weights if self.weights else [1/3, 1/3, 1/3],
            'training_date': datetime.now().isoformat(),
            'model_names': ['catboost', 'lgbm', 'xgboost']
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
            with open(metadata_path, 'r') as f:
                self.metadata = json.load(f)
                self.weights = self.metadata.get('weights')

