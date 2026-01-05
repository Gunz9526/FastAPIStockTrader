from datetime import datetime, date
from typing import Optional, List
from pydantic import BaseModel, ConfigDict, Field

# --- Shared Properties ---
class StockTickerBase(BaseModel):
    name: str
    market: str
    sector: Optional[str] = None
    is_active: bool = True

class StockOHLCVBase(BaseModel):
    date_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    adj_close: Optional[float] = None
    vwap: Optional[float] = None
    trade_count: Optional[int] = None

class CorporateActionBase(BaseModel):
    action_type: str
    execution_date: date
    value: float

# --- Creation Schemas ---
class StockTickerCreate(StockTickerBase):
    symbol: str

class StockOHLCVCreate(StockOHLCVBase):
    symbol: str
    timeframe: str = '1d'  # Support different timeframes

class CorporateActionCreate(CorporateActionBase):
    symbol: str

# --- Response Schemas ---
class StockTickerResponse(StockTickerBase):
    symbol: str
    created_at: datetime
    updated_at: datetime
    
    model_config = ConfigDict(from_attributes=True)

class StockOHLCVResponse(StockOHLCVBase):
    symbol: str
    
    model_config = ConfigDict(from_attributes=True)

class CorporateActionResponse(CorporateActionBase):
    id: int
    symbol: str
    applied_date: datetime

    model_config = ConfigDict(from_attributes=True)

# --- Extended Response ---
class StockTickerDetail(StockTickerResponse):
    recent_ohlcv: List[StockOHLCVResponse] = []
    actions: List[CorporateActionResponse] = []
