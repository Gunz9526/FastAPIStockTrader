import logging
from app.worker import celery_app
from app.core.database import SessionLocal
from app.repositories.stock_repo_sync import SyncStockRepository
from datetime import datetime
import yfinance as yf

logger = logging.getLogger(__name__)

@celery_app.task(name="app.tasks.data_tasks.collect_fundamentals")
def collect_fundamentals():
    """
    Collect fundamental data from yfinance (SYNC VERSION).
    Updates stock_fundamentals table.
    """
    logger.info("Collecting fundamentals (sync)")
    
    session = SessionLocal()
    try:
        from sqlalchemy import select, update
        from app.domain.models.stock import StockTicker, StockFundamentals
        
        # Get active symbols
        stmt = select(StockTicker.symbol).where(StockTicker.is_active == True)
        result = session.execute(stmt)
        symbols = [row[0] for row in result]
        
        if not symbols:
            logger.warning("No symbols for fundamentals")
            return
        
        logger.info(f"Collecting fundamentals for {len(symbols)} symbols")
        
        success_count = 0
        error_count = 0
        
        for symbol in symbols:
            try:
                # Fetch from yfinance
                ticker = yf.Ticker(symbol)
                info = ticker.info
                
                if not info:
                    logger.warning(f"No info for {symbol}")
                    continue
                
                # Extract key metrics matching StockFundamentals model
                fundamentals_data = {
                    'symbol': symbol,
                    'market_cap': info.get('marketCap'),
                    'per': info.get('trailingPE'),
                    'pbr': info.get('priceToBook'),
                    'roe': info.get('returnOnEquity'),
                    'sector': info.get('sector'),                
                    'date': datetime.utcnow()
                }
                
                # Upsert (update or insert)
                existing = session.execute(
                    select(StockFundamentals).where(StockFundamentals.symbol == symbol)
                ).scalar_one_or_none()
                
                if existing:
                    # Update
                    for key, value in fundamentals_data.items():
                        if key != 'symbol':
                            setattr(existing, key, value)
                else:
                    # Insert
                    new_fundamental = StockFundamentals(**fundamentals_data)
                    session.add(new_fundamental)
                
                success_count += 1
                logger.debug(f"{symbol} fundamentals updated")
                
            except Exception as e:
                error_count += 1
                logger.error(f"Failed {symbol}: {e}")
                continue
        
        session.commit()
        logger.info(f"Fundamentals collection complete: {success_count} success, {error_count} errors")
        
        return {
            'success': success_count,
            'errors': error_count,
            'total': len(symbols)
        }
        
    except Exception as e:
        logger.error(f"Fundamentals error: {e}", exc_info=True)
        session.rollback()
        raise
    finally:
        session.close()
