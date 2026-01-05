import asyncio
import logging
import sys
import os

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.database import engine, Base
from app.domain.models.stock import StockOHLCV
import sqlalchemy as sa

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def migrate():
    """
    Drops and Recreates the stock_ohlcv table to support 15m timeframe.
    WARNING: This deletes all existing OHLCV data.
    """
    try:
        async with engine.begin() as conn:
            logger.info("⚠️  Dropping existing table: stock_ohlcv")
            logger.info("⚠️  Dropping existing table: stock_ohlcv (CASCADE)")
            # Use CASACADE to remove dependent TimescaleDB views/objects
            await conn.execute(sa.text("DROP TABLE IF EXISTS stock_ohlcv CASCADE"))
            await conn.execute(sa.text("DROP SEQUENCE IF EXISTS stock_ohlcv_id_seq"))
            
            logger.info("✨ Re-creating table with new schema (Composite PK: symbol, date_time, timeframe)")
            
            # Create Sequence manually to ensure it exists for the ID column
            # await conn.execute(sa.text("CREATE SEQUENCE stock_ohlcv_id_seq"))
            
            await conn.run_sync(StockOHLCV.__table__.create)
            
            # --- TimescaleDB & Optimization Restoration ---
            logger.info("🔧 Restoring TimescaleDB Hypertable & Optimizations...")
            
            # 1. Convert to Hypertable (partition by date_time)
            await conn.execute(sa.text("""
                SELECT create_hypertable(
                    'stock_ohlcv',
                    'date_time',
                    chunk_time_interval => INTERVAL '30 days',
                    if_not_exists => TRUE
                );
            """))
            
            # 2. Re-create Optimized Index
            await conn.execute(sa.text("""
                CREATE INDEX IF NOT EXISTS idx_ohlcv_symbol_time 
                ON stock_ohlcv (symbol, date_time DESC);
            """))
            
            # 3. Re-create Continuous Aggregate (Daily view from 15m/1d data)
            # This is critical for efficient daily queries if needed
            await conn.execute(sa.text("""
                CREATE MATERIALIZED VIEW IF NOT EXISTS daily_ohlcv
                WITH (timescaledb.continuous) AS
                SELECT
                    time_bucket('1 day', date_time) AS bucket,
                    symbol,
                    first(open, date_time) AS open,
                    max(high) AS high,
                    min(low) AS low,
                    last(close, date_time) AS close,
                    sum(volume) AS volume
                FROM stock_ohlcv
                GROUP BY bucket, symbol
                WITH NO DATA;
            """))
            
            # 4. Refresh Policy
            await conn.execute(sa.text("""
                SELECT add_continuous_aggregate_policy('daily_ohlcv',
                    start_offset => INTERVAL '3 days',
                    end_offset => INTERVAL '1 hour',
                    schedule_interval => INTERVAL '1 hour'
                );
            """))
            # -----------------------------------------------
            
            logger.info("✅ Migration script execution completed successfully.")
    except Exception as e:
        logger.error(f"❌ Migration failed: {e}")
    finally:
        await engine.dispose()

if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(migrate())
