"""Backtest type definitions for the L.3 Backtesting Validation module.

Pure Pydantic v2 schemas — no business logic.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, computed_field

BacktestMode = Literal["daily_only", "hybrid"]


class BacktestConfig(BaseModel):
    """Configuration parameters for a single backtest run.

    Attributes:
        mode: Backtest execution mode — daily signals only or hybrid with
            intraday entry approximation.
        initial_cash: Starting portfolio cash in USD.
        commission: Per-trade commission as a decimal fraction (0.001 = 0.1%).
        slippage_bps: Estimated slippage in basis points per trade.
        start_date: Inclusive start of the backtest window.
        end_date: Inclusive end of the backtest window.
        max_positions: Maximum number of concurrent open positions.
        risk_per_trade: Fraction of portfolio equity risked per trade.
        trailing_stop_pct: Trailing-stop distance as a fraction below peak price.
    """

    mode: BacktestMode = Field(
        default="daily_only",
        description="Backtest execution mode: daily signals only or hybrid with intraday approximation.",
    )
    initial_cash: float = Field(
        default=100_000.0,
        description="Starting portfolio cash in USD.",
    )
    commission: float = Field(
        default=0.001,
        description="Per-trade commission as a decimal fraction (0.001 = 0.1%).",
    )
    slippage_bps: float = Field(
        default=5.0,
        description="Estimated slippage in basis points per trade.",
    )
    start_date: datetime = Field(
        description="Inclusive start of the backtest window.",
    )
    end_date: datetime = Field(
        description="Inclusive end of the backtest window.",
    )
    max_positions: int = Field(
        default=5,
        description="Maximum number of concurrent open positions.",
    )
    risk_per_trade: float = Field(
        default=0.02,
        description="Fraction of portfolio equity risked per trade (0.02 = 2%).",
    )
    trailing_stop_pct: float = Field(
        default=0.015,
        description="Trailing-stop distance as a fraction below peak price (0.015 = 1.5%).",
    )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def round_trip_cost(self) -> float:
        """Round-trip transaction cost combining commission and slippage."""
        return (self.commission + self.slippage_bps / 10_000) * 2


class DayPrediction(BaseModel):
    """Per-day ML prediction record used internally by the backtest engine.

    Attributes:
        date: Trading date for this prediction.
        symbol: Ticker symbol.
        predicted_class: ML model output class (0=sell, 1=hold, 2=buy).
        confidence: Model confidence score for the predicted class.
        regime: Detected market regime label.
        threshold: Regime-specific confidence threshold for signal activation.
        close_price: End-of-day closing price.
        rsi_14: Daily RSI(14) value for hybrid intraday approximation.
        macd_histogram: Daily MACD histogram value.
        prev_macd_histogram: Previous day MACD histogram value.
    """

    date: datetime = Field(
        description="Trading date for this prediction.",
    )
    symbol: str = Field(
        description="Ticker symbol.",
    )
    predicted_class: int = Field(
        description="ML model output class (0=sell, 1=hold, 2=buy).",
    )
    confidence: float = Field(
        description="Model confidence score for the predicted class.",
    )
    regime: str = Field(
        description="Detected market regime label.",
    )
    threshold: float = Field(
        description="Regime-specific confidence threshold for signal activation.",
    )
    close_price: float = Field(
        description="End-of-day closing price.",
    )
    rsi_14: float | None = Field(
        default=None,
        description="Daily RSI(14) value for hybrid intraday approximation.",
    )
    macd_histogram: float | None = Field(
        default=None,
        description="Daily MACD histogram value.",
    )
    prev_macd_histogram: float | None = Field(
        default=None,
        description="Previous day MACD histogram value.",
    )

    @property
    def is_buy_signal(self) -> bool:
        """True when model predicts BUY with sufficient confidence."""
        return self.predicted_class == 2 and self.confidence >= self.threshold

    @property
    def is_sell_signal(self) -> bool:
        """True when model predicts SELL with sufficient confidence."""
        return self.predicted_class == 0 and self.confidence >= self.threshold

    @property
    def has_intraday_entry_approx(self) -> bool:
        """Approximate intraday entry condition: RSI < 40 AND MACD crosses up.

        MACD cross-up is defined as previous histogram <= 0 and current > 0.
        Returns False if any required indicator value is missing.
        """
        if (
            self.rsi_14 is None
            or self.macd_histogram is None
            or self.prev_macd_histogram is None
        ):
            return False
        return self.rsi_14 < 40 and self.prev_macd_histogram <= 0 and self.macd_histogram > 0


class TradeRecord(BaseModel):
    """Single completed (closed) trade record.

    Attributes:
        symbol: Ticker symbol traded.
        entry_date: Date the position was opened.
        exit_date: Date the position was closed.
        entry_price: Execution price at entry.
        exit_price: Execution price at exit.
        quantity: Number of shares traded.
        direction: Trade direction (currently only long).
        entry_reason: Reason for entering the trade.
        exit_reason: Reason for exiting (e.g. signal_down, trailing_stop, min_hold_sell).
        gross_pnl: Gross profit / loss before commissions.
        commission_cost: Total commission paid for the round trip.
        net_pnl: Net profit / loss after commissions.
        holding_days: Number of calendar days the position was held.
    """

    symbol: str = Field(
        description="Ticker symbol traded.",
    )
    entry_date: datetime = Field(
        description="Date the position was opened.",
    )
    exit_date: datetime = Field(
        description="Date the position was closed.",
    )
    entry_price: float = Field(
        description="Execution price at entry.",
    )
    exit_price: float = Field(
        description="Execution price at exit.",
    )
    quantity: int = Field(
        description="Number of shares traded.",
    )
    direction: Literal["long"] = Field(
        default="long",
        description="Trade direction (currently only long).",
    )
    entry_reason: str = Field(
        description="Reason for entering the trade.",
    )
    exit_reason: str = Field(
        description="Reason for exiting (e.g. signal_down, trailing_stop, min_hold_sell).",
    )
    gross_pnl: float = Field(
        description="Gross profit / loss before commissions.",
    )
    commission_cost: float = Field(
        description="Total commission paid for the round trip.",
    )
    net_pnl: float = Field(
        description="Net profit / loss after commissions.",
    )
    holding_days: int = Field(
        description="Number of calendar days the position was held.",
    )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def pnl_pct(self) -> float:
        """Net PnL as a percentage of entry notional value."""
        notional = self.entry_price * self.quantity
        if notional == 0:
            return 0.0
        return self.net_pnl / notional * 100

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_winner(self) -> bool:
        """True if the trade was profitable after costs."""
        return self.net_pnl > 0


class BacktestMetrics(BaseModel):
    """Aggregated performance metrics computed from a backtest run.

    Attributes:
        total_return_pct: Total portfolio return as a percentage.
        sharpe_ratio: Annualized Sharpe ratio of daily returns.
        max_drawdown_pct: Maximum peak-to-trough drawdown as a percentage.
        win_rate_pct: Percentage of trades that were profitable.
        profit_factor: Ratio of gross profit to gross loss (inf if no losses).
        total_trades: Total number of completed round-trip trades.
        avg_holding_days: Average holding period in calendar days.
        avg_pnl_pct: Average net PnL percentage per trade.
        best_trade_pct: Highest single-trade PnL percentage.
        worst_trade_pct: Lowest single-trade PnL percentage.
    """

    total_return_pct: float = Field(
        description="Total portfolio return as a percentage.",
    )
    sharpe_ratio: float = Field(
        description="Annualized Sharpe ratio of daily returns.",
    )
    max_drawdown_pct: float = Field(
        description="Maximum peak-to-trough drawdown as a percentage.",
    )
    win_rate_pct: float = Field(
        description="Percentage of trades that were profitable.",
    )
    profit_factor: float = Field(
        default=float("inf"),
        description="Ratio of gross profit to gross loss (inf if no losses).",
    )
    total_trades: int = Field(
        description="Total number of completed round-trip trades.",
    )
    avg_holding_days: float = Field(
        description="Average holding period in calendar days.",
    )
    avg_pnl_pct: float = Field(
        description="Average net PnL percentage per trade.",
    )
    best_trade_pct: float = Field(
        description="Highest single-trade PnL percentage.",
    )
    worst_trade_pct: float = Field(
        description="Lowest single-trade PnL percentage.",
    )


class BacktestResult(BaseModel):
    """Full output of a completed backtest run.

    Attributes:
        mode: Backtest execution mode used.
        config: Configuration that produced this result.
        symbols: List of ticker symbols included in the backtest.
        metrics: Aggregated performance metrics.
        trades: List of all completed trade records.
        equity_curve: Daily equity values over the backtest period.
        daily_returns: Daily return percentages over the backtest period.
    """

    mode: BacktestMode = Field(
        description="Backtest execution mode used.",
    )
    config: BacktestConfig = Field(
        description="Configuration that produced this result.",
    )
    symbols: list[str] = Field(
        description="List of ticker symbols included in the backtest.",
    )
    metrics: BacktestMetrics = Field(
        description="Aggregated performance metrics.",
    )
    trades: list[TradeRecord] = Field(
        description="List of all completed trade records.",
    )
    equity_curve: list[float] = Field(
        description="Daily equity values over the backtest period.",
    )
    daily_returns: list[float] = Field(
        description="Daily return percentages over the backtest period.",
    )


class ComparisonResult(BaseModel):
    """Side-by-side comparison of daily-only vs hybrid backtest results.

    Attributes:
        daily_only: Backtest result using daily signals only.
        hybrid: Backtest result using hybrid (daily + intraday approximation).
    """

    daily_only: BacktestResult = Field(
        description="Backtest result using daily signals only.",
    )
    hybrid: BacktestResult = Field(
        description="Backtest result using hybrid (daily + intraday approximation).",
    )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def return_delta_pct(self) -> float:
        """Difference in total return: hybrid minus daily-only."""
        return self.hybrid.metrics.total_return_pct - self.daily_only.metrics.total_return_pct

    @computed_field  # type: ignore[prop-decorator]
    @property
    def sharpe_delta(self) -> float:
        """Difference in Sharpe ratio: hybrid minus daily-only."""
        return self.hybrid.metrics.sharpe_ratio - self.daily_only.metrics.sharpe_ratio

    @computed_field  # type: ignore[prop-decorator]
    @property
    def drawdown_delta_pct(self) -> float:
        """Difference in max drawdown: hybrid minus daily-only (negative = better)."""
        return self.hybrid.metrics.max_drawdown_pct - self.daily_only.metrics.max_drawdown_pct

    @computed_field  # type: ignore[prop-decorator]
    @property
    def trade_count_delta(self) -> int:
        """Difference in total trades: hybrid minus daily-only."""
        return self.hybrid.metrics.total_trades - self.daily_only.metrics.total_trades

    @computed_field  # type: ignore[prop-decorator]
    @property
    def win_rate_delta_pct(self) -> float:
        """Difference in win rate: hybrid minus daily-only."""
        return self.hybrid.metrics.win_rate_pct - self.daily_only.metrics.win_rate_pct


class CostSensitivityPoint(BaseModel):
    """Single data point in a transaction-cost sensitivity sweep.

    Attributes:
        commission_rate: Commission rate used for this sweep point.
        daily_only_return_pct: Total return for daily-only mode at this cost level.
        hybrid_return_pct: Total return for hybrid mode at this cost level.
        daily_only_sharpe: Sharpe ratio for daily-only mode at this cost level.
        hybrid_sharpe: Sharpe ratio for hybrid mode at this cost level.
    """

    commission_rate: float = Field(
        description="Commission rate used for this sweep point.",
    )
    daily_only_return_pct: float = Field(
        description="Total return for daily-only mode at this cost level.",
    )
    hybrid_return_pct: float = Field(
        description="Total return for hybrid mode at this cost level.",
    )
    daily_only_sharpe: float = Field(
        description="Sharpe ratio for daily-only mode at this cost level.",
    )
    hybrid_sharpe: float = Field(
        description="Sharpe ratio for hybrid mode at this cost level.",
    )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def return_delta_pct(self) -> float:
        """Return difference: hybrid minus daily-only."""
        return self.hybrid_return_pct - self.daily_only_return_pct

    @computed_field  # type: ignore[prop-decorator]
    @property
    def hybrid_advantage(self) -> bool:
        """True if hybrid mode outperforms daily-only on total return."""
        return self.hybrid_return_pct > self.daily_only_return_pct
