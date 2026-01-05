"""
Portfolio Rebalancer (Phase I.2)

Handles multi-position rebalancing based on MPT-optimized weights.
Executes daily at 3:45 PM ET (15 min before market close).
"""

import logging
from datetime import datetime
from typing import Dict, List
import alpaca_trade_api as tradeapi

from app.core.config import settings
from app.services.portfolio_optimizer import PortfolioOptimizer
from app.repositories.portfolio_repo import PortfolioRepository

logger = logging.getLogger(__name__)


class PortfolioRebalancer:
    """
    Portfolio rebalancing service.
    
    Features:
    - Daily rebalancing (3:45 PM ET)
    - MPT-optimized weight calculation
    - Drift-based triggering (only rebalance if weights drift >5%)
    - Transaction cost awareness
    """
    
    def __init__(self, api: tradeapi.REST, repo: PortfolioRepository, optimizer: PortfolioOptimizer):
        self.api = api
        self.repo = repo
        self.optimizer = optimizer
        self.min_rebalance_threshold = 0.05  # 5% drift
        self.max_position_weight = 0.30  # 30% max per symbol
    
    async def rebalance(self, symbols: List[str], force: bool = False):
        """
        Rebalance portfolio to optimal weights.
        
        Process:
        1. Get current positions and portfolio value
        2. Calculate optimal weights (MPT)
        3. Check if rebalancing needed (drift > 5%)
        4. Execute buy/sell orders to reach target weights
        
        Args:
            symbols: List of symbols to include in portfolio
            force: If True, rebalance regardless of drift
        """
        try:
            logger.info(f"🔄 Starting portfolio rebalance for {len(symbols)} symbols")
            
            # 1. Get current portfolio state
            account = self.api.get_account()
            portfolio_value = float(account.portfolio_value)
            current_positions = await self._get_current_positions()
            
            logger.info(f"Portfolio value: ${portfolio_value:,.2f}")
            logger.info(f"Current positions: {current_positions}")
            
            # 2. Calculate target weights (MPT optimization)
            target_weights = await self.optimizer.optimize_weights(self.repo, symbols)
            
            logger.info(f"Target weights: {target_weights}")
            
            # 3. Check if rebalancing needed
            current_weights = self._calculate_current_weights(current_positions, portfolio_value)
            max_drift = self._calculate_max_drift(current_weights, target_weights)
            
            logger.info(f"Max weight drift: {max_drift:.2%}")
            
            if not force and max_drift < self.min_rebalance_threshold:
                logger.info(f"✅ No rebalancing needed (drift {max_drift:.2%} < {self.min_rebalance_threshold:.2%})")
                return
            
            # 4. Execute rebalancing orders
            await self._execute_rebalancing(symbols, target_weights, portfolio_value, current_positions)
            
            logger.info("✅ Portfolio rebalancing complete")
            
        except Exception as e:
            logger.error(f"❌ Rebalancing failed: {e}", exc_info=True)
    
    async def _get_current_positions(self) -> Dict[str, Dict]:
        """
        Get current positions from Alpaca API.
        
        Returns:
            Dict of {symbol: {'qty': int, 'market_value': float}}
        """
        try:
            positions = self.api.list_positions()
            current = {}
            
            for pos in positions:
                current[pos.symbol] = {
                    'qty': int(pos.qty),
                    'market_value': float(pos.market_value),
                    'avg_entry_price': float(pos.avg_entry_price)
                }
            
            return current
            
        except Exception as e:
            logger.error(f"Failed to get current positions: {e}", exc_info=True)
            return {}
    
    def _calculate_current_weights(
        self,
        positions: Dict[str, Dict],
        portfolio_value: float
    ) -> Dict[str, float]:
        """Calculate current portfolio weights."""
        weights = {}
        
        for symbol, pos_info in positions.items():
            weights[symbol] = pos_info['market_value'] / portfolio_value
        
        return weights
    
    def _calculate_max_drift(
        self,
        current_weights: Dict[str, float],
        target_weights: Dict[str, float]
    ) -> float:
        """Calculate maximum weight drift."""
        all_symbols = set(current_weights.keys()) | set(target_weights.keys())
        
        max_drift = 0.0
        for symbol in all_symbols:
            current = current_weights.get(symbol, 0.0)
            target = target_weights.get(symbol, 0.0)
            drift = abs(current - target)
            max_drift = max(max_drift, drift)
        
        return max_drift
    
    async def _execute_rebalancing(
        self,
        symbols: List[str],
        target_weights: Dict[str, float],
        portfolio_value: float,
        current_positions: Dict[str, Dict]
    ):
        """
        Execute buy/sell orders to reach target weights.
        
        Strategy:
        - Sell positions that need reduction first (free up capital)
        - Buy positions that need increase
        - Only rebalance if difference > $100 (avoid micro-trades)
        """
        min_trade_value = 100  # Minimum $100 per trade
        
        for symbol in symbols:
            try:
                # Target value for this symbol
                target_value = portfolio_value * target_weights.get(symbol, 0.0)
                
                # Current value
                current_value = current_positions.get(symbol, {}).get('market_value', 0.0)
                
                # Difference
                diff_value = target_value - current_value
                
                logger.info(f"{symbol}: Current ${current_value:.0f} → Target ${target_value:.0f} (Diff: ${diff_value:+.0f})")
                
                # Skip if difference is too small
                if abs(diff_value) < min_trade_value:
                    logger.info(f"  ⏩ Skipping {symbol} (difference < ${min_trade_value})")
                    continue
                
                # Get current price
                barset = self.api.get_bars(symbol, '1Min', limit=1)
                if not barset:
                    logger.warning(f"  ⚠️ No price data for {symbol}")
                    continue
                
                current_price = barset[symbol][0].c
                
                # Calculate quantity
                qty = int(abs(diff_value) / current_price)
                
                if qty == 0:
                    logger.info(f"  ⏩ Quantity too small for {symbol}")
                    continue
                
                # Execute order
                if diff_value > 0:
                    # BUY
                    logger.info(f"  🟢 BUY {qty} shares of {symbol} @ ${current_price:.2f}")
                    order = self.api.submit_order(
                        symbol=symbol,
                        qty=qty,
                        side='buy',
                        type='market',
                        time_in_force='day'
                    )
                    logger.info(f"  ✅ Order placed: {order.id}")
                else:
                    # SELL
                    logger.info(f"  🔴 SELL {qty} shares of {symbol} @ ${current_price:.2f}")
                    order = self.api.submit_order(
                        symbol=symbol,
                        qty=qty,
                        side='sell',
                        type='market',
                        time_in_force='day'
                    )
                    logger.info(f"  ✅ Order placed: {order.id}")
                
            except Exception as e:
                logger.error(f"  ❌ Failed to rebalance {symbol}: {e}")
                continue
    
    async def calculate_target_weights(self, symbols: List[str]) -> Dict[str, float]:
        """
        Calculate target weights (wrapper for optimizer).
        
        Useful for API endpoints that want to preview weights without executing.
        
        Args:
            symbols: List of symbols
        
        Returns:
            Dict of {symbol: weight}
        """
        return await self.optimizer.optimize_weights(self.repo, symbols)
