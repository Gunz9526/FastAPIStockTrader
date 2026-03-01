"""Daily ML Signal Cache — Redis-based prediction storage.

Stores per-symbol ML predictions after market close (17:30 ET) so that
trading operations (market scan, portfolio rebalance) can read signals
from cache instead of re-computing predictions for every symbol.

Key format: ``signal:daily:{symbol}:{regime}``
TTL: 86400 seconds (24 hours)
"""
import json
import logging

from app.core.cache import cache
from app.domain.schemas.signal import CachedSignal, DailySignalSummary

logger = logging.getLogger(__name__)

# 24 hours in seconds
_SIGNAL_TTL: int = 86_400
_KEY_PREFIX: str = "signal:daily"


class DailySignalCache:
    """Redis cache for daily ML prediction signals.

    Wraps the global ``CacheService`` with signal-specific key format,
    TTL, and convenience methods.
    """

    def __init__(self) -> None:
        self._cache = cache

    @staticmethod
    def _make_key(symbol: str, regime: str) -> str:
        """Build Redis key for a signal.

        Args:
            symbol: Stock ticker (e.g. ``"AAPL"``).
            regime: Market regime value (e.g. ``"sideways_calm"``).

        Returns:
            Key string like ``"signal:daily:AAPL:sideways_calm"``.
        """
        return f"{_KEY_PREFIX}:{symbol}:{regime}"

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def set_signal(self, signal: CachedSignal) -> None:
        """Store a daily signal in Redis.

        Args:
            signal: Fully populated ``CachedSignal`` instance.
        """
        key = self._make_key(signal.symbol, signal.regime)
        try:
            self._cache.set(key, signal.model_dump(mode="json"), ttl_seconds=_SIGNAL_TTL)
            logger.debug("Cached signal for %s (%s)", signal.symbol, signal.regime)
        except Exception:
            logger.exception("Failed to cache signal for %s", signal.symbol)

    def set_signals_bulk(self, signals: list[CachedSignal]) -> int:
        """Store multiple signals at once.

        Args:
            signals: List of ``CachedSignal`` instances.

        Returns:
            Number of signals successfully cached.
        """
        count = 0
        for sig in signals:
            try:
                self.set_signal(sig)
                count += 1
            except Exception:
                logger.exception("Failed to cache signal for %s", sig.symbol)
        logger.info("Cached %d/%d daily signals", count, len(signals))
        return count

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get_signal(self, symbol: str, regime: str) -> CachedSignal | None:
        """Retrieve a cached signal for a specific symbol and regime.

        Args:
            symbol: Stock ticker.
            regime: Market regime value.

        Returns:
            ``CachedSignal`` if found, ``None`` otherwise.
        """
        key = self._make_key(symbol, regime)
        try:
            data = self._cache.get(key)
            if data is None:
                return None
            return CachedSignal.model_validate(data)
        except Exception:
            logger.exception("Failed to read cached signal for %s", symbol)
            return None

    def get_all_signals(self, regime: str | None = None) -> list[CachedSignal]:
        """Retrieve all cached daily signals, optionally filtered by regime.

        Args:
            regime: If provided, only return signals for this regime.

        Returns:
            List of ``CachedSignal`` instances.
        """
        if not self._cache.enabled:
            return []

        try:
            pattern = f"{_KEY_PREFIX}:*"
            if regime:
                pattern = f"{_KEY_PREFIX}:*:{regime}"

            client = self._cache.redis_client
            if client is None:
                return []
            signals: list[CachedSignal] = []

            for key in client.scan_iter(match=pattern, count=100):
                raw = client.get(key)
                if raw:
                    data = json.loads(raw)
                    sig = CachedSignal.model_validate(data)
                    signals.append(sig)

            return sorted(signals, key=lambda s: s.symbol)
        except Exception:
            logger.exception("Failed to fetch all cached signals")
            return []

    def get_summary(self, regime: str | None = None) -> DailySignalSummary:
        """Build a summary of cached signals.

        Args:
            regime: Optional regime filter.

        Returns:
            ``DailySignalSummary`` with counts and average confidence.
        """
        signals = self.get_all_signals(regime)
        if not signals:
            return DailySignalSummary(regime=regime or "all")

        up = sum(1 for s in signals if s.predicted_class == 2)
        neutral = sum(1 for s in signals if s.predicted_class == 1)
        down = sum(1 for s in signals if s.predicted_class == 0)
        avg_conf = sum(s.confidence for s in signals) / len(signals)
        latest = max(s.generated_at for s in signals)

        return DailySignalSummary(
            total_signals=len(signals),
            regime=regime or "all",
            up_count=up,
            neutral_count=neutral,
            down_count=down,
            avg_confidence=round(avg_conf, 4),
            generated_at=latest,
            signals=signals,
        )

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    def invalidate_all(self) -> int:
        """Remove all cached daily signals.

        Returns:
            Number of keys deleted.
        """
        if not self._cache.enabled:
            return 0

        try:
            pattern = f"{_KEY_PREFIX}:*"
            client = self._cache.redis_client
            if client is None:
                return 0
            keys = client.keys(pattern)
            if keys:
                deleted = client.delete(*keys)
                logger.info("Invalidated %d daily signal cache entries", deleted)
                return deleted
            return 0
        except Exception:
            logger.exception("Failed to invalidate signal cache")
            return 0

    def invalidate_symbol(self, symbol: str) -> int:
        """Remove cached signals for a specific symbol (all regimes).

        Args:
            symbol: Stock ticker.

        Returns:
            Number of keys deleted.
        """
        if not self._cache.enabled:
            return 0

        try:
            pattern = f"{_KEY_PREFIX}:{symbol}:*"
            client = self._cache.redis_client
            if client is None:
                return 0
            keys = client.keys(pattern)
            if keys:
                deleted = client.delete(*keys)
                logger.info("Invalidated %d signal entries for %s", deleted, symbol)
                return deleted
            return 0
        except Exception:
            logger.exception("Failed to invalidate signals for %s", symbol)
            return 0

    def get_cache_stats(self) -> dict:
        """Return basic statistics about the signal cache.

        Returns:
            Dict with ``total``, ``regime_counts``, ``oldest``, ``newest``.
        """
        signals = self.get_all_signals()
        if not signals:
            return {"total": 0, "regime_counts": {}, "oldest": None, "newest": None}

        regime_counts: dict[str, int] = {}
        for s in signals:
            regime_counts[s.regime] = regime_counts.get(s.regime, 0) + 1

        timestamps = [s.generated_at for s in signals]
        return {
            "total": len(signals),
            "regime_counts": regime_counts,
            "oldest": min(timestamps).isoformat(),
            "newest": max(timestamps).isoformat(),
        }


# Global singleton
daily_signal_cache = DailySignalCache()
