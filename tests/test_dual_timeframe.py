"""Tests for DualTimeframeOrchestrator (Phase L.2b).

Covers entry, exit, scan_entries, regime threshold resolution, and edge cases.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from app.core.config import REGIME_TRADING_CONFIG
from app.domain.schemas.intraday import EntrySignal, ExitSignal, IntradayIndicators
from app.domain.schemas.signal import CachedSignal
from app.services.dual_timeframe import DualTimeframeOrchestrator

# ---------------------------------------------------------------------------
# Frozen timestamp used across all tests
# ---------------------------------------------------------------------------
_FROZEN_NOW = datetime(2026, 2, 28, 10, 30, 0)

# Module path prefix for patching
_MOD = "app.services.dual_timeframe"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_db() -> MagicMock:
    """Provide a MagicMock stand-in for SQLAlchemy ``Session``."""
    return MagicMock(spec_set=["execute", "query", "commit", "close"])


def _make_indicators(
    symbol: str = "AAPL",
    rsi_14: float | None = 30.0,
    macd_histogram: float | None = 0.002,
    prev_macd_histogram: float | None = -0.001,
    macd_line: float | None = 0.05,
    macd_signal: float | None = 0.048,
) -> IntradayIndicators:
    """Factory for ``IntradayIndicators`` with sensible defaults.

    Defaults produce an indicator set that satisfies all entry conditions
    (RSI < 35, MACD cross-up, bars sufficient).
    """
    return IntradayIndicators(
        symbol=symbol,
        timestamp=_FROZEN_NOW,
        rsi_14=rsi_14,
        macd_line=macd_line,
        macd_signal=macd_signal,
        macd_histogram=macd_histogram,
        prev_macd_histogram=prev_macd_histogram,
    )


def _make_signal(
    symbol: str = "AAPL",
    predicted_class: int = 2,
    confidence: float = 0.65,
    regime: str = "sideways_calm",
) -> CachedSignal:
    """Factory for ``CachedSignal`` with sensible defaults.

    Defaults produce an UP signal with high confidence.
    """
    probs = {
        "DOWN": round(1.0 - confidence - 0.10, 4),
        "NEUTRAL": 0.10,
        "UP": confidence,
    }
    if predicted_class == 0:
        probs = {"DOWN": confidence, "NEUTRAL": 0.10, "UP": round(1.0 - confidence - 0.10, 4)}
    elif predicted_class == 1:
        probs = {"DOWN": 0.15, "NEUTRAL": confidence, "UP": round(1.0 - confidence - 0.15, 4)}

    return CachedSignal(
        symbol=symbol,
        predicted_class=predicted_class,
        confidence=confidence,
        probabilities=probs,
        regime=regime,
        generated_at=_FROZEN_NOW,
        model_version="test-v1",
    )


@pytest.fixture()
def orchestrator(mock_db: MagicMock) -> tuple[DualTimeframeOrchestrator, MagicMock]:
    """Create a ``DualTimeframeOrchestrator`` with a mocked ``DailySignalCache``.

    Returns:
        Tuple of (orchestrator instance, mock_signal_cache).
    """
    with patch(f"{_MOD}.DailySignalCache") as mock_cache_cls:
        mock_cache_instance = MagicMock()
        mock_cache_cls.return_value = mock_cache_instance
        orch = DualTimeframeOrchestrator(db=mock_db)
    return orch, mock_cache_instance


# ---------------------------------------------------------------------------
# Helper: patch settings + datetime for a standard enabled-test
# ---------------------------------------------------------------------------


def _patch_enabled(enabled: bool = True) -> Any:
    """Return a context-manager that patches ``settings.DUAL_TIMEFRAME_ENABLED``."""
    mock_settings = MagicMock()
    mock_settings.DUAL_TIMEFRAME_ENABLED = enabled
    return patch(f"{_MOD}.settings", mock_settings)


def _patch_datetime() -> Any:
    """Return a context-manager that freezes ``datetime.now`` in the module."""
    mock_dt = MagicMock(wraps=datetime)
    mock_dt.now.return_value = _FROZEN_NOW
    return patch(f"{_MOD}.datetime", mock_dt)


# ===================================================================
# TestGetRegimeThreshold  (tests 18–22)
# ===================================================================


class TestGetRegimeThreshold:
    """Tests for ``_get_regime_threshold`` regime→threshold resolution."""

    def test_bull_trending_falls_back_to_sideways_calm(
        self, orchestrator: tuple[DualTimeframeOrchestrator, MagicMock],
    ) -> None:
        """bull_trending uses sideways_calm threshold (0.40) via fallback_to_regime."""
        orch, _ = orchestrator
        threshold = orch._get_regime_threshold("bull_trending")
        expected = REGIME_TRADING_CONFIG["sideways_calm"]["confidence_threshold"]
        assert threshold == expected == 0.40

    def test_bear_trending_threshold(
        self, orchestrator: tuple[DualTimeframeOrchestrator, MagicMock],
    ) -> None:
        """bear_trending uses its own threshold (0.55)."""
        orch, _ = orchestrator
        threshold = orch._get_regime_threshold("bear_trending")
        assert threshold == 0.55

    def test_sideways_volatile_threshold(
        self, orchestrator: tuple[DualTimeframeOrchestrator, MagicMock],
    ) -> None:
        """sideways_volatile uses its own threshold (0.60)."""
        orch, _ = orchestrator
        threshold = orch._get_regime_threshold("sideways_volatile")
        assert threshold == 0.60

    def test_sideways_calm_threshold(
        self, orchestrator: tuple[DualTimeframeOrchestrator, MagicMock],
    ) -> None:
        """sideways_calm uses its own threshold (0.40)."""
        orch, _ = orchestrator
        threshold = orch._get_regime_threshold("sideways_calm")
        assert threshold == 0.40

    def test_unknown_regime_returns_default(
        self, orchestrator: tuple[DualTimeframeOrchestrator, MagicMock],
    ) -> None:
        """Unknown regime falls back to default 0.50."""
        orch, _ = orchestrator
        threshold = orch._get_regime_threshold("alien_apocalypse")
        assert threshold == 0.50


# ===================================================================
# TestCheckEntry  (tests 1–9, 23, 25)
# ===================================================================


class TestCheckEntry:
    """Tests for ``check_entry`` — 5-gate entry logic."""

    def test_all_conditions_met_returns_entry_signal(
        self, orchestrator: tuple[DualTimeframeOrchestrator, MagicMock],
    ) -> None:
        """All gates pass → returns EntrySignal with correct fields."""
        orch, mock_cache = orchestrator
        indicators = _make_indicators(rsi_14=30.0, macd_histogram=0.002, prev_macd_histogram=-0.001)
        signal = _make_signal(predicted_class=2, confidence=0.65, regime="sideways_calm")
        mock_cache.get_signal.return_value = signal

        with _patch_enabled(True), _patch_datetime():
            result = orch.check_entry("AAPL", "sideways_calm", indicators)

        assert result is not None
        assert isinstance(result, EntrySignal)
        assert result.symbol == "AAPL"
        assert result.daily_class == 2
        assert result.daily_confidence == 0.65
        assert result.regime == "sideways_calm"
        assert result.rsi_14 == 30.0
        assert result.macd_histogram == 0.002
        assert result.suggested_action == "BUY"
        assert result.timestamp == _FROZEN_NOW

    def test_daily_class_down_returns_none(
        self, orchestrator: tuple[DualTimeframeOrchestrator, MagicMock],
    ) -> None:
        """Daily class DOWN (0) → entry blocked."""
        orch, mock_cache = orchestrator
        indicators = _make_indicators()
        signal = _make_signal(predicted_class=0, confidence=0.65)
        mock_cache.get_signal.return_value = signal

        with _patch_enabled(True), _patch_datetime():
            result = orch.check_entry("AAPL", "sideways_calm", indicators)

        assert result is None

    def test_daily_class_neutral_returns_none(
        self, orchestrator: tuple[DualTimeframeOrchestrator, MagicMock],
    ) -> None:
        """Daily class NEUTRAL (1) → entry blocked."""
        orch, mock_cache = orchestrator
        indicators = _make_indicators()
        signal = _make_signal(predicted_class=1, confidence=0.65)
        mock_cache.get_signal.return_value = signal

        with _patch_enabled(True), _patch_datetime():
            result = orch.check_entry("AAPL", "sideways_calm", indicators)

        assert result is None

    def test_rsi_above_35_returns_none(
        self, orchestrator: tuple[DualTimeframeOrchestrator, MagicMock],
    ) -> None:
        """Daily UP but RSI > 35 → has_entry_signal is False → None."""
        orch, mock_cache = orchestrator
        indicators = _make_indicators(rsi_14=40.0)  # RSI above threshold
        signal = _make_signal(predicted_class=2, confidence=0.65)
        mock_cache.get_signal.return_value = signal

        with _patch_enabled(True), _patch_datetime():
            result = orch.check_entry("AAPL", "sideways_calm", indicators)

        assert result is None

    def test_no_macd_cross_up_returns_none(
        self, orchestrator: tuple[DualTimeframeOrchestrator, MagicMock],
    ) -> None:
        """UP + RSI < 35 but no MACD cross-up → None."""
        orch, mock_cache = orchestrator
        # prev_macd_histogram > 0 → no cross-up
        indicators = _make_indicators(rsi_14=30.0, macd_histogram=0.002, prev_macd_histogram=0.001)
        signal = _make_signal(predicted_class=2, confidence=0.65)
        mock_cache.get_signal.return_value = signal

        with _patch_enabled(True), _patch_datetime():
            result = orch.check_entry("AAPL", "sideways_calm", indicators)

        assert result is None

    def test_confidence_below_threshold_returns_none(
        self, orchestrator: tuple[DualTimeframeOrchestrator, MagicMock],
    ) -> None:
        """UP + entry signal but confidence below regime threshold → None."""
        orch, mock_cache = orchestrator
        indicators = _make_indicators()
        # sideways_calm threshold is 0.40 — use 0.35 which is below
        signal = _make_signal(predicted_class=2, confidence=0.35, regime="sideways_calm")
        mock_cache.get_signal.return_value = signal

        with _patch_enabled(True), _patch_datetime():
            result = orch.check_entry("AAPL", "sideways_calm", indicators)

        assert result is None

    def test_feature_flag_disabled_returns_none(
        self, orchestrator: tuple[DualTimeframeOrchestrator, MagicMock],
    ) -> None:
        """DUAL_TIMEFRAME_ENABLED=False → None immediately; cache never queried."""
        orch, mock_cache = orchestrator
        indicators = _make_indicators()

        with _patch_enabled(False):
            result = orch.check_entry("AAPL", "sideways_calm", indicators)

        assert result is None
        mock_cache.get_signal.assert_not_called()

    def test_no_daily_signal_in_cache_returns_none(
        self, orchestrator: tuple[DualTimeframeOrchestrator, MagicMock],
    ) -> None:
        """Cache returns None → entry blocked."""
        orch, mock_cache = orchestrator
        indicators = _make_indicators()
        mock_cache.get_signal.return_value = None

        with _patch_enabled(True), _patch_datetime():
            result = orch.check_entry("AAPL", "sideways_calm", indicators)

        assert result is None

    def test_insufficient_bars_returns_none(
        self, orchestrator: tuple[DualTimeframeOrchestrator, MagicMock],
    ) -> None:
        """bars_sufficient=False (rsi_14 is None) → None."""
        orch, mock_cache = orchestrator
        indicators = _make_indicators(rsi_14=None)

        with _patch_enabled(True):
            result = orch.check_entry("AAPL", "sideways_calm", indicators)

        assert result is None
        mock_cache.get_signal.assert_not_called()

    def test_confidence_exactly_at_threshold_passes(
        self, orchestrator: tuple[DualTimeframeOrchestrator, MagicMock],
    ) -> None:
        """Confidence == threshold → passes (≥ not >)."""
        orch, mock_cache = orchestrator
        indicators = _make_indicators()
        # sideways_calm threshold is 0.40 — use exactly 0.40
        signal = _make_signal(predicted_class=2, confidence=0.40, regime="sideways_calm")
        mock_cache.get_signal.return_value = signal

        with _patch_enabled(True), _patch_datetime():
            result = orch.check_entry("AAPL", "sideways_calm", indicators)

        assert result is not None
        assert isinstance(result, EntrySignal)
        assert result.daily_confidence == 0.40

    def test_macd_histogram_zero_prev_negative_triggers_entry(
        self, orchestrator: tuple[DualTimeframeOrchestrator, MagicMock],
    ) -> None:
        """prev_macd_histogram < 0, macd_histogram > 0 (just above 0) → cross-up True.

        Edge case: prev exactly at 0 is also valid (<=0), but here we test
        prev < 0 with a tiny positive histogram.
        """
        orch, mock_cache = orchestrator
        indicators = _make_indicators(
            rsi_14=28.0,
            macd_histogram=0.0001,
            prev_macd_histogram=-0.005,
        )
        signal = _make_signal(predicted_class=2, confidence=0.65)
        mock_cache.get_signal.return_value = signal

        with _patch_enabled(True), _patch_datetime():
            result = orch.check_entry("AAPL", "sideways_calm", indicators)

        assert result is not None
        assert isinstance(result, EntrySignal)

    def test_prev_histogram_exactly_zero_counts_as_cross_up(
        self, orchestrator: tuple[DualTimeframeOrchestrator, MagicMock],
    ) -> None:
        """prev_macd_histogram == 0.0 (<=0) + histogram > 0 → cross-up is True."""
        orch, mock_cache = orchestrator
        indicators = _make_indicators(
            rsi_14=28.0,
            macd_histogram=0.003,
            prev_macd_histogram=0.0,
        )
        signal = _make_signal(predicted_class=2, confidence=0.65)
        mock_cache.get_signal.return_value = signal

        with _patch_enabled(True), _patch_datetime():
            result = orch.check_entry("AAPL", "sideways_calm", indicators)

        assert result is not None

    def test_entry_reason_string_contains_details(
        self, orchestrator: tuple[DualTimeframeOrchestrator, MagicMock],
    ) -> None:
        """EntrySignal.reason includes ML class, confidence, RSI, and MACD info."""
        orch, mock_cache = orchestrator
        indicators = _make_indicators(rsi_14=32.0, macd_histogram=0.005, prev_macd_histogram=-0.002)
        signal = _make_signal(predicted_class=2, confidence=0.55)
        mock_cache.get_signal.return_value = signal

        with _patch_enabled(True), _patch_datetime():
            result = orch.check_entry("AAPL", "sideways_calm", indicators)

        assert result is not None
        assert "UP" in result.reason
        assert "RSI" in result.reason
        assert "MACD" in result.reason


# ===================================================================
# TestCheckExit  (tests 10–14, 24)
# ===================================================================


class TestCheckExit:
    """Tests for ``check_exit`` — exit condition evaluation."""

    def test_signal_down_returns_exit_signal(
        self, orchestrator: tuple[DualTimeframeOrchestrator, MagicMock],
    ) -> None:
        """Daily signal class DOWN (0) → ExitSignal with exit_reason='signal_down'."""
        orch, mock_cache = orchestrator
        signal = _make_signal(predicted_class=0, confidence=0.70)
        mock_cache.get_signal.return_value = signal

        with _patch_enabled(True), _patch_datetime():
            result = orch.check_exit("AAPL", "sideways_calm", 150.0, 140.0)

        assert result is not None
        assert isinstance(result, ExitSignal)
        assert result.exit_reason == "signal_down"
        assert result.symbol == "AAPL"
        assert result.daily_class == 0
        assert result.current_price == 150.0
        assert result.suggested_action == "SELL"

    def test_trailing_stop_hit_returns_exit_signal(
        self, orchestrator: tuple[DualTimeframeOrchestrator, MagicMock],
    ) -> None:
        """current_price < trailing_stop → ExitSignal with exit_reason='trailing_stop'."""
        orch, mock_cache = orchestrator

        with _patch_enabled(True), _patch_datetime():
            result = orch.check_exit("AAPL", "sideways_calm", 138.0, 140.0)

        assert result is not None
        assert isinstance(result, ExitSignal)
        assert result.exit_reason == "trailing_stop"
        assert result.current_price == 138.0
        assert result.trailing_stop == 140.0
        # NOTE: trailing stop is checked before signal, so cache should NOT be queried
        mock_cache.get_signal.assert_not_called()

    def test_signal_expired_returns_exit_signal(
        self, orchestrator: tuple[DualTimeframeOrchestrator, MagicMock],
    ) -> None:
        """Cache returns None → ExitSignal with exit_reason='signal_expired'."""
        orch, mock_cache = orchestrator
        mock_cache.get_signal.return_value = None

        with _patch_enabled(True), _patch_datetime():
            result = orch.check_exit("AAPL", "sideways_calm", 150.0, 140.0)

        assert result is not None
        assert isinstance(result, ExitSignal)
        assert result.exit_reason == "signal_expired"
        assert result.current_price == 150.0

    def test_hold_up_signal_above_stop_returns_none(
        self, orchestrator: tuple[DualTimeframeOrchestrator, MagicMock],
    ) -> None:
        """UP signal + price above stop → None (hold)."""
        orch, mock_cache = orchestrator
        signal = _make_signal(predicted_class=2, confidence=0.65)
        mock_cache.get_signal.return_value = signal

        with _patch_enabled(True), _patch_datetime():
            result = orch.check_exit("AAPL", "sideways_calm", 155.0, 140.0)

        assert result is None

    def test_feature_flag_disabled_returns_none(
        self, orchestrator: tuple[DualTimeframeOrchestrator, MagicMock],
    ) -> None:
        """DUAL_TIMEFRAME_ENABLED=False → None immediately."""
        orch, mock_cache = orchestrator

        with _patch_enabled(False):
            result = orch.check_exit("AAPL", "sideways_calm", 138.0, 140.0)

        assert result is None
        mock_cache.get_signal.assert_not_called()

    def test_trailing_stop_exactly_equals_price_returns_exit(
        self, orchestrator: tuple[DualTimeframeOrchestrator, MagicMock],
    ) -> None:
        """current_price == trailing_stop → ExitSignal (<=, not <)."""
        orch, mock_cache = orchestrator

        with _patch_enabled(True), _patch_datetime():
            result = orch.check_exit("AAPL", "sideways_calm", 140.0, 140.0)

        assert result is not None
        assert result.exit_reason == "trailing_stop"
        assert result.current_price == 140.0
        assert result.trailing_stop == 140.0

    def test_neutral_signal_holds(
        self, orchestrator: tuple[DualTimeframeOrchestrator, MagicMock],
    ) -> None:
        """NEUTRAL (1) signal + price above stop → hold (None)."""
        orch, mock_cache = orchestrator
        signal = _make_signal(predicted_class=1, confidence=0.50)
        mock_cache.get_signal.return_value = signal

        with _patch_enabled(True), _patch_datetime():
            result = orch.check_exit("AAPL", "sideways_calm", 150.0, 140.0)

        assert result is None

    def test_exit_reason_contains_price_info(
        self, orchestrator: tuple[DualTimeframeOrchestrator, MagicMock],
    ) -> None:
        """Trailing stop ExitSignal.reason includes price details."""
        orch, mock_cache = orchestrator

        with _patch_enabled(True), _patch_datetime():
            result = orch.check_exit("AAPL", "sideways_calm", 139.50, 140.0)

        assert result is not None
        assert "139.50" in result.reason
        assert "140.00" in result.reason

    def test_trailing_stop_takes_priority_over_signal_down(
        self, orchestrator: tuple[DualTimeframeOrchestrator, MagicMock],
    ) -> None:
        """When both trailing stop hit AND signal is DOWN, trailing_stop wins (checked first)."""
        orch, mock_cache = orchestrator
        signal = _make_signal(predicted_class=0, confidence=0.80)
        mock_cache.get_signal.return_value = signal

        with _patch_enabled(True), _patch_datetime():
            result = orch.check_exit("AAPL", "sideways_calm", 135.0, 140.0)

        assert result is not None
        assert result.exit_reason == "trailing_stop"
        # Cache NOT queried because trailing stop exits early
        mock_cache.get_signal.assert_not_called()


# ===================================================================
# TestScanEntries  (tests 15–17)
# ===================================================================


class TestScanEntries:
    """Tests for ``scan_entries`` — multi-symbol batch scanning."""

    def test_mixed_symbols_returns_only_passing(
        self, orchestrator: tuple[DualTimeframeOrchestrator, MagicMock],
    ) -> None:
        """Some symbols produce entry signals, others don't → only passing returned."""
        orch, mock_cache = orchestrator

        good_indicators = _make_indicators(symbol="AAPL", rsi_14=28.0)
        bad_indicators = _make_indicators(symbol="MSFT", rsi_14=50.0)  # RSI too high

        def compute_side_effect(symbol: str, db: Any) -> IntradayIndicators:
            """Route symbol to appropriate indicator set."""
            if symbol == "AAPL":
                return good_indicators
            return bad_indicators

        up_signal = _make_signal(predicted_class=2, confidence=0.65)
        mock_cache.get_signal.return_value = up_signal

        with (
            _patch_enabled(True),
            _patch_datetime(),
            patch(f"{_MOD}.compute_indicators_for_symbol", side_effect=compute_side_effect),
        ):
            results = orch.scan_entries(["AAPL", "MSFT"], "sideways_calm")

        assert len(results) == 1
        assert results[0].symbol == "AAPL"

    def test_feature_flag_disabled_returns_empty(
        self, orchestrator: tuple[DualTimeframeOrchestrator, MagicMock],
    ) -> None:
        """DUAL_TIMEFRAME_ENABLED=False → empty list, nothing computed."""
        orch, mock_cache = orchestrator

        with (
            _patch_enabled(False),
            patch(f"{_MOD}.compute_indicators_for_symbol") as mock_compute,
        ):
            results = orch.scan_entries(["AAPL", "MSFT"], "sideways_calm")

        assert results == []
        mock_compute.assert_not_called()

    def test_exception_in_one_symbol_continues_others(
        self, orchestrator: tuple[DualTimeframeOrchestrator, MagicMock],
    ) -> None:
        """Exception for one symbol is caught; other symbols still processed."""
        orch, mock_cache = orchestrator

        good_indicators = _make_indicators(symbol="TSLA", rsi_14=28.0)
        up_signal = _make_signal(predicted_class=2, confidence=0.65)
        mock_cache.get_signal.return_value = up_signal

        def compute_side_effect(symbol: str, db: Any) -> IntradayIndicators:
            """Raise for AAPL, succeed for TSLA."""
            if symbol == "AAPL":
                raise RuntimeError("DB connection lost")
            return good_indicators

        with (
            _patch_enabled(True),
            _patch_datetime(),
            patch(f"{_MOD}.compute_indicators_for_symbol", side_effect=compute_side_effect),
        ):
            results = orch.scan_entries(["AAPL", "TSLA"], "sideways_calm")

        assert len(results) == 1
        assert results[0].symbol == "TSLA"

    def test_scan_entries_empty_symbol_list(
        self, orchestrator: tuple[DualTimeframeOrchestrator, MagicMock],
    ) -> None:
        """Empty symbol list → empty result list."""
        orch, mock_cache = orchestrator

        with _patch_enabled(True):
            results = orch.scan_entries([], "sideways_calm")

        assert results == []

    def test_scan_entries_all_fail(
        self, orchestrator: tuple[DualTimeframeOrchestrator, MagicMock],
    ) -> None:
        """All symbols raise exceptions → empty list returned."""
        orch, mock_cache = orchestrator

        def compute_side_effect(symbol: str, db: Any) -> IntradayIndicators:
            """Every symbol fails."""
            raise RuntimeError("DB unreachable")

        with (
            _patch_enabled(True),
            patch(f"{_MOD}.compute_indicators_for_symbol", side_effect=compute_side_effect),
        ):
            results = orch.scan_entries(["AAPL", "MSFT", "TSLA"], "sideways_calm")

        assert results == []

    def test_scan_entries_all_pass(
        self, orchestrator: tuple[DualTimeframeOrchestrator, MagicMock],
    ) -> None:
        """All symbols meet entry conditions → all returned."""
        orch, mock_cache = orchestrator

        up_signal = _make_signal(predicted_class=2, confidence=0.65)
        mock_cache.get_signal.return_value = up_signal

        def compute_side_effect(symbol: str, db: Any) -> IntradayIndicators:
            """All symbols produce valid indicators."""
            return _make_indicators(symbol=symbol, rsi_14=28.0)

        with (
            _patch_enabled(True),
            _patch_datetime(),
            patch(f"{_MOD}.compute_indicators_for_symbol", side_effect=compute_side_effect),
        ):
            results = orch.scan_entries(["AAPL", "MSFT"], "sideways_calm")

        assert len(results) == 2
        symbols = {r.symbol for r in results}
        assert symbols == {"AAPL", "MSFT"}


# ===================================================================
# Additional Entry Edge Cases
# ===================================================================


class TestCheckEntryRegimeIntegration:
    """Entry tests that validate regime threshold interaction."""

    def test_bear_trending_high_confidence_passes(
        self, orchestrator: tuple[DualTimeframeOrchestrator, MagicMock],
    ) -> None:
        """bear_trending threshold=0.55 — confidence 0.60 passes."""
        orch, mock_cache = orchestrator
        indicators = _make_indicators()
        signal = _make_signal(predicted_class=2, confidence=0.60, regime="bear_trending")
        mock_cache.get_signal.return_value = signal

        with _patch_enabled(True), _patch_datetime():
            result = orch.check_entry("AAPL", "bear_trending", indicators)

        assert result is not None
        assert result.daily_confidence == 0.60
        assert result.regime == "bear_trending"

    def test_bear_trending_low_confidence_blocked(
        self, orchestrator: tuple[DualTimeframeOrchestrator, MagicMock],
    ) -> None:
        """bear_trending threshold=0.55 — confidence 0.50 is below → None."""
        orch, mock_cache = orchestrator
        indicators = _make_indicators()
        signal = _make_signal(predicted_class=2, confidence=0.50, regime="bear_trending")
        mock_cache.get_signal.return_value = signal

        with _patch_enabled(True), _patch_datetime():
            result = orch.check_entry("AAPL", "bear_trending", indicators)

        assert result is None

    def test_sideways_volatile_exactly_at_threshold(
        self, orchestrator: tuple[DualTimeframeOrchestrator, MagicMock],
    ) -> None:
        """sideways_volatile threshold=0.60 — confidence exactly 0.60 passes."""
        orch, mock_cache = orchestrator
        indicators = _make_indicators()
        signal = _make_signal(predicted_class=2, confidence=0.60, regime="sideways_volatile")
        mock_cache.get_signal.return_value = signal

        with _patch_enabled(True), _patch_datetime():
            result = orch.check_entry("AAPL", "sideways_volatile", indicators)

        assert result is not None

    def test_unknown_regime_uses_default_threshold(
        self, orchestrator: tuple[DualTimeframeOrchestrator, MagicMock],
    ) -> None:
        """Unknown regime → default 0.50 threshold. Confidence 0.55 passes."""
        orch, mock_cache = orchestrator
        indicators = _make_indicators()
        signal = _make_signal(predicted_class=2, confidence=0.55, regime="unknown_regime")
        mock_cache.get_signal.return_value = signal

        with _patch_enabled(True), _patch_datetime():
            result = orch.check_entry("AAPL", "unknown_regime", indicators)

        assert result is not None

    def test_macd_histogram_none_means_insufficient_bars(
        self, orchestrator: tuple[DualTimeframeOrchestrator, MagicMock],
    ) -> None:
        """macd_histogram=None → bars_sufficient is False → None."""
        orch, mock_cache = orchestrator
        indicators = _make_indicators(rsi_14=28.0, macd_histogram=None)

        with _patch_enabled(True):
            result = orch.check_entry("AAPL", "sideways_calm", indicators)

        assert result is None
