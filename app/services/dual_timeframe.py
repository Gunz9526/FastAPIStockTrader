"""
Phase L.2b: DualTimeframeOrchestrator — bridges daily ML signals with 15min entry timing.

The orchestrator does NOT execute trades. It produces EntrySignal/ExitSignal objects
that will be consumed by the execution layer in Phase L.2c.

Architecture:
  Daily ML (L.1) —→ DualTimeframeOrchestrator (L.2b) —→ Execution (L.2c)

Entry Rule: daily UP + confidence ≥ threshold + RSI(14) < 35 + MACD cross-up
Exit Rule: daily DOWN | trailing stop hit | signal expired

Ref: .agent/plan-report/Plan_2026-02-28_L2b-DualTimeframeOrchestrator.md
"""
from __future__ import annotations

import logging
from datetime import datetime

from pytz import timezone
from sqlalchemy.orm import Session

from app.core.config import REGIME_TRADING_CONFIG, settings
from app.domain.schemas.intraday import EntrySignal, ExitSignal, IntradayIndicators
from app.services.intraday_features import compute_indicators_for_symbol
from app.services.signal_cache import DailySignalCache

logger = logging.getLogger(__name__)

# Default confidence threshold when regime is unknown
_DEFAULT_CONFIDENCE_THRESHOLD: float = 0.50

# Eastern timezone for timestamps
_ET = timezone("America/New_York")


class DualTimeframeOrchestrator:
    """Bridges daily ML classification with 15min rule-based entry timing.

    This class checks entry/exit conditions by combining:
    - Daily ML signals (class + confidence) from ``DailySignalCache``
    - 15min RSI(14) / MACD(12,26,9) indicators from ``compute_indicators_for_symbol``

    It does NOT execute trades or modify the database.
    """

    def __init__(self, db: Session) -> None:
        """Initialize the orchestrator.

        Args:
            db: SQLAlchemy sync session for 15min bar access.
        """
        self._db = db
        self._signal_cache = DailySignalCache()

    def _get_regime_threshold(self, regime: str) -> float:
        """Get confidence threshold for a specific regime.

        Handles the ``bull_trending → sideways_calm`` fallback: if a regime
        config has a ``fallback_to_regime`` key, use that regime's threshold
        instead.

        Args:
            regime: Market regime string (e.g. ``"sideways_calm"``).

        Returns:
            Confidence threshold (0.0–1.0). Defaults to 0.50 if regime unknown.
        """
        config = REGIME_TRADING_CONFIG.get(regime)
        if config is None:
            logger.warning("Unknown regime '%s', using default threshold %.2f", regime, _DEFAULT_CONFIDENCE_THRESHOLD)
            return _DEFAULT_CONFIDENCE_THRESHOLD

        # Follow fallback chain (e.g. bull_trending → sideways_calm)
        fallback = config.get("fallback_to_regime")
        if fallback:
            fallback_config = REGIME_TRADING_CONFIG.get(fallback)
            if fallback_config:
                logger.debug(
                    "Regime '%s' falls back to '%s' (threshold=%.2f)",
                    regime, fallback, fallback_config["confidence_threshold"],
                )
                return fallback_config["confidence_threshold"]

        return config["confidence_threshold"]

    def check_entry(
        self,
        symbol: str,
        regime: str,
        indicators: IntradayIndicators,
    ) -> EntrySignal | None:
        """Check if a symbol meets all entry conditions.

        Entry requires ALL of:
        1. ``DUAL_TIMEFRAME_ENABLED`` is ``True``
        2. Daily signal class == 2 (UP)
        3. Daily signal confidence >= regime threshold
        4. RSI(14) < 35 (oversold)
        5. MACD histogram crosses above 0

        Args:
            symbol: Stock ticker.
            regime: Current market regime.
            indicators: 15min indicator values for this symbol.

        Returns:
            EntrySignal if all conditions met, ``None`` otherwise.
        """
        # Gate: feature flag
        if not settings.DUAL_TIMEFRAME_ENABLED:
            return None

        # Gate: 15min indicators must be sufficient
        if not indicators.bars_sufficient:
            logger.debug("%s: insufficient 15min indicator data, skipping entry check", symbol)
            return None

        # Gate: 15min rule-based entry signal (RSI oversold + MACD cross-up)
        if not indicators.has_entry_signal:
            return None

        # Gate: daily ML signal must exist
        signal = self._signal_cache.get_signal(symbol, regime)
        if signal is None:
            logger.debug("%s: no daily signal in cache for regime '%s'", symbol, regime)
            return None

        # Gate: daily class must be UP (2)
        if signal.predicted_class != 2:
            logger.debug(
                "%s: daily class=%d (%s), need UP(2) for entry",
                symbol, signal.predicted_class, signal.class_name,
            )
            return None

        # Gate: confidence must meet regime threshold
        threshold = self._get_regime_threshold(regime)
        if signal.confidence < threshold:
            logger.debug(
                "%s: confidence %.3f < threshold %.3f for regime '%s'",
                symbol, signal.confidence, threshold, regime,
            )
            return None

        # All conditions met — produce entry signal
        now = datetime.now(_ET)
        reason = (
            f"Daily ML={signal.class_name} (conf={signal.confidence:.2f} >= {threshold:.2f}), "
            f"RSI(14)={indicators.rsi_14:.1f} < 35, "
            f"MACD hist cross-up ({indicators.prev_macd_histogram:.4f} → {indicators.macd_histogram:.4f})"
        )

        logger.info("ENTRY signal: %s — %s", symbol, reason)

        return EntrySignal(
            symbol=symbol,
            timestamp=now,
            daily_class=signal.predicted_class,
            daily_confidence=signal.confidence,
            regime=regime,
            rsi_14=indicators.rsi_14,
            macd_histogram=indicators.macd_histogram,
            reason=reason,
        )

    def check_exit(
        self,
        symbol: str,
        regime: str,
        current_price: float,
        trailing_stop: float,
    ) -> ExitSignal | None:
        """Check if a position should be exited.

        Exit if ANY of:
        1. Daily signal class == 0 (DOWN)
        2. ``current_price <= trailing_stop``
        3. Daily signal not found (expired/missing)

        Args:
            symbol: Stock ticker with active position.
            regime: Current market regime.
            current_price: Current market price.
            trailing_stop: Trailing stop price.

        Returns:
            ExitSignal if exit warranted, ``None`` if position should be held.
        """
        # Gate: feature flag
        if not settings.DUAL_TIMEFRAME_ENABLED:
            return None

        now = datetime.now(_ET)

        # Check 1: trailing stop hit (highest priority — price-based)
        if current_price <= trailing_stop:
            reason = f"Price ${current_price:.2f} <= trailing stop ${trailing_stop:.2f}"
            logger.info("EXIT signal (trailing_stop): %s — %s", symbol, reason)
            return ExitSignal(
                symbol=symbol,
                timestamp=now,
                exit_reason="trailing_stop",
                current_price=current_price,
                trailing_stop=trailing_stop,
                reason=reason,
            )

        # Check 2 & 3: daily signal
        signal = self._signal_cache.get_signal(symbol, regime)

        if signal is None:
            reason = f"Daily signal expired or missing for regime '{regime}'"
            logger.info("EXIT signal (signal_expired): %s — %s", symbol, reason)
            return ExitSignal(
                symbol=symbol,
                timestamp=now,
                exit_reason="signal_expired",
                current_price=current_price,
                reason=reason,
            )

        if signal.predicted_class == 0:
            reason = (
                f"Daily ML flipped to {signal.class_name} "
                f"(conf={signal.confidence:.2f})"
            )
            logger.info("EXIT signal (signal_down): %s — %s", symbol, reason)
            return ExitSignal(
                symbol=symbol,
                timestamp=now,
                exit_reason="signal_down",
                daily_class=signal.predicted_class,
                current_price=current_price,
                reason=reason,
            )

        # No exit condition met
        return None

    def scan_entries(
        self,
        symbols: list[str],
        regime: str,
    ) -> list[EntrySignal]:
        """Scan multiple symbols for entry signals.

        Computes 15min indicators for each symbol and checks entry conditions.

        Args:
            symbols: List of active symbols to scan.
            regime: Current market regime.

        Returns:
            List of EntrySignal for symbols meeting all entry conditions.
        """
        if not settings.DUAL_TIMEFRAME_ENABLED:
            return []

        entries: list[EntrySignal] = []

        for symbol in symbols:
            try:
                indicators = compute_indicators_for_symbol(symbol, self._db)
                entry = self.check_entry(symbol, regime, indicators)
                if entry is not None:
                    entries.append(entry)
            except Exception:
                logger.exception("Error scanning entry for %s", symbol)

        if entries:
            logger.info(
                "scan_entries: %d/%d symbols produced entry signals (regime=%s)",
                len(entries), len(symbols), regime,
            )

        return entries
