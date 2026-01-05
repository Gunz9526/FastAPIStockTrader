import asyncio
import logging
import sys
import os
import pandas as pd
from sqlalchemy import select, func
from app.core.database import AsyncSessionLocal
from app.domain.models.stock import StockOHLCV
from datetime import datetime, timedelta

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def check_data():
    """Check OHLCV data quality."""
    async with AsyncSessionLocal() as db:
        # 1. Total Count
        result = await db.execute(select(func.count()).select_from(StockOHLCV))
        count = result.scalar()
        logger.info(f"Total Rows: {count}")
        
        if count == 0:
            logger.error("❌ No data in DB.")
            return

        # 2. Check 15m data specifically
        result = await db.execute(
            select(func.count())
            .select_from(StockOHLCV)
            .where(StockOHLCV.timeframe == '15m')
        )
        count_15m = result.scalar()
        logger.info(f"15m Rows: {count_15m}")

        # 3. Check for Zeros in Close (Fatal for pct_change)
        result = await db.execute(
            select(func.count())
            .select_from(StockOHLCV)
            .where(StockOHLCV.close == 0)
        )
        zeros = result.scalar()
        if zeros > 0:
            logger.warning(f"⚠️  Found {zeros} rows with Close=0.0")

        # 4. Sample Data
        stmt = select(StockOHLCV).limit(5)
        result = await db.execute(stmt)
        rows = result.scalars().all()
        logger.info("--- Sample Data ---")
        for row in rows:
            logger.info(f"{row.symbol} {row.date_time} {row.timeframe} Close={row.close}")

if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(check_data())
