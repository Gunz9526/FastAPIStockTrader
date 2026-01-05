"""
OHLCV Historical Data Backfill Script

과거 N년 데이터를 Alpaca API에서 가져와 DB에 저장합니다.
"""
import asyncio
import sys
import os
from datetime import datetime, timedelta
import logging

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select
from app.core.database import AsyncSessionLocal
from app.domain.models.stock import StockTicker, StockOHLCV
from app.services.data_provider import AlpacaDataProvider
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Configuration
BACKFILL_YEARS = 2  # 2년 권장 (과적합 방지)
BATCH_SIZE = 10     # 한번에 처리할 종목 수

async def backfill_ohlcv(years: int = BACKFILL_YEARS):
    """
    과거 N년 OHLCV 데이터 백필
    
    Args:
        years: 백필할 년수 (기본 2년)
    """
    logger.info(f"🚀 Starting OHLCV backfill for {years} years...")
    
    provider = AlpacaDataProvider()
    
    async with AsyncSessionLocal() as db:
        # 1. 활성 종목 가져오기
        result = await db.execute(
            select(StockTicker.symbol).where(StockTicker.is_active == True)
        )
        symbols = result.scalars().all()
        
        if not symbols:
            logger.warning("No active tickers found in database!")
            return
        
        logger.info(f"Found {len(symbols)} active tickers")
        
        # 2. 날짜 범위 설정
        end_date = datetime.now()
        start_date = end_date - timedelta(days=years * 365)
        
        logger.info(f"Date range: {start_date.date()} to {end_date.date()}")
        
        total_inserted = 0
        total_skipped = 0
        failed_symbols = []
        
        # 3. 각 종목별로 데이터 수집
        for idx, symbol in enumerate(symbols, 1):
            # symbol is already a string
            logger.info(f"[{idx}/{len(symbols)}] Processing {symbol}...")
            
            try:
                # 3.1. 기존 데이터 확인 (중복 방지)
                existing = await db.execute(
                    select(StockOHLCV)
                    .where(StockOHLCV.symbol == symbol)
                    .where(StockOHLCV.date_time >= start_date)
                )
                existing_count = len(existing.scalars().all())
                
                if existing_count > 0:
                    logger.info(f"  ⚠️  {symbol} already has {existing_count} bars. Skipping.")
                    total_skipped += existing_count
                    continue
                
                # 3.2. Alpaca에서 데이터 가져오기
                # Request 15-minute bars
                bars = await provider.get_historical_data(
                    symbol, 
                    start_date, 
                    end_date,
                    timeframe=TimeFrame(15, TimeFrameUnit.Minute)
                )
                
                if not bars:
                    logger.warning(f"  ⚠️  No 15m data for {symbol}. Testing 1Day fallback...")
                    # Fallback to 1Day to check if symbol data exists at all
                    bars_daily = await provider.get_historical_data(
                        symbol, 
                        start_date, 
                        end_date,
                        timeframe=TimeFrame.Day
                    )
                    if bars_daily:
                        logger.error(f"  ❌ {symbol}: 1Day data exists ({len(bars_daily)} bars), but 15m failed. Likely IEX feed restriction or timeout.")
                    else:
                        logger.error(f"  ❌ {symbol}: No data found even for 1Day. Symbol might be delisted or inactive.")
                    
                    failed_symbols.append(symbol)
                    continue
                
                # 3.3. DB에 저장
                inserted_count = 0
                for bar in bars:
                    ohlcv = StockOHLCV(
                        symbol=symbol,
                        date_time=bar.date_time,
                        timeframe='15m',
                        open=bar.open,
                        high=bar.high,
                        low=bar.low,
                        close=bar.close,
                        volume=bar.volume
                    )
                    db.add(ohlcv)
                    inserted_count += 1
                
                await db.commit()
                total_inserted += inserted_count
                
                logger.info(f"  ✅ {symbol}: {inserted_count} bars inserted")
                
                # Rate limiting (Alpaca API 제한)
                await asyncio.sleep(0.5)
                
            except Exception as e:
                logger.error(f"  ❌ Failed to process {symbol}: {e}")
                failed_symbols.append(symbol)
                await db.rollback()
                continue
    
    # 4. 결과 요약
    logger.info("=" * 60)
    logger.info("📊 Backfill Summary:")
    logger.info(f"  - Total inserted: {total_inserted} bars")
    logger.info(f"  - Total skipped: {total_skipped} bars")
    logger.info(f"  - Failed symbols: {len(failed_symbols)}")
    
    if failed_symbols:
        logger.warning(f"  Failed: {', '.join(failed_symbols)}")
    
    logger.info("=" * 60)
    logger.info("✅ Backfill complete!")

async def verify_backfill():
    """백필 결과 검증"""
    logger.info("🔍 Verifying backfill results...")
    
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(StockTicker).where(StockTicker.is_active == True)
        )
        tickers = result.scalars().all()
        
        for ticker in tickers:
            symbol = ticker.symbol
            
            # 데이터 카운트
            count_result = await db.execute(
                select(StockOHLCV).where(StockOHLCV.symbol == symbol)
            )
            bars = count_result.scalars().all()
            
            if bars:
                dates = [bar.date_time for bar in bars]
                logger.info(
                    f"  {symbol}: {len(bars)} bars "
                    f"({min(dates).date()} to {max(dates).date()})"
                )
            else:
                logger.warning(f"  {symbol}: No data found!")

async def main():
    """메인 실행 함수"""
    import argparse
    
    parser = argparse.ArgumentParser(description='Backfill OHLCV historical data')
    parser.add_argument(
        '--years',
        type=int,
        default=BACKFILL_YEARS,
        help=f'Number of years to backfill (default: {BACKFILL_YEARS})'
    )
    parser.add_argument(
        '--verify',
        action='store_true',
        help='Verify backfill results only'
    )
    
    args = parser.parse_args()
    
    if args.verify:
        await verify_backfill()
    else:
        await backfill_ohlcv(args.years)
        await verify_backfill()

if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    
    asyncio.run(main())
