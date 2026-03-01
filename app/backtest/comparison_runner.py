"""Comparison runner for L.3 Backtesting Validation.

Runs daily-only vs hybrid backtests side-by-side and performs
transaction-cost sensitivity analysis.
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.orm import Session

from app.backtest.backtest_types import (
    BacktestConfig,
    ComparisonResult,
    CostSensitivityPoint,
)
from app.backtest.dual_timeframe_backtester import DualTimeframeBacktester

logger = logging.getLogger(__name__)

# Default commission rates for sensitivity sweep (0 % → 1 %)
_DEFAULT_COMMISSION_RATES: list[float] = [0.0, 0.0005, 0.001, 0.002, 0.005, 0.01]


class ComparisonRunner:
    """Runs daily-only vs hybrid backtests and compares results.

    Provides head-to-head mode comparison and transaction-cost sensitivity
    analysis with human-readable report formatting.

    Args:
        db: SQLAlchemy synchronous session for data access.
    """

    def __init__(self, db: Session) -> None:
        self._db = db

    # ------------------------------------------------------------------
    # Core comparison
    # ------------------------------------------------------------------

    def compare(
        self,
        symbols: list[str],
        start_date: datetime,
        end_date: datetime,
        initial_cash: float = 100_000.0,
        commission: float = 0.001,
    ) -> ComparisonResult:
        """Run daily-only and hybrid backtests side-by-side.

        Args:
            symbols: Ticker symbols to include in the backtest.
            start_date: Inclusive start of the backtest window.
            end_date: Inclusive end of the backtest window.
            initial_cash: Starting portfolio cash in USD.
            commission: Per-trade commission as a decimal fraction.

        Returns:
            ``ComparisonResult`` with both backtest results and computed deltas.

        Raises:
            ValueError: If *symbols* is empty.
        """
        if not symbols:
            raise ValueError("symbols list must not be empty")

        daily_config = BacktestConfig(
            mode="daily_only",
            initial_cash=initial_cash,
            commission=commission,
            start_date=start_date,
            end_date=end_date,
        )
        hybrid_config = BacktestConfig(
            mode="hybrid",
            initial_cash=initial_cash,
            commission=commission,
            start_date=start_date,
            end_date=end_date,
        )

        logger.info(
            "Running comparison: %d symbols, %s → %s, commission=%.4f",
            len(symbols),
            start_date.strftime("%Y-%m-%d"),
            end_date.strftime("%Y-%m-%d"),
            commission,
        )

        daily_result = DualTimeframeBacktester(daily_config, self._db).run_multi(symbols)
        hybrid_result = DualTimeframeBacktester(hybrid_config, self._db).run_multi(symbols)

        comparison = ComparisonResult(daily_only=daily_result, hybrid=hybrid_result)

        logger.info(
            "Comparison complete — return delta: %+.2f%%, sharpe delta: %+.3f, "
            "trade count delta: %+d",
            comparison.return_delta_pct,
            comparison.sharpe_delta,
            comparison.trade_count_delta,
        )

        return comparison

    # ------------------------------------------------------------------
    # Cost sensitivity
    # ------------------------------------------------------------------

    def cost_sensitivity(
        self,
        symbols: list[str],
        start_date: datetime,
        end_date: datetime,
        commission_rates: list[float] | None = None,
        initial_cash: float = 100_000.0,
    ) -> list[CostSensitivityPoint]:
        """Sweep commission rates and record how each mode responds.

        Args:
            symbols: Ticker symbols to include.
            start_date: Inclusive start of the backtest window.
            end_date: Inclusive end of the backtest window.
            commission_rates: List of commission rates to test.  Defaults to
                ``[0.0, 0.0005, 0.001, 0.002, 0.005, 0.01]``.
            initial_cash: Starting portfolio cash in USD.

        Returns:
            List of ``CostSensitivityPoint`` — one per commission rate.

        Raises:
            ValueError: If *symbols* is empty.
        """
        if not symbols:
            raise ValueError("symbols list must not be empty")

        rates = commission_rates if commission_rates is not None else _DEFAULT_COMMISSION_RATES

        logger.info(
            "Cost-sensitivity sweep: %d rates across %d symbols",
            len(rates),
            len(symbols),
        )

        points: list[CostSensitivityPoint] = []

        for rate in rates:
            comparison = self.compare(
                symbols,
                start_date,
                end_date,
                initial_cash=initial_cash,
                commission=rate,
            )

            point = CostSensitivityPoint(
                commission_rate=rate,
                daily_only_return_pct=comparison.daily_only.metrics.total_return_pct,
                hybrid_return_pct=comparison.hybrid.metrics.total_return_pct,
                daily_only_sharpe=comparison.daily_only.metrics.sharpe_ratio,
                hybrid_sharpe=comparison.hybrid.metrics.sharpe_ratio,
            )
            points.append(point)

            logger.info(
                "Cost sensitivity: at %.2f%% commission, hybrid advantage = %s",
                rate * 100,
                "Y" if point.hybrid_advantage else "N",
            )

        return points

    # ------------------------------------------------------------------
    # Report formatting
    # ------------------------------------------------------------------

    def format_report(self, comparison: ComparisonResult) -> str:
        """Generate a human-readable markdown-style comparison report.

        Args:
            comparison: Completed comparison result to format.

        Returns:
            Multi-line markdown string.
        """
        d = comparison.daily_only
        h = comparison.hybrid

        start = d.config.start_date.strftime("%Y-%m-%d")
        end = d.config.end_date.strftime("%Y-%m-%d")
        symbols_str = ", ".join(d.symbols) if d.symbols else "(none)"

        winner = "Hybrid" if comparison.return_delta_pct > 0 else "Daily-only"
        sharpe_winner = "Hybrid" if comparison.sharpe_delta > 0 else "Daily-only"
        dd_winner = "Hybrid" if comparison.drawdown_delta_pct < 0 else "Daily-only"

        lines: list[str] = [
            "# Backtest Comparison Report",
            "",
            f"**Period:** {start} → {end}",
            f"**Symbols:** {symbols_str}",
            f"**Initial Cash:** ${d.config.initial_cash:,.0f}",
            f"**Commission:** {d.config.commission * 100:.2f}%",
            "",
            "## Performance Comparison",
            "",
            "| Metric              | Daily-only | Hybrid   | Delta    |",
            "|---------------------|------------|----------|----------|",
            f"| Total Return        | {d.metrics.total_return_pct:+.2f}% "
            f"| {h.metrics.total_return_pct:+.2f}% "
            f"| {comparison.return_delta_pct:+.2f}% |",
            f"| Sharpe Ratio        | {d.metrics.sharpe_ratio:.3f}  "
            f"| {h.metrics.sharpe_ratio:.3f}  "
            f"| {comparison.sharpe_delta:+.3f} |",
            f"| Max Drawdown        | {d.metrics.max_drawdown_pct:.2f}% "
            f"| {h.metrics.max_drawdown_pct:.2f}% "
            f"| {comparison.drawdown_delta_pct:+.2f}% |",
            f"| Win Rate            | {d.metrics.win_rate_pct:.1f}%  "
            f"| {h.metrics.win_rate_pct:.1f}%  "
            f"| {comparison.win_rate_delta_pct:+.1f}% |",
            f"| Profit Factor       | {_fmt_profit_factor(d.metrics.profit_factor)} "
            f"| {_fmt_profit_factor(h.metrics.profit_factor)} "
            f"| — |",
            "",
            "## Delta Analysis",
            "",
            f"- **Return winner:** {winner} ({comparison.return_delta_pct:+.2f}%)",
            f"- **Risk-adjusted winner:** {sharpe_winner} ({comparison.sharpe_delta:+.3f})",
            f"- **Drawdown winner:** {dd_winner} ({comparison.drawdown_delta_pct:+.2f}%)",
            "",
            "## Trading Activity",
            "",
            "| Metric              | Daily-only | Hybrid   |",
            "|---------------------|------------|----------|",
            f"| Total Trades        | {d.metrics.total_trades}  "
            f"| {h.metrics.total_trades}  |",
            f"| Avg Holding Days    | {d.metrics.avg_holding_days:.1f}  "
            f"| {h.metrics.avg_holding_days:.1f}  |",
            f"| Avg PnL %           | {d.metrics.avg_pnl_pct:+.2f}% "
            f"| {h.metrics.avg_pnl_pct:+.2f}% |",
            f"| Best Trade          | {d.metrics.best_trade_pct:+.2f}% "
            f"| {h.metrics.best_trade_pct:+.2f}% |",
            f"| Worst Trade         | {d.metrics.worst_trade_pct:+.2f}% "
            f"| {h.metrics.worst_trade_pct:+.2f}% |",
            "",
        ]

        return "\n".join(lines)

    def format_cost_sensitivity_report(
        self,
        points: list[CostSensitivityPoint],
    ) -> str:
        """Generate a markdown table for transaction-cost sensitivity results.

        Args:
            points: List of sensitivity data points from ``cost_sensitivity``.

        Returns:
            Multi-line markdown string including a summary footer.
        """
        if not points:
            return "No cost-sensitivity data available."

        lines: list[str] = [
            "# Cost Sensitivity Analysis",
            "",
            "| Commission | Daily Return | Hybrid Return | Delta    | Advantage |",
            "|------------|-------------|---------------|----------|-----------|",
        ]

        for pt in points:
            lines.append(
                f"| {pt.commission_rate * 100:.2f}%     "
                f"| {pt.daily_only_return_pct:+.2f}%    "
                f"| {pt.hybrid_return_pct:+.2f}%      "
                f"| {pt.return_delta_pct:+.2f}%  "
                f"| {'Hybrid' if pt.hybrid_advantage else 'Daily'}    |"
            )

        # Summary: find the highest commission where hybrid still wins
        advantageous = [pt for pt in points if pt.hybrid_advantage]
        if advantageous:
            max_rate = max(pt.commission_rate for pt in advantageous)
            lines.append("")
            lines.append(
                f"**Summary:** Hybrid remains advantageous at commission rates "
                f"up to {max_rate * 100:.2f}%."
            )
        else:
            lines.append("")
            lines.append(
                "**Summary:** Daily-only outperforms hybrid at all tested "
                "commission rates."
            )

        lines.append("")
        return "\n".join(lines)


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------


def _fmt_profit_factor(value: float) -> str:
    """Format profit factor, handling infinity."""
    if value == float("inf"):
        return "∞"
    return f"{value:.2f}"
