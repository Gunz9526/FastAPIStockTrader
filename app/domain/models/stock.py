from datetime import datetime, date
from typing import Optional
from sqlalchemy import Column, String, Integer, Float, Date, DateTime, Boolean, ForeignKey, Index, Enum as SQLEnum
import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func
import enum
from app.core.database import Base

class StockTicker(Base):
    __tablename__ = "stock_tickers"

    symbol: Mapped[str] = mapped_column(String(20), primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    market: Mapped[str] = mapped_column(String(20), nullable=False)
    sector: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), onupdate=func.now(), server_default=func.now())

    ohlcv_data: Mapped[list["StockOHLCV"]] = relationship(back_populates="ticker", cascade="all, delete-orphan")
    corporate_actions: Mapped[list["CorporateAction"]] = relationship(back_populates="ticker", cascade="all, delete-orphan")
    fundamentals: Mapped[list["StockFundamentals"]] = relationship(back_populates="ticker", cascade="all, delete-orphan")
    positions: Mapped[list["Position"]] = relationship(back_populates="ticker", cascade="all, delete-orphan")
    trade_logs: Mapped[list["TradeLog"]] = relationship(back_populates="ticker", cascade="all, delete-orphan")
    position_tracking: Mapped[list["PositionTracking"]] = relationship(back_populates="ticker", cascade="all, delete-orphan")

class StockOHLCV(Base):
    __tablename__ = "stock_ohlcv"
    __table_args__ = (
        sa.PrimaryKeyConstraint('symbol', 'date_time', 'timeframe'),
    )

    id: Mapped[int] = mapped_column(Integer, sa.Sequence('stock_ohlcv_id_seq'), autoincrement=True)
    symbol: Mapped[str] = mapped_column(ForeignKey("stock_tickers.symbol"), nullable=False, index=True)
    date_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    timeframe: Mapped[str] = mapped_column(String(10), nullable=False, default='1d', index=True) # 1m, 15m, 1d
    
    open: Mapped[float] = mapped_column(Float, nullable=False)
    high: Mapped[float] = mapped_column(Float, nullable=False)
    low: Mapped[float] = mapped_column(Float, nullable=False)
    close: Mapped[float] = mapped_column(Float, nullable=False)
    volume: Mapped[float] = mapped_column(Float, nullable=False)
    adj_close: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    
    # NEW: Alpaca-provided metrics for enhanced features
    vwap: Mapped[Optional[float]] = mapped_column(Float, nullable=True, comment="Volume Weighted Average Price")
    trade_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, comment="Number of trades in this bar")

    ticker: Mapped["StockTicker"] = relationship(back_populates="ohlcv_data")

class CorporateAction(Base):
    __tablename__ = "corporate_actions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(ForeignKey("stock_tickers.symbol"), nullable=False, index=True)
    action_type: Mapped[str] = mapped_column(String(50), nullable=False)
    execution_date: Mapped[date] = mapped_column(Date, nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    applied_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    ticker: Mapped["StockTicker"] = relationship(back_populates="corporate_actions")

class StockFundamentals(Base):
    """
    RAG Data: Fundamental analysis data.
    
    NOTE (2026-01-06): Sentiment and fundamentals removed from DB.
    - Sentiment: Volatile data, Redis-only with 1-hour TTL
    - Fundamentals: On-demand fetch via yfinance with LRU cache
    - No historical tracking needed for current trading strategy
    
    This table is kept for future use (earnings reports, etc.)
    """
    __tablename__ = "stock_fundamentals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(ForeignKey("stock_tickers.symbol"), nullable=False, index=True)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    
    # Reserved for future fundamental data (earnings, revenue, etc.)
    # per, pbr, roe, market_cap, sector, sentiment_score removed (2026-01-06)

    ticker: Mapped["StockTicker"] = relationship(back_populates="fundamentals")

class PortfolioStatus(Base):
    """RAG Data: User portfolio status."""
    __tablename__ = "portfolio_status"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(ForeignKey("stock_tickers.symbol"), nullable=False)
    
    avg_price: Mapped[float] = mapped_column(Float, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    current_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

class PositionStatus(str, enum.Enum):
    """Position status enum."""
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    PARTIAL = "PARTIAL"

class Position(Base):
    """
    Active position tracking.
    Updated in real-time as trades are executed.
    """
    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(ForeignKey("stock_tickers.symbol"), nullable=False, index=True)
    
    # Entry
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    entry_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    initial_qty: Mapped[int] = mapped_column(Integer, nullable=False)
    
    # Current
    current_qty: Mapped[int] = mapped_column(Integer, nullable=False)
    current_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    
    # Risk Management
    stop_loss_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    take_profit_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    trailing_stop_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    
    # P&L
    realized_pl: Mapped[float] = mapped_column(Float, default=0.0)
    unrealized_pl: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    
    # Status
    status: Mapped[str] = mapped_column(String(20), default=PositionStatus.OPEN.value)
    exit_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    exit_time: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    ticker: Mapped["StockTicker"] = relationship(back_populates="positions")

class TradeLog(Base):
    """
    Trade execution log for audit and analysis.
    """
    __tablename__ = "trade_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(ForeignKey("stock_tickers.symbol"), nullable=False, index=True)
    
    action: Mapped[str] = mapped_column(String(10), nullable=False)  # BUY, SELL
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    
    order_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    execution_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    
    strategy_name: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    signal_strength: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    
    # P&L (for SELL orders)
    realized_pl: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    
    ticker: Mapped["StockTicker"] = relationship(back_populates="trade_logs")

class PositionTracking(Base):
    """
    Position tracking for defense mechanisms.
    
    Purpose:
    - Enforce minimum holding periods (e.g., 60 minutes)
    - Prevent rapid re-trading of same symbol (cooldown)
    - Calculate P&L for minimum profit threshold checks
    - Persist across container restarts
    """
    __tablename__ = "position_tracking"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(ForeignKey("stock_tickers.symbol"), nullable=False, index=True)
    
    entry_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    
    exit_time: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    exit_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    ticker: Mapped["StockTicker"] = relationship(back_populates="position_tracking")

__table_args__ = (
    Index('idx_positions_symbol_status', Position.symbol, Position.status),
    Index('idx_trades_symbol_time', TradeLog.symbol, TradeLog.execution_time),
    Index('idx_position_tracking_active', PositionTracking.symbol, PositionTracking.exit_time),
)
