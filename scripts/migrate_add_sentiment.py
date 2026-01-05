import asyncio
import logging
import sys
import os
import sqlalchemy as sa
from sqlalchemy import text

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.database import engine

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def migrate_sentiment():
    """Add sentiment_score column to stock_fundamentals table."""
    try:
        async with engine.begin() as conn:
            logger.info("🔧 Adding sentiment_score column to stock_fundamentals...")
            await conn.execute(text("""
                ALTER TABLE stock_fundamentals 
                ADD COLUMN IF NOT EXISTS sentiment_score FLOAT;
            """))
            await conn.execute(text("""
                COMMENT ON COLUMN stock_fundamentals.sentiment_score IS 'News/Social Sentiment (-1.0 to 1.0)';
            """))
            logger.info("✅ Column added successfully.")
            
    except Exception as e:
        logger.error(f"❌ Migration failed: {e}")
    finally:
        await engine.dispose()

if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(migrate_sentiment())
