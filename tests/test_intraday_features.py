"""
Phase L.2a: Tests for intraday features and 15min data collection.

Tests cover:
- RSI(14) computation on 15min bars
- MACD(12,26,9) computation and cross-up detection
- Market hours guard logic
- IntradayIndicators schema properties
- collect_15min_ohlcv task behavior (feature flag, market hours)
"""
from datetime import datetime
from unittest.mock import MagicMock, patch

import numpy as np
from pytz import timezone

from app.domain.schemas.intraday import IntradayIndicators, IntradayIndicatorsSummary
from app.services.intraday_features import (
    MACD_FAST,
    MACD_SIGNAL,
    MACD_SLOW,
    MIN_BARS_REQUIRED,
    RSI_PERIOD,
    compute_all_indicators,
    compute_indicators_for_symbol,
    compute_intraday_indicators,
    is_market_hours,
)


# ──────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────

ET_TZ = timezone("America/New_York")


def _generate_close_prices(n: int = 60, base: float = 100.0, seed: int = 42) -> np.ndarray:
    """Generate synthetic close prices for testing."""
    rng = np.random.default_rng(seed)
    returns = rng.normal(0.0, 0.005, size=n)
    prices = base * np.cumprod(1 + returns)
    return prices.astype(np.float64)


def _generate_declining_prices(n: int = 60, base: float = 100.0) -> np.ndarray:
    """Generate steadily declining prices to produce RSI < 35."""
    decline = np.linspace(0, -0.3, n)
    noise = np.random.default_rng(42).normal(0, 0.002, n)
    prices = base * (1 + decline + noise)
    return prices.astype(np.float64)


# ──────────────────────────────────────────────────────────────
# is_market_hours
# ──────────────────────────────────────────────────────────────


class TestIsMarketHours:
    """Test market hours guard function."""

    def test_weekday_during_market(self) -> None:
        """Monday 10:30 ET → True."""
        dt = ET_TZ.localize(datetime(2026, 1, 5, 10, 30))  # Monday
        assert is_market_hours(dt) is True

    def test_weekday_before_open(self) -> None:
        """Monday 9:00 ET → False (before 9:30)."""
        dt = ET_TZ.localize(datetime(2026, 1, 5, 9, 0))
        assert is_market_hours(dt) is False

    def test_weekday_after_close(self) -> None:
        """Monday 16:30 ET → False (after 16:00)."""
        dt = ET_TZ.localize(datetime(2026, 1, 5, 16, 30))
        assert is_market_hours(dt) is False

    def test_exact_open(self) -> None:
        """Monday 9:30 ET → True."""
        dt = ET_TZ.localize(datetime(2026, 1, 5, 9, 30))
        assert is_market_hours(dt) is True

    def test_exact_close(self) -> None:
        """Monday 16:00 ET → True (inclusive)."""
        dt = ET_TZ.localize(datetime(2026, 1, 5, 16, 0))
        assert is_market_hours(dt) is True

    def test_saturday(self) -> None:
        """Saturday 12:00 ET → False."""
        dt = ET_TZ.localize(datetime(2026, 1, 10, 12, 0))  # Saturday
        assert is_market_hours(dt) is False

    def test_sunday(self) -> None:
        """Sunday 12:00 ET → False."""
        dt = ET_TZ.localize(datetime(2026, 1, 11, 12, 0))  # Sunday
        assert is_market_hours(dt) is False

    def test_naive_datetime_treated_as_local(self) -> None:
        """Naive datetime gets localized to ET."""
        dt = datetime(2026, 1, 5, 12, 0)  # Naive Monday noon
        assert is_market_hours(dt) is True

    def test_none_uses_current_time(self) -> None:
        """None argument doesn't raise — returns bool."""
        result = is_market_hours(None)
        assert isinstance(result, bool)


# ──────────────────────────────────────────────────────────────
# compute_intraday_indicators
# ──────────────────────────────────────────────────────────────


class TestComputeIntradayIndicators:
    """Test RSI + MACD computation from close prices."""

    def test_sufficient_bars(self) -> None:
        """60 bars → all indicators populated."""
        prices = _generate_close_prices(60)
        ts = datetime.now(ET_TZ)

        result = compute_intraday_indicators("AAPL", prices, ts)

        assert result.symbol == "AAPL"
        assert result.rsi_14 is not None
        assert 0.0 <= result.rsi_14 <= 100.0
        assert result.macd_line is not None
        assert result.macd_signal is not None
        assert result.macd_histogram is not None
        assert result.prev_macd_histogram is not None
        assert result.bars_sufficient is True

    def test_insufficient_bars(self) -> None:
        """10 bars → all indicators None."""
        prices = _generate_close_prices(10)
        ts = datetime.now(ET_TZ)

        result = compute_intraday_indicators("AAPL", prices, ts)

        assert result.rsi_14 is None
        assert result.macd_histogram is None
        assert result.bars_sufficient is False

    def test_exact_minimum_bars(self) -> None:
        """Exactly MIN_BARS_REQUIRED bars → indicators computed."""
        prices = _generate_close_prices(MIN_BARS_REQUIRED)
        ts = datetime.now(ET_TZ)

        result = compute_intraday_indicators("AAPL", prices, ts)
        # MACD(26) needs 25 bars before producing first value,
        # plus signal(9) needs 8 more = 33 bars minimum for MACD
        # With 50 bars, MACD should be computed
        assert result.bars_sufficient is True

    def test_rsi_range(self) -> None:
        """RSI must be in [0, 100]."""
        prices = _generate_close_prices(100, seed=99)
        ts = datetime.now(ET_TZ)

        result = compute_intraday_indicators("TEST", prices, ts)

        assert result.rsi_14 is not None
        assert 0.0 <= result.rsi_14 <= 100.0

    def test_macd_histogram_consistency(self) -> None:
        """MACD histogram = MACD line - MACD signal."""
        prices = _generate_close_prices(100)
        ts = datetime.now(ET_TZ)

        result = compute_intraday_indicators("TEST", prices, ts)

        assert result.macd_line is not None
        assert result.macd_signal is not None
        assert result.macd_histogram is not None
        expected_hist = result.macd_line - result.macd_signal
        assert abs(result.macd_histogram - expected_hist) < 1e-8

    def test_declining_prices_low_rsi(self) -> None:
        """Declining prices should produce low RSI."""
        prices = _generate_declining_prices(80)
        ts = datetime.now(ET_TZ)

        result = compute_intraday_indicators("WEAK", prices, ts)

        assert result.rsi_14 is not None
        # Strongly declining prices → RSI should be low
        assert result.rsi_14 < 50.0


# ──────────────────────────────────────────────────────────────
# IntradayIndicators schema properties
# ──────────────────────────────────────────────────────────────


class TestIntradayIndicatorsProperties:
    """Test schema computed properties."""

    def test_is_rsi_oversold_true(self) -> None:
        ind = IntradayIndicators(
            symbol="TEST", timestamp=datetime.now(ET_TZ), rsi_14=30.0
        )
        assert ind.is_rsi_oversold is True

    def test_is_rsi_oversold_false(self) -> None:
        ind = IntradayIndicators(
            symbol="TEST", timestamp=datetime.now(ET_TZ), rsi_14=50.0
        )
        assert ind.is_rsi_oversold is False

    def test_is_rsi_oversold_none(self) -> None:
        ind = IntradayIndicators(
            symbol="TEST", timestamp=datetime.now(ET_TZ), rsi_14=None
        )
        assert ind.is_rsi_oversold is False

    def test_is_rsi_oversold_boundary(self) -> None:
        """RSI exactly 35 → NOT oversold (strict less-than)."""
        ind = IntradayIndicators(
            symbol="TEST", timestamp=datetime.now(ET_TZ), rsi_14=35.0
        )
        assert ind.is_rsi_oversold is False

    def test_macd_cross_up_true(self) -> None:
        ind = IntradayIndicators(
            symbol="TEST",
            timestamp=datetime.now(ET_TZ),
            macd_histogram=0.01,
            prev_macd_histogram=-0.02,
        )
        assert ind.is_macd_cross_up is True

    def test_macd_cross_up_false_both_positive(self) -> None:
        ind = IntradayIndicators(
            symbol="TEST",
            timestamp=datetime.now(ET_TZ),
            macd_histogram=0.01,
            prev_macd_histogram=0.005,
        )
        assert ind.is_macd_cross_up is False

    def test_macd_cross_up_false_both_negative(self) -> None:
        ind = IntradayIndicators(
            symbol="TEST",
            timestamp=datetime.now(ET_TZ),
            macd_histogram=-0.01,
            prev_macd_histogram=-0.02,
        )
        assert ind.is_macd_cross_up is False

    def test_macd_cross_up_from_zero(self) -> None:
        """Previous exactly 0, current > 0 → cross-up."""
        ind = IntradayIndicators(
            symbol="TEST",
            timestamp=datetime.now(ET_TZ),
            macd_histogram=0.001,
            prev_macd_histogram=0.0,
        )
        assert ind.is_macd_cross_up is True

    def test_macd_cross_up_none_histogram(self) -> None:
        ind = IntradayIndicators(
            symbol="TEST",
            timestamp=datetime.now(ET_TZ),
            macd_histogram=None,
            prev_macd_histogram=-0.01,
        )
        assert ind.is_macd_cross_up is False

    def test_has_entry_signal_true(self) -> None:
        """Both conditions met → entry signal."""
        ind = IntradayIndicators(
            symbol="TEST",
            timestamp=datetime.now(ET_TZ),
            rsi_14=25.0,
            macd_histogram=0.01,
            prev_macd_histogram=-0.005,
        )
        assert ind.has_entry_signal is True

    def test_has_entry_signal_only_rsi(self) -> None:
        """RSI oversold but no MACD cross → no signal."""
        ind = IntradayIndicators(
            symbol="TEST",
            timestamp=datetime.now(ET_TZ),
            rsi_14=25.0,
            macd_histogram=0.01,
            prev_macd_histogram=0.005,
        )
        assert ind.has_entry_signal is False

    def test_has_entry_signal_only_macd(self) -> None:
        """MACD cross but RSI not oversold → no signal."""
        ind = IntradayIndicators(
            symbol="TEST",
            timestamp=datetime.now(ET_TZ),
            rsi_14=50.0,
            macd_histogram=0.01,
            prev_macd_histogram=-0.005,
        )
        assert ind.has_entry_signal is False

    def test_bars_sufficient_true(self) -> None:
        ind = IntradayIndicators(
            symbol="TEST",
            timestamp=datetime.now(ET_TZ),
            rsi_14=50.0,
            macd_histogram=0.01,
        )
        assert ind.bars_sufficient is True

    def test_bars_sufficient_false(self) -> None:
        ind = IntradayIndicators(
            symbol="TEST", timestamp=datetime.now(ET_TZ)
        )
        assert ind.bars_sufficient is False


# ──────────────────────────────────────────────────────────────
# IntradayIndicatorsSummary
# ──────────────────────────────────────────────────────────────


class TestIntradayIndicatorsSummary:
    """Test batch summary schema."""

    def test_empty_summary(self) -> None:
        summary = IntradayIndicatorsSummary(
            timestamp=datetime.now(ET_TZ),
            total_symbols=0,
            signals_found=0,
            indicators=[],
        )
        assert summary.total_symbols == 0
        assert len(summary.indicators) == 0

    def test_summary_with_indicators(self) -> None:
        ind1 = IntradayIndicators(
            symbol="AAPL",
            timestamp=datetime.now(ET_TZ),
            rsi_14=30.0,
            macd_histogram=0.01,
            prev_macd_histogram=-0.01,
        )
        ind2 = IntradayIndicators(
            symbol="MSFT",
            timestamp=datetime.now(ET_TZ),
            rsi_14=55.0,
            macd_histogram=0.01,
        )
        summary = IntradayIndicatorsSummary(
            timestamp=datetime.now(ET_TZ),
            total_symbols=2,
            signals_found=1,
            indicators=[ind1, ind2],
        )
        assert summary.total_symbols == 2
        assert summary.signals_found == 1
        assert summary.indicators[0].has_entry_signal is True
        assert summary.indicators[1].has_entry_signal is False


# ──────────────────────────────────────────────────────────────
# compute_indicators_for_symbol (DB interaction mock)
# ──────────────────────────────────────────────────────────────


class TestComputeIndicatorsForSymbol:
    """Test DB-backed indicator computation with mocked repo."""

    @patch("app.services.intraday_features.SyncStockRepository")
    def test_no_bars_returns_empty(self, mock_repo_cls: MagicMock) -> None:
        """No 15min bars → all indicators None."""
        mock_db = MagicMock()
        mock_repo = mock_repo_cls.return_value
        mock_repo.get_ohlcv_range.return_value = []

        result = compute_indicators_for_symbol("AAPL", mock_db)

        assert result.symbol == "AAPL"
        assert result.rsi_14 is None
        assert result.bars_sufficient is False

    @patch("app.services.intraday_features.SyncStockRepository")
    def test_sufficient_bars_returns_indicators(self, mock_repo_cls: MagicMock) -> None:
        """60 bars → indicators computed."""
        mock_db = MagicMock()
        mock_repo = mock_repo_cls.return_value

        # Create mock bars
        mock_bars = []
        prices = _generate_close_prices(60)
        for price in prices:
            bar = MagicMock()
            bar.close = price
            mock_bars.append(bar)

        mock_repo.get_ohlcv_range.return_value = mock_bars

        result = compute_indicators_for_symbol("AAPL", mock_db)

        assert result.symbol == "AAPL"
        assert result.rsi_14 is not None
        assert result.macd_histogram is not None
        assert result.bars_sufficient is True


# ──────────────────────────────────────────────────────────────
# compute_all_indicators (batch)
# ──────────────────────────────────────────────────────────────


class TestComputeAllIndicators:
    """Test batch indicator computation."""

    @patch("app.services.intraday_features.SyncStockRepository")
    def test_no_active_symbols(self, mock_repo_cls: MagicMock) -> None:
        """No active symbols → empty summary."""
        mock_db = MagicMock()
        mock_repo = mock_repo_cls.return_value
        mock_repo.get_active_symbols.return_value = []

        result = compute_all_indicators(mock_db)

        assert result.total_symbols == 0
        assert result.signals_found == 0
        assert len(result.indicators) == 0

    @patch("app.services.intraday_features.SyncStockRepository")
    def test_with_specific_symbols(self, mock_repo_cls: MagicMock) -> None:
        """Specific symbols list processed correctly."""
        mock_db = MagicMock()
        mock_repo = mock_repo_cls.return_value
        mock_repo.get_ohlcv_range.return_value = []  # No bars → empty indicators

        result = compute_all_indicators(mock_db, symbols=["AAPL", "MSFT"])

        assert result.total_symbols == 2
        assert len(result.indicators) == 2
        assert result.indicators[0].symbol == "AAPL"
        assert result.indicators[1].symbol == "MSFT"


# ──────────────────────────────────────────────────────────────
# collect_15min_ohlcv task
# ──────────────────────────────────────────────────────────────


class TestCollect15minOhlcvTask:
    """Test the Celery task function logic (without Celery runtime)."""

    @patch("app.tasks.realtime_data.settings")
    def test_feature_flag_disabled(self, mock_settings: MagicMock) -> None:
        """DUAL_TIMEFRAME_ENABLED=False → skip."""
        mock_settings.DUAL_TIMEFRAME_ENABLED = False

        from app.tasks.realtime_data import collect_15min_ohlcv

        # Call the underlying function (bypass Celery decorator)
        result = collect_15min_ohlcv()
        assert result["status"] == "skipped"
        assert result["reason"] == "feature_flag_disabled"

    @patch("app.tasks.realtime_data.is_market_hours", return_value=False)
    @patch("app.tasks.realtime_data.settings")
    def test_outside_market_hours(
        self, mock_settings: MagicMock, mock_market: MagicMock
    ) -> None:
        """Outside market hours → skip."""
        mock_settings.DUAL_TIMEFRAME_ENABLED = True

        from app.tasks.realtime_data import collect_15min_ohlcv

        result = collect_15min_ohlcv()
        assert result["status"] == "skipped"
        assert result["reason"] == "outside_market_hours"


# ──────────────────────────────────────────────────────────────
# Module constants
# ──────────────────────────────────────────────────────────────


class TestModuleConstants:
    """Verify module constants match L.2 plan specification."""

    def test_min_bars(self) -> None:
        assert MIN_BARS_REQUIRED == 50

    def test_rsi_period(self) -> None:
        assert RSI_PERIOD == 14

    def test_macd_params(self) -> None:
        assert MACD_FAST == 12
        assert MACD_SLOW == 26
        assert MACD_SIGNAL == 9
