import enum
from datetime import date, datetime

import sqlalchemy as sa
from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.core.database import Base


class StockTicker(Base):
    __tablename__ = "stock_tickers"

    symbol: Mapped[str] = mapped_column(String(20), primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    market: Mapped[str] = mapped_column(String(20), nullable=False)
    sector: Mapped[str | None] = mapped_column(String(100), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), onupdate=func.now(), server_default=func.now())

    ohlcv_data: Mapped[list[StockOHLCV]] = relationship(back_populates="ticker", cascade="all, delete-orphan")
    corporate_actions: Mapped[list[CorporateAction]] = relationship(back_populates="ticker", cascade="all, delete-orphan")
    fundamentals: Mapped[list[StockFundamentals]] = relationship(back_populates="ticker", cascade="all, delete-orphan")
    positions: Mapped[list[Position]] = relationship(back_populates="ticker", cascade="all, delete-orphan")
    trade_logs: Mapped[list[TradeLog]] = relationship(back_populates="ticker", cascade="all, delete-orphan")
    position_tracking: Mapped[list[PositionTracking]] = relationship(back_populates="ticker", cascade="all, delete-orphan")

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
    adj_close: Mapped[float | None] = mapped_column(Float, nullable=True)

    # NEW: Alpaca-provided metrics for enhanced features
    vwap: Mapped[float | None] = mapped_column(Float, nullable=True, comment="Volume Weighted Average Price")
    trade_count: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="Number of trades in this bar")

    ticker: Mapped[StockTicker] = relationship(back_populates="ohlcv_data")

class CorporateAction(Base):
    __tablename__ = "corporate_actions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(ForeignKey("stock_tickers.symbol"), nullable=False, index=True)
    action_type: Mapped[str] = mapped_column(String(50), nullable=False)
    execution_date: Mapped[date] = mapped_column(Date, nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    applied_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    ticker: Mapped[StockTicker] = relationship(back_populates="corporate_actions")

class StockFundamentals(Base):
    """
    RAG Data: Fundamental analysis data.
    """
    __tablename__ = "stock_fundamentals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(ForeignKey("stock_tickers.symbol"), nullable=False, index=True)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    per: Mapped[float | None] = mapped_column(Float, nullable=True)
    pbr: Mapped[float | None] = mapped_column(Float, nullable=True)
    roe: Mapped[float | None] = mapped_column(Float, nullable=True)
    market_cap: Mapped[float | None] = mapped_column(Float, nullable=True)

    sector: Mapped[str | None] = mapped_column(String(100), nullable=True)

    ticker: Mapped[StockTicker] = relationship(back_populates="fundamentals")

class PortfolioStatus(Base):
    """RAG Data: User portfolio status."""
    __tablename__ = "portfolio_status"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(ForeignKey("stock_tickers.symbol"), nullable=False)

    avg_price: Mapped[float] = mapped_column(Float, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    current_price: Mapped[float | None] = mapped_column(Float, nullable=True)
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
    current_price: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Risk Management
    stop_loss_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    trailing_stop_price: Mapped[float | None] = mapped_column(Float, nullable=True)

    # P&L
    realized_pl: Mapped[float] = mapped_column(Float, default=0.0)
    unrealized_pl: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Status
    status: Mapped[str] = mapped_column(String(20), default=PositionStatus.OPEN.value)
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    ticker: Mapped[StockTicker] = relationship(back_populates="positions")

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

    order_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    execution_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)

    strategy_name: Mapped[str | None] = mapped_column(String(50), nullable=True)
    signal_strength: Mapped[float | None] = mapped_column(Float, nullable=True)

    # P&L (for SELL orders)
    realized_pl: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Phase O: Extended P&L tracking
    fill_price: Mapped[float | None] = mapped_column(Float, nullable=True, comment="Actual Alpaca filled_avg_price")
    commission: Mapped[float | None] = mapped_column(Float, nullable=True, default=0.0, comment="Trading commission/fees")
    regime: Mapped[str | None] = mapped_column(String(50), nullable=True, comment="Market regime at trade time")
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True, comment="ML prediction confidence")
    predicted_class: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="ML predicted class (0/1/2)")
    entry_trade_id: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="FK to BUY trade for SELL orders")

    ticker: Mapped[StockTicker] = relationship(back_populates="trade_logs")

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

    exit_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    regime: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)

    # Risk management (Phase K trailing stops)
    stop_loss_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    trailing_stop_price: Mapped[float | None] = mapped_column(Float, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    ticker: Mapped[StockTicker] = relationship(back_populates="position_tracking")

    @property
    def pnl(self) -> float | None:
        """Calculate realized P&L if position is closed."""
        if self.exit_price and self.exit_time:
            return (self.exit_price - self.entry_price) * self.quantity
        return None
