from celery import shared_task
from celery.utils.log import get_task_logger
from app.core.database import get_sync_session
from app.domain.models.stock import StockOHLCV
from datetime import datetime, timedelta
import logging
import redis
import os

logger = get_task_logger(__name__)


@shared_task(name="tasks.collect_vix_data", bind=True, max_retries=3)
def collect_vix_data(self):
    """
    Collect VIX (Volatility Index) data from Alpaca.
    
    Schedule: Daily at 6:30 AM EST (before market open)
    
    VIX Interpretation:
    - VIX < 12: Low volatility (calm market)
    - VIX 12-20: Normal volatility
    - VIX 20-30: Elevated volatility (high fear)
    - VIX > 30: Extreme volatility (panic)
    
    Data is stored in:
    1. PostgreSQL/TimescaleDB (historical tracking)
    2. Redis (latest value with 24-hour TTL for fast access)
    """
    logger.info("Starting VIX data collection")
    
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        
        # Initialize Alpaca client
        api_key = os.getenv("ALPACA_API_KEY")
        api_secret = os.getenv("ALPACA_SECRET_KEY")
        
        if not api_key or not api_secret:
            logger.error("Alpaca API credentials not found")
            return {'status': 'error', 'message': 'Missing Alpaca credentials'}
        
        client = StockHistoricalDataClient(api_key, api_secret)
        
        # Fetch VIX data (symbol: VIX)
        # NOTE: Alpaca may provide VIX as ^VIX or VIX depending on data feed
        vix_symbol = "VIX"
        
        end_date = datetime.now()
        start_date = end_date - timedelta(days=7)  # Get last 7 days
        
        request_params = StockBarsRequest(
            symbol_or_symbols=[vix_symbol],
            timeframe=TimeFrame.Day,
            start=start_date,
            end=end_date
        )
        
        bars = client.get_stock_bars(request_params)
        
        if not bars or vix_symbol not in bars:
            logger.warning(f"No VIX data returned from Alpaca for {vix_symbol}")
            return {'status': 'warning', 'message': 'No VIX data available'}
        
        vix_bars = bars[vix_symbol]
        
        if not vix_bars:
            logger.warning("VIX bars list is empty")
            return {'status': 'warning', 'message': 'VIX bars empty'}
        
        # Get latest VIX value
        latest_vix_bar = vix_bars[-1]
        latest_vix_value = latest_vix_bar.close
        latest_vix_time = latest_vix_bar.timestamp
        
        logger.info(f"Latest VIX: {latest_vix_value:.2f} at {latest_vix_time}")
        
        # Store in PostgreSQL
        with get_sync_session() as db:
            for bar in vix_bars:
                vix_record = StockOHLCV(
                    symbol=vix_symbol,
                    date_time=bar.timestamp,
                    open=bar.open,
                    high=bar.high,
                    low=bar.low,
                    close=bar.close,
                    volume=bar.volume,
                    vwap=bar.vwap if hasattr(bar, 'vwap') else None,
                    trade_count=bar.trade_count if hasattr(bar, 'trade_count') else None,
                    timeframe='1d'
                )
                
                # Merge (upsert) to avoid duplicates
                db.merge(vix_record)
            
            db.commit()
            logger.info(f"Stored {len(vix_bars)} VIX bars in database")
        
        # Store latest VIX in Redis for fast access
        try:
            redis_client = redis.Redis(
                host=os.getenv('REDIS_HOST', 'localhost'),
                port=int(os.getenv('REDIS_PORT', 6379)),
                db=int(os.getenv('REDIS_DB', 0)),
                decode_responses=True
            )
            
            redis_client.setex(
                'vix:latest_value',
                86400,  # 24-hour TTL
                str(latest_vix_value)
            )
            
            redis_client.setex(
                'vix:latest_timestamp',
                86400,
                latest_vix_time.isoformat()
            )
            
            logger.info(f"Cached VIX in Redis: {latest_vix_value:.2f}")
        
        except Exception as redis_err:
            logger.warning(f"Redis cache failed (non-critical): {redis_err}")
        
        return {
            'status': 'success',
            'vix_value': latest_vix_value,
            'vix_timestamp': latest_vix_time.isoformat(),
            'bars_collected': len(vix_bars)
        }
    
    except Exception as e:
        logger.error(f"VIX data collection failed: {e}", exc_info=True)
        raise self.retry(exc=e, countdown=300)  # Retry after 5 minutes


def get_latest_vix() -> float:
    """
    Get latest VIX value from Redis cache.
    
    Returns:
        Latest VIX value or None if not available
    """
    try:
        redis_client = redis.Redis(
            host=os.getenv('REDIS_HOST', 'localhost'),
            port=int(os.getenv('REDIS_PORT', 6379)),
            db=int(os.getenv('REDIS_DB', 0)),
            decode_responses=True
        )
        
        vix_str = redis_client.get('vix:latest_value')
        
        if vix_str:
            return float(vix_str)
        else:
            logger.warning("VIX not found in Redis cache")
            return None
    
    except Exception as e:
        logger.error(f"Failed to get VIX from Redis: {e}")
        return None
