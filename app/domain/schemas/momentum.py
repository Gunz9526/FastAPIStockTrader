"""Cross-Sectional Momentum schemas for ranking and sector rotation.

Phase M.1 — Relative strength scoring across 60-symbol universe.
Stored in Redis with 24h TTL after post-market computation.
"""
from datetime import datetime

from pydantic import BaseModel, Field


class MomentumScore(BaseModel):
    """Per-symbol momentum score computed from daily OHLCV data.

    Attributes:
        symbol: Stock ticker (e.g. ``"AAPL"``).
        sector: GICS sector name from ``sector_map.py``.
        return_1m: 1-month (21 trading days) return as decimal.
        return_3m: 3-month (63 trading days) return as decimal.
        return_6m_skip_1m: 6-month return skipping most recent month
            (academic momentum convention to avoid reversal).
        volatility_63d: 63-day annualised volatility.
        vol_adjusted_momentum: ``(return_3m) / volatility_63d``.
        sector_relative_strength: Symbol return minus sector average.
        composite_score: Weighted composite in ``[0.0, 1.0]``.
        universe_percentile_rank: Percentile across all symbols ``[0.0, 1.0]``.
        computed_at: UTC timestamp when score was computed.
    """

    symbol: str
    sector: str
    return_1m: float = Field(default=0.0, description="21-day return (decimal)")
    return_3m: float = Field(default=0.0, description="63-day return (decimal)")
    return_6m_skip_1m: float = Field(
        default=0.0,
        description="126-day return excluding most recent 21 days",
    )
    volatility_63d: float = Field(
        default=0.0, ge=0.0, description="63-day annualised volatility",
    )
    vol_adjusted_momentum: float = Field(
        default=0.0,
        description="return_3m / volatility_63d (risk-adjusted)",
    )
    sector_relative_strength: float = Field(
        default=0.0,
        description="Symbol return_3m minus sector average return_3m",
    )
    composite_score: float = Field(
        default=0.0, ge=0.0, le=1.0, description="Weighted composite score",
    )
    universe_percentile_rank: float = Field(
        default=0.0, ge=0.0, le=1.0, description="Rank percentile across universe",
    )
    computed_at: datetime = Field(default_factory=datetime.now)


class SectorRotation(BaseModel):
    """Aggregated sector momentum for rotation signals.

    Attributes:
        sector: GICS sector name.
        avg_momentum: Average 3-month return across sector symbols.
        symbol_count: Number of symbols in this sector.
        rank: Rank among all sectors (1 = strongest).
        top_symbols: Top-3 symbols by composite score in this sector.
    """

    sector: str
    avg_momentum: float = Field(default=0.0, description="Sector average 3m return")
    symbol_count: int = Field(default=0, ge=0)
    rank: int = Field(default=0, ge=0, description="1 = strongest sector")
    top_symbols: list[str] = Field(
        default_factory=list,
        description="Top 3 symbols in this sector by composite score",
    )


class MomentumSummary(BaseModel):
    """Summary response for momentum rankings API.

    Attributes:
        computed_at: When scores were last computed.
        total_symbols: Number of scored symbols.
        top_10: Top 10 symbols by composite score.
        bottom_10: Bottom 10 symbols.
        sector_rotations: All sectors ranked by momentum.
    """

    computed_at: datetime | None = None
    total_symbols: int = 0
    top_10: list[MomentumScore] = Field(default_factory=list)
    bottom_10: list[MomentumScore] = Field(default_factory=list)
    sector_rotations: list[SectorRotation] = Field(default_factory=list)
