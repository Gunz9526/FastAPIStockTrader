"""Momentum ranking & sector rotation API — Phase M.1.

Endpoints:
    GET /momentum/rankings       — Full momentum rankings (today)
    GET /momentum/rankings/{sym} — Single symbol score
    GET /momentum/sectors        — Sector rotation table
    POST /momentum/compute       — Trigger manual recomputation
"""
import logging

from fastapi import APIRouter, HTTPException

from app.domain.schemas.momentum import (
    MomentumScore,
    MomentumSummary,
    SectorRotation,
)
from app.services.momentum_scorer import CrossSectionalMomentum

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get(
    "/rankings",
    response_model=MomentumSummary,
    summary="Get momentum rankings summary",
)
def get_momentum_rankings(date: str | None = None) -> MomentumSummary:
    """Return top-10, bottom-10, and sector rotation from cache.

    Args:
        date: Optional ``YYYY-MM-DD`` to query a specific date.
              Defaults to today.
    """
    summary = CrossSectionalMomentum.get_cached_summary(date)
    if summary.total_symbols == 0:
        raise HTTPException(
            status_code=404,
            detail="Momentum scores not found. Run compute task first.",
        )
    return summary


@router.get(
    "/rankings/{symbol}",
    response_model=MomentumScore,
    summary="Get momentum score for a symbol",
)
def get_symbol_momentum(symbol: str, date: str | None = None) -> MomentumScore:
    """Return a single symbol's momentum score from cache.

    Args:
        symbol: Stock ticker (e.g. ``"AAPL"``).
        date: Optional ``YYYY-MM-DD``.
    """
    scores = CrossSectionalMomentum.get_cached_scores(date)
    for s in scores:
        if s.symbol == symbol.upper():
            return s
    raise HTTPException(status_code=404, detail=f"Score for {symbol} not found")


@router.get(
    "/sectors",
    response_model=list[SectorRotation],
    summary="Get sector rotation table",
)
def get_sector_rotation(date: str | None = None) -> list[SectorRotation]:
    """Return sector rotation rankings from cache.

    Args:
        date: Optional ``YYYY-MM-DD``.
    """
    sectors = CrossSectionalMomentum.get_cached_sectors(date)
    if not sectors:
        raise HTTPException(
            status_code=404,
            detail="Sector rotation data not found. Run compute task first.",
        )
    return sectors


@router.post(
    "/compute",
    summary="Trigger momentum score computation",
)
def trigger_momentum_compute() -> dict:
    """Manually trigger cross-sectional momentum computation.

    Enqueues the Celery task and returns task ID.
    """
    from app.tasks.market_analysis import compute_momentum_scores

    task = compute_momentum_scores.delay()
    return {"task_id": str(task.id), "status": "queued"}
