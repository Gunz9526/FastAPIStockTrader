"""Tests for Phase L.2c: Intraday Execution Integration.

Covers:
- ``execute_intraday_entries`` Celery task (feature-flag, market-hours, delegation)
- ``SyncTradingStrategy.process_intraday_cycle`` (exit + entry orchestration)
- ``SyncTradingStrategy._process_intraday_entry`` (BUY flow)
- ``SyncTradingStrategy._process_intraday_exit`` (SELL flow)
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from app.domain.schemas.intraday import EntrySignal, ExitSignal

# ---------------------------------------------------------------------------
# Module paths for patching
# ---------------------------------------------------------------------------
_TASK_MOD = "app.tasks.trading"
_STRAT_MOD = "app.services.trading_strategy_sync"
_ORCH_MOD = "app.services.dual_timeframe"
_LOCK_MOD = "app.core.distributed_lock"
_CONFIG_MOD = "app.core.config"
_INTRA_MOD = "app.services.intraday_features"


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------
def _make_entry_signal(
    symbol: str = "AAPL",
    regime: str = "sideways_calm",
    confidence: float = 0.65,
) -> EntrySignal:
    """Build a minimal ``EntrySignal`` for testing."""
    return EntrySignal(
        symbol=symbol,
        timestamp=datetime(2026, 2, 28, 10, 30, tzinfo=UTC),
        daily_class=2,
        daily_confidence=confidence,
        regime=regime,
        rsi_14=28.0,
        macd_histogram=0.003,
        reason="test entry",
    )


def _make_exit_signal(
    symbol: str = "AAPL",
    exit_reason: str = "trailing_stop",
    current_price: float | None = 138.0,
) -> ExitSignal:
    """Build a minimal ``ExitSignal`` for testing."""
    return ExitSignal(
        symbol=symbol,
        timestamp=datetime(2026, 2, 28, 10, 30, tzinfo=UTC),
        exit_reason=exit_reason,
        current_price=current_price,
        reason="test exit",
    )


def _make_alpaca_position(
    symbol: str = "AAPL",
    current_price: float = 140.0,
    avg_entry_price: float = 135.0,
    qty: float = 10.0,
    unrealized_pl: float = 50.0,
) -> MagicMock:
    """Build a mock Alpaca position object."""
    pos = MagicMock()
    pos.symbol = symbol
    pos.current_price = str(current_price)
    pos.avg_entry_price = str(avg_entry_price)
    pos.qty = str(qty)
    pos.unrealized_pl = str(unrealized_pl)
    return pos


def _make_db_position(
    entry_price: float = 135.0,
    trailing_stop_price: float | None = 133.0,
    quantity: int = 10,
) -> MagicMock:
    """Build a mock DB Position row."""
    pos = MagicMock()
    pos.entry_price = entry_price
    pos.trailing_stop_price = trailing_stop_price
    pos.quantity = quantity
    pos.entry_time = datetime(2026, 2, 27, 14, 0, tzinfo=UTC)
    return pos


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture()
def strategy() -> Any:
    """Create ``SyncTradingStrategy`` with all dependencies mocked.

    Bypasses ``__init__`` (which requires DB/Alpaca/Redis) by using
    ``__new__`` and manually attaching mocked collaborators.
    """
    from app.services.trading_strategy_sync import SyncTradingStrategy

    strat = SyncTradingStrategy.__new__(SyncTradingStrategy)
    strat.session = MagicMock()
    strat.db = strat.session
    strat.repo = MagicMock()
    strat.portfolio_repo = MagicMock()
    strat.api = MagicMock()
    strat.risk_manager = MagicMock()
    strat.optimizer = MagicMock()
    strat.max_positions = 5
    strat.multi_position_mode = True

    # Regime
    strat.current_regime = MagicMock()
    strat.current_regime.value = "sideways_calm"
    strat.regime_config = {
        "sideways_calm": {"confidence_threshold": 0.40, "position_scale": 1.0},
        "bear_trending": {"confidence_threshold": 0.55, "position_scale": 0.5},
    }
    strat.circuit_breaker = None
    strat.detect_market_regime = MagicMock()
    strat._execute_sell_order = MagicMock()
    return strat


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _mock_lock(acquired: bool = True) -> MagicMock:
    """Return a context-manager mock for ``get_trading_lock``."""
    lock = MagicMock()
    lock.acquired = acquired
    lock.__enter__ = MagicMock(return_value=lock)
    lock.__exit__ = MagicMock(return_value=False)
    return lock


# ===================================================================
# 1. Celery Task Tests
# ===================================================================
class TestExecuteIntradayEntriesTask:
    """Tests for the ``execute_intraday_entries`` Celery task."""

    def test_feature_flag_disabled(self) -> None:
        """Task returns {'status': 'disabled'} when DUAL_TIMEFRAME_ENABLED is False."""
        with patch(f"{_CONFIG_MOD}.settings") as mock_settings:
            mock_settings.DUAL_TIMEFRAME_ENABLED = False
            from app.tasks.trading import execute_intraday_entries

            result = execute_intraday_entries(MagicMock())

        assert result == {"status": "disabled"}

    def test_outside_market_hours(self) -> None:
        """Task returns {'status': 'outside_hours'} when market is closed."""
        with (
            patch(f"{_CONFIG_MOD}.settings") as mock_settings,
            patch(f"{_INTRA_MOD}.is_market_hours", return_value=False),
        ):
            mock_settings.DUAL_TIMEFRAME_ENABLED = True
            from app.tasks.trading import execute_intraday_entries

            result = execute_intraday_entries(MagicMock())

        assert result == {"status": "outside_hours"}

    def test_no_active_symbols(self) -> None:
        """Task returns {'status': 'no_symbols'} when repo has no symbols."""
        mock_session = MagicMock()
        mock_repo = MagicMock()
        mock_repo.get_active_symbols.return_value = []

        with (
            patch(f"{_CONFIG_MOD}.settings") as mock_settings,
            patch(f"{_INTRA_MOD}.is_market_hours", return_value=True),
            patch(f"{_TASK_MOD}.SessionLocal", return_value=mock_session),
            patch(
                f"{_STRAT_MOD}.SyncTradingStrategy",
                return_value=MagicMock(),
            ),
            patch(
                f"{_STRAT_MOD}.SyncStockRepository",
                return_value=mock_repo,
            ),
        ):
            mock_settings.DUAL_TIMEFRAME_ENABLED = True
            from app.tasks.trading import execute_intraday_entries

            result = execute_intraday_entries(MagicMock())

        assert result == {"status": "no_symbols"}

    def test_normal_execution(self) -> None:
        """Task delegates to process_intraday_cycle and commits session."""
        mock_session = MagicMock()
        mock_strategy = MagicMock()
        expected_result = {"status": "success", "entries": 1, "exits": 0, "skipped": 0, "errors": 0}
        mock_strategy.process_intraday_cycle.return_value = expected_result

        mock_repo = MagicMock()
        mock_repo.get_active_symbols.return_value = ["AAPL", "MSFT"]

        with (
            patch(f"{_CONFIG_MOD}.settings") as mock_settings,
            patch(f"{_INTRA_MOD}.is_market_hours", return_value=True),
            patch(f"{_TASK_MOD}.SessionLocal", return_value=mock_session),
            patch(
                f"{_STRAT_MOD}.SyncTradingStrategy",
                return_value=mock_strategy,
            ),
            patch(
                f"{_STRAT_MOD}.SyncStockRepository",
                return_value=mock_repo,
            ),
        ):
            mock_settings.DUAL_TIMEFRAME_ENABLED = True
            from app.tasks.trading import execute_intraday_entries

            result = execute_intraday_entries(MagicMock())

        assert result == expected_result
        mock_strategy.process_intraday_cycle.assert_called_once_with(["AAPL", "MSFT"])
        mock_session.commit.assert_called_once()

    def test_exception_triggers_rollback_and_reraise(self) -> None:
        """Task rolls back session and re-raises on unhandled exception."""
        mock_session = MagicMock()
        mock_strategy = MagicMock()
        mock_strategy.process_intraday_cycle.side_effect = RuntimeError("boom")

        mock_repo = MagicMock()
        mock_repo.get_active_symbols.return_value = ["AAPL"]

        with (
            patch(f"{_CONFIG_MOD}.settings") as mock_settings,
            patch(f"{_INTRA_MOD}.is_market_hours", return_value=True),
            patch(f"{_TASK_MOD}.SessionLocal", return_value=mock_session),
            patch(
                f"{_STRAT_MOD}.SyncTradingStrategy",
                return_value=mock_strategy,
            ),
            patch(
                f"{_STRAT_MOD}.SyncStockRepository",
                return_value=mock_repo,
            ),
        ):
            mock_settings.DUAL_TIMEFRAME_ENABLED = True
            from app.tasks.trading import execute_intraday_entries

            with pytest.raises(RuntimeError, match="boom"):
                execute_intraday_entries(MagicMock())

        mock_session.rollback.assert_called_once()
        mock_session.close.assert_called_once()


# ===================================================================
# 2. process_intraday_cycle Tests
# ===================================================================
class TestProcessIntradayCycle:
    """Tests for ``SyncTradingStrategy.process_intraday_cycle``."""

    def test_feature_flag_disabled(self, strategy: Any) -> None:
        """Returns disabled summary when ``DUAL_TIMEFRAME_ENABLED`` is False."""
        with patch(f"{_STRAT_MOD}.settings") as mock_settings:
            mock_settings.DUAL_TIMEFRAME_ENABLED = False
            result = strategy.process_intraday_cycle(["AAPL"])

        assert result["status"] == "disabled"
        assert result["entries"] == 0
        assert result["exits"] == 0

    def test_no_positions_no_entries(self, strategy: Any) -> None:
        """Empty positions and no entry signals yield zero counts."""
        mock_orch = MagicMock()
        mock_orch.scan_entries.return_value = []

        strategy.api.get_all_positions.return_value = []
        strategy.api.get_account.return_value = MagicMock(portfolio_value="100000")

        with (
            patch(f"{_STRAT_MOD}.settings") as mock_settings,
            patch(f"{_ORCH_MOD}.DualTimeframeOrchestrator", return_value=mock_orch),
        ):
            mock_settings.DUAL_TIMEFRAME_ENABLED = True
            result = strategy.process_intraday_cycle(["AAPL"])

        assert result["entries"] == 0
        assert result["exits"] == 0
        assert result["status"] == "success"

    def test_one_exit_signal(self, strategy: Any) -> None:
        """A held position with an exit signal increments exit_count."""
        exit_sig = _make_exit_signal("AAPL")
        mock_orch = MagicMock()
        mock_orch.check_exit.return_value = exit_sig
        mock_orch.scan_entries.return_value = []

        strategy.api.get_all_positions.return_value = [_make_alpaca_position("AAPL")]
        strategy.api.get_account.return_value = MagicMock(portfolio_value="100000")
        strategy.repo.get_active_position.return_value = _make_db_position()
        strategy._process_intraday_exit = MagicMock(return_value=True)

        with (
            patch(f"{_STRAT_MOD}.settings") as mock_settings,
            patch(f"{_ORCH_MOD}.DualTimeframeOrchestrator", return_value=mock_orch),
        ):
            mock_settings.DUAL_TIMEFRAME_ENABLED = True
            result = strategy.process_intraday_cycle(["AAPL"])

        assert result["exits"] == 1
        strategy._process_intraday_exit.assert_called_once_with(exit_sig)

    def test_one_entry_signal(self, strategy: Any) -> None:
        """A candidate symbol with an entry signal increments entry_count."""
        entry_sig = _make_entry_signal("MSFT")
        mock_orch = MagicMock()
        mock_orch.check_exit.return_value = None  # no exit
        mock_orch.scan_entries.return_value = [entry_sig]

        strategy.api.get_all_positions.return_value = []
        strategy.api.get_account.return_value = MagicMock(portfolio_value="100000")
        strategy._process_intraday_entry = MagicMock(return_value=True)

        with (
            patch(f"{_STRAT_MOD}.settings") as mock_settings,
            patch(f"{_ORCH_MOD}.DualTimeframeOrchestrator", return_value=mock_orch),
        ):
            mock_settings.DUAL_TIMEFRAME_ENABLED = True
            result = strategy.process_intraday_cycle(["MSFT"])

        assert result["entries"] == 1
        strategy._process_intraday_entry.assert_called_once_with(entry_sig, 100_000.0)

    def test_mixed_exit_and_entry(self, strategy: Any) -> None:
        """One exit and one entry in the same cycle are both counted."""
        exit_sig = _make_exit_signal("AAPL")
        entry_sig = _make_entry_signal("MSFT")

        mock_orch = MagicMock()
        mock_orch.check_exit.return_value = exit_sig
        mock_orch.scan_entries.return_value = [entry_sig]

        strategy.api.get_all_positions.return_value = [_make_alpaca_position("AAPL")]
        strategy.api.get_account.return_value = MagicMock(portfolio_value="100000")
        strategy.repo.get_active_position.return_value = _make_db_position()
        strategy._process_intraday_exit = MagicMock(return_value=True)
        strategy._process_intraday_entry = MagicMock(return_value=True)

        with (
            patch(f"{_STRAT_MOD}.settings") as mock_settings,
            patch(f"{_ORCH_MOD}.DualTimeframeOrchestrator", return_value=mock_orch),
        ):
            mock_settings.DUAL_TIMEFRAME_ENABLED = True
            result = strategy.process_intraday_cycle(["AAPL", "MSFT"])

        assert result["exits"] == 1
        assert result["entries"] == 1

    def test_max_positions_reached_skips_entries(self, strategy: Any) -> None:
        """No available slots → all candidates skipped."""
        # 5 existing positions, max_positions=5, no exits
        positions = [_make_alpaca_position(f"SYM{i}") for i in range(5)]
        mock_orch = MagicMock()
        mock_orch.check_exit.return_value = None

        strategy.api.get_all_positions.return_value = positions
        strategy.api.get_account.return_value = MagicMock(portfolio_value="100000")
        strategy.repo.get_active_position.return_value = _make_db_position()
        strategy.max_positions = 5

        with (
            patch(f"{_STRAT_MOD}.settings") as mock_settings,
            patch(f"{_ORCH_MOD}.DualTimeframeOrchestrator", return_value=mock_orch),
        ):
            mock_settings.DUAL_TIMEFRAME_ENABLED = True
            result = strategy.process_intraday_cycle(["NEW1", "NEW2"])

        assert result["entries"] == 0
        assert result["skipped"] == 2

    def test_alpaca_position_query_fails(self, strategy: Any) -> None:
        """Alpaca position API failure → returns error status."""
        strategy.api.get_all_positions.side_effect = RuntimeError("timeout")

        mock_orch = MagicMock()

        with (
            patch(f"{_STRAT_MOD}.settings") as mock_settings,
            patch(f"{_ORCH_MOD}.DualTimeframeOrchestrator", return_value=mock_orch),
        ):
            mock_settings.DUAL_TIMEFRAME_ENABLED = True
            result = strategy.process_intraday_cycle(["AAPL"])

        assert result["status"] == "error"
        assert result["errors"] == 1

    def test_alpaca_account_query_fails(self, strategy: Any) -> None:
        """Alpaca account API failure → returns error status."""
        strategy.api.get_all_positions.return_value = []
        strategy.api.get_account.side_effect = RuntimeError("auth error")

        mock_orch = MagicMock()

        with (
            patch(f"{_STRAT_MOD}.settings") as mock_settings,
            patch(f"{_ORCH_MOD}.DualTimeframeOrchestrator", return_value=mock_orch),
        ):
            mock_settings.DUAL_TIMEFRAME_ENABLED = True
            result = strategy.process_intraday_cycle(["AAPL"])

        assert result["status"] == "error"
        assert result["errors"] == 1

    def test_scan_entries_raises_increments_error(self, strategy: Any) -> None:
        """``scan_entries`` exception → error counted, no crash."""
        mock_orch = MagicMock()
        mock_orch.scan_entries.side_effect = RuntimeError("nope")

        strategy.api.get_all_positions.return_value = []
        strategy.api.get_account.return_value = MagicMock(portfolio_value="100000")

        with (
            patch(f"{_STRAT_MOD}.settings") as mock_settings,
            patch(f"{_ORCH_MOD}.DualTimeframeOrchestrator", return_value=mock_orch),
        ):
            mock_settings.DUAL_TIMEFRAME_ENABLED = True
            result = strategy.process_intraday_cycle(["AAPL"])

        assert result["entries"] == 0
        assert result["errors"] == 1
        assert result["status"] == "success"

    def test_individual_exit_processing_fails(self, strategy: Any) -> None:
        """A single exit failure increments error without crashing cycle."""
        exit_sig = _make_exit_signal("AAPL")
        mock_orch = MagicMock()
        mock_orch.check_exit.return_value = exit_sig
        mock_orch.scan_entries.return_value = []

        strategy.api.get_all_positions.return_value = [_make_alpaca_position("AAPL")]
        strategy.api.get_account.return_value = MagicMock(portfolio_value="100000")
        strategy.repo.get_active_position.return_value = _make_db_position()
        strategy._process_intraday_exit = MagicMock(return_value=False)

        with (
            patch(f"{_STRAT_MOD}.settings") as mock_settings,
            patch(f"{_ORCH_MOD}.DualTimeframeOrchestrator", return_value=mock_orch),
        ):
            mock_settings.DUAL_TIMEFRAME_ENABLED = True
            result = strategy.process_intraday_cycle(["AAPL"])

        assert result["exits"] == 0
        assert result["errors"] == 1


# ===================================================================
# 3. _process_intraday_entry Tests
# ===================================================================
class TestProcessIntradayEntry:
    """Tests for ``SyncTradingStrategy._process_intraday_entry``."""

    def test_valid_entry_buy_submitted(self, strategy: Any) -> None:
        """Valid entry → BUY order submitted, DB recorded, returns True."""
        entry = _make_entry_signal("AAPL")
        bar = MagicMock(close=140.0)

        strategy.repo.get_ohlcv_range.return_value = [bar]
        strategy.risk_manager.can_enter_position.return_value = (True, "")
        strategy.optimizer.kelly_criterion.return_value = 0.10
        strategy.api.submit_order.return_value = MagicMock(id="order-123")

        lock = _mock_lock(acquired=True)

        with (
            patch(f"{_LOCK_MOD}.get_trading_lock", return_value=lock),
            patch(f"{_STRAT_MOD}.discord_notifier"),
        ):
            result = strategy._process_intraday_entry(entry, 100_000.0)

        assert result is True
        strategy.api.submit_order.assert_called_once()
        strategy.repo.record_position_entry.assert_called_once()
        strategy.session.commit.assert_called_once()

    def test_lock_not_acquired(self, strategy: Any) -> None:
        """Returns False when distributed lock cannot be acquired."""
        entry = _make_entry_signal("AAPL")
        lock = _mock_lock(acquired=False)

        with patch(f"{_LOCK_MOD}.get_trading_lock", return_value=lock):
            result = strategy._process_intraday_entry(entry, 100_000.0)

        assert result is False
        strategy.api.submit_order.assert_not_called()

    def test_risk_manager_blocks(self, strategy: Any) -> None:
        """Returns False when risk manager blocks entry."""
        entry = _make_entry_signal("AAPL")
        strategy.risk_manager.can_enter_position.return_value = (False, "cooldown")

        lock = _mock_lock(acquired=True)

        with patch(f"{_LOCK_MOD}.get_trading_lock", return_value=lock):
            result = strategy._process_intraday_entry(entry, 100_000.0)

        assert result is False

    def test_no_price_data(self, strategy: Any) -> None:
        """Returns False when no bars available for pricing."""
        entry = _make_entry_signal("AAPL")
        strategy.risk_manager.can_enter_position.return_value = (True, "")
        strategy.repo.get_ohlcv_range.return_value = []  # no bars at all

        lock = _mock_lock(acquired=True)

        with patch(f"{_LOCK_MOD}.get_trading_lock", return_value=lock):
            result = strategy._process_intraday_entry(entry, 100_000.0)

        assert result is False

    def test_kelly_size_too_small(self, strategy: Any) -> None:
        """Returns False when Kelly sizing yields qty < 1."""
        entry = _make_entry_signal("AAPL")
        bar = MagicMock(close=500.0)

        strategy.repo.get_ohlcv_range.return_value = [bar]
        strategy.risk_manager.can_enter_position.return_value = (True, "")
        strategy.optimizer.kelly_criterion.return_value = 0.001  # tiny → qty=0

        lock = _mock_lock(acquired=True)

        with patch(f"{_LOCK_MOD}.get_trading_lock", return_value=lock):
            result = strategy._process_intraday_entry(entry, 100.0)

        assert result is False
        strategy.api.submit_order.assert_not_called()

    def test_alpaca_order_fails(self, strategy: Any) -> None:
        """Returns False when Alpaca order submission raises."""
        entry = _make_entry_signal("AAPL")
        bar = MagicMock(close=140.0)

        strategy.repo.get_ohlcv_range.return_value = [bar]
        strategy.risk_manager.can_enter_position.return_value = (True, "")
        strategy.optimizer.kelly_criterion.return_value = 0.10
        strategy.api.submit_order.side_effect = RuntimeError("API error")

        lock = _mock_lock(acquired=True)

        with (
            patch(f"{_LOCK_MOD}.get_trading_lock", return_value=lock),
            patch(f"{_STRAT_MOD}.discord_notifier"),
        ):
            result = strategy._process_intraday_entry(entry, 100_000.0)

        assert result is False

    def test_position_scale_from_regime_config(self, strategy: Any) -> None:
        """Regime-specific ``position_scale`` is applied to Kelly sizing."""
        entry = _make_entry_signal("AAPL", regime="bear_trending")
        bar = MagicMock(close=100.0)

        strategy.repo.get_ohlcv_range.return_value = [bar]
        strategy.risk_manager.can_enter_position.return_value = (True, "")
        # kelly=0.10, bear_trending scale=0.5 → adjusted=0.05
        # portfolio=100000 * 0.05 = 5000 / 100 = 50 shares
        strategy.optimizer.kelly_criterion.return_value = 0.10
        strategy.api.submit_order.return_value = MagicMock(id="order-456")

        lock = _mock_lock(acquired=True)

        with (
            patch(f"{_LOCK_MOD}.get_trading_lock", return_value=lock),
            patch(f"{_STRAT_MOD}.discord_notifier"),
        ):
            result = strategy._process_intraday_entry(entry, 100_000.0)

        assert result is True
        order_call = strategy.api.submit_order.call_args
        order_data = order_call.kwargs.get("order_data") or order_call[1].get("order_data")
        assert order_data.qty == 50


# ===================================================================
# 4. _process_intraday_exit Tests
# ===================================================================
class TestProcessIntradayExit:
    """Tests for ``SyncTradingStrategy._process_intraday_exit``."""

    def test_valid_exit_trailing_stop(self, strategy: Any) -> None:
        """Trailing-stop exit → SELL submitted, returns True."""
        exit_sig = _make_exit_signal("AAPL", exit_reason="trailing_stop", current_price=132.0)
        strategy.repo.get_active_position.return_value = _make_db_position()

        with patch(f"{_STRAT_MOD}.discord_notifier"):
            result = strategy._process_intraday_exit(exit_sig)

        assert result is True
        strategy._execute_sell_order.assert_called_once()
        # Verify reason string format
        call_args = strategy._execute_sell_order.call_args
        reason = call_args[0][3]
        assert "INTRADAY_EXIT (trailing_stop)" in reason

    def test_valid_exit_signal_down(self, strategy: Any) -> None:
        """Signal-down exit → SELL submitted, returns True."""
        exit_sig = _make_exit_signal("MSFT", exit_reason="signal_down", current_price=200.0)
        strategy.repo.get_active_position.return_value = _make_db_position(entry_price=210.0)

        with patch(f"{_STRAT_MOD}.discord_notifier"):
            result = strategy._process_intraday_exit(exit_sig)

        assert result is True
        strategy._execute_sell_order.assert_called_once()
        call_args = strategy._execute_sell_order.call_args
        reason = call_args[0][3]
        assert "signal_down" in reason

    def test_no_db_position(self, strategy: Any) -> None:
        """Returns False when no DB position exists for the symbol."""
        exit_sig = _make_exit_signal("AAPL")
        strategy.repo.get_active_position.return_value = None

        result = strategy._process_intraday_exit(exit_sig)

        assert result is False
        strategy._execute_sell_order.assert_not_called()

    def test_current_price_none_uses_bar_fallback(self, strategy: Any) -> None:
        """When ``exit_signal.current_price`` is None, uses 15min bar price."""
        exit_sig = _make_exit_signal("AAPL", current_price=None)
        db_pos = _make_db_position(entry_price=135.0)
        strategy.repo.get_active_position.return_value = db_pos

        bar = MagicMock(close=137.5)
        strategy.repo.get_ohlcv_range.return_value = [bar]

        with patch(f"{_STRAT_MOD}.discord_notifier"):
            result = strategy._process_intraday_exit(exit_sig)

        assert result is True
        call_args = strategy._execute_sell_order.call_args
        price_arg = call_args[0][2]
        assert price_arg == 137.5

    def test_execute_sell_order_raises(self, strategy: Any) -> None:
        """Returns False when ``_execute_sell_order`` raises an exception."""
        exit_sig = _make_exit_signal("AAPL")
        strategy.repo.get_active_position.return_value = _make_db_position()
        strategy._execute_sell_order.side_effect = RuntimeError("sell failed")

        result = strategy._process_intraday_exit(exit_sig)

        assert result is False


# ===================================================================
# 5. Edge Cases
# ===================================================================
class TestEdgeCases:
    """Additional edge-case scenarios for full coverage."""

    def test_empty_symbol_list(self, strategy: Any) -> None:
        """Empty symbol list → zero entries and exits."""
        mock_orch = MagicMock()
        mock_orch.scan_entries.return_value = []

        strategy.api.get_all_positions.return_value = []
        strategy.api.get_account.return_value = MagicMock(portfolio_value="100000")

        with (
            patch(f"{_STRAT_MOD}.settings") as mock_settings,
            patch(f"{_ORCH_MOD}.DualTimeframeOrchestrator", return_value=mock_orch),
        ):
            mock_settings.DUAL_TIMEFRAME_ENABLED = True
            result = strategy.process_intraday_cycle([])

        assert result["entries"] == 0
        assert result["exits"] == 0
        assert result["status"] == "success"

    def test_db_position_with_trailing_stop_price(self, strategy: Any) -> None:
        """DB position ``trailing_stop_price`` is used when available."""
        db_pos = _make_db_position(entry_price=135.0, trailing_stop_price=131.0)
        alpaca_pos = _make_alpaca_position("AAPL", current_price=130.0)

        mock_orch = MagicMock()
        mock_orch.scan_entries.return_value = []

        strategy.api.get_all_positions.return_value = [alpaca_pos]
        strategy.api.get_account.return_value = MagicMock(portfolio_value="100000")
        strategy.repo.get_active_position.return_value = db_pos
        strategy._process_intraday_exit = MagicMock(return_value=True)

        # Make orchestrator return an exit signal so we can verify trailing_stop was passed
        exit_sig = _make_exit_signal("AAPL")
        mock_orch.check_exit.return_value = exit_sig

        with (
            patch(f"{_STRAT_MOD}.settings") as mock_settings,
            patch(f"{_ORCH_MOD}.DualTimeframeOrchestrator", return_value=mock_orch),
        ):
            mock_settings.DUAL_TIMEFRAME_ENABLED = True
            strategy.process_intraday_cycle(["AAPL"])

        # Verify check_exit was called with DB trailing_stop_price (131.0)
        mock_orch.check_exit.assert_called_once_with("AAPL", "sideways_calm", 140.0, 131.0)

    def test_db_position_without_trailing_stop_uses_default(self, strategy: Any) -> None:
        """No ``trailing_stop_price`` → default = entry_price * 0.985."""
        db_pos = _make_db_position(entry_price=200.0, trailing_stop_price=None)
        alpaca_pos = _make_alpaca_position("TSLA", current_price=195.0, avg_entry_price=200.0)

        mock_orch = MagicMock()
        mock_orch.check_exit.return_value = None
        mock_orch.scan_entries.return_value = []

        strategy.api.get_all_positions.return_value = [alpaca_pos]
        strategy.api.get_account.return_value = MagicMock(portfolio_value="100000")
        strategy.repo.get_active_position.return_value = db_pos

        with (
            patch(f"{_STRAT_MOD}.settings") as mock_settings,
            patch(f"{_ORCH_MOD}.DualTimeframeOrchestrator", return_value=mock_orch),
        ):
            mock_settings.DUAL_TIMEFRAME_ENABLED = True
            strategy.process_intraday_cycle(["TSLA"])

        # entry_price * 0.985 = 200 * 0.985 = 197.0
        expected_stop = 200.0 * 0.985
        mock_orch.check_exit.assert_called_once_with("TSLA", "sideways_calm", 195.0, expected_stop)
