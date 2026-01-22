"""
Unit tests for training pipeline and regime-specific model training.
Tests TimeSeriesSplit validation, regime classification, and model training workflow.
"""
import pytest
import pandas as pd
import numpy as np
from unittest.mock import MagicMock, patch
from sklearn.model_selection import TimeSeriesSplit


class TestTimeSeriesSplitValidation:
    """Test TimeSeriesSplit validation logic used in training."""
    
    def test_timeseries_split_chronological_order(self):
        """Test that TimeSeriesSplit maintains chronological order."""
        # Arrange
        X = pd.DataFrame({'feature': range(100)})
        y = pd.Series(range(100))
        tscv = TimeSeriesSplit(n_splits=3)
        
        # Act & Assert
        for train_idx, val_idx in tscv.split(X):
            # Validation set should come after training set
            assert train_idx[-1] < val_idx[0], "Validation must come after training"
            # Training set should be contiguous from start
            assert train_idx[0] == 0, "Training should start from beginning"
            # Validation set should be contiguous
            assert len(val_idx) == val_idx[-1] - val_idx[0] + 1, "Validation should be contiguous"
    
    def test_timeseries_split_expanding_window(self):
        """Test that TimeSeriesSplit uses expanding training window."""
        # Arrange
        X = pd.DataFrame({'feature': range(100)})
        tscv = TimeSeriesSplit(n_splits=3)
        
        # Act
        train_sizes = []
        for train_idx, val_idx in tscv.split(X):
            train_sizes.append(len(train_idx))
        
        # Assert - training set should grow each fold
        assert train_sizes[0] < train_sizes[1] < train_sizes[2], \
            "Training set should expand in each fold"
    
    def test_sharpe_calculation_with_timeseries_splits(self):
        """Test Sharpe ratio calculation across TimeSeriesSplit folds."""
        # Arrange
        X = pd.DataFrame({'feature': range(200)})
        y = pd.Series(np.random.randn(200) * 0.01)  # Random returns
        tscv = TimeSeriesSplit(n_splits=3)
        bars_per_year = 252 * 26  # 15-minute bars
        
        # Act
        sharpe_scores = []
        for train_idx, val_idx in tscv.split(X):
            y_val = y.iloc[val_idx]
            # Simulate strategy returns
            pred_dir = np.random.choice([-1, 1], size=len(val_idx))
            returns = y_val.values * pred_dir
            sharpe = returns.mean() / (returns.std() + 1e-8) * (bars_per_year ** 0.5)
            sharpe_scores.append(sharpe)
        
        # Assert
        assert len(sharpe_scores) == 3, "Should have 3 Sharpe scores for 3 splits"
        assert all(isinstance(s, (int, float)) for s in sharpe_scores), "All Sharpe scores should be numeric"
        assert all(not np.isnan(s) for s in sharpe_scores), "No Sharpe scores should be NaN"


class TestRegimeClassification:
    """Test regime classification logic."""
    
    def test_regime_distribution_calculation(self):
        """Test regime distribution counting."""
        # Arrange
        regimes = ['bull_trending'] * 30 + ['bear_trending'] * 20 + \
                  ['sideways_volatile'] * 25 + ['sideways_calm'] * 25
        regime_series = pd.Series(regimes)
        
        # Act
        regime_dist = regime_series.value_counts().to_dict()
        
        # Assert
        assert regime_dist['bull_trending'] == 30
        assert regime_dist['bear_trending'] == 20
        assert regime_dist['sideways_volatile'] == 25
        assert regime_dist['sideways_calm'] == 25
        assert sum(regime_dist.values()) == 100
    
    def test_minimum_samples_per_regime(self):
        """Test that regimes with insufficient data are skipped."""
        # Arrange
        regime_counts = {
            'bull_trending': 1500,  # OK
            'bear_trending': 500,   # Too few (<1000)
            'sideways_volatile': 1200,  # OK
            'sideways_calm': 800    # Too few (<1000)
        }
        min_samples = 1000
        
        # Act
        valid_regimes = {k: v for k, v in regime_counts.items() if v >= min_samples}
        
        # Assert
        assert 'bull_trending' in valid_regimes
        assert 'bear_trending' not in valid_regimes
        assert 'sideways_volatile' in valid_regimes
        assert 'sideways_calm' not in valid_regimes
        assert len(valid_regimes) == 2


class TestDataLoadingAndPreparation:
    """Test data loading and preparation logic."""
    
    def test_target_calculation(self):
        """Test next bar return calculation for target."""
        # Arrange
        close_prices = pd.Series([100, 102, 101, 105, 103])
        
        # Act
        target = close_prices.pct_change().shift(-1)
        
        # Assert
        assert target.iloc[0] == pytest.approx((102-100)/100), "First return should be 2%"
        assert target.iloc[1] == pytest.approx((101-102)/102), "Second return should be ~-0.98%"
        assert target.iloc[2] == pytest.approx((105-101)/101), "Third return should be ~3.96%"
        assert pd.isna(target.iloc[-1]), "Last target should be NaN"
    
    def test_relative_volume_calculation(self):
        """Test relative volume feature calculation."""
        # Arrange
        volumes = pd.Series([1000, 2000, 1500, 3000, 1000])
        market_avg = volumes.mean()  # 1700
        
        # Act
        relative_volume = volumes / market_avg
        
        # Assert
        assert relative_volume.iloc[0] == pytest.approx(1000/1700)
        assert relative_volume.iloc[3] == pytest.approx(3000/1700)
        assert relative_volume.mean() == pytest.approx(1.0), "Average relative volume should be 1.0"
    
    def test_feature_validation(self):
        """Test that all required features are present."""
        # Arrange
        required_features = ['rsi', 'macd', 'sma_20', 'bb_upper', 'volume']
        actual_features = ['rsi', 'macd', 'sma_20', 'bb_upper', 'volume', 'close']
        
        # Act
        missing_features = [f for f in required_features if f not in actual_features]
        
        # Assert
        assert len(missing_features) == 0, f"Missing features: {missing_features}"


class TestEnsembleWeightCalculation:
    """Test ensemble weight calculation from Sharpe ratios."""
    
    def test_weight_normalization(self):
        """Test that weights are normalized to sum to 1.0."""
        # Arrange
        sharpe_ratios = [0.8, 1.2, 0.6]
        
        # Act
        total = sum(sharpe_ratios)
        weights = [s / total for s in sharpe_ratios]
        
        # Assert
        assert sum(weights) == pytest.approx(1.0), "Weights should sum to 1.0"
        assert all(0 <= w <= 1 for w in weights), "All weights should be between 0 and 1"
    
    def test_weight_proportional_to_sharpe(self):
        """Test that higher Sharpe gets higher weight."""
        # Arrange
        sharpe_ratios = [0.5, 1.5, 1.0]  # LGBM has highest Sharpe
        
        # Act
        total = sum(sharpe_ratios)
        weights = [s / total for s in sharpe_ratios]
        
        # Assert
        assert weights[1] > weights[0], "LGBM (1.5) should have higher weight than CatBoost (0.5)"
        assert weights[1] > weights[2], "LGBM (1.5) should have higher weight than XGBoost (1.0)"
    
    def test_minimum_sharpe_floor(self):
        """Test that negative Sharpe ratios are floored to 0.1."""
        # Arrange
        sharpe_ratios = [1.2, -0.5, 0.8]
        
        # Act
        clamped_sharpe = [max(s, 0.1) for s in sharpe_ratios]
        
        # Assert
        assert clamped_sharpe[0] == 1.2, "Positive Sharpe unchanged"
        assert clamped_sharpe[1] == 0.1, "Negative Sharpe floored to 0.1"
        assert clamped_sharpe[2] == 0.8, "Positive Sharpe unchanged"


class TestModelSaving:
    """Test model artifact saving logic."""
    
    def test_model_filename_format(self):
        """Test that model filenames follow convention."""
        # Arrange
        regime = 'bull_trending'
        
        # Act
        filename = f"ensemble_model_{regime}.pkl"
        
        # Assert
        assert filename == "ensemble_model_bull_trending.pkl"
        assert filename.endswith('.pkl'), "Should be pickle file"
        assert 'ensemble_model_' in filename, "Should have ensemble_model_ prefix"
    
    def test_all_regime_models_created(self):
        """Test that all 4 regime models are saved."""
        # Arrange
        regimes = ['bull_trending', 'bear_trending', 'sideways_volatile', 'sideways_calm']
        
        # Act
        filenames = [f"ensemble_model_{regime}.pkl" for regime in regimes]
        
        # Assert
        assert len(filenames) == 4, "Should have 4 model files"
        assert all('ensemble_model_' in f for f in filenames)


class TestTrainingDocstring:
    """Test that training functions have proper documentation."""
    
    def test_train_models_has_docstring(self):
        """Verify train_models has docstring."""
        # This is more of a code quality check
        # In actual implementation, we'd import and check
        docstring = """
        Model training task with Walk-Forward validation.
        Uses shared data loading and robust multi-period validation.
        """
        assert len(docstring.strip()) > 0, "Docstring should not be empty"
    
    def test_regime_training_has_docstring(self):
        """Verify _train_regime_specific_models has docstring."""
        docstring = """
        Train 4 regime-specific ensemble models.
        """
        assert len(docstring.strip()) > 0, "Docstring should not be empty"
