from abc import ABC, abstractmethod
from datetime import datetime
from typing import List, Optional, Dict, Any
import logging
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from app.domain.schemas.stock import StockOHLCVCreate
from app.core.config import settings
from app.core.cache import cache
import asyncio

logger = logging.getLogger(__name__)

class AbstractDataProvider(ABC):
    @abstractmethod
    async def get_current_price(self, symbol: str) -> float:
        pass

    @abstractmethod
    async def get_historical_data(self, symbol: str, start_date: datetime, end_date: datetime) -> List[StockOHLCVCreate]:
        pass

    @abstractmethod
    async def place_order(self, symbol: str, quantity: int, side: str) -> Optional[str]:
        pass
    
    @abstractmethod
    async def get_account_info(self) -> Dict[str, Any]:
        pass
    
    @abstractmethod
    async def get_position(self, symbol: str) -> Optional[Dict[str, Any]]:
        pass

class AlpacaDataProvider(AbstractDataProvider):
    """
    Cached Alpaca data provider for performance.
    """
    
    def __init__(self):
        # Market Data API
        self.data_client = StockHistoricalDataClient(
            api_key=settings.ALPACA_API_KEY,
            secret_key=settings.ALPACA_SECRET_KEY,
        )
        
        # Trading API (Paper)
        self.trading_client = TradingClient(
            api_key=settings.ALPACA_API_KEY,
            secret_key=settings.ALPACA_SECRET_KEY,
            paper=True,
        )
        logger.info("Alpaca clients initialized: Data API + Paper Trading")

    async def get_current_price(self, symbol: str) -> float:
        """Get latest price (no cache - real-time needed)."""
        try:
            loop = asyncio.get_event_loop()
            
            def _fetch():
                request = StockBarsRequest(
                    symbol_or_symbols=[symbol],
                    timeframe=TimeFrame.Minute,
                    limit=1,
                    feed='iex'  # Free tier compatible
                )
                bars_response = self.data_client.get_stock_bars(request)
                
                # Access via .data attribute
                if bars_response and bars_response.data:
                    symbol_bars = bars_response.data.get(symbol, [])
                    if symbol_bars and len(symbol_bars) > 0:
                        return float(symbol_bars[0].close)
                return 0.0
            
            price = await loop.run_in_executor(None, _fetch)
            return price
            
        except Exception as e:
            logger.error(f"Failed to get price for {symbol}: {e}")
            return 0.0

    async def get_historical_data(
        self, 
        symbol: str, 
        start_date: datetime, 
        end_date: datetime,
        timeframe: Optional[TimeFrame] = None
    ) -> List[StockOHLCVCreate]:
        """Fetch historical data with caching."""
        try:
            if timeframe is None:
                timeframe = TimeFrame.Day

            # Calculate days for cache key
            days = (end_date - start_date).days
            
            # Try cache first (Only for Daily for now)
            cached = None
            if timeframe == TimeFrame.Day:
                cached = cache.get_ohlcv(symbol, days)
                
            if cached:
                logger.debug(f"Cache HIT: {symbol} {days} days")
                return [StockOHLCVCreate(**item) for item in cached]
            
            # Cache miss - fetch from API
            logger.debug(f"Cache MISS: {symbol} {days} days")
            loop = asyncio.get_event_loop()
            
            def _fetch():
                request = StockBarsRequest(
                    symbol_or_symbols=[symbol],
                    timeframe=timeframe,
                    start=start_date,
                    end=end_date,
                    feed='iex'  # Use IEX feed for free tier (SIP requires paid subscription)
                )
                bars_response = self.data_client.get_stock_bars(request)
                
                # Alpaca returns BarSet object, access via .data dict or .df()
                if not bars_response or not bars_response.data:
                    return []
                
                # bars_response.data is a dict: {symbol: [Bar, Bar, ...]}
                symbol_bars = bars_response.data.get(symbol, [])
                if not symbol_bars:
                    return []
                
                result = []
                for bar in symbol_bars:
                    result.append(StockOHLCVCreate(
                        symbol=symbol,
                        date_time=bar.timestamp,
                        open=float(bar.open),
                        high=float(bar.high),
                        low=float(bar.low),
                        close=float(bar.close),
                        volume=float(bar.volume),
                        adj_close=None,  # Not available for intraday bars
                        vwap=float(bar.vwap) if hasattr(bar, 'vwap') and bar.vwap is not None else None,
                        trade_count=int(bar.trade_count) if hasattr(bar, 'trade_count') and bar.trade_count is not None else None
                    ))
                return result
            
            data = await loop.run_in_executor(None, _fetch)
            
            # Cache for 1 hour
            if data:
                cache_data = [item.model_dump() for item in data]
                cache.set_ohlcv(symbol, days, cache_data, ttl=3600)
            
            logger.info(f"Fetched {len(data)} bars for {symbol}")
            return data
            
        except Exception as e:
            logger.error(f"Failed to fetch history for {symbol}: {e}", exc_info=True)
            return []

    async def place_order(self, symbol: str, quantity: int, side: str) -> Optional[str]:
        """Place order and invalidate cache."""
        try:
            if quantity <= 0:
                logger.warning(f"Invalid quantity {quantity} for {symbol}")
                return None
            
            loop = asyncio.get_event_loop()
            
            def _place():
                order_side = OrderSide.BUY if side.lower() == 'buy' else OrderSide.SELL
                
                order_request = MarketOrderRequest(
                    symbol=symbol,
                    qty=quantity,
                    side=order_side,
                    time_in_force=TimeInForce.DAY
                )
                
                order = self.trading_client.submit_order(order_request)
                return order.id
            
            order_id = await loop.run_in_executor(None, _place)
            
            # Invalidate cache for this symbol
            cache.invalidate_symbol(symbol)
            cache.delete("account:info")  # Account info changed
            
            logger.info(f"Order placed: {side} {quantity} {symbol}, ID: {order_id}")
            return str(order_id)
            
        except Exception as e:
            logger.error(f"Failed to place order for {symbol}: {e}", exc_info=True)
            return None
    
    async def get_account_info(self) -> Dict[str, Any]:
        """Get account info with caching (30 seconds)."""
        try:
            # Try cache first
            cached = cache.get_account_info()
            if cached:
                logger.debug("Cache HIT: account info")
                return cached
            
            logger.debug("Cache MISS: account info")
            loop = asyncio.get_event_loop()
            
            def _fetch():
                account = self.trading_client.get_account()
                return {
                    'buying_power': float(account.buying_power),
                    'cash': float(account.cash),
                    'portfolio_value': float(account.portfolio_value),
                    'equity': float(account.equity),
                    'long_market_value': float(account.long_market_value),
                    'short_market_value': float(account.short_market_value),
                    'pattern_day_trader': account.pattern_day_trader,
                    'account_blocked': account.account_blocked,
                    'trading_blocked': account.trading_blocked
                }
            
            info = await loop.run_in_executor(None, _fetch)
            
            # Cache for 30 seconds
            cache.set_account_info(info, ttl=30)
            
            logger.info(f"Account info: ${info['buying_power']:.2f} buying power")
            return info
            
        except Exception as e:
            logger.error(f"Failed to get account info: {e}", exc_info=True)
            return {}
    
    async def get_position(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Get position with caching (1 minute)."""
        try:
            # Try cache first
            cached = cache.get_position(symbol)
            if cached:
                logger.debug(f"Cache HIT: position {symbol}")
                return cached
            
            logger.debug(f"Cache MISS: position {symbol}")
            loop = asyncio.get_event_loop()
            
            def _fetch():
                try:
                    position = self.trading_client.get_open_position(symbol)
                    return {
                        'symbol': position.symbol,
                        'qty': int(position.qty),
                        'avg_entry_price': float(position.avg_entry_price),
                        'current_price': float(position.current_price),
                        'market_value': float(position.market_value),
                        'unrealized_pl': float(position.unrealized_pl),
                        'unrealized_plpc': float(position.unrealized_plpc),
                        'side': position.side
                    }
                except Exception:
                    return None
            
            position = await loop.run_in_executor(None, _fetch)
            
            # Cache for 1 minute
            if position:
                cache.set_position(symbol, position, ttl=60)
            
            return position
            
        except Exception as e:
            logger.error(f"Failed to get position for {symbol}: {e}", exc_info=True)
            return None
    
    async def close_position(self, symbol: str, qty: Optional[int] = None) -> bool:
        """Close position and invalidate cache."""
        try:
            loop = asyncio.get_event_loop()
            
            def _close():
                if qty is None:
                    self.trading_client.close_position(symbol)
                else:
                    order_request = MarketOrderRequest(
                        symbol=symbol,
                        qty=qty,
                        side=OrderSide.SELL,
                        time_in_force=TimeInForce.DAY
                    )
                    self.trading_client.submit_order(order_request)
                return True
            
            success = await loop.run_in_executor(None, _close)
            
            # Invalidate cache
            cache.invalidate_symbol(symbol)
            cache.delete("account:info")
            
            logger.info(f"Closed position: {symbol} qty={qty or 'ALL'}")
            return success
            
        except Exception as e:
            logger.error(f"Failed to close position {symbol}: {e}", exc_info=True)
            return False
