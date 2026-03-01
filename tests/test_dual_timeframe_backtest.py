"""Comprehensive unit tests for the L.3 Backtesting Validation module.

Covers:
    - ``backtest_types.py`` — Pydantic schema validation and computed fields.
    - ``dual_timeframe_backtester.py`` — Daily-only / hybrid simulation logic,
      metric helpers, and position management.
    - ``comparison_runner.py`` — Side-by-side comparison, cost sensitivity, and
      report formatting.

All external dependencies (DB, PredictorService, FeatureEngineer, RegimeDetector)
are mocked to ensure deterministic, isolated test runs.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from app.backtest.backtest_types import (
    BacktestConfig,
    BacktestMetrics,
    BacktestResult,
    CostSensitivityPoint,
    DayPrediction,
    TradeRecord,
)
from app.backtest.comparison_runner import ComparisonRunner
from app.backtest.dual_timeframe_backtester import (
    DualTimeframeBacktester,
    _is_nan,
    _max_drawdown,
    _std,
)

# ---------------------------------------------------------------------------
# Factory helpers
# ---------------------------------------------------------------------------

_BASE_DATE = datetime(2025, 1, 1)


def _make_prediction(
    symbol: str = "AAPL",
    predicted_class: int = 2,
    confidence: float = 0.60,
    regime: str = "sideways_calm",
    threshold: float = 0.40,
    close_price: float = 150.0,
    rsi_14: float | None = 30.0,
    macd_histogram: float | None = 0.5,
    prev_macd_histogram: float | None = -0.1,
    date: datetime | None = None,
) -> DayPrediction:
    """Create a ``DayPrediction`` with sensible defaults for testing."""
    return DayPrediction(
        date=date or _BASE_DATE,
        symbol=symbol,
        predicted_class=predicted_class,
        confidence=confidence,
        regime=regime,
        threshold=threshold,
        close_price=close_price,
        rsi_14=rsi_14,
        macd_histogram=macd_histogram,
        prev_macd_histogram=prev_macd_histogram,
    )


def _make_trade(
    symbol: str = "AAPL",
    entry_price: float = 100.0,
    exit_price: float = 110.0,
    quantity: int = 10,
    gross_pnl: float = 100.0,
    commission_cost: float = 5.0,
    net_pnl: float = 95.0,
    holding_days: int = 3,
    entry_reason: str = "ml_buy_sideways_calm",
    exit_reason: str = "signal_down",
    entry_date: datetime | None = None,
    exit_date: datetime | None = None,
) -> TradeRecord:
    """Create a ``TradeRecord`` with sensible defaults for testing."""
    return TradeRecord(
        symbol=symbol,
        entry_date=entry_date or _BASE_DATE,
        exit_date=exit_date or _BASE_DATE + timedelta(days=holding_days),
        entry_price=entry_price,
        exit_price=exit_price,
        quantity=quantity,
        direction="long",
        entry_reason=entry_reason,
        exit_reason=exit_reason,
        gross_pnl=gross_pnl,
        commission_cost=commission_cost,
        net_pnl=net_pnl,
        holding_days=holding_days,
    )


def _make_config(
    mode: str = "daily_only",
    start_date: datetime | None = None,
    end_date: datetime | None = None,
    initial_cash: float = 100_000.0,
    commission: float = 0.001,
    slippage_bps: float = 5.0,
    max_positions: int = 5,
    risk_per_trade: float = 0.02,
    trailing_stop_pct: float = 0.015,
) -> BacktestConfig:
    """Create a ``BacktestConfig`` with sensible defaults for testing."""
    return BacktestConfig(
        mode=mode,
        initial_cash=initial_cash,
        commission=commission,
        slippage_bps=slippage_bps,
        start_date=start_date or _BASE_DATE,
        end_date=end_date or _BASE_DATE + timedelta(days=90),
        max_positions=max_positions,
        risk_per_trade=risk_per_trade,
        trailing_stop_pct=trailing_stop_pct,
    )


def _make_metrics(
    total_return_pct: float = 10.0,
    sharpe_ratio: float = 1.0,
    max_drawdown_pct: float = 5.0,
    win_rate_pct: float = 60.0,
    profit_factor: float = 2.0,
    total_trades: int = 10,
    avg_holding_days: float = 3.0,
    avg_pnl_pct: float = 1.0,
    best_trade_pct: float = 5.0,
    worst_trade_pct: float = -2.0,
) -> BacktestMetrics:
    """Create a ``BacktestMetrics`` with sensible defaults for testing."""
    return BacktestMetrics(
        total_return_pct=total_return_pct,
        sharpe_ratio=sharpe_ratio,
        max_drawdown_pct=max_drawdown_pct,
        win_rate_pct=win_rate_pct,
        profit_factor=profit_factor,
        total_trades=total_trades,
        avg_holding_days=avg_holding_days,
        avg_pnl_pct=avg_pnl_pct,
        best_trade_pct=best_trade_pct,
        worst_trade_pct=worst_trade_pct,
    )


def _make_result(
    mode: str = "daily_only",
    total_return_pct: float = 10.0,
    sharpe_ratio: float = 1.0,
    max_drawdown_pct: float = 5.0,
    win_rate_pct: float = 60.0,
    profit_factor: float = 2.0,
    total_trades: int = 10,
) -> BacktestResult:
    """Create a ``BacktestResult`` with sensible defaults for testing."""
    cfg = _make_config(mode=mode)
    metrics = _make_metrics(
        total_return_pct=total_return_pct,
        sharpe_ratio=sharpe_ratio,
        max_drawdown_pct=max_drawdown_pct,
        win_rate_pct=win_rate_pct,
        profit_factor=profit_factor,
        total_trades=total_trades,
    )
    return BacktestResult(
        mode=mode,
        config=cfg,
        symbols=["AAPL"],
        metrics=metrics,
        trades=[],
        equity_curve=[100_000.0],
        daily_returns=[],
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def backtester() -> DualTimeframeBacktester:
    """Create a ``DualTimeframeBacktester`` with all dependencies mocked.

    Uses ``__new__`` to bypass ``__init__``, then manually assigns mock
    attributes.
    """
    bt = object.__new__(DualTimeframeBacktester)
    bt._config = _make_config()
    bt._db = MagicMock()
    bt._repo = MagicMock()
    bt._predictor = MagicMock()
    bt._feature_engineer = MagicMock()
    bt._regime_detector = MagicMock()
    return bt


@pytest.fixture()
def hybrid_backtester() -> DualTimeframeBacktester:
    """Create a hybrid-mode ``DualTimeframeBacktester`` with mocked deps."""
    bt = object.__new__(DualTimeframeBacktester)
    bt._config = _make_config(mode="hybrid")
    bt._db = MagicMock()
    bt._repo = MagicMock()
    bt._predictor = MagicMock()
    bt._feature_engineer = MagicMock()
    bt._regime_detector = MagicMock()
    return bt


# ===================================================================
# Class 1: TestBacktestTypes
# ===================================================================


class TestBacktestTypes:
    """Tests for Pydantic schemas and computed fields in ``backtest_types``."""

    def test_backtest_config_defaults(self) -> None:
        """Verify default values and ``round_trip_cost`` computed field.

        round_trip_cost = (commission + slippage_bps / 10_000) * 2
                        = (0.001 + 5 / 10_000) * 2
                        = (0.001 + 0.0005) * 2
                        = 0.003
        """
        # Arrange / Act
        config = _make_config()

        # Assert
        assert config.initial_cash == 100_000.0
        assert config.commission == 0.001
        assert config.slippage_bps == 5.0
        assert config.max_positions == 5
        assert config.risk_per_trade == 0.02
        assert config.trailing_stop_pct == 0.015
        assert config.round_trip_cost == pytest.approx(0.003)

    def test_day_prediction_buy_signal(self) -> None:
        """predicted_class=2 with confidence >= threshold → is_buy_signal True."""
        # Arrange
        pred = _make_prediction(predicted_class=2, confidence=0.60, threshold=0.40)

        # Act / Assert
        assert pred.is_buy_signal is True
        assert pred.is_sell_signal is False

    def test_day_prediction_sell_signal(self) -> None:
        """predicted_class=0 with confidence >= threshold → is_sell_signal True."""
        # Arrange
        pred = _make_prediction(predicted_class=0, confidence=0.55, threshold=0.55)

        # Act / Assert
        assert pred.is_sell_signal is True
        assert pred.is_buy_signal is False

    def test_day_prediction_intraday_approx(self) -> None:
        """RSI < 40 and MACD cross-up → has_intraday_entry_approx True; RSI=45 → False."""
        # Arrange — positive case: rsi=30, macd crosses from -0.1 to +0.5
        pred_yes = _make_prediction(
            rsi_14=30.0,
            macd_histogram=0.5,
            prev_macd_histogram=-0.1,
        )
        # Arrange — negative case: rsi > 40
        pred_no = _make_prediction(
            rsi_14=45.0,
            macd_histogram=0.5,
            prev_macd_histogram=-0.1,
        )
        # Arrange — negative case: missing indicator
        pred_missing = _make_prediction(rsi_14=None)

        # Act / Assert
        assert pred_yes.has_intraday_entry_approx is True
        assert pred_no.has_intraday_entry_approx is False
        assert pred_missing.has_intraday_entry_approx is False

    def test_trade_record_computed_fields(self) -> None:
        """pnl_pct and is_winner are computed correctly from trade data."""
        # Arrange — winning trade: entry=100, qty=10, net_pnl=95
        winner = _make_trade(entry_price=100.0, quantity=10, net_pnl=95.0)
        # notional = 100 * 10 = 1000; pnl_pct = 95/1000 * 100 = 9.5%

        # Arrange — losing trade
        loser = _make_trade(entry_price=100.0, quantity=10, net_pnl=-50.0)

        # Assert
        assert winner.pnl_pct == pytest.approx(9.5)
        assert winner.is_winner is True
        assert loser.pnl_pct == pytest.approx(-5.0)
        assert loser.is_winner is False


# ===================================================================
# Class 2: TestMetricHelpers
# ===================================================================


class TestMetricHelpers:
    """Tests for module-level helper functions in ``dual_timeframe_backtester``."""

    def test_is_nan(self) -> None:
        """True for float('nan') and np.nan; False for 0.0 and None."""
        assert _is_nan(float("nan")) is True
        assert _is_nan(np.nan) is True
        assert _is_nan(0.0) is False
        assert _is_nan(None) is False
        assert _is_nan(42) is False

    def test_std_normal(self) -> None:
        """Sample standard deviation matches manual calculation for known values."""
        # Arrange
        values = [2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0]
        # mean = 40/8 = 5.0
        # variance (sample) = sum((v-5)^2) / 7
        #   = (9+1+1+1+0+0+4+16) / 7 = 32/7
        expected_std = math.sqrt(32 / 7)

        # Act
        result = _std(values)

        # Assert
        assert result == pytest.approx(expected_std, rel=1e-6)

    def test_std_empty(self) -> None:
        """Empty or single-element lists return 0.0."""
        assert _std([]) == 0.0
        assert _std([42.0]) == 0.0

    def test_max_drawdown(self) -> None:
        """[100, 110, 90, 100] → max drawdown = (110-90)/110*100 ≈ 18.18%."""
        # Arrange
        equity = [100.0, 110.0, 90.0, 100.0]

        # Act
        result = _max_drawdown(equity)

        # Assert — peak=110, trough=90 → dd = 20/110*100
        assert result == pytest.approx(20.0 / 110.0 * 100, rel=1e-6)


# ===================================================================
# Class 3: TestDualTimeframeBacktesterDailyOnly
# ===================================================================


class TestDualTimeframeBacktesterDailyOnly:
    """Tests for daily-only simulation mode of ``DualTimeframeBacktester``.

    All external dependencies (repo, predictor, feature engineer, regime
    detector) are mocked via the ``backtester`` fixture.
    """

    def test_run_insufficient_bars(self, backtester: DualTimeframeBacktester) -> None:
        """Less than 60 bars → returns empty result with zero metrics."""
        # Arrange — repo returns < 60 bars
        backtester._repo.get_ohlcv_range.return_value = [MagicMock()] * 30

        # Act
        result = backtester.run("AAPL")

        # Assert
        assert result.trades == []
        assert result.metrics.total_trades == 0
        assert result.metrics.total_return_pct == 0.0

    def test_run_no_predictor(self) -> None:
        """Predictor initialisation failure → returns empty result."""
        # Arrange — bypass __init__, set predictor to None
        bt = object.__new__(DualTimeframeBacktester)
        bt._config = _make_config()
        bt._db = MagicMock()
        bt._repo = MagicMock()
        bt._predictor = None
        bt._feature_engineer = MagicMock()
        bt._regime_detector = MagicMock()

        # Act
        result = bt.run("AAPL")

        # Assert
        assert result.trades == []
        assert result.metrics.total_trades == 0

    def test_simulate_daily_buy_and_sell(
        self, backtester: DualTimeframeBacktester
    ) -> None:
        """Buy on day 0, hold day 1, sell on day 2 → 1 completed trade.

        Predictions:
            T0: BUY signal  → entry at T1 close (100.0)
            T1: HOLD        → no action
            T2: SELL signal → exit at T3 close (110.0)
            T3: final day   (execution target for T2 exit)

        With sideways_calm regime, min_hold_days=2.
        Entry at T1 means holding to T3 = 2 days, which satisfies min_hold.
        """
        # Arrange
        predictions = [
            _make_prediction(  # T0 — BUY
                predicted_class=2,
                confidence=0.60,
                threshold=0.40,
                close_price=95.0,
                date=_BASE_DATE,
                regime="sideways_calm",
            ),
            _make_prediction(  # T1 — HOLD (entry execution day)
                predicted_class=1,
                confidence=0.30,
                threshold=0.40,
                close_price=100.0,
                date=_BASE_DATE + timedelta(days=1),
                regime="sideways_calm",
            ),
            _make_prediction(  # T2 — SELL
                predicted_class=0,
                confidence=0.60,
                threshold=0.40,
                close_price=105.0,
                date=_BASE_DATE + timedelta(days=2),
                regime="sideways_calm",
            ),
            _make_prediction(  # T3 — execution day for sell
                predicted_class=1,
                confidence=0.30,
                threshold=0.40,
                close_price=110.0,
                date=_BASE_DATE + timedelta(days=3),
                regime="sideways_calm",
            ),
        ]

        # Act
        trades, equity_curve = backtester._simulate_daily_only(predictions)

        # Assert — exactly 1 trade produced
        assert len(trades) == 1
        trade = trades[0]
        assert trade.entry_price == 100.0  # T1 close
        assert trade.exit_price == 110.0  # T3 close
        assert trade.entry_reason == "ml_buy_sideways_calm"
        assert trade.exit_reason == "signal_down"
        assert trade.net_pnl > 0  # profitable trade

    def test_simulate_daily_max_positions(
        self, backtester: DualTimeframeBacktester
    ) -> None:
        """max_positions=1 with two buy signals → only 1 position opened."""
        # Arrange
        backtester._config = _make_config(max_positions=1)
        predictions = [
            _make_prediction(  # T0 — BUY AAPL
                symbol="AAPL",
                predicted_class=2,
                confidence=0.60,
                threshold=0.40,
                close_price=100.0,
                date=_BASE_DATE,
            ),
            _make_prediction(  # T1 — BUY MSFT (should be blocked)
                symbol="MSFT",
                predicted_class=2,
                confidence=0.60,
                threshold=0.40,
                close_price=200.0,
                date=_BASE_DATE + timedelta(days=1),
            ),
            _make_prediction(  # T2 — final day
                symbol="AAPL",
                predicted_class=1,
                confidence=0.30,
                threshold=0.40,
                close_price=100.0,
                date=_BASE_DATE + timedelta(days=2),
            ),
        ]

        # Act
        trades, _ = backtester._simulate_daily_only(predictions)

        # Assert — force-closed at end, but only 1 position was ever opened
        # All trades should be for AAPL
        assert all(t.symbol == "AAPL" for t in trades)

    def test_simulate_daily_min_hold_guard(
        self, backtester: DualTimeframeBacktester
    ) -> None:
        """Sell signal on day 1 but min_hold_days=2 → should NOT exit early.

        sideways_calm has min_hold_days=2. Entry on T1, sell signal on T1
        with holding=0 days should be blocked.
        """
        # Arrange
        predictions = [
            _make_prediction(  # T0 — BUY
                predicted_class=2,
                confidence=0.60,
                threshold=0.40,
                close_price=100.0,
                date=_BASE_DATE,
                regime="sideways_calm",
            ),
            _make_prediction(  # T1 — SELL signal (too early)
                predicted_class=0,
                confidence=0.60,
                threshold=0.40,
                close_price=105.0,
                date=_BASE_DATE + timedelta(days=1),
                regime="sideways_calm",
            ),
            _make_prediction(  # T2 — final day
                predicted_class=1,
                confidence=0.30,
                threshold=0.40,
                close_price=110.0,
                date=_BASE_DATE + timedelta(days=2),
                regime="sideways_calm",
            ),
        ]

        # Act
        trades, _ = backtester._simulate_daily_only(predictions)

        # Assert — the sell at T1 was blocked (holding < 2 days).
        # Position force-closed at end → exit_reason = "backtest_end"
        assert len(trades) == 1
        assert trades[0].exit_reason == "backtest_end"

    def test_simulate_daily_force_close(
        self, backtester: DualTimeframeBacktester
    ) -> None:
        """Position still open at backtest end → force closed with 'backtest_end'."""
        # Arrange — only buy signals, no sell
        predictions = [
            _make_prediction(  # T0 — BUY
                predicted_class=2,
                confidence=0.60,
                threshold=0.40,
                close_price=100.0,
                date=_BASE_DATE,
            ),
            _make_prediction(  # T1 — HOLD
                predicted_class=1,
                confidence=0.30,
                threshold=0.40,
                close_price=105.0,
                date=_BASE_DATE + timedelta(days=1),
            ),
            _make_prediction(  # T2 — HOLD (last day)
                predicted_class=1,
                confidence=0.30,
                threshold=0.40,
                close_price=110.0,
                date=_BASE_DATE + timedelta(days=2),
            ),
        ]

        # Act
        trades, _ = backtester._simulate_daily_only(predictions)

        # Assert
        assert len(trades) == 1
        assert trades[0].exit_reason == "backtest_end"


# ===================================================================
# Class 4: TestDualTimeframeBacktesterHybrid
# ===================================================================


class TestDualTimeframeBacktesterHybrid:
    """Tests for hybrid simulation mode of ``DualTimeframeBacktester``.

    Hybrid mode adds an intraday entry filter (RSI + MACD) and trailing-stop
    exit logic on top of daily ML signals.
    """

    def test_hybrid_entry_with_intraday_filter(
        self, hybrid_backtester: DualTimeframeBacktester
    ) -> None:
        """Buy signal + has_intraday_entry_approx True → position entered."""
        # Arrange — RSI < 40, MACD cross-up → intraday approx passes
        hybrid_backtester._repo.get_ohlcv_range.return_value = []  # no 15m bars → fallback
        predictions = [
            _make_prediction(  # T0 — BUY + intraday OK
                predicted_class=2,
                confidence=0.60,
                threshold=0.40,
                close_price=100.0,
                rsi_14=30.0,
                macd_histogram=0.5,
                prev_macd_histogram=-0.1,
                date=_BASE_DATE,
            ),
            _make_prediction(  # T1 — HOLD
                predicted_class=1,
                confidence=0.30,
                threshold=0.40,
                close_price=105.0,
                date=_BASE_DATE + timedelta(days=1),
            ),
            _make_prediction(  # T2 — final
                predicted_class=1,
                confidence=0.30,
                threshold=0.40,
                close_price=110.0,
                date=_BASE_DATE + timedelta(days=2),
            ),
        ]

        # Act
        trades, _ = hybrid_backtester._simulate_hybrid(predictions)

        # Assert — position was entered and force-closed
        assert len(trades) == 1
        assert trades[0].entry_reason.startswith("hybrid_buy_")

    def test_hybrid_entry_blocked_by_intraday(
        self, hybrid_backtester: DualTimeframeBacktester
    ) -> None:
        """Buy signal + has_intraday_entry_approx False → NO entry."""
        # Arrange — RSI > 40 → intraday approx fails
        hybrid_backtester._repo.get_ohlcv_range.return_value = []  # no 15m bars
        predictions = [
            _make_prediction(  # T0 — BUY but intraday FAILS (RSI > 40)
                predicted_class=2,
                confidence=0.60,
                threshold=0.40,
                close_price=100.0,
                rsi_14=50.0,  # > 40 → intraday filter blocks
                macd_histogram=0.5,
                prev_macd_histogram=-0.1,
                date=_BASE_DATE,
            ),
            _make_prediction(  # T1 — final
                predicted_class=1,
                confidence=0.30,
                threshold=0.40,
                close_price=105.0,
                date=_BASE_DATE + timedelta(days=1),
            ),
        ]

        # Act
        trades, _ = hybrid_backtester._simulate_hybrid(predictions)

        # Assert — no trades (entry was blocked, nothing to force-close)
        assert len(trades) == 0

    def test_hybrid_trailing_stop_exit(
        self, hybrid_backtester: DualTimeframeBacktester
    ) -> None:
        """Price drops below peak × (1 - 0.015) → exit with 'trailing_stop'.

        Entry at 100.0, peak stays at 100.0.
        Trailing stop triggers at 100 * (1 - 0.015) = 98.5.
        If next close = 98.0 → trailing stop fires.
        """
        # Arrange
        hybrid_backtester._repo.get_ohlcv_range.return_value = []
        predictions = [
            _make_prediction(  # T0 — BUY
                predicted_class=2,
                confidence=0.60,
                threshold=0.40,
                close_price=100.0,
                rsi_14=30.0,
                macd_histogram=0.5,
                prev_macd_histogram=-0.1,
                date=_BASE_DATE,
            ),
            _make_prediction(  # T1 — entry execution
                predicted_class=1,
                confidence=0.30,
                threshold=0.40,
                close_price=100.0,
                date=_BASE_DATE + timedelta(days=1),
            ),
            _make_prediction(  # T2 — price drops → trailing stop
                predicted_class=1,
                confidence=0.30,
                threshold=0.40,
                close_price=98.0,  # below 98.5 stop
                date=_BASE_DATE + timedelta(days=2),
            ),
            _make_prediction(  # T3 — execution day for trailing stop exit
                predicted_class=1,
                confidence=0.30,
                threshold=0.40,
                close_price=97.0,
                date=_BASE_DATE + timedelta(days=3),
            ),
        ]

        # Act
        trades, _ = hybrid_backtester._simulate_hybrid(predictions)

        # Assert — should have exited via trailing stop
        trailing_trades = [t for t in trades if t.exit_reason == "trailing_stop"]
        assert len(trailing_trades) >= 1

    def test_hybrid_signal_down_exit(
        self, hybrid_backtester: DualTimeframeBacktester
    ) -> None:
        """Sell signal with min_hold met → exit with 'signal_down'."""
        # Arrange — bull_trending has min_hold_days=2
        hybrid_backtester._repo.get_ohlcv_range.return_value = []
        predictions = [
            _make_prediction(  # T0 — BUY
                predicted_class=2,
                confidence=0.60,
                threshold=0.40,
                close_price=100.0,
                rsi_14=30.0,
                macd_histogram=0.5,
                prev_macd_histogram=-0.1,
                date=_BASE_DATE,
                regime="sideways_calm",
            ),
            _make_prediction(  # T1 — entry execution
                predicted_class=1,
                confidence=0.30,
                threshold=0.40,
                close_price=102.0,
                date=_BASE_DATE + timedelta(days=1),
                regime="sideways_calm",
            ),
            _make_prediction(  # T2 — price holds above stop
                predicted_class=1,
                confidence=0.30,
                threshold=0.40,
                close_price=103.0,
                date=_BASE_DATE + timedelta(days=2),
                regime="sideways_calm",
            ),
            _make_prediction(  # T3 — SELL signal, holding = 2 days → exit
                predicted_class=0,
                confidence=0.60,
                threshold=0.40,
                close_price=101.0,
                date=_BASE_DATE + timedelta(days=3),
                regime="sideways_calm",
            ),
            _make_prediction(  # T4 — execution day for sell
                predicted_class=1,
                confidence=0.30,
                threshold=0.40,
                close_price=100.0,
                date=_BASE_DATE + timedelta(days=4),
                regime="sideways_calm",
            ),
        ]

        # Act
        trades, _ = hybrid_backtester._simulate_hybrid(predictions)

        # Assert
        signal_trades = [t for t in trades if t.exit_reason == "signal_down"]
        assert len(signal_trades) >= 1

    def test_hybrid_15min_actual_data(
        self, hybrid_backtester: DualTimeframeBacktester
    ) -> None:
        """When _check_actual_intraday returns True → entry proceeds."""
        # Arrange — patch _check_actual_intraday to return True
        with patch.object(
            hybrid_backtester, "_check_actual_intraday", return_value=True
        ):
            predictions = [
                _make_prediction(  # T0 — BUY
                    predicted_class=2,
                    confidence=0.60,
                    threshold=0.40,
                    close_price=100.0,
                    rsi_14=50.0,  # RSI > 40 → approx would FAIL
                    macd_histogram=0.5,
                    prev_macd_histogram=-0.1,
                    date=_BASE_DATE,
                ),
                _make_prediction(  # T1 — HOLD
                    predicted_class=1,
                    confidence=0.30,
                    threshold=0.40,
                    close_price=105.0,
                    date=_BASE_DATE + timedelta(days=1),
                ),
                _make_prediction(  # T2 — final
                    predicted_class=1,
                    confidence=0.30,
                    threshold=0.40,
                    close_price=110.0,
                    date=_BASE_DATE + timedelta(days=2),
                ),
            ]

            # Act
            trades, _ = hybrid_backtester._simulate_hybrid(predictions)

        # Assert — entry succeeded despite approx failing (actual 15m data used)
        assert len(trades) == 1

    def test_hybrid_15min_fallback_to_approx(
        self, hybrid_backtester: DualTimeframeBacktester
    ) -> None:
        """When _check_actual_intraday returns None → falls back to has_intraday_entry_approx."""
        # Arrange — patch _check_actual_intraday to return None (no 15m data)
        with patch.object(
            hybrid_backtester, "_check_actual_intraday", return_value=None
        ):
            predictions = [
                _make_prediction(  # T0 — BUY + intraday approx OK
                    predicted_class=2,
                    confidence=0.60,
                    threshold=0.40,
                    close_price=100.0,
                    rsi_14=30.0,  # RSI < 40 → approx passes
                    macd_histogram=0.5,
                    prev_macd_histogram=-0.1,
                    date=_BASE_DATE,
                ),
                _make_prediction(  # T1 — HOLD
                    predicted_class=1,
                    confidence=0.30,
                    threshold=0.40,
                    close_price=105.0,
                    date=_BASE_DATE + timedelta(days=1),
                ),
                _make_prediction(  # T2 — final
                    predicted_class=1,
                    confidence=0.30,
                    threshold=0.40,
                    close_price=110.0,
                    date=_BASE_DATE + timedelta(days=2),
                ),
            ]

            # Act
            trades, _ = hybrid_backtester._simulate_hybrid(predictions)

        # Assert — fallback to approx succeeded
        assert len(trades) == 1
        assert trades[0].entry_reason.startswith("hybrid_buy_")


# ===================================================================
# Class 5: TestComputeMetrics
# ===================================================================


class TestComputeMetrics:
    """Tests for ``_compute_metrics`` and related metric calculations."""

    def test_metrics_with_trades(
        self, backtester: DualTimeframeBacktester
    ) -> None:
        """2 winners + 1 loser → correct win_rate, profit_factor, averages."""
        # Arrange
        trades = [
            _make_trade(  # winner 1
                entry_price=100.0,
                exit_price=120.0,
                quantity=10,
                gross_pnl=200.0,
                commission_cost=10.0,
                net_pnl=190.0,
                holding_days=3,
            ),
            _make_trade(  # winner 2
                entry_price=100.0,
                exit_price=115.0,
                quantity=10,
                gross_pnl=150.0,
                commission_cost=10.0,
                net_pnl=140.0,
                holding_days=2,
            ),
            _make_trade(  # loser
                entry_price=100.0,
                exit_price=90.0,
                quantity=10,
                gross_pnl=-100.0,
                commission_cost=10.0,
                net_pnl=-110.0,
                holding_days=1,
            ),
        ]
        equity_curve = [100_000.0, 100_200.0, 100_350.0, 100_240.0]

        # Act
        metrics = backtester._compute_metrics(trades, equity_curve, 100_000.0)

        # Assert
        assert metrics.total_trades == 3
        assert metrics.win_rate_pct == pytest.approx(66.67, rel=0.01)
        # profit_factor = gross_wins / gross_losses = (200+150) / 100 = 3.5
        assert metrics.profit_factor == pytest.approx(3.5, rel=0.01)
        assert metrics.avg_holding_days == pytest.approx(2.0)
        assert metrics.total_return_pct == pytest.approx(0.24, rel=0.01)

    def test_metrics_no_trades(
        self, backtester: DualTimeframeBacktester
    ) -> None:
        """Empty trades → all zero metrics."""
        # Arrange / Act
        metrics = backtester._compute_metrics([], [100_000.0], 100_000.0)

        # Assert
        assert metrics.total_trades == 0
        assert metrics.win_rate_pct == 0.0
        assert metrics.profit_factor == 0.0
        assert metrics.avg_holding_days == 0.0
        assert metrics.avg_pnl_pct == 0.0
        assert metrics.best_trade_pct == 0.0
        assert metrics.worst_trade_pct == 0.0

    def test_metrics_sharpe_ratio(
        self, backtester: DualTimeframeBacktester
    ) -> None:
        """Known equity curve → verify Sharpe calculation.

        equity = [100, 101, 102, 103, 104]
        daily returns ~ 1/100, 1/101, 1/102, 1/103
        Sharpe = mean(returns) / std(returns) * sqrt(252)
        """
        # Arrange
        equity = [100.0, 101.0, 102.0, 103.0, 104.0]
        trades: list[TradeRecord] = []

        # Act
        metrics = backtester._compute_metrics(trades, equity, 100.0)

        # Assert — steady returns → high Sharpe
        assert metrics.sharpe_ratio > 0
        assert metrics.total_return_pct == pytest.approx(4.0, rel=0.01)

    def test_metrics_max_drawdown_flat(
        self, backtester: DualTimeframeBacktester
    ) -> None:
        """Constant equity curve → max drawdown = 0."""
        # Arrange
        equity = [100_000.0] * 10
        trades: list[TradeRecord] = []

        # Act
        metrics = backtester._compute_metrics(trades, equity, 100_000.0)

        # Assert
        assert metrics.max_drawdown_pct == 0.0


# ===================================================================
# Class 6: TestComparisonRunner
# ===================================================================


class TestComparisonRunner:
    """Tests for ``ComparisonRunner``."""

    @patch("app.backtest.comparison_runner.DualTimeframeBacktester")
    def test_compare_basic(self, mock_bt_cls: MagicMock) -> None:
        """Mock DualTimeframeBacktester → verify ComparisonResult deltas."""
        # Arrange
        daily_result = _make_result(
            mode="daily_only",
            total_return_pct=10.0,
            sharpe_ratio=1.0,
            max_drawdown_pct=5.0,
            win_rate_pct=60.0,
            total_trades=10,
        )
        hybrid_result = _make_result(
            mode="hybrid",
            total_return_pct=15.0,
            sharpe_ratio=1.5,
            max_drawdown_pct=4.0,
            win_rate_pct=65.0,
            total_trades=8,
        )

        # First call → daily-only backtester, second → hybrid
        mock_instance_daily = MagicMock()
        mock_instance_daily.run_multi.return_value = daily_result
        mock_instance_hybrid = MagicMock()
        mock_instance_hybrid.run_multi.return_value = hybrid_result
        mock_bt_cls.side_effect = [mock_instance_daily, mock_instance_hybrid]

        db = MagicMock()
        runner = ComparisonRunner(db)

        # Act
        comparison = runner.compare(
            ["AAPL"],
            start_date=_BASE_DATE,
            end_date=_BASE_DATE + timedelta(days=90),
        )

        # Assert
        assert comparison.return_delta_pct == pytest.approx(5.0)
        assert comparison.sharpe_delta == pytest.approx(0.5)
        assert comparison.drawdown_delta_pct == pytest.approx(-1.0)
        assert comparison.trade_count_delta == -2
        assert comparison.win_rate_delta_pct == pytest.approx(5.0)

    def test_compare_empty_symbols_raises(self) -> None:
        """Empty symbol list → ValueError."""
        # Arrange
        db = MagicMock()
        runner = ComparisonRunner(db)

        # Act / Assert
        with pytest.raises(ValueError, match="symbols list must not be empty"):
            runner.compare(
                [],
                start_date=_BASE_DATE,
                end_date=_BASE_DATE + timedelta(days=90),
            )

    @patch("app.backtest.comparison_runner.DualTimeframeBacktester")
    def test_cost_sensitivity_sweep(self, mock_bt_cls: MagicMock) -> None:
        """Verify correct number of CostSensitivityPoints returned for default rates."""
        # Arrange — make every compare() call return a fixed result
        daily_result = _make_result(mode="daily_only", total_return_pct=10.0, sharpe_ratio=1.0)
        hybrid_result = _make_result(mode="hybrid", total_return_pct=12.0, sharpe_ratio=1.2)

        mock_instance = MagicMock()
        mock_instance.run_multi.side_effect = [daily_result, hybrid_result] * 6
        mock_bt_cls.return_value = mock_instance

        db = MagicMock()
        runner = ComparisonRunner(db)

        # Act
        points = runner.cost_sensitivity(
            ["AAPL"],
            start_date=_BASE_DATE,
            end_date=_BASE_DATE + timedelta(days=90),
        )

        # Assert — default: 6 commission rates
        assert len(points) == 6
        assert all(isinstance(p, CostSensitivityPoint) for p in points)

    @patch("app.backtest.comparison_runner.DualTimeframeBacktester")
    def test_format_report_contains_sections(self, mock_bt_cls: MagicMock) -> None:
        """Verify report has key headers, tables, and delta sections."""
        # Arrange
        daily_result = _make_result(mode="daily_only", total_return_pct=10.0, sharpe_ratio=1.0)
        hybrid_result = _make_result(mode="hybrid", total_return_pct=15.0, sharpe_ratio=1.5)

        mock_instance_daily = MagicMock()
        mock_instance_daily.run_multi.return_value = daily_result
        mock_instance_hybrid = MagicMock()
        mock_instance_hybrid.run_multi.return_value = hybrid_result
        mock_bt_cls.side_effect = [mock_instance_daily, mock_instance_hybrid]

        db = MagicMock()
        runner = ComparisonRunner(db)
        comparison = runner.compare(
            ["AAPL"],
            start_date=_BASE_DATE,
            end_date=_BASE_DATE + timedelta(days=90),
        )

        # Act
        report = runner.format_report(comparison)

        # Assert
        assert "# Backtest Comparison Report" in report
        assert "## Performance Comparison" in report
        assert "## Delta Analysis" in report
        assert "## Trading Activity" in report
        assert "Total Return" in report
        assert "Sharpe Ratio" in report

    def test_format_cost_sensitivity_report(self) -> None:
        """Verify cost sensitivity table has proper structure and summary line."""
        # Arrange
        points = [
            CostSensitivityPoint(
                commission_rate=0.0,
                daily_only_return_pct=12.0,
                hybrid_return_pct=15.0,
                daily_only_sharpe=1.0,
                hybrid_sharpe=1.3,
            ),
            CostSensitivityPoint(
                commission_rate=0.001,
                daily_only_return_pct=11.0,
                hybrid_return_pct=10.5,
                daily_only_sharpe=0.9,
                hybrid_sharpe=0.85,
            ),
        ]

        db = MagicMock()
        runner = ComparisonRunner(db)

        # Act
        report = runner.format_cost_sensitivity_report(points)

        # Assert
        assert "# Cost Sensitivity Analysis" in report
        assert "Commission" in report
        assert "Daily Return" in report
        assert "Hybrid Return" in report
        assert "**Summary:**" in report
        # First point: hybrid advantage True; second: False
        assert "Hybrid" in report
        assert "Daily" in report
