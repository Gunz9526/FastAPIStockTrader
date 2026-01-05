"""
Portfolio Repository (Phase I.2)

Database access layer for multi-position portfolio management.
Provides methods for P&L tracking, trade history, and position queries.
"""

import logging
from datetime import datetime, timedelta
from typing import List, Dict, Optional
import pandas as pd
from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models.stock import PositionTracking, OHLCV15m

logger = logging.getLogger(__name__)


class PortfolioRepository:
    """
    Repository for portfolio-level queries.
    
    Features:
    - Daily P&L aggregation
    - Trade history retrieval
    - Multi-position management
    - Live trade counting
    """
    
    def __init__(self, session: AsyncSession):
        self.session = session
    
    async def get_daily_pnl(self, days: int = 14) -> pd.DataFrame:
        """
        Get daily portfolio P&L for the last N days.
        
        Args:
            days: Number of days to retrieve
        
        Returns:
            DataFrame with columns: [date, daily_return]
            
        Example:
            date       | daily_return
            -----------|-------------
            2026-01-04 | -0.0215
            2026-01-03 |  0.0342
        """
        try:
            cutoff_date = datetime.now() - timedelta(days=days)
            
            query = select(
                func.date(PositionTracking.exit_time).label('date'),
                func.sum(
                    (PositionTracking.exit_price - PositionTracking.entry_price) * PositionTracking.quantity
                    / (PositionTracking.entry_price * PositionTracking.quantity)
                ).label('daily_return')
            ).where(
                and_(
                    PositionTracking.exit_time >= cutoff_date,
                    PositionTracking.exit_time.isnot(None)
                )
            ).group_by(
                func.date(PositionTracking.exit_time)
            ).order_by(
                func.date(PositionTracking.exit_time).desc()
            )
            
            result = await self.session.execute(query)
            rows = result.all()
            
            if not rows:
                logger.warning("No daily P&L data found")
                return pd.DataFrame(columns=['date', 'daily_return'])
            
            df = pd.DataFrame([{
                'date': row.date,
                'daily_return': row.daily_return
            } for row in rows])
            
            logger.info(f"Retrieved {len(df)} days of P&L data")
            return df
            
        except Exception as e:
            logger.error(f"Failed to get daily P&L: {e}", exc_info=True)
            return pd.DataFrame(columns=['date', 'daily_return'])
    
    async def get_trade_history(
        self,
        symbol: str,
        start_date: datetime,
        end_date: datetime
    ) -> List[Dict]:
        """
        Get trade history for a specific symbol.
        
        Args:
            symbol: Stock symbol
            start_date: Start date (inclusive)
            end_date: End date (inclusive)
        
        Returns:
            List of trade dicts with keys:
            - entry_time, exit_time, entry_price, exit_price, quantity, pnl
        
        Example:
            [
                {
                    'entry_time': '2026-01-04 09:30',
                    'exit_time': '2026-01-04 11:45',
                    'entry_price': 180.50,
                    'exit_price': 183.20,
                    'quantity': 10,
                    'pnl': 0.0149  # (183.20 - 180.50) / 180.50
                },
                ...
            ]
        """
        try:
            query = select(PositionTracking).where(
                and_(
                    PositionTracking.symbol == symbol,
                    PositionTracking.exit_time >= start_date,
                    PositionTracking.exit_time <= end_date,
                    PositionTracking.exit_time.isnot(None)
                )
            ).order_by(PositionTracking.exit_time.desc())
            
            result = await self.session.execute(query)
            positions = result.scalars().all()
            
            trades = []
            for pos in positions:
                pnl = (pos.exit_price - pos.entry_price) / pos.entry_price
                trades.append({
                    'entry_time': pos.entry_time,
                    'exit_time': pos.exit_time,
                    'entry_price': pos.entry_price,
                    'exit_price': pos.exit_price,
                    'quantity': pos.quantity,
                    'pnl': pnl
                })
            
            logger.info(f"Retrieved {len(trades)} trades for {symbol}")
            return trades
            
        except Exception as e:
            logger.error(f"Failed to get trade history for {symbol}: {e}", exc_info=True)
            return []
    
    async def count_live_trades(self, days: int = 14) -> int:
        """
        Count number of completed trades in the last N days.
        
        Used to determine if enough live data exists for parameter calculation.
        
        Args:
            days: Number of days to count
        
        Returns:
            Number of trades
        """
        try:
            cutoff_date = datetime.now() - timedelta(days=days)
            
            query = select(func.count(PositionTracking.id)).where(
                and_(
                    PositionTracking.exit_time >= cutoff_date,
                    PositionTracking.exit_time.isnot(None)
                )
            )
            
            result = await self.session.execute(query)
            count = result.scalar()
            
            logger.info(f"Live trades in last {days} days: {count}")
            return count
            
        except Exception as e:
            logger.error(f"Failed to count live trades: {e}", exc_info=True)
            return 0
    
    async def get_all_active_positions(self) -> List[Dict]:
        """
        Get all currently active positions (exit_time IS NULL).
        
        Returns:
            List of dicts with keys: [symbol, entry_time, entry_price, quantity]
        
        Example:
            [
                {'symbol': 'AAPL', 'entry_time': '...', 'entry_price': 180.5, 'quantity': 10},
                {'symbol': 'MSFT', 'entry_time': '...', 'entry_price': 380.2, 'quantity': 5},
            ]
        """
        try:
            query = select(PositionTracking).where(
                PositionTracking.exit_time.is_(None)
            ).order_by(PositionTracking.entry_time.desc())
            
            result = await self.session.execute(query)
            positions = result.scalars().all()
            
            active = []
            for pos in positions:
                active.append({
                    'id': pos.id,
                    'symbol': pos.symbol,
                    'entry_time': pos.entry_time,
                    'entry_price': pos.entry_price,
                    'quantity': pos.quantity
                })
            
            logger.info(f"Active positions: {len(active)}")
            return active
            
        except Exception as e:
            logger.error(f"Failed to get active positions: {e}", exc_info=True)
            return []
    
    async def get_ohlcv_range(
        self,
        symbol: str,
        start_date: datetime,
        end_date: datetime,
        timeframe: str = '15m'
    ) -> List:
        """
        Get OHLCV data for correlation/VaR calculations.
        
        Args:
            symbol: Stock symbol
            start_date: Start date
            end_date: End date
            timeframe: '15m' or '1d'
        
        Returns:
            List of OHLCV objects
        """
        try:
            # Note: This assumes OHLCV15m model exists
            # For '1d' timeframe, you would use a different model
            
            query = select(OHLCV15m).where(
                and_(
                    OHLCV15m.symbol == symbol,
                    OHLCV15m.date_time >= start_date,
                    OHLCV15m.date_time <= end_date
                )
            ).order_by(OHLCV15m.date_time.asc())
            
            result = await self.session.execute(query)
            bars = result.scalars().all()
            
            logger.info(f"Retrieved {len(bars)} {timeframe} bars for {symbol}")
            return bars
            
        except Exception as e:
            logger.error(f"Failed to get OHLCV for {symbol}: {e}", exc_info=True)
            return []
    
    async def get_portfolio_value(self) -> float:
        """
        Calculate total portfolio value (via Alpaca API wrapper).
        
        Note: This is a placeholder. Actual implementation should call
        Alpaca API: api.get_account().portfolio_value
        
        Returns:
            Portfolio value in USD
        """
        # Placeholder: In production, this would call Alpaca API
        # For now, return a mock value
        logger.warning("get_portfolio_value() not fully implemented - using mock value")
        return 100000.0  # $100k mock
