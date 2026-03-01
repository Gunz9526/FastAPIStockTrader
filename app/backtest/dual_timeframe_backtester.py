"""Dual-timeframe event-driven backtester supporting daily-only and hybrid modes.

Daily-only mode: ML signal → immediate entry at next day's close (with 1-day
look-ahead offset).

Hybrid mode: ML signal + intraday RSI/MACD filter → filtered entry with
trailing-stop exits.

Key design decisions:
    - Look-ahead bias prevention: prediction on day T → trade on day T+1.
    - Position tracking: ``max_positions`` limit, one position per symbol.
    - Transaction costs: commission + slippage per trade (round-trip).
    - Trailing stop: in hybrid mode tracks peak price and exits when price
      drops below ``peak × (1 - trailing_stop_pct)``.

Ref: .agent/plan-report/ (Phase L.3 — Backtesting Validation)
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from app.backtest.backtest_types import (
    BacktestConfig,
    BacktestMetrics,
    BacktestResult,
    DayPrediction,
    TradeRecord,
)
from app.core.config import REGIME_TRADING_CONFIG
from app.ml.features import FeatureEngineer
from app.ml.predictor import PredictorService
from app.repositories.stock_repo_sync import SyncStockRepository
from app.services.intraday_features import MIN_BARS_REQUIRED, compute_intraday_indicators
from app.services.regime import RegimeDetector

logger = logging.getLogger(__name__)

# Minimum bars required for feature engineering lookback
_FEATURE_LOOKBACK: int = 60

# Default regime config when key is missing
_DEFAULT_REGIME_CFG: dict[str, float | int] = {
    "confidence_threshold": 0.50,
    "min_hold_days": 1,
    "position_scale": 1.0,
}


# ---------------------------------------------------------------------------
# Internal helper dataclass
# ---------------------------------------------------------------------------

@dataclass
class _OpenPosition:
    """Tracks an open position during simulation."""

    symbol: str
    entry_date: datetime
    entry_price: float
    quantity: int
    entry_reason: str
    peak_price: float  # For trailing stop (initialised to entry_price)
    regime: str
    min_hold_days: int


# ---------------------------------------------------------------------------
# Backtester
# ---------------------------------------------------------------------------

class DualTimeframeBacktester:
    """Event-driven backtester supporting daily-only and hybrid dual-timeframe modes.

    Daily-only: ML signal → immediate entry at next day's close.
    Hybrid: ML signal + intraday RSI/MACD filter → filtered entry, trailing
    stop exit.

    Args:
        config: Backtest configuration parameters.
        db: SQLAlchemy synchronous session for data access.
    """

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def __init__(self, config: BacktestConfig, db: Session) -> None:
        self._config = config
        self._db = db
        self._repo = SyncStockRepository(db)

        # ML components — initialised once, reused across symbols
        try:
            self._predictor: PredictorService | None = PredictorService()
        except Exception as exc:
            logger.warning("PredictorService initialisation failed: %s", exc)
            self._predictor = None

        self._feature_engineer = FeatureEngineer()
        self._regime_detector = RegimeDetector()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self, symbol: str) -> BacktestResult:
        """Run a single-symbol backtest.

        Args:
            symbol: Ticker symbol to backtest.

        Returns:
            Completed ``BacktestResult`` with metrics, trades, and equity
            curve.
        """
        if self._predictor is None:
            logger.warning("No predictor available — returning empty result for %s", symbol)
            return self._empty_result([symbol])

        bars = self._repo.get_ohlcv_range(
            symbol,
            self._config.start_date,
            self._config.end_date,
            timeframe="1d",
        )

        if len(bars) < _FEATURE_LOOKBACK:
            logger.warning(
                "%s: insufficient bars (%d < %d) — returning empty result",
                symbol,
                len(bars),
                _FEATURE_LOOKBACK,
            )
            return self._empty_result([symbol])

        # Build OHLCV DataFrame expected by FeatureEngineer
        features_df = self._bars_to_features(bars)
        if features_df.empty:
            return self._empty_result([symbol])

        predictions = self._build_predictions(symbol, bars, features_df)
        if not predictions:
            return self._empty_result([symbol])

        # Dispatch to simulation mode
        if self._config.mode == "hybrid":
            trades, equity_curve = self._simulate_hybrid(predictions)
        else:
            trades, equity_curve = self._simulate_daily_only(predictions)

        metrics = self._compute_metrics(trades, equity_curve, self._config.initial_cash)
        daily_returns = self._equity_to_daily_returns(equity_curve)

        return BacktestResult(
            mode=self._config.mode,
            config=self._config,
            symbols=[symbol],
            metrics=metrics,
            trades=trades,
            equity_curve=equity_curve,
            daily_returns=daily_returns,
        )

    def run_multi(self, symbols: list[str]) -> BacktestResult:
        """Run a portfolio-level backtest across multiple symbols.

        Predictions are generated per-symbol and merged chronologically.
        Position limits are enforced across the entire portfolio.

        Args:
            symbols: List of ticker symbols to include.

        Returns:
            Aggregated ``BacktestResult``.
        """
        if self._predictor is None:
            logger.warning("No predictor available — returning empty result")
            return self._empty_result(symbols)

        all_predictions: list[DayPrediction] = []

        for symbol in symbols:
            bars = self._repo.get_ohlcv_range(
                symbol,
                self._config.start_date,
                self._config.end_date,
                timeframe="1d",
            )
            if len(bars) < _FEATURE_LOOKBACK:
                logger.warning(
                    "%s: insufficient bars (%d) — skipping",
                    symbol,
                    len(bars),
                )
                continue

            features_df = self._bars_to_features(bars)
            if features_df.empty:
                continue

            predictions = self._build_predictions(symbol, bars, features_df)
            all_predictions.extend(predictions)

        if not all_predictions:
            return self._empty_result(symbols)

        # Sort chronologically for correct portfolio simulation
        all_predictions.sort(key=lambda p: p.date)

        if self._config.mode == "hybrid":
            trades, equity_curve = self._simulate_hybrid(all_predictions)
        else:
            trades, equity_curve = self._simulate_daily_only(all_predictions)

        metrics = self._compute_metrics(trades, equity_curve, self._config.initial_cash)
        daily_returns = self._equity_to_daily_returns(equity_curve)

        return BacktestResult(
            mode=self._config.mode,
            config=self._config,
            symbols=symbols,
            metrics=metrics,
            trades=trades,
            equity_curve=equity_curve,
            daily_returns=daily_returns,
        )

    # ------------------------------------------------------------------
    # Prediction generation
    # ------------------------------------------------------------------

    def _build_predictions(
        self,
        symbol: str,
        bars: list,
        features_df: pd.DataFrame,
    ) -> list[DayPrediction]:
        """Generate daily ML predictions for *symbol*.

        Iterates from index ``_FEATURE_LOOKBACK`` to ensure enough history for
        feature engineering.

        Args:
            symbol: Ticker symbol.
            bars: Raw OHLCV bar objects from the repository.
            features_df: DataFrame with computed technical features.

        Returns:
            List of ``DayPrediction`` — one per tradable day.
        """
        predictions: list[DayPrediction] = []
        bar_count = min(len(bars), len(features_df))

        for i in range(_FEATURE_LOOKBACK, bar_count):
            try:
                current_features = features_df.iloc[[i]]
                history_slice = features_df.iloc[: i + 1]

                # Regime detection on historical window
                regime = self._regime_detector.detect_regime(history_slice)
                regime_cfg = REGIME_TRADING_CONFIG.get(
                    regime.value, _DEFAULT_REGIME_CFG,
                )
                threshold: float = regime_cfg.get(
                    "confidence_threshold",
                    _DEFAULT_REGIME_CFG["confidence_threshold"],
                )

                # Scaled feature vector
                regime_suffix = regime.value
                scaled = self._feature_engineer.extract_feature_vector(
                    current_features,
                    fit_scaler=False,
                    feature_set="base",
                    scaler_suffix=regime_suffix,
                )

                if scaled.empty:
                    continue

                predictor = self._predictor
                if predictor is None:
                    continue
                predicted_class, confidence, _probs = predictor.predict_class(
                    scaled, regime=regime,
                )

                # Extract daily indicators for hybrid approximation
                close_price = float(features_df.iloc[i].get("close", 0.0))
                rsi_14 = features_df.iloc[i].get("rsi", None)
                macd_hist = features_df.iloc[i].get("macd_hist", None)
                prev_macd_hist = (
                    features_df.iloc[i - 1].get("macd_hist", None)
                    if i > 0
                    else None
                )

                # Safely cast to float | None
                rsi_14 = float(rsi_14) if rsi_14 is not None and not _is_nan(rsi_14) else None
                macd_hist = float(macd_hist) if macd_hist is not None and not _is_nan(macd_hist) else None
                prev_macd_hist = (
                    float(prev_macd_hist)
                    if prev_macd_hist is not None and not _is_nan(prev_macd_hist)
                    else None
                )

                bar_date = bars[i].date_time if hasattr(bars[i], "date_time") else features_df.index[i]

                predictions.append(
                    DayPrediction(
                        date=bar_date,
                        symbol=symbol,
                        predicted_class=predicted_class,
                        confidence=confidence,
                        regime=regime.value,
                        threshold=threshold,
                        close_price=close_price,
                        rsi_14=rsi_14,
                        macd_histogram=macd_hist,
                        prev_macd_histogram=prev_macd_hist,
                    )
                )
            except Exception as exc:
                logger.debug(
                    "%s bar %d prediction failed: %s", symbol, i, exc,
                )
                continue

        return predictions

    # ------------------------------------------------------------------
    # Daily-only simulation
    # ------------------------------------------------------------------

    def _simulate_daily_only(
        self,
        predictions: list[DayPrediction],
    ) -> tuple[list[TradeRecord], list[float]]:
        """Simulate trades using daily ML signals only.

        Entry: ``is_buy_signal`` on day T → enter at day T+1 close.
        Exit: ``is_sell_signal`` on day T and holding period ≥ min_hold_days
        → exit at day T+1 close.

        Args:
            predictions: Chronologically sorted daily predictions.

        Returns:
            Tuple of (completed trades, daily equity curve).
        """
        cash: float = self._config.initial_cash
        positions: dict[str, _OpenPosition] = {}
        trades: list[TradeRecord] = []
        equity_curve: list[float] = [cash]

        for idx in range(len(predictions) - 1):
            pred = predictions[idx]
            next_pred = predictions[idx + 1]
            execution_price = next_pred.close_price  # T+1 close

            regime_cfg = REGIME_TRADING_CONFIG.get(
                pred.regime, _DEFAULT_REGIME_CFG,
            )
            min_hold: int = int(regime_cfg.get("min_hold_days", 1))

            # --- Exits first ---
            if pred.symbol in positions and pred.is_sell_signal:
                pos = positions[pred.symbol]
                holding = (next_pred.date - pos.entry_date).days
                if holding >= pos.min_hold_days:
                    trade, proceeds = self._close_position(
                        pos, next_pred.date, execution_price, "signal_down",
                    )
                    cash += proceeds
                    trades.append(trade)
                    del positions[pred.symbol]

            # --- Entries ---
            if (
                pred.is_buy_signal
                and len(positions) < self._config.max_positions
                and pred.symbol not in positions
            ):
                qty = self._calc_position_size(
                    self._portfolio_equity(cash, positions, pred.close_price),
                    execution_price,
                )
                cost = qty * execution_price * (1 + self._config.commission + self._config.slippage_bps / 10_000)
                if qty > 0 and cost <= cash:
                    cash -= cost
                    positions[pred.symbol] = _OpenPosition(
                        symbol=pred.symbol,
                        entry_date=next_pred.date,
                        entry_price=execution_price,
                        quantity=qty,
                        entry_reason=f"ml_buy_{pred.regime}",
                        peak_price=execution_price,
                        regime=pred.regime,
                        min_hold_days=min_hold,
                    )

            # Daily equity snapshot
            equity = self._portfolio_equity(cash, positions, next_pred.close_price)
            equity_curve.append(equity)

        # Force-close remaining positions at last known price
        if predictions:
            last_price = predictions[-1].close_price
            last_date = predictions[-1].date
            for sym in list(positions):
                trade, proceeds = self._close_position(
                    positions[sym], last_date, last_price, "backtest_end",
                )
                cash += proceeds
                trades.append(trade)
            positions.clear()

        return trades, equity_curve

    # ------------------------------------------------------------------
    # Hybrid simulation
    # ------------------------------------------------------------------

    def _simulate_hybrid(
        self,
        predictions: list[DayPrediction],
    ) -> tuple[list[TradeRecord], list[float]]:
        """Simulate trades using ML signals filtered by intraday indicators.

        Entry: ``is_buy_signal`` AND intraday entry condition (RSI < 40 + MACD
        cross-up).  If 15 min bars exist in the DB the actual intraday check is
        used; otherwise the daily approximation
        ``has_intraday_entry_approx`` is used as fallback.

        Exit: trailing stop (price drops below peak × (1 - trailing_stop_pct))
        OR ``is_sell_signal``.

        Args:
            predictions: Chronologically sorted daily predictions.

        Returns:
            Tuple of (completed trades, daily equity curve).
        """
        cash: float = self._config.initial_cash
        positions: dict[str, _OpenPosition] = {}
        trades: list[TradeRecord] = []
        equity_curve: list[float] = [cash]

        for idx in range(len(predictions) - 1):
            pred = predictions[idx]
            next_pred = predictions[idx + 1]
            execution_price = next_pred.close_price

            # --- Exits (trailing stop + signal) ---
            cash = self._hybrid_check_exit(
                pred, next_pred, execution_price, positions, trades, cash,
            )

            # --- Entries (ML buy + intraday filter) ---
            cash = self._hybrid_check_entry(
                pred, next_pred, execution_price, positions, cash,
            )

            equity = self._portfolio_equity(cash, positions, next_pred.close_price)
            equity_curve.append(equity)

        # Force-close remaining positions
        if predictions:
            last_price = predictions[-1].close_price
            last_date = predictions[-1].date
            for sym in list(positions):
                trade, proceeds = self._close_position(
                    positions[sym], last_date, last_price, "backtest_end",
                )
                cash += proceeds
                trades.append(trade)
            positions.clear()

        return trades, equity_curve

    # ------------------------------------------------------------------
    # Hybrid simulation helpers
    # ------------------------------------------------------------------

    def _hybrid_check_exit(
        self,
        pred: DayPrediction,
        next_pred: DayPrediction,
        execution_price: float,
        positions: dict[str, _OpenPosition],
        trades: list[TradeRecord],
        cash: float,
    ) -> float:
        """Check and execute trailing-stop / signal exits for hybrid mode.

        Returns:
            Updated cash balance after any exit.
        """
        if pred.symbol not in positions:
            return cash

        pos = positions[pred.symbol]
        if execution_price > pos.peak_price:
            pos.peak_price = execution_price

        holding = (next_pred.date - pos.entry_date).days
        stop_price = pos.peak_price * (1.0 - self._config.trailing_stop_pct)
        trailing_triggered = execution_price <= stop_price and holding >= 1
        signal_exit = pred.is_sell_signal and holding >= pos.min_hold_days

        if trailing_triggered or signal_exit:
            reason = "trailing_stop" if trailing_triggered else "signal_down"
            trade, proceeds = self._close_position(
                pos, next_pred.date, execution_price, reason,
            )
            cash += proceeds
            trades.append(trade)
            del positions[pred.symbol]

        return cash

    def _hybrid_check_entry(
        self,
        pred: DayPrediction,
        next_pred: DayPrediction,
        execution_price: float,
        positions: dict[str, _OpenPosition],
        cash: float,
    ) -> float:
        """Check and execute filtered entries for hybrid mode.

        Returns:
            Updated cash balance after any entry.
        """
        if not pred.is_buy_signal:
            return cash
        if len(positions) >= self._config.max_positions:
            return cash
        if pred.symbol in positions:
            return cash

        if not self._evaluate_intraday_entry(pred):
            return cash

        regime_cfg = REGIME_TRADING_CONFIG.get(pred.regime, _DEFAULT_REGIME_CFG)
        min_hold: int = int(regime_cfg.get("min_hold_days", 1))

        qty = self._calc_position_size(
            self._portfolio_equity(cash, positions, pred.close_price),
            execution_price,
        )
        cost = qty * execution_price * (
            1 + self._config.commission + self._config.slippage_bps / 10_000
        )
        if qty > 0 and cost <= cash:
            cash -= cost
            positions[pred.symbol] = _OpenPosition(
                symbol=pred.symbol,
                entry_date=next_pred.date,
                entry_price=execution_price,
                quantity=qty,
                entry_reason=f"hybrid_buy_{pred.regime}",
                peak_price=execution_price,
                regime=pred.regime,
                min_hold_days=min_hold,
            )
        return cash

    # ------------------------------------------------------------------
    # Intraday helpers
    # ------------------------------------------------------------------

    def _evaluate_intraday_entry(self, pred: DayPrediction) -> bool:
        """Determine whether the intraday entry condition is satisfied.

        Tries actual 15 min bars first; falls back to daily approximation.

        Args:
            pred: Current day's prediction with daily indicator values.

        Returns:
            ``True`` if the intraday entry filter passes.
        """
        actual = self._check_actual_intraday(pred.symbol, pred.date)
        if actual is not None:
            return actual
        return pred.has_intraday_entry_approx

    def _check_actual_intraday(
        self,
        symbol: str,
        date: datetime,
    ) -> bool | None:
        """Check real 15 min bars for intraday entry conditions.

        Args:
            symbol: Ticker symbol.
            date: Trading date to inspect.

        Returns:
            ``True`` / ``False`` if enough bars are available, ``None`` to
            signal that the caller should fall back to the daily approximation.
        """
        try:
            start = date.replace(hour=0, minute=0, second=0, microsecond=0)
            end = start + timedelta(days=1)

            bars_15m = self._repo.get_ohlcv_range(
                symbol, start, end, timeframe="15m",
            )

            if len(bars_15m) < MIN_BARS_REQUIRED:
                return None  # not enough — use approximation

            close_prices = np.array(
                [float(b.close) for b in bars_15m], dtype=np.float64,
            )
            indicators = compute_intraday_indicators(symbol, close_prices, date)
            return indicators.has_entry_signal
        except Exception as exc:
            logger.debug(
                "Intraday check failed for %s on %s: %s",
                symbol,
                date,
                exc,
            )
            return None

    # ------------------------------------------------------------------
    # Position helpers
    # ------------------------------------------------------------------

    def _close_position(
        self,
        pos: _OpenPosition,
        exit_date: datetime,
        exit_price: float,
        exit_reason: str,
    ) -> tuple[TradeRecord, float]:
        """Close an open position and return the trade record + cash proceeds.

        Args:
            pos: Position to close.
            exit_date: Date of the exit.
            exit_price: Execution price at exit.
            exit_reason: Human-readable reason for the exit.

        Returns:
            Tuple of (``TradeRecord``, net cash received after costs).
        """
        gross_pnl = (exit_price - pos.entry_price) * pos.quantity
        notional_entry = pos.entry_price * pos.quantity
        notional_exit = exit_price * pos.quantity
        commission_cost = (notional_entry + notional_exit) * self._config.commission
        slippage_cost = (notional_entry + notional_exit) * (self._config.slippage_bps / 10_000)
        total_cost = commission_cost + slippage_cost
        net_pnl = gross_pnl - total_cost

        holding_days = max((exit_date - pos.entry_date).days, 0)

        trade = TradeRecord(
            symbol=pos.symbol,
            entry_date=pos.entry_date,
            exit_date=exit_date,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            quantity=pos.quantity,
            direction="long",
            entry_reason=pos.entry_reason,
            exit_reason=exit_reason,
            gross_pnl=gross_pnl,
            commission_cost=total_cost,
            net_pnl=net_pnl,
            holding_days=holding_days,
        )

        # Cash returned: sale proceeds minus exit-side costs only
        # (Entry costs were deducted at entry time.)
        proceeds = notional_exit - (notional_exit * (self._config.commission + self._config.slippage_bps / 10_000))
        return trade, proceeds

    def _calc_position_size(self, equity: float, price: float) -> int:
        """Compute the number of shares to buy for a new position.

        Args:
            equity: Current portfolio equity.
            price: Expected execution price per share.

        Returns:
            Number of shares (≥ 0).
        """
        if price <= 0:
            return 0
        size = math.floor(equity * self._config.risk_per_trade / price)
        return max(size, 0)

    @staticmethod
    def _portfolio_equity(
        cash: float,
        positions: dict[str, _OpenPosition],
        current_price: float,
    ) -> float:
        """Calculate total portfolio equity (cash + mark-to-market positions).

        For multi-symbol runs the *current_price* is the latest available
        price — a simplification that is acceptable for single-symbol but
        approximate for multi-symbol (positions valued at their entry
        price when no intraday price exists for that symbol on a given day).
        """
        position_value = sum(
            pos.quantity * current_price for pos in positions.values()
        )
        return cash + position_value

    # ------------------------------------------------------------------
    # Metrics computation
    # ------------------------------------------------------------------

    def _compute_metrics(
        self,
        trades: list[TradeRecord],
        equity_curve: list[float],
        initial_cash: float,
    ) -> BacktestMetrics:
        """Aggregate performance metrics from completed trades and equity curve.

        Args:
            trades: Completed trade records.
            equity_curve: Daily portfolio equity values.
            initial_cash: Starting cash amount.

        Returns:
            ``BacktestMetrics`` with all fields populated.
        """
        total_trades = len(trades)

        # -- Return --
        final_equity = equity_curve[-1] if equity_curve else initial_cash
        total_return_pct = (
            (final_equity - initial_cash) / initial_cash * 100
            if initial_cash > 0
            else 0.0
        )

        # -- Daily returns --
        daily_returns = self._equity_to_daily_returns(equity_curve)
        if daily_returns:
            mean_ret = sum(daily_returns) / len(daily_returns)
            std_ret = _std(daily_returns)
            sharpe_ratio = (mean_ret / std_ret * math.sqrt(252)) if std_ret > 0 else 0.0
        else:
            sharpe_ratio = 0.0

        # -- Max drawdown --
        max_drawdown_pct = _max_drawdown(equity_curve)

        # -- Trade statistics --
        if total_trades > 0:
            winners = [t for t in trades if t.is_winner]
            losers = [t for t in trades if not t.is_winner]
            win_rate_pct = len(winners) / total_trades * 100

            gross_wins = sum(t.gross_pnl for t in winners)
            gross_losses = abs(sum(t.gross_pnl for t in losers))
            profit_factor = (
                gross_wins / gross_losses if gross_losses > 0 else float("inf")
            )

            avg_holding_days = sum(t.holding_days for t in trades) / total_trades
            pnl_pcts = [t.pnl_pct for t in trades]
            avg_pnl_pct = sum(pnl_pcts) / total_trades
            best_trade_pct = max(pnl_pcts)
            worst_trade_pct = min(pnl_pcts)
        else:
            win_rate_pct = 0.0
            profit_factor = 0.0
            avg_holding_days = 0.0
            avg_pnl_pct = 0.0
            best_trade_pct = 0.0
            worst_trade_pct = 0.0

        return BacktestMetrics(
            total_return_pct=round(total_return_pct, 4),
            sharpe_ratio=round(sharpe_ratio, 4),
            max_drawdown_pct=round(max_drawdown_pct, 4),
            win_rate_pct=round(win_rate_pct, 2),
            profit_factor=round(profit_factor, 4) if profit_factor != float("inf") else float("inf"),
            total_trades=total_trades,
            avg_holding_days=round(avg_holding_days, 2),
            avg_pnl_pct=round(avg_pnl_pct, 4),
            best_trade_pct=round(best_trade_pct, 4),
            worst_trade_pct=round(worst_trade_pct, 4),
        )

    # ------------------------------------------------------------------
    # Data helpers
    # ------------------------------------------------------------------

    def _bars_to_features(self, bars: list) -> pd.DataFrame:
        """Convert repository bar objects into a feature DataFrame.

        Args:
            bars: List of ``StockOHLCV`` model instances.

        Returns:
            DataFrame with technical features or empty DataFrame on failure.
        """
        try:
            records = [
                {
                    "open": float(b.open),
                    "high": float(b.high),
                    "low": float(b.low),
                    "close": float(b.close),
                    "volume": float(b.volume),
                    "date_time": b.date_time,
                    "symbol": b.symbol,
                    "vwap": float(b.vwap) if b.vwap is not None else None,
                    "trade_count": int(b.trade_count) if b.trade_count is not None else None,
                }
                for b in bars
            ]
            df = pd.DataFrame(records)
            df.set_index("date_time", inplace=True)
            features_df = self._feature_engineer.create_features(df)
            return features_df
        except Exception as exc:
            logger.error("Feature engineering failed: %s", exc)
            return pd.DataFrame()

    @staticmethod
    def _equity_to_daily_returns(equity_curve: list[float]) -> list[float]:
        """Derive daily percentage returns from an equity curve.

        Args:
            equity_curve: Sequential equity values.

        Returns:
            List of daily return fractions (e.g. 0.01 = +1 %).
        """
        if len(equity_curve) < 2:
            return []
        returns: list[float] = []
        for i in range(1, len(equity_curve)):
            prev = equity_curve[i - 1]
            if prev == 0:
                returns.append(0.0)
            else:
                returns.append((equity_curve[i] - prev) / prev)
        return returns

    def _empty_result(self, symbols: list[str]) -> BacktestResult:
        """Return a zeroed-out ``BacktestResult`` when no trading is possible.

        Args:
            symbols: Symbol list for the result metadata.

        Returns:
            ``BacktestResult`` with empty trades and flat equity.
        """
        return BacktestResult(
            mode=self._config.mode,
            config=self._config,
            symbols=symbols,
            metrics=BacktestMetrics(
                total_return_pct=0.0,
                sharpe_ratio=0.0,
                max_drawdown_pct=0.0,
                win_rate_pct=0.0,
                profit_factor=0.0,
                total_trades=0,
                avg_holding_days=0.0,
                avg_pnl_pct=0.0,
                best_trade_pct=0.0,
                worst_trade_pct=0.0,
            ),
            trades=[],
            equity_curve=[self._config.initial_cash],
            daily_returns=[],
        )


# ---------------------------------------------------------------------------
# Module-level helpers (pure functions, no state)
# ---------------------------------------------------------------------------

def _is_nan(value: object) -> bool:
    """Return ``True`` if *value* is NaN (works for float, numpy, pandas)."""
    try:
        return math.isnan(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False


def _std(values: list[float]) -> float:
    """Population standard deviation, safe for empty lists."""
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    variance = sum((v - mean) ** 2 for v in values) / (n - 1)
    return math.sqrt(variance)


def _max_drawdown(equity_curve: list[float]) -> float:
    """Compute maximum peak-to-trough drawdown as a positive percentage.

    Args:
        equity_curve: Sequential equity values.

    Returns:
        Maximum drawdown percentage (≥ 0).  Returns 0 for empty / constant
        curves.
    """
    if len(equity_curve) < 2:
        return 0.0
    peak = equity_curve[0]
    max_dd = 0.0
    for eq in equity_curve:
        if eq > peak:
            peak = eq
        if peak > 0:
            dd = (peak - eq) / peak * 100
            if dd > max_dd:
                max_dd = dd
    return max_dd
