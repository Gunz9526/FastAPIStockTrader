import logging
from app.worker import celery_app
from datetime import datetime
from app.core.database import SessionLocal
from app.repositories.stock_repo_sync import SyncStockRepository

logger = logging.getLogger(__name__)

@celery_app.task(name="app.tasks.trading.execute_market_scan")
def execute_market_scan():
    """
    Execute market scan with multi-position portfolio strategy (Phase I.2).
    
    Workflow:
    1. Get all active symbols from DB
    2. Market regime detection (SPY)
    3. Multi-position portfolio processing (max 5 concurrent)
    4. Auto-selection based on correlation + signals
    """
    logger.info("Starting multi-position market scan...")
    
    session = SessionLocal()
    try:
        from app.services.trading_strategy_sync import SyncTradingStrategy
        from app.repositories.stock_repo_sync import SyncStockRepository
        
        strategy = SyncTradingStrategy(session)
        repo = SyncStockRepository(session)
        
        # Get active symbols from DB (dynamic, no hardcoding)
        symbols = repo.get_active_symbols()
        logger.info(f"Candidate symbols: {len(symbols)}")
        
        # Enable multi-position mode
        if strategy.multi_position_mode:
            logger.info("Multi-position mode ENABLED")
            
            # Process portfolio (max 5 positions)
            import asyncio
            asyncio.run(strategy.process_portfolio(symbols))
        else:
            logger.info("Single-position mode (legacy)")
            
            # Legacy: Sequential single-position
            for symbol in symbols[:5]:  # Limit to 5 for safety
                strategy.process_symbol(symbol)
        
        session.commit()
        logger.info("Market scan complete")
        
    except Exception as e:
        logger.error(f"Market scan error: {e}", exc_info=True)
        session.rollback()
        raise
    finally:
        session.close()


@celery_app.task(name="app.tasks.trading.update_trailing_stops")
def update_trailing_stops():
    """
    Update trailing stops (SYNC VERSION).
    """
    logger.info("Updating trailing stops (sync)...")
    
    session = SessionLocal()
    try:
        from sqlalchemy import select
        from app.domain.models.stock import Position, PositionStatus
        
        # Get open positions
        stmt = select(Position).where(Position.status == PositionStatus.OPEN.value)
        result = session.execute(stmt)
        positions = list(result.scalars().all())
        
        if not positions:
            logger.info("No open positions")
            return
        
        logger.info(f"Updating {len(positions)} positions")
        
        # TODO: Implement sync price fetching and trailing stop logic
        logger.warning("Trailing stop update temporarily disabled - needs sync refactoring")
        
        session.commit()
        logger.info("Trailing stops checked")
        
    except Exception as e:
        logger.error(f"Trailing stop error: {e}", exc_info=True)
        session.rollback()
        raise
    finally:
        session.close()
