import os
from datetime import datetime, timedelta

import yfinance as yf  # For VIX data (fallback from Alpaca)
from celery.utils.log import get_task_logger

from app.services.discord_notifier import notify_on_failure
from app.worker import celery_app

logger = get_task_logger(__name__)


@celery_app.task(name="app.tasks.vix_data.collect_vix_data", bind=True, max_retries=3)
@notify_on_failure("collect_vix_data")
def collect_vix_data(self, days: int = 7):
    """
    VIX 데이터를 수집하여 Redis에 저장합니다.
    
    Args:
        days: 조회 기간 (기본: 7일)

    VIX 해석:
    - VIX < 12: 낮은 변동성 (안정)
    - VIX 12-20: 보통 변동성
    - VIX 20-30: 상승한 변동성 (불안)
    - VIX > 30: 높은 변동성 (공황)

    데이터 소스:
    1. yfinance (Primary - 무료, 안정적)
    2. Alpaca IEX (Fallback - 무료 계정은 IEX feed만)
    """
    logger.info("VIX 데이터 수집 시작 (기간: %d일)", days)

    try:
        # Primary: yfinance (no subscription required)
        try:
            vix_ticker = yf.Ticker("^VIX")

            end_date = datetime.now()
            start_date = end_date - timedelta(days=days)

            vix_df = vix_ticker.history(
                start=start_date,
                end=end_date,
                interval="1d"
            )

            if vix_df.empty:
                raise ValueError("No VIX data from yfinance")

            latest_vix_value = float(vix_df['Close'].iloc[-1])
            latest_vix_time = vix_df.index[-1].to_pydatetime()

            logger.info("yfinance VIX: %.2f @ %s", latest_vix_value, latest_vix_time)
            data_source = 'yfinance'

        except Exception as yf_error:
            logger.warning("yfinance 실패: %s, Alpaca IEX 시도", yf_error)

            # Fallback: Alpaca IEX
            from alpaca.data.historical import StockHistoricalDataClient
            from alpaca.data.requests import StockBarsRequest
            from alpaca.data.timeframe import TimeFrame

            api_key = os.getenv("ALPACA_API_KEY")
            api_secret = os.getenv("ALPACA_SECRET_KEY")

            if not api_key or not api_secret:
                raise ValueError("Alpaca credentials missing")

            client = StockHistoricalDataClient(api_key, api_secret)

            end_date = datetime.now()
            start_date = end_date - timedelta(days=days)

            request_params = StockBarsRequest(
                symbol_or_symbols=["VIX"],
                timeframe=TimeFrame.Day,
                start=start_date,
                end=end_date,
                feed='iex'  # IEX feed (free tier)
            )

            bars = client.get_stock_bars(request_params)

            if not bars or "VIX" not in bars:
                raise ValueError("No VIX from Alpaca IEX")

            vix_bars = bars["VIX"]
            if not vix_bars:
                raise ValueError("Empty VIX bars")

            latest_vix_bar = vix_bars[-1]
            latest_vix_value = float(latest_vix_bar.close)
            latest_vix_time = latest_vix_bar.timestamp

            logger.info("Alpaca IEX VIX: %.2f", latest_vix_value)
            data_source = 'alpaca_iex'

        # Store latest VIX in Redis (CRITICAL for regime detection)
        try:
            from app.core.cache import get_shared_redis
            redis_client = get_shared_redis()

            redis_client.setex(
                'vix:latest',
                86400,  # 24-hour TTL
                str(latest_vix_value)
            )

            redis_client.setex(
                'vix:latest_timestamp',
                86400,
                latest_vix_time.isoformat()
            )

            logger.info("Redis VIX 캐시: %.2f (source: %s)", latest_vix_value, data_source)

        except Exception as redis_err:
            logger.error("Redis VIX 캐시 실패: %s", redis_err)
            raise  # Redis 실패는 재시도

        return {
            'status': 'success',
            'vix_value': latest_vix_value,
            'vix_timestamp': latest_vix_time.isoformat(),
            'source': data_source
        }

    except Exception as e:
        logger.error("VIX 수집 최종 실패: %s", e, exc_info=True)
        raise self.retry(exc=e, countdown=300)  # 5분 후 재시도


def get_latest_vix() -> float | None:
    """
    Redis 캐시에서 최신 VIX 값을 가져옵니다.

    Returns:
        최신 VIX 값 또는 없을 경우 None
    """
    try:
        from app.core.cache import get_shared_redis
        redis_client = get_shared_redis()

        vix_str = redis_client.get('vix:latest')  # Changed to match key name

        if vix_str:
            return float(vix_str)
        else:
            logger.warning("VIX not found in Redis cache")
            return None

    except Exception as e:
        logger.error("Failed to get VIX from Redis: %s", e)
        return None
