"""Daily ML signal schemas for Redis cache."""
from datetime import UTC, datetime

from pydantic import BaseModel, Field


class CachedSignal(BaseModel):
    """Cached daily ML prediction signal.

    Stored in Redis with 24h TTL after post-market signal generation.
    """

    symbol: str = Field(description="Stock ticker symbol (e.g. AAPL)")
    predicted_class: int = Field(
        ge=0, le=2,
        description="Predicted class: 0=DOWN, 1=NEUTRAL, 2=UP",
    )
    confidence: float = Field(
        ge=0.0, le=1.0,
        description="Probability of predicted class",
    )
    probabilities: dict[str, float] = Field(
        description="Class probability distribution: {'DOWN': x, 'NEUTRAL': y, 'UP': z}",
    )
    regime: str = Field(
        description="Market regime used for prediction (e.g. 'sideways_calm')",
    )
    generated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="UTC timestamp when signal was generated",
    )
    model_version: str | None = Field(
        default=None,
        description="Model artifact version identifier",
    )

    @property
    def class_name(self) -> str:
        """Human-readable class name."""
        return {0: "DOWN", 1: "NEUTRAL", 2: "UP"}.get(self.predicted_class, "UNKNOWN")


class DailySignalSummary(BaseModel):
    """Summary of all cached daily signals."""

    total_signals: int = 0
    regime: str = ""
    up_count: int = 0
    neutral_count: int = 0
    down_count: int = 0
    avg_confidence: float = 0.0
    generated_at: datetime | None = None
    signals: list[CachedSignal] = Field(default_factory=list)
