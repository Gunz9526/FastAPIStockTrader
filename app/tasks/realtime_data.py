"""
일봉 OHLCV 데이터 수집 및 VWAP 계산.
활성 거래 심볼에 대해 장 마감 후 일봉 데이터를 수집합니다.
"""
import logging
from datetime import datetime, timedelta

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from pytz import timezone

from app.core.config import settings
from app.core.database import SessionLocal
from app.repositories.stock_repo_sync import SyncStockRepository
from app.worker import celery_app

logger = logging.getLogger(__name__)

@celery_app.task(name="app.tasks.realtime_data.collect_daily_ohlcv")
def collect_daily_ohlcv():
    """
    일봉 OHLCV 및 VWAP 데이터를 수집합니다.
    장 마감 후(ET 기준 17:00) 1일 1회 실행됩니다.
    """
    logger.info("일봉 데이터 수집 시작...")

    # Check if market was open today
    et_tz = timezone('America/New_York')
    current_time = datetime.now(et_tz)
    day_of_week = current_time.weekday()  # 0=Monday, 6=Sunday

    if day_of_week > 4:  # Weekend
        logger.info("시장 휴장(주말). 일봉 수집 건너뜁니다.")
        return {"status": "skipped", "reason": "weekend"}

    session = SessionLocal()
    try:
        repo = SyncStockRepository(session)

        # Get active symbols
        symbols = repo.get_active_symbols()
        if not symbols:
            logger.warning("일봉 수집 대상 활성 심볼이 없습니다")
            return {"status": "no_symbols"}

        logger.info(f"{len(symbols)}개 심볼에 대해 일봉 데이터 수집 중")

        # Initialize Alpaca client
        data_client = StockHistoricalDataClient(
            api_key=settings.ALPACA_API_KEY,
            secret_key=settings.ALPACA_SECRET_KEY,
        )

        # Fetch last 3 trading days to ensure we capture the most recent completed daily bar
        end_time = datetime.now()
        start_time = end_time - timedelta(days=5)

        success_count = 0
        error_count = 0

        for symbol in symbols:
            try:
                request = StockBarsRequest(
                    symbol_or_symbols=[symbol],
                    timeframe=TimeFrame(1, TimeFrameUnit.Day),
                    start=start_time,
                    end=end_time,
                    feed='iex'  # Free tier compatible
                )

                bars_response = data_client.get_stock_bars(request)

                if not bars_response or not bars_response.data:
                    logger.debug(f"{symbol}: 일봉 데이터 없음")
                    continue

                symbol_bars = bars_response.data.get(symbol, [])
                if not symbol_bars:
                    logger.debug(f"{symbol}: 바 목록 비어있음")
                    continue

                # Process each bar
                inserted_bars = 0
                updated_bars = 0

                for bar in symbol_bars:
                    # Check if this bar already exists in DB (exact datetime match)
                    existing = repo.get_ohlcv_by_datetime(symbol, bar.timestamp, timeframe='1d')

                    if existing:
                        # Update existing bar (in case of corrections)
                        existing.open = float(bar.open)
                        existing.high = float(bar.high)
                        existing.low = float(bar.low)
                        existing.close = float(bar.close)
                        existing.volume = float(bar.volume)
                        existing.vwap = float(bar.vwap) if hasattr(bar, 'vwap') and bar.vwap else None
                        existing.trade_count = int(bar.trade_count) if hasattr(bar, 'trade_count') and bar.trade_count else None
                        updated_bars += 1
                    else:
                        # Insert new bar
                        from app.domain.schemas.stock import StockOHLCVCreate

                        ohlcv_data = StockOHLCVCreate(
                            symbol=symbol,
                            date_time=bar.timestamp,
                            open=float(bar.open),
                            high=float(bar.high),
                            low=float(bar.low),
                            close=float(bar.close),
                            volume=float(bar.volume),
                            adj_close=None,
                            timeframe='1d',
                            vwap=float(bar.vwap) if hasattr(bar, 'vwap') and bar.vwap else None,
                            trade_count=int(bar.trade_count) if hasattr(bar, 'trade_count') and bar.trade_count else None
                        )

                        repo.create_ohlcv(ohlcv_data)
                        inserted_bars += 1

                success_count += 1
                logger.debug(f"{symbol}: 신규 {inserted_bars}개, 갱신 {updated_bars}개")

            except Exception as e:
                error_count += 1
                logger.error(f"{symbol} 일봉 데이터 수집 실패: {e}")
                continue

        session.commit()
        logger.info(f"일봉 수집 완료: 성공 {success_count}건, 오류 {error_count}건")

        return {
            'status': 'completed',
            'success': success_count,
            'errors': error_count,
            'total': len(symbols)
        }

    except Exception as e:
        logger.error(f"일봉 수집 오류: {e}", exc_info=True)
        session.rollback()
        raise
    finally:
        session.close()
