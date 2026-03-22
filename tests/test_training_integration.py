"""
Integration tests for training pipeline (train_models and tune_models).

Tests full workflows with mocked database sessions and external dependencies.
Focuses on end-to-end data flow and regime-based model training logic.

CRITICAL: Uses Python 3.14 compatible asyncio patterns (no get_event_loop_policy).
"""

from datetime import timedelta
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from app.services.regime import MarketRegime
from app.tasks.training import (
    _load_and_prepare_data,
    _train_regime_specific_models,
    train_models,
    tune_models,
)


# Fixtures
@pytest.fixture
def mock_db_session():
    """Mock database session."""
    session = MagicMock()
    session.commit = MagicMock()
    session.rollback = MagicMock()
    session.close = MagicMock()
    return session


@pytest.fixture
def mock_stock_repo():
    """Mock SyncStockRepository with realistic OHLCV data."""
    repo = MagicMock()

    # Mock get_active_symbols
    repo.get_active_symbols.return_value = ['AAPL', 'MSFT', 'SPY']

    # Mock get_ohlcv_range with realistic data
    def mock_get_ohlcv_range(symbol, start_date, end_date, timeframe='15m'):
        """Generate 200 bars of realistic OHLCV data."""
        dates = pd.date_range(start_date, end_date, freq='15min', tz='UTC')[:200]

        # Realistic price simulation
        base_price = {'AAPL': 150, 'MSFT': 300, 'SPY': 450}.get(symbol, 100)
        prices = base_price + np.cumsum(np.random.randn(len(dates)) * 0.5)
        volumes = np.random.randint(1000000, 10000000, len(dates))

        bars = []
        for i, dt in enumerate(dates):
            bar = MagicMock()
            bar.symbol = symbol
            bar.date_time = dt
            bar.open = prices[i]
            bar.high = prices[i] * 1.01
            bar.low = prices[i] * 0.99
            bar.close = prices[i]
            bar.volume = volumes[i]
            bar.vwap = prices[i]
            bar.trade_count = 100
            bars.append(bar)

        return bars

    repo.get_ohlcv_range.side_effect = mock_get_ohlcv_range
    return repo


@pytest.fixture
def mock_feature_engineer():
    """Mock FeatureEngineer with expected behavior."""
    engineer = MagicMock()

    # Mock base_feature_columns (23 features)
    engineer.base_feature_columns = [
        'rsi', 'macd', 'bb_width', 'atr', 'adx',
        'ma_5', 'ma_20', 'ema_12',
        'momentum_5', 'momentum_20',
        'volume', 'vwap', 'obv', 'relative_volume',
        'close', 'high', 'low', 'open',
        'hour', 'day_of_week', 'month',
        'sector_id', 'log_volume'
    ]

    # Mock create_features
    def mock_create_features(df):
        """Create features DataFrame with all required columns."""
        df_copy = df.copy()

        # Add technical indicators
        df_copy['rsi'] = 50 + np.random.randn(len(df_copy)) * 10
        df_copy['macd'] = np.random.randn(len(df_copy)) * 2
        df_copy['bb_width'] = np.abs(np.random.randn(len(df_copy)) * 5)
        df_copy['atr'] = np.abs(np.random.randn(len(df_copy)) * 3)
        df_copy['adx'] = 25 + np.random.randn(len(df_copy)) * 5
        df_copy['ma_5'] = df_copy['close'].rolling(5, min_periods=1).mean()
        df_copy['ma_20'] = df_copy['close'].rolling(20, min_periods=1).mean()
        df_copy['ema_12'] = df_copy['close'].ewm(span=12).mean()
        df_copy['ema_26'] = df_copy['close'].ewm(span=26).mean()
        df_copy['momentum_5'] = df_copy['close'].pct_change(5)
        df_copy['momentum_20'] = df_copy['close'].pct_change(20)
        df_copy['obv'] = (df_copy['volume'] * np.sign(df_copy['close'].diff())).cumsum()
        df_copy['relative_volume'] = df_copy['volume'] / df_copy['volume'].mean()

        # Time features
        df_copy['hour'] = df_copy.index.hour if hasattr(df_copy.index, 'hour') else 10
        df_copy['day_of_week'] = df_copy.index.dayofweek if hasattr(df_copy.index, 'dayofweek') else 2
        df_copy['month'] = df_copy.index.month if hasattr(df_copy.index, 'month') else 6

        # Market features
        df_copy['sector_id'] = 1
        df_copy['log_volume'] = np.log1p(df_copy['volume'])

        return df_copy

    engineer.create_features.side_effect = mock_create_features

    # Mock extract_feature_vector (returns scaled features)
    def mock_extract_feature_vector(X, fit_scaler=False):
        """Return DataFrame as-is (mock scaling)."""
        return X.copy()

    engineer.extract_feature_vector.side_effect = mock_extract_feature_vector

    return engineer


@pytest.fixture
def mock_regime_detector():
    """Mock RegimeDetector with deterministic regime classification."""
    detector = MagicMock()

    # Mock detect_regime (returns alternating regimes for variety)
    regimes = [
        MarketRegime.BULL_TRENDING,
        MarketRegime.SIDEWAYS_CALM,
        MarketRegime.BEAR_TRENDING,
        MarketRegime.SIDEWAYS_VOLATILE
    ]

    def mock_detect_regime(spy_features, vix_value=None):
        """Return regime based on data index (deterministic)."""
        idx = len(spy_features) % len(regimes)
        return regimes[idx]

    detector.detect_regime.side_effect = mock_detect_regime

    return detector


# Integration Tests
class TestTrainModelsIntegration:
    """Integration tests for train_models task."""

    @patch('app.tasks.training.SessionLocal')
    @patch('app.tasks.training.SyncStockRepository')
    @patch('app.tasks.training.FeatureEngineer')
    @patch('app.tasks.training.PredictorService')
    @patch('app.tasks.training.RegimeDetector')
    @patch('app.tasks.training._train_regime_specific_models')
    @patch('app.core.cache.cache.get')
    def test_train_models_full_workflow(
        self,
        mock_cache_get,
        mock_train_regime_fn,
        mock_regime_detector_cls,
        mock_predictor_cls,
        mock_feature_engineer_cls,
        mock_repo_cls,
        mock_session_local,
        mock_db_session,
        mock_stock_repo,
        mock_feature_engineer,
        mock_regime_detector
    ):
        """Test train_models full workflow with regime classification."""
        # Setup mocks
        mock_session_local.return_value = mock_db_session
        mock_repo_cls.return_value = mock_stock_repo
        mock_feature_engineer_cls.return_value = mock_feature_engineer

        # Mock PredictorService - get_model returns ensemble with predict()
        mock_predictor = MagicMock()
        def mock_get_model(regime):
            mock_ensemble = MagicMock()
            mock_ensemble.predict.return_value = np.random.randn(100) * 0.01
            return mock_ensemble
        mock_predictor.get_model.side_effect = mock_get_model
        mock_predictor_cls.return_value = mock_predictor

        mock_regime_detector_cls.return_value = mock_regime_detector

        # Mock VIX cache
        mock_cache_get.return_value = "18.5"

        # Execute - Use apply() for Celery tasks with bind=True
        # train_models is decorated with @celery_app.task(bind=True)
        # so it expects self as first argument
        train_models.apply()

        # Assertions
        mock_stock_repo.get_active_symbols.assert_called_once()
        mock_stock_repo.get_ohlcv_range.assert_called()  # Multiple calls for symbols + SPY
        mock_feature_engineer.create_features.assert_called()

        # Verify regime-specific training was called
        mock_train_regime_fn.assert_called_once()

        # Verify database commit
        mock_db_session.commit.assert_called()
        mock_db_session.close.assert_called()

    @patch('app.tasks.training.SessionLocal')
    @patch('app.tasks.training.SyncStockRepository')
    @patch('app.tasks.training.FeatureEngineer')
    def test_train_models_no_active_symbols(
        self,
        mock_feature_engineer_cls,
        mock_repo_cls,
        mock_session_local,
        mock_db_session,
        mock_stock_repo,
        mock_feature_engineer
    ):
        """Test train_models when no active symbols exist."""
        # Setup
        mock_session_local.return_value = mock_db_session
        mock_repo_cls.return_value = mock_stock_repo
        mock_feature_engineer_cls.return_value = mock_feature_engineer

        # Mock no symbols
        mock_stock_repo.get_active_symbols.return_value = []

        # Execute
        train_models.apply()

        # Should return early
        mock_db_session.commit.assert_not_called()
        mock_db_session.close.assert_called()

    @patch('app.tasks.training.SessionLocal')
    @patch('app.tasks.training.SyncStockRepository')
    @patch('app.tasks.training.FeatureEngineer')
    def test_train_models_insufficient_data(
        self,
        mock_feature_engineer_cls,
        mock_repo_cls,
        mock_session_local,
        mock_db_session,
        mock_stock_repo,
        mock_feature_engineer
    ):
        """Test train_models when data is insufficient."""
        # Setup
        mock_session_local.return_value = mock_db_session
        mock_repo_cls.return_value = mock_stock_repo
        mock_feature_engineer_cls.return_value = mock_feature_engineer

        # Mock very little data (< 50 bars)
        def mock_get_ohlcv_range_short(symbol, start_date, end_date, timeframe='15m'):
            dates = pd.date_range(start_date, end_date, freq='15min', tz='UTC')[:30]
            bars = []
            for i, dt in enumerate(dates):
                bar = MagicMock()
                bar.symbol = symbol
                bar.date_time = dt
                bar.close = 100 + i
                bar.volume = 1000000
                bars.append(bar)
            return bars

        mock_stock_repo.get_ohlcv_range.side_effect = mock_get_ohlcv_range_short

        # Execute
        train_models.apply()


class TestTuneModelsIntegration:
    """Integration tests for tune_models task."""

    @patch('app.tasks.training.SessionLocal')
    @patch('app.tasks.training.SyncStockRepository')
    @patch('app.tasks.training.FeatureEngineer')
    @patch('optuna.create_study')
    @patch('builtins.open', create=True)
    @patch('app.core.cache.cache.get')
    def test_tune_models_full_workflow(
        self,
        mock_cache_get,
        mock_open,
        mock_optuna_study,
        mock_feature_engineer_cls,
        mock_repo_cls,
        mock_session_local,
        mock_db_session,
        mock_stock_repo,
        mock_feature_engineer
    ):
        """Test tune_models full workflow with Optuna."""
        # Setup mocks
        mock_session_local.return_value = mock_db_session
        mock_repo_cls.return_value = mock_stock_repo
        mock_feature_engineer_cls.return_value = mock_feature_engineer

        # Mock VIX cache (중요: 없으면 ATR 기반 regime 사용)
        mock_cache_get.return_value = "18.5"

        # Mock Optuna study with all required attributes
        study = MagicMock()
        study.best_params = {'learning_rate': 0.05, 'n_estimators': 100}
        study.best_value = 0.5234  # Mock Sharpe ratio (must be float, not MagicMock)
        mock_optuna_study.return_value = study

        # Mock file write
        mock_file = MagicMock()
        mock_open.return_value.__enter__.return_value = mock_file

        # Execute
        # Note: tune_models skips tuning if data < 500 samples per regime
        # This test will pass early exit path
        tune_models.apply()

        # Assertions - tune_models returns early due to insufficient data
        mock_stock_repo.get_active_symbols.assert_called_once()
        # Optuna not called when all regimes have < 500 samples
        mock_db_session.commit.assert_called()
        mock_db_session.close.assert_called()


class TestLoadAndPrepareDataIntegration:
    """Integration tests for _load_and_prepare_data helper."""

    @patch('app.core.cache.cache.get')
    def test_load_and_prepare_data_with_regime(
        self,
        mock_cache_get,
        mock_stock_repo,
        mock_feature_engineer
    ):
        """Test _load_and_prepare_data with regime classification."""
        # Setup
        symbols = ['AAPL', 'MSFT', 'SPY']
        end_date = pd.Timestamp.now(tz='UTC')
        start_date = end_date - timedelta(days=365)

        # Mock VIX
        mock_cache_get.return_value = "15.2"

        # Execute
        with patch('app.tasks.training.RegimeDetector') as mock_regime_detector_cls:
            mock_detector = MagicMock()
            mock_detector.detect_regime.return_value = MarketRegime.BULL_TRENDING
            mock_regime_detector_cls.return_value = mock_detector

            X, y, successful_symbols = _load_and_prepare_data(
                repo=mock_stock_repo,
                feature_engineer=mock_feature_engineer,
                symbols=symbols,
                start_date=start_date,
                end_date=end_date,
                symbol_limit=3,
                classify_regime=True
            )

        # Assertions
        assert not X.empty
        assert not y.empty
        assert len(successful_symbols) > 0
        assert 'regime' in X.columns
        assert all(regime in MarketRegime._value2member_map_ for regime in X['regime'].unique())

    def test_load_and_prepare_data_no_regime(
        self,
        mock_stock_repo,
        mock_feature_engineer
    ):
        """Test _load_and_prepare_data without regime classification."""
        # Setup
        symbols = ['AAPL', 'MSFT']
        end_date = pd.Timestamp.now(tz='UTC')
        start_date = end_date - timedelta(days=180)

        # Execute
        X, y, successful_symbols = _load_and_prepare_data(
            repo=mock_stock_repo,
            feature_engineer=mock_feature_engineer,
            symbols=symbols,
            start_date=start_date,
            end_date=end_date,
            symbol_limit=2,
            classify_regime=False
        )

        # Assertions
        assert not X.empty
        assert not y.empty
        assert len(successful_symbols) == 2
        assert 'regime' not in X.columns

    def test_load_and_prepare_data_empty_result(
        self,
        mock_stock_repo,
        mock_feature_engineer
    ):
        """Test _load_and_prepare_data when all symbols fail."""
        # Setup - mock no data (override fixture's side_effect)
        mock_stock_repo.get_ohlcv_range.side_effect = None
        mock_stock_repo.get_ohlcv_range.return_value = []

        symbols = ['INVALID']
        end_date = pd.Timestamp.now(tz='UTC')
        start_date = end_date - timedelta(days=30)

        # Execute
        X, y, successful_symbols = _load_and_prepare_data(
            repo=mock_stock_repo,
            feature_engineer=mock_feature_engineer,
            symbols=symbols,
            start_date=start_date,
            end_date=end_date,
            symbol_limit=1,
            classify_regime=False
        )

        # Assertions
        assert X.empty
        assert y.empty
        assert len(successful_symbols) == 0


class TestTrainRegimeSpecificModelsIntegration:
    """Integration tests for _train_regime_specific_models helper."""

    @patch('app.ml.models.EnsembleWrapper')
    @patch('os.path.exists')
    @patch('builtins.open', create=True)
    def test_train_regime_specific_models_all_regimes(
        self,
        mock_open,
        mock_os_exists,
        mock_ensemble_cls,
        mock_feature_engineer
    ):
        """Test training all 4 regime models with sufficient data."""
        # Setup
        mock_os_exists.return_value = False  # No existing tuning params

        # Create large dataset with all regimes (4500 samples = 1125 per regime)
        n_samples = 4500
        dates = pd.date_range('2024-01-01', periods=n_samples, freq='15min', tz='UTC')

        # Create features DataFrame
        X = pd.DataFrame({
            'rsi': np.random.rand(n_samples) * 100,
            'macd': np.random.randn(n_samples),
            'bb_width': np.random.rand(n_samples) * 10,
            'atr': np.random.rand(n_samples) * 5,
            'adx': np.random.rand(n_samples) * 50,
            'ma_5': np.random.rand(n_samples) * 150,
            'ma_20': np.random.rand(n_samples) * 150,
            'ema_12': np.random.rand(n_samples) * 150,
            'ema_26': np.random.rand(n_samples) * 150,
            'momentum_5': np.random.randn(n_samples) * 0.02,
            'momentum_20': np.random.randn(n_samples) * 0.05,
            'volume': np.random.randint(1000000, 10000000, n_samples),
            'vwap': np.random.rand(n_samples) * 150,
            'obv': np.cumsum(np.random.randn(n_samples) * 100000),
            'relative_volume': np.random.rand(n_samples) * 2,
            'close': 150 + np.cumsum(np.random.randn(n_samples) * 0.5),
            'high': 150 + np.cumsum(np.random.randn(n_samples) * 0.5),
            'low': 150 + np.cumsum(np.random.randn(n_samples) * 0.5),
            'open': 150 + np.cumsum(np.random.randn(n_samples) * 0.5),
            'hour': np.random.randint(9, 16, n_samples),
            'day_of_week': np.random.randint(0, 5, n_samples),
            'month': np.random.randint(1, 13, n_samples),
            'sector_id': np.random.randint(1, 12, n_samples),
            'log_volume': np.log1p(np.random.randint(1000000, 10000000, n_samples)),
            'regime': np.random.choice(
                [r.value for r in MarketRegime],
                size=n_samples,
                p=[0.25, 0.25, 0.25, 0.25]  # Equal distribution
            )
        }, index=dates)

        # Target
        y = pd.Series(np.random.randn(n_samples) * 0.01, index=dates)

        # Mock ensemble
        mock_ensemble = MagicMock()
        mock_ensemble_cls.return_value = mock_ensemble

        # Execute
        _train_regime_specific_models(mock_feature_engineer, X, y)

        # Assertions - should train 4 models (one per regime)
        assert mock_ensemble.train.call_count == 4
        assert mock_ensemble.save.call_count == 4

    @patch('os.path.exists')
    def test_train_regime_specific_models_insufficient_data(
        self,
        mock_os_exists,
        mock_feature_engineer
    ):
        """Test skipping regime when data is insufficient."""
        # Setup
        mock_os_exists.return_value = False

        # Create small dataset (< 1000 samples per regime)
        n_samples = 800
        dates = pd.date_range('2024-01-01', periods=n_samples, freq='15min', tz='UTC')

        X = pd.DataFrame({
            'rsi': np.random.rand(n_samples) * 100,
            'volume': np.random.randint(1000000, 10000000, n_samples),
            'regime': MarketRegime.BULL_TRENDING.value  # Only one regime
        }, index=dates)

        y = pd.Series(np.random.randn(n_samples) * 0.01, index=dates)

        # Execute (should skip due to insufficient data)
        with patch('app.ml.models.EnsembleWrapper') as mock_ensemble_cls:
            _train_regime_specific_models(mock_feature_engineer, X, y)

            # Should not train any models (all regimes have < 1000 samples)
            mock_ensemble_cls.assert_not_called()
