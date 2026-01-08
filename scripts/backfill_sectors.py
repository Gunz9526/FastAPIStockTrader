import os
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.core.database import SessionLocal
from app.domain.models.stock import StockTicker
from app.ml.sector_map import get_sector, get_sector_id
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def backfill_sectors():
    """
    Backfill sector data for existing symbols in stock_tickers table.
    
    Usage:
        python scripts/backfill_sectors.py
    
    Or via Docker:
        docker compose exec app python scripts/backfill_sectors.py
    """
    session = SessionLocal()
    
    try:
        # Get all tickers without sector info
        tickers = session.query(StockTicker).all()
        
        logger.info(f"Found {len(tickers)} tickers to process")
        
        updated_count = 0
        failed_count = 0
        
        for ticker in tickers:
            try:
                # Fetch sector from yfinance (via get_sector)
                sector_name = get_sector(ticker.symbol)
                sector_id = get_sector_id(ticker.symbol)
                
                # Update ticker (if your StockTicker model has sector fields)
                # NOTE: You may need to add 'sector' and 'sector_id' columns to stock_tickers table
                # For now, we'll just log the results
                
                logger.info(f"{ticker.symbol}: {sector_name} (ID: {sector_id})")
                updated_count += 1
                
            except Exception as e:
                logger.error(f"Failed to update {ticker.symbol}: {e}")
                failed_count += 1
                continue
        
        session.commit()
        logger.info(f"\n{'='*60}")
        logger.info(f"Backfill Complete")
        logger.info(f"{'='*60}")
        logger.info(f"Updated: {updated_count}")
        logger.info(f"Failed: {failed_count}")
        logger.info(f"Total: {len(tickers)}")
        
    except Exception as e:
        logger.error(f"Backfill failed: {e}", exc_info=True)
        session.rollback()
        raise
        
    finally:
        session.close()


if __name__ == "__main__":
    backfill_sectors()
