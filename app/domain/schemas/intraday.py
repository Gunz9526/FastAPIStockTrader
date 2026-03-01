"""
Phase L.2a: Intraday indicator schemas for 15min rule-based entry layer.

15min RSI/MACD indicators are rule-based (non-ML) entry signals that complement
the daily ML classification layer (L.1). They are computed on-the-fly from
recent 15min OHLCV bars and NOT stored in the database.

Ref: .agent/plan-report/Plan_2026-02-27_L2-Dual-Timeframe-Split.md
"""
from datetime import datetime

from pydantic import BaseModel, Field


class IntradayIndicators(BaseModel):
    """15min RSI + MACD indicators for a single symbol at a point in time."""

    symbol: str = Field(..., description="Stock ticker symbol")
    timestamp: datetime = Field(..., description="Indicator calculation timestamp (ET)")

    # RSI(14) on 15min bars
    rsi_14: float | None = Field(None, ge=0.0, le=100.0, description="RSI(14) on 15min close prices")

    # MACD(12, 26, 9) on 15min bars
    macd_line: float | None = Field(None, description="MACD line (fast EMA - slow EMA)")
    macd_signal: float | None = Field(None, description="MACD signal line (9-period EMA of MACD)")
    macd_histogram: float | None = Field(None, description="MACD histogram (line - signal)")

    # Previous bar's MACD histogram for cross-up detection
    prev_macd_histogram: float | None = Field(
        None, description="Previous bar's MACD histogram for cross-up detection"
    )

    @property
    def is_rsi_oversold(self) -> bool:
        """RSI(14) < 35 entry condition."""
        return self.rsi_14 is not None and self.rsi_14 < 35.0

    @property
    def is_macd_cross_up(self) -> bool:
        """MACD histogram crosses above 0 (previous <= 0, current > 0)."""
        if self.macd_histogram is None or self.prev_macd_histogram is None:
            return False
        return self.prev_macd_histogram <= 0.0 and self.macd_histogram > 0.0

    @property
    def has_entry_signal(self) -> bool:
        """Combined entry rule: RSI < 35 AND MACD cross-up."""
        return self.is_rsi_oversold and self.is_macd_cross_up

    @property
    def bars_sufficient(self) -> bool:
        """All indicators were computed (enough bars available)."""
        return self.rsi_14 is not None and self.macd_histogram is not None


class IntradayIndicatorsSummary(BaseModel):
    """Batch response for multiple symbols' intraday indicators."""

    timestamp: datetime = Field(..., description="Collection timestamp (ET)")
    total_symbols: int = Field(..., description="Total symbols processed")
    signals_found: int = Field(0, description="Symbols with entry signal (RSI+MACD)")
    indicators: list[IntradayIndicators] = Field(
        default_factory=list, description="Per-symbol indicator values"
    )


class EntrySignal(BaseModel):
    """15min entry signal from DualTimeframeOrchestrator.

    Generated when the daily ML layer classifies UP (class=2) and the 15min
    rule-based indicators confirm an entry condition (RSI oversold + MACD
    cross-up).
    """

    symbol: str = Field(..., description="Stock ticker symbol")
    timestamp: datetime = Field(..., description="When signal was generated (ET)")

    # Daily ML layer data
    daily_class: int = Field(
        ..., ge=0, le=2, description="Daily ML class: 0=DOWN, 1=NEUTRAL, 2=UP (must be 2 for entry)"
    )
    daily_confidence: float = Field(
        ..., ge=0.0, le=1.0, description="Confidence score from daily ML model"
    )
    regime: str = Field(..., description="Market regime used for prediction")

    # 15min indicator data at time of signal
    rsi_14: float = Field(
        ..., ge=0.0, le=100.0, description="RSI(14) value on 15min bars (must be < 35)"
    )
    macd_histogram: float = Field(
        ..., description="MACD histogram value on 15min bars (must be > 0, crossing up)"
    )

    # Execution guidance
    suggested_action: str = Field("BUY", description="Always 'BUY' for entry signals")
    reason: str = Field(..., description="Human-readable reason string")


class ExitSignal(BaseModel):
    """Exit signal from DualTimeframeOrchestrator.

    Generated when a position should be closed due to a daily signal flip,
    trailing stop hit, or signal expiration.
    """

    symbol: str = Field(..., description="Stock ticker symbol")
    timestamp: datetime = Field(..., description="When signal was generated (ET)")

    # Exit reason
    exit_reason: str = Field(
        ..., description="Exit reason: 'signal_down' | 'trailing_stop' | 'signal_expired'"
    )

    # Context
    daily_class: int | None = Field(None, ge=0, le=2, description="Current daily signal class")
    current_price: float | None = Field(None, description="Current price at exit signal time")
    trailing_stop: float | None = Field(None, description="Trailing stop price that was hit")

    # Execution guidance
    suggested_action: str = Field("SELL", description="Always 'SELL' for exit signals")
    reason: str = Field(..., description="Human-readable exit reason")
