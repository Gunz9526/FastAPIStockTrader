import logging
from datetime import datetime, timedelta
from io import StringIO

import numpy as np
import pandas as pd
from scipy.optimize import minimize

from app.core.cache import cache

logger = logging.getLogger(__name__)


class PortfolioOptimizer:
    """
    MPT, Kelly 기준 및 VaR을 사용하는 포트폴리오 최적화 도구입니다.

    특징:
    - 상관 행렬 계산 (롤링 14일 윈도우)
    - Value-at-Risk (VaR) 추정 (95% 신뢰구간)
    - Kelly 기준에 따른 포지션 사이징
    - 샤프 비율 최대화 (MPT)
    - 백테스트에서 라이브 데이터로 자동 전환
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
        symbols: list[str],
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
            # 캐시 먼저 확인
            cache_key = f"corr_matrix:{datetime.now().date()}"
            cached = cache.get(cache_key) if cache else None
            if cached:
                logger.info("캐시에서 상관 행렬 로드됨")
                return pd.read_json(StringIO(cached))

            # Decide data source
            if use_live_data:
                live_trade_count = repo.count_live_trades(days=self.lookback_days)
                use_live = live_trade_count >= self.min_live_trades
            else:
                use_live = False
                live_trade_count = 0

            if use_live:
                logger.info(f"실거래 데이터 사용 ({live_trade_count} 건)")
                returns_data = self._get_live_returns(repo, symbols)
            else:
                logger.warning(f"백테스트 데이터 사용 (실거래: {live_trade_count}/{self.min_live_trades})")
                returns_data = self._get_backtest_returns(repo, symbols)

            # Calculate correlation
            # Check minimum data
            non_empty = {k: v for k, v in returns_data.items() if len(v) > 0}
            if len(non_empty) < 2:
                logger.warning("수익률 데이터 부족 — 단위 행렬을 사용합니다")
                return pd.DataFrame(np.eye(len(symbols)), index=symbols, columns=symbols)

            # Align by index (date-based for backtest, position-based for live)
            if isinstance(next(iter(non_empty.values())), pd.Series):
                # Date-indexed Series: align on shared dates
                df_returns = pd.concat(non_empty, axis=1).dropna()
                n_samples = len(df_returns)
            else:
                # Numpy arrays (live trades): align by shortest length
                min_length = min(len(v) for v in non_empty.values())
                if min_length == 0:
                    return pd.DataFrame(np.eye(len(symbols)), index=symbols, columns=symbols)
                aligned = {k: v[:min_length] for k, v in non_empty.items()}
                df_returns = pd.DataFrame(aligned)
                n_samples = min_length

            if df_returns.empty or len(df_returns) < 2:
                logger.warning("정렬 후 데이터 부족 — 단위 행렬을 사용합니다")
                return pd.DataFrame(np.eye(len(symbols)), index=symbols, columns=symbols)

            corr_matrix = df_returns.corr()

            # Cache for 24 hours
            cache.set(cache_key, corr_matrix.to_json(), ttl_seconds=self.cache_ttl)

            logger.info(f"상관 행렬 계산 완료 ({n_samples} 샘플):\n{corr_matrix}")
            return corr_matrix

        except Exception as e:
            logger.error(f"상관 행렬 계산 실패: {e}", exc_info=True)
            # Return identity matrix as fallback (no correlation)
            return pd.DataFrame(np.eye(len(symbols)), index=symbols, columns=symbols)

    def _get_live_returns(self, repo, symbols: list[str]) -> dict[str, np.ndarray]:
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

    def _get_backtest_returns(self, repo, symbols: list[str]) -> dict[str, pd.Series]:
        """Get returns from historical OHLCV data (backtest).

        Returns date-indexed Series so that correlation matrix alignment
        uses matching timestamps rather than positional truncation.

        Args:
            repo: Repository instance
            symbols: List of stock symbols

        Returns:
            Dict of {symbol: pd.Series} with datetime index
        """
        returns_data: dict[str, pd.Series] = {}
        end_date = datetime.now()
        start_date = end_date - timedelta(days=self.lookback_days)

        for symbol in symbols:
            ohlcv = repo.get_ohlcv_range(symbol, start_date, end_date, timeframe='1d')
            if len(ohlcv) > 0:
                df = pd.DataFrame([{
                    'date_time': bar.date_time,
                    'close': bar.close
                } for bar in ohlcv])
                df.set_index('date_time', inplace=True)
                df.sort_index(inplace=True)
                returns = df['close'].pct_change().dropna()
                returns_data[symbol] = returns
            else:
                returns_data[symbol] = pd.Series(dtype=float)

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
                logger.info("캐시에서 VaR 로드됨")
                return float(cached)

            # Get daily P&L data
            if use_live_data:
                daily_returns = repo.get_daily_pnl(days=self.lookback_days)
                if len(daily_returns) >= 7:  # 최소 1주
                    logger.info(f"라이브 데이터로 VaR 계산 중 ({len(daily_returns)} 일)")
                    returns = daily_returns['daily_return'].values
                else:
                    logger.warning(f"백테스트 데이터로 VaR 계산 (라이브 일수: {len(daily_returns)})")
                    returns = self._get_backtest_portfolio_returns(repo)
            else:
                returns = self._get_backtest_portfolio_returns(repo)

            # Calculate VaR (percentile method)
            var_percentile = np.percentile(returns, (1 - confidence) * 100)
            var_amount = portfolio_value * var_percentile

            # Cache for 24 hours
            cache.set(cache_key, str(var_amount), ttl_seconds=self.cache_ttl)

            logger.info(f"VaR ({confidence:.0%}): ${var_amount:,.2f} (포트폴리오: ${portfolio_value:,.0f})")
            return var_amount

        except Exception as e:
            logger.error(f"VaR 계산 실패: {e}", exc_info=True)
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
                logger.info(f"{symbol}의 Kelly 지수 캐시 로드됨")
                return float(cached)

            # Get trade history
            end_date = datetime.now()
            start_date = end_date - timedelta(days=self.lookback_days)

            if use_live_data:
                trades = repo.get_trade_history(symbol, start_date, end_date)
                if len(trades) >= 10:  # 신뢰성을 위한 최소 거래 수
                    logger.info(f"라이브 거래로 Kelly 계산 ({len(trades)} 건)")
                else:
                    logger.warning(f"백테스트 데이터로 Kelly 계산 (실거래: {len(trades)} 건)")
                    trades = self._get_backtest_trades(repo, symbol)
            else:
                trades = self._get_backtest_trades(repo, symbol)

            if len(trades) < 5:
                logger.warning(f"{symbol}: 거래 이력 부족 — 보수적으로 10% 사용")
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
                f"(승률: {win_rate:.1%}, 손익비: {profit_loss_ratio:.2f})"
            )
            return kelly_safe

        except Exception as e:
            logger.error(f"{symbol}의 Kelly 계산 실패: {e}", exc_info=True)
            return 0.10  # Conservative fallback

    def _get_backtest_trades(self, repo, symbol: str) -> list[dict]:
        """
        Simulate trades from backtest data using SMA crossover strategy.

        Uses SMA(5) vs SMA(20) crossover on 15-min bars to generate
        realistic trade entries/exits. Falls back to empty list when
        insufficient data.

        Args:
            repo: Repository instance with get_ohlcv_range()
            symbol: Stock symbol

        Returns:
            List of trade dicts with entry_price, exit_price, pnl
        """
        end_date = datetime.now()
        start_date = end_date - timedelta(days=self.lookback_days)

        ohlcv = repo.get_ohlcv_range(symbol, start_date, end_date, timeframe='1d')
        if len(ohlcv) < 25:  # Need at least SMA(20) + buffer
            return []

        closes = [bar.close for bar in ohlcv]

        # Simple SMA crossover: SMA(5) vs SMA(20)
        trades: list[dict] = []
        in_trade = False
        entry_price = 0.0

        for i in range(20, len(closes)):
            sma_fast = sum(closes[i - 5:i]) / 5
            sma_slow = sum(closes[i - 20:i]) / 20

            if not in_trade and sma_fast > sma_slow:
                # Entry signal: fast crosses above slow
                entry_price = closes[i]
                in_trade = True
            elif in_trade and sma_fast < sma_slow:
                # Exit signal: fast crosses below slow
                exit_price = closes[i]
                pnl = (exit_price - entry_price) / entry_price
                trades.append({
                    'entry_price': entry_price,
                    'exit_price': exit_price,
                    'pnl': pnl
                })
                in_trade = False

        return trades

    def optimize_weights(
        self,
        repo,
        symbols: list[str],
        target_return: float | None = None
    ) -> dict[str, float]:
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

            # Filter to symbols present in correlation matrix
            valid_symbols = [s for s in symbols if s in corr_matrix.columns]
            if len(valid_symbols) < 2:
                logger.warning("유효 심볼 부족 (%d개) — 균등 가중치 사용", len(valid_symbols))
                return {s: 1.0 / len(symbols) for s in symbols}
            corr_matrix = corr_matrix.loc[valid_symbols, valid_symbols]

            # Mean returns (simplified: use backtest data)
            end_date = datetime.now()
            start_date = end_date - timedelta(days=self.lookback_days)

            mean_returns = {}
            for symbol in valid_symbols:
                ohlcv = repo.get_ohlcv_range(symbol, start_date, end_date, timeframe='1d')
                if len(ohlcv) > 0:
                    df = pd.DataFrame([{'close': bar.close} for bar in ohlcv])
                    returns = df['close'].pct_change().dropna()
                    mean_returns[symbol] = returns.mean()
                else:
                    mean_returns[symbol] = 0.0

            # Compute per-symbol std for covariance conversion
            std_returns_list: list[float] = []
            for symbol in valid_symbols:
                ohlcv = repo.get_ohlcv_range(symbol, start_date, end_date, timeframe='1d')
                if len(ohlcv) > 1:
                    df_std = pd.DataFrame([{'close': bar.close} for bar in ohlcv])
                    rets = df_std['close'].pct_change().dropna()
                    std_returns_list.append(rets.std())
                else:
                    std_returns_list.append(0.01)  # fallback small std

            std_arr = np.array(std_returns_list)
            cov_matrix = corr_matrix * np.outer(std_arr, std_arr)

            # Optimization setup
            n_assets = len(valid_symbols)
            init_weights = np.array([1.0 / n_assets] * n_assets)

            def neg_sharpe(weights: np.ndarray) -> float:
                """Negative Sharpe ratio (for minimization)."""
                portfolio_return = np.dot(weights, [mean_returns[s] for s in valid_symbols])
                portfolio_std = np.sqrt(np.dot(weights.T, np.dot(cov_matrix, weights)))
                if portfolio_std <= 0:
                    return float('inf')
                return -(portfolio_return / portfolio_std)

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
                # Build result — excluded symbols get weight 0.0
                weights_dict = {s: float(w) for s, w in zip(valid_symbols, result.x)}
                for s in symbols:
                    if s not in weights_dict:
                        weights_dict[s] = 0.0
                logger.info("MPT 최적화 가중치: %s", weights_dict)
                return weights_dict
            else:
                logger.warning("MPT 최적화 실패 — 균등 가중치 사용")
                return {s: 1.0 / len(symbols) for s in symbols}

        except Exception as e:
            logger.error("가중치 최적화 실패: %s", e, exc_info=True)
            # Fallback: equal weights
            return {s: 1.0 / len(symbols) for s in symbols}
