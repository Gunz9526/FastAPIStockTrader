from sqlalchemy.orm import Session
from sqlalchemy import select
from typing import List, Optional
from app.domain.models.stock import StockTicker, StockOHLCV, PositionTracking
from app.domain.schemas.stock import StockOHLCVCreate
from datetime import datetime

class SyncStockRepository:
    """Synchronous repository for Celery tasks"""
    
    def __init__(self, db: Session):
        self.db = db
    
    def get_active_symbols(self) -> List[str]:
        """Get active stock symbols (sync)"""
        result = self.db.execute(
            select(StockTicker.symbol).where(StockTicker.is_active == True)
        )
        return [row[0] for row in result]
    
    def get_ohlcv_range(
        self, 
        symbol: str, 
        start_date: datetime, 
        end_date: datetime,
        timeframe: str = '15m'
    ) -> List[StockOHLCV]:
        """Get OHLCV data for symbol in date range (sync)"""
        result = self.db.execute(
            select(StockOHLCV)
            .where(StockOHLCV.symbol == symbol)
            .where(StockOHLCV.timeframe == timeframe)
            .where(StockOHLCV.date_time >= start_date)
            .where(StockOHLCV.date_time <= end_date)
            .order_by(StockOHLCV.date_time)
        )
        return list(result.scalars().all())
    
    def get_ohlcv_by_datetime(
        self, 
        symbol: str, 
        date_time: datetime,
        timeframe: str = '15m'
    ) -> Optional[StockOHLCV]:
        """Get specific OHLCV bar by symbol and datetime (sync)"""
        result = self.db.execute(
            select(StockOHLCV)
            .where(StockOHLCV.symbol == symbol)
            .where(StockOHLCV.timeframe == timeframe)
            .where(StockOHLCV.date_time == date_time)
        )
        return result.scalar_one_or_none()
    
    def create_ohlcv(self, ohlcv_data: StockOHLCVCreate) -> StockOHLCV:
        """Create new OHLCV record (sync)"""
        ohlcv = StockOHLCV(**ohlcv_data.model_dump())
        self.db.add(ohlcv)
        self.db.flush()
        return ohlcv
    
    # Position Tracking Methods (Phase I.1)
    
    def record_position_entry(
        self,
        symbol: str,
        entry_price: float,
        quantity: int,
        entry_time: Optional[datetime] = None
    ) -> PositionTracking:
        """
        Record position entry for defense mechanisms.
        
        Args:
            symbol: Stock symbol
            entry_price: Entry price
            quantity: Position size
            entry_time: Entry timestamp (default: now)
        
        Returns:
            Created PositionTracking record
        """
        if entry_time is None:
            entry_time = datetime.now()
        
        position = PositionTracking(
            symbol=symbol,
            entry_time=entry_time,
            entry_price=float(entry_price),  # Convert NumPy types to native Python
            quantity=int(quantity)
        )
        self.db.add(position)
        self.db.flush()
        return position
    
    def get_active_position(self, symbol: str) -> Optional[PositionTracking]:
        """
        Get active position (exit_time is NULL) for symbol.
        
        Args:
            symbol: Stock symbol
        
        Returns:
            Active PositionTracking or None
        """
        result = self.db.execute(
            select(PositionTracking)
            .where(PositionTracking.symbol == symbol)
            .where(PositionTracking.exit_time.is_(None))
            .order_by(PositionTracking.entry_time.desc())
        )
        return result.scalar_one_or_none()
    
    def get_active_position_for_update(self, symbol: str) -> Optional[PositionTracking]:
        """
        Get active position with pessimistic lock (FOR UPDATE).
        
        This method acquires a row-level lock on the position record,
        preventing other transactions from reading or modifying it
        until this transaction completes.
        
        Use Case:
            - Before updating exit_price/exit_time
            - When checking position state before trade execution
        
        Args:
            symbol: Stock symbol
        
        Returns:
            Active PositionTracking with lock, or None if no active position
            
        Note:
            MUST be used within a transaction. Call session.commit() or
            session.rollback() to release the lock.
        """
        result = self.db.execute(
            select(PositionTracking)
            .where(PositionTracking.symbol == symbol)
            .where(PositionTracking.exit_time.is_(None))
            .order_by(PositionTracking.entry_time.desc())
            .with_for_update()  # Pessimistic lock
        )
        return result.scalar_one_or_none()
    
    def update_position_exit(
        self,
        position_id: int,
        exit_price: float,
        exit_time: Optional[datetime] = None
    ) -> Optional[PositionTracking]:
        """
        Update position with exit information.
        
        Args:
            position_id: PositionTracking ID
            exit_price: Exit price
            exit_time: Exit timestamp (default: now)
        
        Returns:
            Updated PositionTracking or None
        """
        if exit_time is None:
            exit_time = datetime.now()
        
        result = self.db.execute(
            select(PositionTracking)
            .where(PositionTracking.id == position_id)
        )
        position = result.scalar_one_or_none()
        
        if position:
            position.exit_time = exit_time
            position.exit_price = float(exit_price)  # Convert NumPy types to native Python
            self.db.flush()
        
        return position
