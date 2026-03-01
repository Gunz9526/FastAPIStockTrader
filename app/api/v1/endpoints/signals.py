"""Daily ML Signal API endpoints.

Provides read-only access to cached daily ML predictions stored in Redis.
"""
import logging
from typing import Annotated

from fastapi import APIRouter, Query

from app.domain.schemas.signal import CachedSignal, DailySignalSummary
from app.services.signal_cache import daily_signal_cache

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get(
    "/daily",
    response_model=DailySignalSummary,
    summary="Get all cached daily ML signals",
)
def get_daily_signals(
    regime: Annotated[str | None, Query(description="Filter by market regime")] = None,
    class_filter: Annotated[
        str | None,
        Query(description="Filter by class: UP, DOWN, or NEUTRAL"),
    ] = None,
) -> DailySignalSummary:
    """Retrieve all cached daily ML prediction signals.

    Args:
        regime: Optional market regime filter (e.g. ``sideways_calm``).
        class_filter: Optional class filter (``UP``, ``DOWN``, or ``NEUTRAL``).

    Returns:
        Summary with signal list, counts, and average confidence.
    """
    summary = daily_signal_cache.get_summary(regime)

    if class_filter:
        class_map = {"DOWN": 0, "NEUTRAL": 1, "UP": 2}
        target_class = class_map.get(class_filter.upper())
        if target_class is not None:
            summary.signals = [
                s for s in summary.signals if s.predicted_class == target_class
            ]
            summary.total_signals = len(summary.signals)

    return summary


@router.get(
    "/daily/stats",
    response_model=dict,
    summary="Get signal cache statistics",
)
def get_signal_stats() -> dict:
    """Return basic statistics about the daily signal cache.

    Returns:
        Dict with total count, regime distribution, oldest/newest timestamps.
    """
    return daily_signal_cache.get_cache_stats()


@router.get(
    "/daily/{symbol}",
    response_model=CachedSignal | None,
    summary="Get cached signal for a specific symbol",
)
def get_signal_by_symbol(
    symbol: str,
    regime: Annotated[
        str, Query(description="Market regime")
    ] = "sideways_calm",
) -> CachedSignal | None:
    """Retrieve the cached daily ML signal for a single symbol.

    Args:
        symbol: Stock ticker (e.g. ``AAPL``).
        regime: Market regime value.

    Returns:
        ``CachedSignal`` if found, ``None`` otherwise.
    """
    return daily_signal_cache.get_signal(symbol.upper(), regime)


@router.delete(
    "/daily",
    summary="Invalidate all cached daily signals",
)
def invalidate_signals() -> dict:
    """Remove all cached daily signals from Redis.

    Returns:
        Dict with number of deleted keys.
    """
    deleted = daily_signal_cache.invalidate_all()
    return {"deleted": deleted}
