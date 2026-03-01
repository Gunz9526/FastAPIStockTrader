import asyncio
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from typing import Any

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest

from app.core.cache import cache
from app.core.config import settings
from app.domain.schemas.stock import StockOHLCVCreate

logger = logging.getLogger(__name__)

class AbstractDataProvider(ABC):
    @abstractmethod
    async def get_current_price(self, symbol: str) -> float:
        pass

    @abstractmethod
    async def get_historical_data(self, symbol: str, start_date: datetime, end_date: datetime) -> list[StockOHLCVCreate]:
        pass

    @abstractmethod
    async def place_order(self, symbol: str, quantity: int, side: str) -> str | None:
        pass

    @abstractmethod
    async def get_account_info(self) -> dict[str, Any]:
        pass

    @abstractmethod
    async def get_position(self, symbol: str) -> dict[str, Any] | None:
        pass

class AlpacaDataProvider(AbstractDataProvider):
    """
    캐시된 Alpaca 데이터 제공자 (성능 개선)
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
        logger.info("Alpaca 클라이언트 초기화: 데이터 API 및 페이퍼 트레이딩")

    async def get_current_price(self, symbol: str) -> float:
        """최신 가격 조회 (캐시 없음 - 실시간 필요)"""
        try:
            loop = asyncio.get_running_loop()

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
            logger.error(f"{symbol} 가격 조회 실패: {e}")
            return 0.0

    async def get_historical_data(
        self,
        symbol: str,
        start_date: datetime,
        end_date: datetime,
        timeframe: TimeFrame | None = None
    ) -> list[StockOHLCVCreate]:
        """
        Fetch historical data with caching and pagination.
        
        페이지네이션 구현:
        - 15분봉 && 180일 이상: 6개월 단위로 분할 요청
        - Alpaca API limit=10,000 제한 회피
        """
        try:
            if timeframe is None:
                timeframe = TimeFrame.Day

            # Calculate days for cache key
            days = (end_date - start_date).days

            # === 페이지네이션 적용 조건 ===
            # 15분봉 && 180일 이상인 경우
            is_intraday = timeframe.unit == TimeFrameUnit.Minute if hasattr(timeframe, 'unit') else False

            if is_intraday and days > 180:
                logger.info(f"{symbol} 페이지네이션 적용: {days}일을 6개월 단위로 분할")
                return await self._get_historical_data_paginated(symbol, start_date, end_date, timeframe)

            # Try cache first (Only for Daily for now)
            cached = None
            if timeframe == TimeFrame.Day:
                cached = cache.get_ohlcv(symbol, days)

            if cached:
                logger.debug(f"캐시 HIT: {symbol} {days}일")
                return [StockOHLCVCreate(**item) for item in cached]

            # Cache miss - fetch from API
            logger.debug(f"캐시 MISS: {symbol} {days}일")
            loop = asyncio.get_running_loop()

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

            logger.info(f"{symbol}에 대해 {len(data)}개 바를 가져왔습니다")
            return data

        except Exception as e:
            logger.error(f"{symbol} 이력 조회 실패: {e}", exc_info=True)
            return []

    async def _get_historical_data_paginated(
        self,
        symbol: str,
        start_date: datetime,
        end_date: datetime,
        timeframe: TimeFrame
    ) -> list[StockOHLCVCreate]:
        """
        긴 기간 데이터를 페이지네이션으로 가져오기
        
        Args:
            symbol: 종목 심볼
            start_date: 시작 날짜
            end_date: 종료 날짜
            timeframe: 시간 단위
        
        Returns:
            전체 StockOHLCVCreate 리스트 (모든 페이지 합산)
        """
        period_days = 182  # 약 6개월
        periods = []
        current_start = start_date

        while current_start < end_date:
            current_end = min(current_start + timedelta(days=period_days), end_date)
            periods.append((current_start, current_end))
            current_start = current_end

        logger.info(f"{symbol}: {len(periods)}개 기간으로 분할하여 요청")

        all_data = []
        loop = asyncio.get_running_loop()

        for period_idx, (period_start, period_end) in enumerate(periods, 1):
            logger.debug(f"{symbol} [{period_idx}/{len(periods)}]: {period_start.date()} ~ {period_end.date()}")

            def _fetch_period():
                request = StockBarsRequest(
                    symbol_or_symbols=[symbol],
                    timeframe=timeframe,
                    start=period_start,
                    end=period_end,
                    limit=10000,
                    feed='iex'
                )
                bars_response = self.data_client.get_stock_bars(request)

                if not bars_response or not bars_response.data:
                    return []

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
                        adj_close=None,
                        vwap=float(bar.vwap) if hasattr(bar, 'vwap') and bar.vwap is not None else None,
                        trade_count=int(bar.trade_count) if hasattr(bar, 'trade_count') and bar.trade_count is not None else None
                    ))
                return result

            try:
                period_data = await loop.run_in_executor(None, _fetch_period)
                if period_data:
                    all_data.extend(period_data)
                    logger.debug(f"{symbol} [{period_idx}/{len(periods)}]: {len(period_data)}개 bar 수신")
                else:
                    logger.warning(f"{symbol} [{period_idx}/{len(periods)}]: 반환된 bar 없음")

                # Rate limiting (각 페이지 요청 후)
                await asyncio.sleep(0.3)

            except Exception as e:
                logger.error(
                    f"{symbol} [{period_idx}/{len(periods)}] 요청 실패 "
                    f"({period_start.date()} ~ {period_end.date()}): {e}",
                    exc_info=True
                )
                # 다음 기간 계속 시도
                continue

        logger.info(f"{symbol}: 총 {len(all_data)}개 bar 수신 완료")
        return all_data

    async def place_order(self, symbol: str, quantity: int, side: str) -> str | None:
        """Place order and invalidate cache."""
        try:
            if quantity <= 0:
                logger.warning(f"잘못된 수량 {quantity} for {symbol}")
                return None

            loop = asyncio.get_running_loop()

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

            # 심볼 관련 캐시 무효화
            cache.invalidate_symbol(symbol)
            cache.delete("account:info")  # 계정 정보 변경

            logger.info(f"주문 실행: {side} {quantity} {symbol}, ID: {order_id}")
            return str(order_id)

        except Exception as e:
            logger.error(f"{symbol} 주문 실패: {e}", exc_info=True)
            return None

    async def get_account_info(self) -> dict[str, Any]:
        """Get account info with caching (30 seconds)."""
        try:
            # Try cache first
            cached = cache.get_account_info()
            if cached:
                logger.debug("캐시 HIT: 계정 정보")
                return cached

            logger.debug("캐시 MISS: 계정 정보")
            loop = asyncio.get_running_loop()

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

            logger.info(f"계정 정보 가져옴: buying_power=${info['buying_power']:.2f}")
            return info

        except Exception as e:
            logger.error(f"계정 정보 조회 실패: {e}", exc_info=True)
            return {}

    async def get_position(self, symbol: str) -> dict[str, Any] | None:
        """Get position with caching (1 minute)."""
        try:
            # Try cache first
            cached = cache.get_position(symbol)
            if cached:
                logger.debug(f"캐시 HIT: 포지션 {symbol}")
                return cached

            logger.debug(f"캐시 MISS: 포지션 {symbol}")
            loop = asyncio.get_running_loop()

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

            # 1분 캐시
            if position:
                cache.set_position(symbol, position, ttl=60)

            return position

        except Exception as e:
            logger.error(f"{symbol} 포지션 조회 실패: {e}", exc_info=True)
            return None

    async def close_position(self, symbol: str, qty: int | None = None) -> bool:
        """Close position and invalidate cache."""
        try:
            loop = asyncio.get_running_loop()

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

            # 캐시 무효화
            cache.invalidate_symbol(symbol)
            cache.delete("account:info")

            logger.info(f"포지션 종료: {symbol} qty={qty or 'ALL'}")
            return success

        except Exception as e:
            logger.error(f"{symbol} 포지션 종료 실패: {e}", exc_info=True)
            return False
