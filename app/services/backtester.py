import logging

import numpy as np
import pandas as pd
from app.services.trading_strategy import TradingStrategyEngine

from app.domain.schemas.stock import StockOHLCVCreate

logger = logging.getLogger(__name__)

class Backtester:
    """
    과거 데이터에 대해 트레이딩 전략을 시뮬레이션합니다.
    """
    def __init__(self, strategy_engine: TradingStrategyEngine, initial_capital: float = 100000.0):
        self.engine = strategy_engine
        self.initial_capital = initial_capital
        self.capital = initial_capital
        self.positions: dict[str, int] = {} # Symbol -> Qty
        self.trades: list[dict] = []
        self.equity_curve: list[dict] = []

    def run(self, symbol: str, data: list[StockOHLCVCreate]):
        """
        Replay history bar by bar using a sliding window.
        Strategy needs min ~50 bars for TA-Lib.
        """
        logger.info(f"백테스트 시작: {symbol}, 바 수={len(data)}")

        df = pd.DataFrame([d.model_dump() for d in data])
        df.sort_values("date_time", inplace=True)

        # We need a window. Let's start from index 50
        min_window = 50

        for i in range(min_window, len(df)):
            # Slice window: 0 to i (inclusive of i-th bar as "current" close?)
            # CAUTION: If we include i-th bar, we assume we trade AT CLOSE of i.
            # Realistically, we calculate on i-1 CLOSE and trade at i OPEN.
            # Simplification: Calculate on i CLOSE, Trade at i CLOSE price.

            window = df.iloc[0 : i+1]
            current_bar = df.iloc[i]
            current_price = float(current_bar["close"])
            date = current_bar["date_time"]

            # Generate Signal
            prediction, _ = self.engine.generate_signal(window)

            # Logic (Simplified from Strategy Engine)
            if prediction > 0.8:
                # BUY
                cost = current_price * 10
                if self.capital >= cost:
                    self.capital -= cost
                    self.positions[symbol] = self.positions.get(symbol, 0) + 10
                    self.trades.append({"type": "BUY", "price": current_price, "date": date, "qty": 10})

            elif prediction < 0.2:
                # SELL
                qty = self.positions.get(symbol, 0)
                if qty > 0:
                    self.capital += current_price * qty
                    self.positions[symbol] = 0
                    self.trades.append({"type": "SELL", "price": current_price, "date": date, "qty": qty})

            # Track Equity
            position_value = self.positions.get(symbol, 0) * current_price
            total_equity = self.capital + position_value
            self.equity_curve.append({"date": date, "equity": total_equity})

        logger.info(f"백테스트 완료. 최종 자산: {self.equity_curve[-1]['equity']}")
        return self.equity_curve


# ============================================================================
# Phase F.4: Monte Carlo Simulation for Portfolio Stress Testing
# ============================================================================

class MonteCarloSimulator:
    """
    포트폴리오 스트레스 테스트를 위한 몬테카를로 시뮬레이션.

    과거 수익률, 변동성, 상관관계를 기반으로 여러 시나리오를 생성하여 위험을 평가합니다.
    """

    def __init__(self, num_simulations: int = 10000, time_horizon_days: int = 252):
        """
        Initialize Monte Carlo simulator.
        
        Args:
            num_simulations: Number of simulation paths to generate
            time_horizon_days: Simulation horizon in trading days (252 = 1 year)
        """
        self.num_simulations = num_simulations
        self.time_horizon_days = time_horizon_days

    def simulate_portfolio(
        self,
        initial_value: float,
        expected_returns: np.ndarray,
        volatilities: np.ndarray,
        correlation_matrix: np.ndarray,
        weights: np.ndarray
    ) -> dict:
        """
        Run Monte Carlo simulation for a portfolio.
        
        Args:
            initial_value: Starting portfolio value (e.g., $100,000)
            expected_returns: Expected daily returns for each asset (array of size N)
            volatilities: Daily volatility (std dev) for each asset (array of size N)
            correlation_matrix: Correlation matrix between assets (N x N)
            weights: Portfolio weights (array of size N, must sum to 1.0)
        
        Returns:
            Dict with simulation results:
            - 'final_values': Array of final portfolio values (length = num_simulations)
            - 'percentiles': Dict with 5th, 25th, 50th, 75th, 95th percentiles
            - 'mean_final_value': Average final value across all simulations
            - 'var_95': Value at Risk (95% confidence level)
            - 'cvar_95': Conditional VaR (expected loss beyond VaR)
            - 'probability_of_loss': Probability of ending below initial value
        """
        logger.info(f"몬테카를로 시뮬레이션 실행: 경로={self.num_simulations}, 기간={self.time_horizon_days}일")

        num_assets = len(expected_returns)

        # Validate inputs
        assert len(volatilities) == num_assets, "Volatilities length mismatch"
        assert correlation_matrix.shape == (num_assets, num_assets), "Correlation matrix shape mismatch"
        assert len(weights) == num_assets, "Weights length mismatch"
        assert np.isclose(weights.sum(), 1.0), f"Weights must sum to 1.0 (current: {weights.sum()})"

        # Convert correlation to covariance matrix
        # Cov = Corr * (σ_i * σ_j)
        volatility_matrix = np.outer(volatilities, volatilities)
        covariance_matrix = correlation_matrix * volatility_matrix

        # Cholesky decomposition for correlated random returns
        try:
            cholesky_matrix = np.linalg.cholesky(covariance_matrix)
        except np.linalg.LinAlgError:
            logger.warning("Covariance matrix not positive definite. Using pseudo-inverse.")
            # Fallback: Use pseudo-inverse or adjust diagonal
            covariance_matrix += np.eye(num_assets) * 1e-6
            cholesky_matrix = np.linalg.cholesky(covariance_matrix)

        # Storage for simulation results
        final_values = np.zeros(self.num_simulations)

        # Run simulations
        for sim_idx in range(self.num_simulations):
            # Generate correlated random returns for each day
            portfolio_value = initial_value

            for day in range(self.time_horizon_days):
                # Generate uncorrelated random returns (standard normal)
                random_returns = np.random.randn(num_assets)

                # Apply correlation via Cholesky matrix
                correlated_returns = cholesky_matrix @ random_returns

                # Add expected returns (drift)
                daily_returns = expected_returns + correlated_returns

                # Calculate portfolio return (weighted sum)
                portfolio_return = np.dot(weights, daily_returns)

                # Update portfolio value
                portfolio_value *= (1 + portfolio_return)

            final_values[sim_idx] = portfolio_value

        # Calculate statistics
        mean_final_value = np.mean(final_values)
        median_final_value = np.median(final_values)

        percentiles = {
            '5th': np.percentile(final_values, 5),
            '25th': np.percentile(final_values, 25),
            '50th': median_final_value,
            '75th': np.percentile(final_values, 75),
            '95th': np.percentile(final_values, 95)
        }

        # Value at Risk (VaR): 95% confidence = 5th percentile loss
        var_95 = initial_value - percentiles['5th']

        # Conditional VaR (CVaR): Average loss beyond VaR
        losses_beyond_var = final_values[final_values <= percentiles['5th']]
        cvar_95 = initial_value - np.mean(losses_beyond_var) if len(losses_beyond_var) > 0 else 0

        # Probability of loss
        probability_of_loss = np.sum(final_values < initial_value) / self.num_simulations

        logger.info("몬테카를로 결과:")
        logger.info(f"  평균 최종값: ${mean_final_value:,.2f}")
        logger.info(f"  중앙값: ${median_final_value:,.2f}")
        logger.info(f"  5퍼센타일: ${percentiles['5th']:,.2f}")
        logger.info(f"  95퍼센타일: ${percentiles['95th']:,.2f}")
        logger.info(f"  VaR (95%): ${var_95:,.2f}")
        logger.info(f"  CVaR (95%): ${cvar_95:,.2f}")
        logger.info(f"  손실 확률: {probability_of_loss:.2%}")

        return {
            'final_values': final_values.tolist(),
            'percentiles': percentiles,
            'mean_final_value': mean_final_value,
            'median_final_value': median_final_value,
            'var_95': var_95,
            'cvar_95': cvar_95,
            'probability_of_loss': probability_of_loss,
            'num_simulations': self.num_simulations,
            'time_horizon_days': self.time_horizon_days
        }

    def simulate_single_asset(
        self,
        initial_value: float,
        expected_daily_return: float,
        daily_volatility: float
    ) -> dict:
        """
        Simplified Monte Carlo for a single asset.
        
        Args:
            initial_value: Starting value
            expected_daily_return: Expected daily return (e.g., 0.001 = 0.1%)
            daily_volatility: Daily volatility (e.g., 0.02 = 2%)
        
        Returns:
            Same structure as simulate_portfolio
        """
        logger.info(f"단일 자산 몬테카를로 실행: 경로={self.num_simulations}")

        final_values = np.zeros(self.num_simulations)

        for sim_idx in range(self.num_simulations):
            value = initial_value

            for day in range(self.time_horizon_days):
                # Geometric Brownian Motion (GBM)
                random_shock = np.random.randn()
                daily_return = expected_daily_return + daily_volatility * random_shock
                value *= (1 + daily_return)

            final_values[sim_idx] = value

        # Calculate statistics (same as portfolio version)
        mean_final_value = np.mean(final_values)
        median_final_value = np.median(final_values)

        percentiles = {
            '5th': np.percentile(final_values, 5),
            '25th': np.percentile(final_values, 25),
            '50th': median_final_value,
            '75th': np.percentile(final_values, 75),
            '95th': np.percentile(final_values, 95)
        }

        var_95 = initial_value - percentiles['5th']
        losses_beyond_var = final_values[final_values <= percentiles['5th']]
        cvar_95 = initial_value - np.mean(losses_beyond_var) if len(losses_beyond_var) > 0 else 0
        probability_of_loss = np.sum(final_values < initial_value) / self.num_simulations

        logger.info("단일 자산 몬테카를로 결과:")
        logger.info(f"  평균 최종값: ${mean_final_value:,.2f}")
        logger.info(f"  VaR (95%): ${var_95:,.2f}")
        logger.info(f"  손실 확률: {probability_of_loss:.2%}")

        return {
            'final_values': final_values.tolist(),
            'percentiles': percentiles,
            'mean_final_value': mean_final_value,
            'median_final_value': median_final_value,
            'var_95': var_95,
            'cvar_95': cvar_95,
            'probability_of_loss': probability_of_loss,
            'num_simulations': self.num_simulations,
            'time_horizon_days': self.time_horizon_days
        }
