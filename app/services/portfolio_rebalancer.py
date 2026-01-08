"""
포트폴리오 리밸런서 (Phase I.2)

MPT 최적화 가중치 기반의 다중 포지션 리밸런싱을 처리합니다.
매일 미국 동부시간 15:45(장 마감 15분 전)에 실행됩니다.
"""

import logging
from datetime import datetime
from typing import Dict, List
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

from app.core.config import settings
from app.services.portfolio_optimizer import PortfolioOptimizer
from app.repositories.portfolio_repo import PortfolioRepository

logger = logging.getLogger(__name__)


class PortfolioRebalancer:
    """
    포트폴리오 리밸런싱 서비스입니다.

    특징:
    - 일일 리밸런싱 (15:45 ET)
    - MPT 기반 목표 가중치 계산
    - 드리프트 기반 트리거(가중치 차이가 5% 초과 시 리밸런싱)
    - 거래비용 고려
    """
    
    def __init__(self, api: TradingClient, repo: PortfolioRepository, optimizer: PortfolioOptimizer):
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
            logger.info(f"{len(symbols)}개 심볼에 대해 포트폴리오 리밸런싱 시작")
            
            # 1. Get current portfolio state
            account = self.api.get_account()
            portfolio_value = float(account.portfolio_value)
            current_positions = await self._get_current_positions()
            
            logger.info(f"포트폴리오 가치: ${portfolio_value:,.2f}")
            logger.info(f"현재 포지션: {current_positions}")
            
            # 2. Calculate target weights (MPT optimization)
            target_weights = await self.optimizer.optimize_weights(self.repo, symbols)
            
            logger.info(f"목표 가중치: {target_weights}")
            
            # 3. Check if rebalancing needed
            current_weights = self._calculate_current_weights(current_positions, portfolio_value)
            max_drift = self._calculate_max_drift(current_weights, target_weights)
            
            logger.info(f"최대 가중치 드리프트: {max_drift:.2%}")
            
            if not force and max_drift < self.min_rebalance_threshold:
                logger.info(f"리밸런싱 불필요 (드리프트 {max_drift:.2%} < {self.min_rebalance_threshold:.2%})")
                return
            
            # 4. Execute rebalancing orders
            await self._execute_rebalancing(symbols, target_weights, portfolio_value, current_positions)
            
            logger.info("포트폴리오 리밸런싱 완료")
            
        except Exception as e:
            logger.error(f"리밸런싱 실패: {e}", exc_info=True)
    
    async def _get_current_positions(self) -> Dict[str, Dict]:
        """
        Get current positions from Alpaca API.
        
        Returns:
            Dict of {symbol: {'qty': int, 'market_value': float}}
        """
        try:
            positions = self.api.get_all_positions()
            current = {}
            
            for pos in positions:
                current[pos.symbol] = {
                    'qty': int(pos.qty),
                    'market_value': float(pos.market_value),
                    'avg_entry_price': float(pos.avg_entry_price)
                }
            
            return current
            
        except Exception as e:
            logger.error(f"현재 포지션 조회 실패: {e}", exc_info=True)
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
                
                logger.info(f"{symbol}: 현재 ${current_value:.0f} → 목표 ${target_value:.0f} (차이: ${diff_value:+.0f})")
                
                # Skip if difference is too small
                if abs(diff_value) < min_trade_value:
                    logger.info(f"  {symbol} 건너뜀 (차이 < ${min_trade_value})")
                    continue
                
                # Get current price from existing position or use market price
                if symbol in current_positions:
                    # Use average entry price as estimate for current price
                    current_price = current_positions[symbol].get('avg_entry_price', 0.0)
                    if current_price <= 0:
                        logger.warning(f"  잘못된 가격 데이터 {symbol}")
                        continue
                else:
                    # For new positions, we need to get price from DB or skip
                    # This should be handled by data provider in production
                    logger.warning(f"  신규 심볼 {symbol}에 대한 가격 데이터 없음 — 건너뜀")
                    continue
                
                # Calculate quantity
                qty = int(abs(diff_value) / current_price)
                
                if qty == 0:
                    logger.info(f"  {symbol}: 주문 수량 부족")
                    continue
                
                # Execute order
                if diff_value > 0:
                    # BUY
                    logger.info(f"  BUY {symbol} {qty}주 @ ${current_price:.2f}")
                    order_data = MarketOrderRequest(
                        symbol=symbol,
                        qty=qty,
                        side=OrderSide.BUY,
                        time_in_force=TimeInForce.DAY
                    )
                    order = self.api.submit_order(order_data=order_data)
                    logger.info(f"  주문 접수: {order.id}")
                else:
                    # SELL
                    logger.info(f"  SELL {symbol} {qty}주 @ ${current_price:.2f}")
                    order_data = MarketOrderRequest(
                        symbol=symbol,
                        qty=qty,
                        side=OrderSide.SELL,
                        time_in_force=TimeInForce.DAY
                    )
                    order = self.api.submit_order(order_data=order_data)
                    logger.info(f"  주문 접수: {order.id}")
                
            except Exception as e:
                logger.error(f"  리밸런싱 실패 {symbol}: {e}")
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
