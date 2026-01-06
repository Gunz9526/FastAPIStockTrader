"""
Real-time 15-minute OHLCV data collection with VWAP.
Collects data during market hours for active trading.
"""
import logging
from app.worker import celery_app
from app.core.database import SessionLocal
from app.repositories.stock_repo_sync import SyncStockRepository
from datetime import datetime, timedelta
import pandas as pd
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from app.core.config import settings

logger = logging.getLogger(__name__)

@celery_app.task(name="app.tasks.realtime_data.collect_15m_realtime")
def collect_15m_realtime():
    """
    Collect 15-minute OHLCV + VWAP data in real-time.
    Runs every 15 minutes during market hours (9:30 AM - 4:00 PM ET).
    """
    logger.info("Starting 15m real-time data collection...")
    
    # Check if market is currently open
    current_time = datetime.now()
    current_hour = current_time.hour
    current_minute = current_time.minute
    day_of_week = current_time.weekday()  # 0=Monday, 6=Sunday
    
    # Market hours: Mon-Fri, 9:30 AM - 4:00 PM ET (14:30-21:00 UTC roughly, but using local)
    # Simplified check: Hour 9-15 on weekdays
    if day_of_week > 4:  # Weekend
        logger.info("Market closed (weekend). Skipping 15m collection.")
        return {"status": "skipped", "reason": "weekend"}
    
    if current_hour < 9 or (current_hour == 9 and current_minute < 30):
        logger.info("Market not yet open. Skipping 15m collection.")
        return {"status": "skipped", "reason": "pre-market"}
    
    if current_hour >= 16:
        logger.info("Market already closed. Skipping 15m collection.")
        return {"status": "skipped", "reason": "post-market"}
    
    session = SessionLocal()
    try:
        repo = SyncStockRepository(session)
        
        # Get active symbols
        symbols = repo.get_active_symbols()
        if not symbols:
            logger.warning("No active symbols for 15m collection")
            return {"status": "no_symbols"}
        
        logger.info(f"Collecting 15m data for {len(symbols)} symbols")
        
        # Initialize Alpaca client
        data_client = StockHistoricalDataClient(
            api_key=settings.ALPACA_API_KEY,
            secret_key=settings.ALPACA_SECRET_KEY,
        )
        
        # Fetch last 2 bars (30 minutes) to ensure we capture the most recent completed 15m bar
        end_time = datetime.now()
        start_time = end_time - timedelta(minutes=30)
        
        success_count = 0
        error_count = 0
        
        for symbol in symbols:
            try:
                request = StockBarsRequest(
                    symbol_or_symbols=[symbol],
                    timeframe=TimeFrame(15, TimeFrameUnit.Minute),
                    start=start_time,
                    end=end_time,
                    feed='iex'  # Free tier compatible
                )
                
                bars_response = data_client.get_stock_bars(request)
                
                if not bars_response or not bars_response.data:
                    logger.debug(f"{symbol}: No 15m data returned")
                    continue
                
                symbol_bars = bars_response.data.get(symbol, [])
                if not symbol_bars:
                    logger.debug(f"{symbol}: Empty bar list")
                    continue
                
                # Process each bar
                for bar in symbol_bars:
                    # Check if this bar already exists in DB
                    existing = repo.get_ohlcv_by_datetime(symbol, bar.timestamp, timeframe='15m')
                    
                    if existing:
                        # Update existing bar (in case of corrections)
                        existing.open = float(bar.open)
                        existing.high = float(bar.high)
                        existing.low = float(bar.low)
                        existing.close = float(bar.close)
                        existing.volume = float(bar.volume)
                        existing.vwap = float(bar.vwap) if hasattr(bar, 'vwap') and bar.vwap else None
                        existing.trade_count = int(bar.trade_count) if hasattr(bar, 'trade_count') and bar.trade_count else None
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
                            timeframe='15m',
                            vwap=float(bar.vwap) if hasattr(bar, 'vwap') and bar.vwap else None,
                            trade_count=int(bar.trade_count) if hasattr(bar, 'trade_count') and bar.trade_count else None
                        )
                        
                        repo.create_ohlcv(ohlcv_data)
                
                success_count += 1
                logger.debug(f"{symbol}: 15m data collected ({len(symbol_bars)} bars)")
                
            except Exception as e:
                error_count += 1
                logger.error(f"Failed to collect 15m data for {symbol}: {e}")
                continue
        
        session.commit()
        logger.info(f"15m collection complete: {success_count} success, {error_count} errors")
        
        return {
            'status': 'completed',
            'success': success_count,
            'errors': error_count,
            'total': len(symbols)
        }
        
    except Exception as e:
        logger.error(f"15m collection error: {e}", exc_info=True)
        session.rollback()
        raise
    finally:
        session.close()
