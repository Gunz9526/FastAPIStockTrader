"""
Portfolio Optimization Service (Phase I.2)

Implements Modern Portfolio Theory, Kelly Criterion, and VaR calculation.
Supports both backtest data (initial) and live trading data (auto-upgrade).
"""

import logging
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Optional
from scipy.optimize import minimize

from app.core.cache import cache

logger = logging.getLogger(__name__)


class PortfolioOptimizer:
    """
    Portfolio optimization using MPT, Kelly Criterion, and VaR.
    
    Features:
    - Correlation matrix calculation (rolling 14-day window)
    - Value-at-Risk (VaR) estimation (95% confidence)
    - Kelly Criterion position sizing
    - Sharpe ratio maximization (MPT)
    - Auto-upgrade from backtest to live data
    """
    
    def __init__(self, lookback_days: int = 14, min_live_trades: int = 50):
        """
        Args:
            lookback_days: Rolling window size for parameter calculation
            min_live_trades: Minimum trades required to switch from backtest to live data
        """
        self.lookback_days = lookback_days
        self.min_live_trades = min_live_trades
        self.cache_ttl = 86400  # 24 hours
    
    def calculate_correlation_matrix(
        self,
        repo,
        symbols: List[str],
        use_live_data: bool = True
    ) -> pd.DataFrame:
        """
        Calculate correlation matrix using returns data.
        
        Strategy:
        - Days 1-13: Use backtest data (initial, less accurate)
        - Day 14+: Use live trading data (accurate with slippage)
        
        Args:
            repo: PortfolioRepository instance
            symbols: List of stock symbols
            use_live_data: If True, prefer live data; else use backtest
        
        Returns:
            Correlation matrix (DataFrame)
        """
        try:
            # Check cache first
            cache_key = f"corr_matrix:{datetime.now().date()}"
            cached = cache.get(cache_key) if cache else None
            if cached:
                logger.info("Correlation matrix loaded from cache")
                return pd.read_json(cached)
            
            # Decide data source
            if use_live_data:
                live_trade_count = repo.count_live_trades(days=self.lookback_days)
                use_live = live_trade_count >= self.min_live_trades
            else:
                use_live = False
            
            if use_live:
                logger.info(f"??Using LIVE trade data ({live_trade_count} trades)")
                returns_data = self._get_live_returns(repo, symbols)
            else:
                logger.warning(f"Using BACKTEST data (live trades: {live_trade_count}/{self.min_live_trades})")
                returns_data = self._get_backtest_returns(repo, symbols)
            
            # Calculate correlation
            # Align returns to same length (use shortest length)
            min_length = min(len(v) for v in returns_data.values())
            if min_length == 0:
                logger.warning("No return data available, using identity matrix")
                return pd.DataFrame(np.eye(len(symbols)), index=symbols, columns=symbols)
            
            aligned_returns = {k: v[:min_length] for k, v in returns_data.items()}
            df_returns = pd.DataFrame(aligned_returns)
            corr_matrix = df_returns.corr()
            
            # Cache for 24 hours
            cache.set(cache_key, corr_matrix.to_json(), ttl_seconds=self.cache_ttl)
            
            logger.info(f"Correlation matrix calculated ({min_length} samples):\n{corr_matrix}")
            return corr_matrix
            
        except Exception as e:
            logger.error(f"Correlation calculation failed: {e}", exc_info=True)
            # Return identity matrix as fallback (no correlation)
            return pd.DataFrame(np.eye(len(symbols)), index=symbols, columns=symbols)
    
    def _get_live_returns(self, repo, symbols: List[str]) -> Dict[str, np.ndarray]:
        """Get returns from live trading data (position_tracking table)."""
        returns_data = {}
        end_date = datetime.now()
        start_date = end_date - timedelta(days=self.lookback_days)
        
        for symbol in symbols:
            trades = repo.get_trade_history(symbol, start_date, end_date)
            if len(trades) > 0:
                # Calculate returns: (exit_price - entry_price) / entry_price
                returns = [(t['exit_price'] - t['entry_price']) / t['entry_price'] 
                          for t in trades if t['exit_price'] is not None]
                returns_data[symbol] = np.array(returns)
            else:
                # No trades, use neutral returns
                returns_data[symbol] = np.array([0.0])
        
        return returns_data
    
    def _get_backtest_returns(self, repo, symbols: List[str]) -> Dict[str, np.ndarray]:
        """Get returns from historical OHLCV data (backtest)."""
        returns_data = {}
        end_date = datetime.now()
        start_date = end_date - timedelta(days=self.lookback_days)
        
        for symbol in symbols:
            ohlcv = repo.get_ohlcv_range(symbol, start_date, end_date, timeframe='15m')
            if len(ohlcv) > 0:
                df = pd.DataFrame([{
                    'close': bar.close
                } for bar in ohlcv])
                returns = df['close'].pct_change().dropna().values
                returns_data[symbol] = returns
            else:
                returns_data[symbol] = np.array([0.0])
        
        return returns_data
    
    def calculate_var(
        self,
        repo,
        portfolio_value: float,
        confidence: float = 0.95,
        use_live_data: bool = True
    ) -> float:
        """
        Calculate Value-at-Risk (VaR) for portfolio.
        
        VaR Definition: "With 95% confidence, daily loss will not exceed $X"
        
        Args:
            repo: PortfolioRepository instance
            portfolio_value: Current portfolio value ($)
            confidence: Confidence level (0.95 = 95%)
            use_live_data: Prefer live trading data if available
        
        Returns:
            VaR amount (negative value, e.g., -$2500)
        """
        try:
            # Check cache
            cache_key = f"var:{datetime.now().date()}:{confidence}"
            cached = cache.get(cache_key)
            if cached:
                logger.info("??VaR loaded from cache")
                return float(cached)
            
            # Get daily P&L data
            if use_live_data:
                daily_returns = repo.get_daily_pnl(days=self.lookback_days)
                if len(daily_returns) >= 7:  # At least 1 week
                    logger.info(f"??VaR using LIVE data ({len(daily_returns)} days)")
                    returns = daily_returns['daily_return'].values
                else:
                    logger.warning(f"?�️ VaR using BACKTEST data (live days: {len(daily_returns)})")
                    returns = self._get_backtest_portfolio_returns(repo)
            else:
                returns = self._get_backtest_portfolio_returns(repo)
            
            # Calculate VaR (percentile method)
            var_percentile = np.percentile(returns, (1 - confidence) * 100)
            var_amount = portfolio_value * var_percentile
            
            # Cache for 24 hours
            cache.set(cache_key, str(var_amount), ttl_seconds=self.cache_ttl)
            
            logger.info(f"?�� VaR ({confidence:.0%}): ${var_amount:,.2f} (Portfolio: ${portfolio_value:,.0f})")
            return var_amount
            
        except Exception as e:
            logger.error(f"VaR calculation failed: {e}", exc_info=True)
            # Conservative fallback: assume -3% daily risk
            return portfolio_value * -0.03
    
    def _get_backtest_portfolio_returns(self, repo) -> np.ndarray:
        """Get portfolio returns from backtest data."""
        # Simplified: Use SPY as market proxy
        end_date = datetime.now()
        start_date = end_date - timedelta(days=self.lookback_days)
        
        spy_data = repo.get_ohlcv_range('SPY', start_date, end_date, timeframe='1d')
        if len(spy_data) > 0:
            df = pd.DataFrame([{'close': bar.close} for bar in spy_data])
            returns = df['close'].pct_change().dropna().values
            return returns
        else:
            # Fallback: neutral returns
            return np.array([0.0] * 14)
    
    def kelly_criterion(
        self,
        repo,
        symbol: str,
        use_live_data: bool = True,
        kelly_fraction: float = 0.25
    ) -> float:
        """
        Calculate Kelly Criterion position size.
        
        Formula: f* = (bp - q) / b
        - b = profit/loss ratio (avg_win / avg_loss)
        - p = win rate
        - q = loss rate (1 - p)
        
        Args:
            repo: PortfolioRepository instance
            symbol: Stock symbol
            use_live_data: Prefer live trading data
            kelly_fraction: Safety factor (0.25 = 25% of full Kelly)
        
        Returns:
            Position size as fraction of portfolio (0.0 ~ 0.3)
        """
        try:
            # Check cache
            cache_key = f"kelly:{symbol}:{datetime.now().date()}"
            cached = cache.get(cache_key)
            if cached:
                logger.info(f"??Kelly for {symbol} loaded from cache")
                return float(cached)
            
            # Get trade history
            end_date = datetime.now()
            start_date = end_date - timedelta(days=self.lookback_days)
            
            if use_live_data:
                trades = repo.get_trade_history(symbol, start_date, end_date)
                if len(trades) >= 10:  # Minimum 10 trades for reliability
                    logger.info(f"??Kelly using LIVE data ({len(trades)} trades)")
                else:
                    logger.warning(f"?�️ Kelly using BACKTEST data (live trades: {len(trades)})")
                    trades = self._get_backtest_trades(repo, symbol)
            else:
                trades = self._get_backtest_trades(repo, symbol)
            
            if len(trades) < 5:
                logger.warning(f"{symbol}: Insufficient trade history, using conservative 10%")
                return 0.10
            
            # Calculate win rate and profit/loss ratio
            pnl_values = [t['pnl'] for t in trades]
            wins = [p for p in pnl_values if p > 0]
            losses = [p for p in pnl_values if p <= 0]
            
            win_rate = len(wins) / len(trades)
            avg_win = np.mean(wins) if wins else 0
            avg_loss = abs(np.mean(losses)) if losses else 1
            
            profit_loss_ratio = avg_win / avg_loss if avg_loss > 0 else 1
            
            # Kelly formula
            kelly = (profit_loss_ratio * win_rate - (1 - win_rate)) / profit_loss_ratio
            
            # Apply safety fraction and cap at 30%
            kelly_safe = max(0, min(kelly * kelly_fraction, 0.30))
            
            # Cache for 24 hours
            cache.set(cache_key, str(kelly_safe), ttl_seconds=self.cache_ttl)
            
            logger.info(
                f"{symbol} Kelly: {kelly_safe:.2%} "
                f"(Win Rate: {win_rate:.1%}, P/L Ratio: {profit_loss_ratio:.2f})"
            )
            return kelly_safe
            
        except Exception as e:
            logger.error(f"Kelly calculation failed for {symbol}: {e}", exc_info=True)
            return 0.10  # Conservative fallback
    
    def _get_backtest_trades(self, repo, symbol: str) -> List[Dict]:
        """Simulate trades from backtest data (mock for initial period)."""
        # Simplified: Generate mock trades based on historical returns
        # In production, this would use actual backtest results
        end_date = datetime.now()
        start_date = end_date - timedelta(days=self.lookback_days)
        
        ohlcv = repo.get_ohlcv_range(symbol, start_date, end_date, timeframe='15m')
        if len(ohlcv) < 10:
            return []
        
        # Mock trades: every 5th bar is a trade
        trades = []
        for i in range(0, len(ohlcv) - 5, 5):
            entry = ohlcv[i]
            exit_bar = ohlcv[i + 5]
            pnl = (exit_bar.close - entry.close) / entry.close
            trades.append({
                'entry_price': entry.close,
                'exit_price': exit_bar.close,
                'pnl': pnl
            })
        
        return trades
    
    def optimize_weights(
        self,
        repo,
        symbols: List[str],
        target_return: Optional[float] = None
    ) -> Dict[str, float]:
        """
        Optimize portfolio weights using Modern Portfolio Theory.
        
        Objective: Maximize Sharpe ratio
        Constraints:
        - Sum of weights = 1.0
        - Each weight: 0.0 ~ 0.30 (max 30% per symbol)
        
        Args:
            repo: PortfolioRepository instance
            symbols: List of symbols
            target_return: Target portfolio return (optional)
        
        Returns:
            Dict of {symbol: weight}
        """
        try:
            # Get correlation matrix and returns
            corr_matrix = self.calculate_correlation_matrix(repo, symbols)
            
            # Mean returns (simplified: use backtest data)
            end_date = datetime.now()
            start_date = end_date - timedelta(days=self.lookback_days)
            
            mean_returns = {}
            for symbol in symbols:
                ohlcv = repo.get_ohlcv_range(symbol, start_date, end_date, timeframe='15m')
                if len(ohlcv) > 0:
                    df = pd.DataFrame([{'close': bar.close} for bar in ohlcv])
                    returns = df['close'].pct_change().dropna()
                    mean_returns[symbol] = returns.mean()
                else:
                    mean_returns[symbol] = 0.0
            
            # Optimization setup
            n_assets = len(symbols)
            init_weights = np.array([1.0 / n_assets] * n_assets)
            
            def neg_sharpe(weights):
                """Negative Sharpe ratio (for minimization)."""
                portfolio_return = np.dot(weights, [mean_returns[s] for s in symbols])
                portfolio_std = np.sqrt(np.dot(weights.T, np.dot(corr_matrix, weights)))
                sharpe = portfolio_return / portfolio_std if portfolio_std > 0 else 0
                return -sharpe
            
            # Constraints
            constraints = [
                {'type': 'eq', 'fun': lambda w: np.sum(w) - 1.0}  # Sum = 1
            ]
            
            # Bounds: 0% ~ 30% per symbol
            bounds = tuple((0.0, 0.30) for _ in range(n_assets))
            
            # Optimize
            result = minimize(
                neg_sharpe,
                init_weights,
                method='SLSQP',
                bounds=bounds,
                constraints=constraints
            )
            
            if result.success:
                weights_dict = {symbols[i]: result.x[i] for i in range(n_assets)}
                logger.info(f"??MPT Optimized weights: {weights_dict}")
                return weights_dict
            else:
                logger.warning("MPT optimization failed, using equal weights")
                return {s: 1.0 / n_assets for s in symbols}
            
        except Exception as e:
            logger.error(f"Weight optimization failed: {e}", exc_info=True)
            # Fallback: equal weights
            return {s: 1.0 / len(symbols) for s in symbols}
