import os
import pandas as pd
import joblib
import logging
from typing import Optional
from app.ml.models import EnsembleWrapper
from app.services.regime import MarketRegime

logger = logging.getLogger(__name__)

class PredictorService:
    """
    Regime-aware predictor service using ensemble models.
    Supports 4 market regimes with dedicated models.
    """
    
    _instance = None
    _models = {}  # Dict: regime -> EnsembleWrapper
    _base_path = "/app/model_artifacts/"
    _model_map = {
        MarketRegime.BULL_TRENDING: "ensemble_model_bull_trending.pkl",
        MarketRegime.BEAR_TRENDING: "ensemble_model_bear_trending.pkl",
        MarketRegime.SIDEWAYS_VOLATILE: "ensemble_model_sideways_volatile.pkl",
        MarketRegime.SIDEWAYS_CALM: "ensemble_model_sideways_calm.pkl"
    }
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(PredictorService, cls).__new__(cls)
            cls._instance._initialize()
        return cls._instance
    
    def _initialize(self):
        """Initialize all regime-specific models."""
        self._models = {}
        
        # Try to load regime-specific models
        for regime, filename in self._model_map.items():
            model_path = os.path.join(self._base_path, filename)
            try:
                if os.path.exists(model_path):
                    model = EnsembleWrapper()
                    model.load(model_path)
                    self._models[regime] = model
                    logger.info(f"✓ Loaded {regime.value} model from {filename}")
                else:
                    logger.warning(f"✗ {regime.value} model not found: {filename}")
            except Exception as e:
                logger.error(f"Failed to load {regime.value} model: {e}")
        
        # Fallback: Load generic model if no regime models
        if not self._models:
            generic_path = os.path.join(self._base_path, "ensemble_model.pkl")
            if os.path.exists(generic_path):
                model = EnsembleWrapper()
                model.load(generic_path)
                # Use generic model for all regimes
                for regime in MarketRegime:
                    self._models[regime] = model
                logger.warning("⚠ No regime-specific models, using generic model for all regimes")
            else:
                logger.error("No models found (regime or generic). Train models first.")
    
    def get_model(self, regime: MarketRegime = MarketRegime.SIDEWAYS_CALM) -> Optional[EnsembleWrapper]:
        """Get model for specific regime."""
        return self._models.get(regime, None)
    
    def predict_next(self, features: pd.DataFrame, regime: MarketRegime = MarketRegime.SIDEWAYS_CALM) -> float:
        """
        Predict next value using regime-aware model.
        
        Args:
            features: Feature DataFrame (single row or multiple)
            regime: Current market regime
        
        Returns:
            Prediction (0.0 to 1.0)
        """
        model = self.get_model(regime)
        if model is None:
            logger.warning(f"No model for regime {regime.value}, returning neutral")
            return 0.5
        
        try:
            prediction = model.predict(features)
            if isinstance(prediction, pd.Series):
                return float(prediction.iloc[0])
            return float(prediction[0])
        except Exception as e:
            logger.error(f"Prediction error for regime {regime.value}: {e}", exc_info=True)
            return 0.5
    
    def retrain(self, X: pd.DataFrame, y: pd.Series) -> bool:
        """
        Retrain model with equal weights (deprecated, use retrain_weighted).
        """
        return self.retrain_weighted(X, y, weights=None)
    
    def retrain_weighted(self, X: pd.DataFrame, y: pd.Series, weights: list = None, model_params: dict = None) -> bool:
        """
        Retrain model with optional weights and hyperparameters.
        
        Args:
            X: Feature DataFrame (already scaled)
            y: Target Series
            weights: Optional list [cat, lgbm, xgb] weights
            model_params: Optional dict {'catboost': ..., 'lgbm': ..., 'xgboost': ...}
        
        Returns:
            Success status
        """
        try:
            model = EnsembleWrapper(weights=weights, model_params=model_params)
            model.train(X, y)
            
            os.makedirs(os.path.dirname(self._model_path), mode=0o777, exist_ok=True)
            model.save(self._model_path)
            
            self._model = model
            logger.info("Model retrained successfully")
            return True
        except Exception as e:
            logger.error(f"Retraining failed: {e}", exc_info=True)
            return False
    
    def get_model_info(self) -> dict:
        """Get model metadata including weights and training date."""
        if self._model and hasattr(self._model, 'metadata'):
            return self._model.metadata
        return {}
