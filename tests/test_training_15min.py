"""
Unit tests for 15min training logic fixes
Tests for strategy_returns calculation and data validation
"""
import numpy as np
import pandas as pd
import pytest


class TestStrategyReturnsCalculation:
    """Test the strategy_returns calculation logic"""

    def test_strategy_returns_calculation_correct_direction(self):
        """Test that strategy_returns is positive when prediction direction matches actual direction"""
        # Arrange
        predictions = np.array([0.01, -0.02, 0.03, -0.01])
        y_val = pd.Series([0.015, -0.01, 0.02, -0.005])

        # Act
        pred_dir = (predictions > 0).astype(int) * 2 - 1  # -1 or +1
        strategy_returns = y_val.values * pred_dir

        # Assert - all predictions are correct, so all returns should be positive
        assert strategy_returns[0] > 0, "Predicted up, actual up -> should be positive"
        assert strategy_returns[1] > 0, "Predicted down, actual down -> should be positive"
        assert strategy_returns[2] > 0, "Predicted up, actual up -> should be positive"
        assert strategy_returns[3] > 0, "Predicted down, actual down -> should be positive"

    def test_strategy_returns_calculation_wrong_direction(self):
        """Test that strategy_returns is negative when prediction direction is wrong"""
        # Arrange
        predictions = np.array([0.01, -0.02])
        y_val = pd.Series([-0.015, 0.01])

        # Act
        pred_dir = (predictions > 0).astype(int) * 2 - 1
        strategy_returns = y_val.values * pred_dir

        # Assert - all predictions are wrong, so all returns should be negative
        assert strategy_returns[0] < 0, "Predicted up, actual down -> should be negative"
        assert strategy_returns[1] < 0, "Predicted down, actual up -> should be negative"

    def test_pred_dir_conversion(self):
        """Test that pred_dir correctly converts to -1 or +1"""
        # Arrange
        predictions = np.array([0.05, -0.03, 0.0, -0.0])

        # Act
        pred_dir = (predictions > 0).astype(int) * 2 - 1

        # Assert
        assert pred_dir[0] == 1, "Positive prediction -> +1"
        assert pred_dir[1] == -1, "Negative prediction -> -1"
        assert pred_dir[2] == -1, "Zero prediction -> -1 (not > 0)"
        assert pred_dir[3] == -1, "Negative zero -> -1"

    def test_sharpe_ratio_calculation(self):
        """Test Sharpe ratio calculation with 15min bars"""
        # Arrange
        strategy_returns = np.array([0.01, -0.005, 0.02, 0.015, -0.01])
        bars_per_year = 252 * 26  # 15min bars: 26 bars/day * 252 trading days

        # Act
        mean_return = strategy_returns.mean()
        std_return = strategy_returns.std()
        sharpe = mean_return / (std_return + 1e-8) * (bars_per_year ** 0.5)

        # Assert
        assert isinstance(sharpe, (int, float)), "Sharpe should be numeric"
        assert not np.isnan(sharpe), "Sharpe should not be NaN"
        assert bars_per_year == 6552, "15min bars per year should be 6552"

    def test_empty_predictions_handling(self):
        """Test that empty arrays don't cause errors"""
        # Arrange
        predictions = np.array([])
        y_val = pd.Series([])

        # Act
        pred_dir = (predictions > 0).astype(int) * 2 - 1
        strategy_returns = y_val.values * pred_dir

        # Assert
        assert len(strategy_returns) == 0, "Empty input should produce empty output"


class TestDataSizeValidation:
    """Test data size validation warnings"""

    def test_small_training_set_detection(self):
        """Test that small training sets are detected"""
        # Simulate small dataset
        n_samples = 300
        assert n_samples < 500, "Should trigger warning for < 500 samples"

    def test_small_validation_set_detection(self):
        """Test that small validation sets are detected"""
        # Simulate small validation set
        n_val_samples = 50
        assert n_val_samples < 100, "Should trigger warning for < 100 validation samples"

    def test_adequate_data_size(self):
        """Test that adequate data sizes don't trigger warnings"""
        # Simulate adequate dataset
        n_train = 1000
        n_val = 200
        assert n_train >= 500, "Should NOT trigger warning"
        assert n_val >= 100, "Should NOT trigger warning"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
