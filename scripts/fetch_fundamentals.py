import asyncio
import sys
import os
import logging
from datetime import date
import yfinance as yf

# Fix path
sys.path.append(os.getcwd())

from sqlalchemy import select, update
from app.core.database import AsyncSessionLocal
from app.domain.models.stock import StockTicker, StockFundamentals

# Setup Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def fetch_fundamentals():
    """
    Fetches PER, PBR, ROE, MarketCap, Sector from yfinance for all active tickers.
    Updates `stock_fundamentals` and `stock_tickers` tables.
    """
    logger.info("Starting Fundamentals Fetcher...")

    async with AsyncSessionLocal() as db:
        # 1. Get Active Tickers
        result = await db.execute(select(StockTicker).where(StockTicker.is_active == True))
        tickers = result.scalars().all()
        
        if not tickers:
            logger.warning("No active tickers found.")
            return

        for ticker in tickers:
            symbol = ticker.symbol
            logger.info(f"Processing {symbol}...")
            
            try:
                # 2. Fetch from YFinance (Sync call)
                # Note: yfinance is blocking. In heavy prod, run in executor.
                # For script, it's fine.
                yf_ticker = yf.Ticker(symbol)
                info = yf_ticker.info
                
                # key names vary in yfinance, need safe get
                sector = info.get("sector")
                market_cap = info.get("marketCap")
                
                # Ratios (May be None)
                per = info.get("trailingPE") or info.get("forwardPE")
                pbr = info.get("priceToBook")
                roe = info.get("returnOnEquity")
                
                # 3. Update Ticker Info (Sector)
                if sector:
                    ticker.sector = sector
                    db.add(ticker) # Mark for update

                # 4. Insert/Update Fundamentals
                # Check if entry exists for today
                today = date.today()
                
                # Upsert Logic roughly
                stmt = select(StockFundamentals).where(
                    StockFundamentals.symbol == symbol,
                    StockFundamentals.date == today
                )
                existing_fund_res = await db.execute(stmt)
                existing_fund = existing_fund_res.scalar_one_or_none()
                
                if existing_fund:
                    existing_fund.per = per
                    existing_fund.pbr = pbr
                    existing_fund.roe = roe
                    existing_fund.market_cap = market_cap
                    existing_fund.sector = sector
                    db.add(existing_fund)
                else:
                    new_fund = StockFundamentals(
                        symbol=symbol,
                        date=today,
                        per=per,
                        pbr=pbr,
                        roe=roe,
                        market_cap=market_cap,
                        sector=sector
                    )
                    db.add(new_fund)
                
                await db.commit()
                logger.info(f"Updated {symbol}: Sector={sector}, PER={per}")

            except Exception as e:
                logger.error(f"Failed to fetch for {symbol}: {e}")
                await db.rollback()

    logger.info("Fundamentals Fetch Completed.")

if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(fetch_fundamentals())
