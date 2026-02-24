"""
OHLCV Historical Data Backfill Script

과거 N년 데이터를 Alpaca API에서 가져와 DB에 저장합니다.
개별 bar 단위로 중복 체크하여 기존 데이터는 보존하고 누락된 데이터만 추가합니다.

Usage:
    python scripts/backfill_ohlcv.py --years 2 --timeframe 1d
    python scripts/backfill_ohlcv.py --years 2 --timeframe 15m
    python scripts/backfill_ohlcv.py --verify --timeframe 1d

Updated: 2026-02-24 (Session 7 — Daily timeframe support)
"""
import asyncio
import logging
import os
import sys
from datetime import datetime, timedelta

from pytz import timezone

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from sqlalchemy import func, select

from app.core.database import AsyncSessionLocal
from app.domain.models.stock import StockOHLCV, StockTicker
from app.services.data_provider import AlpacaDataProvider

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Configuration
BACKFILL_YEARS = 2   # 2년 권장 (과적합 방지)
DEFAULT_TIMEFRAME = '1d'  # 일봉 (daily) — ML 학습용 기본값

# Timeframe mapping: CLI argument → (Alpaca TimeFrame, DB timeframe string)
TIMEFRAME_MAP: dict[str, tuple[TimeFrame, str]] = {
    '1d': (TimeFrame.Day, '1d'),
    '15m': (TimeFrame(15, TimeFrameUnit.Minute), '15m'),
    '1h': (TimeFrame(1, TimeFrameUnit.Hour), '1h'),
}


async def backfill_ohlcv(
    years: int = BACKFILL_YEARS,
    timeframe_key: str = DEFAULT_TIMEFRAME,
) -> None:
    """
    과거 N년 OHLCV 데이터 백필.

    Args:
        years: 백필할 년수 (기본 2년).
        timeframe_key: 타임프레임 키 ('1d', '15m', '1h'). 기본 '1d'.
    """
    if timeframe_key not in TIMEFRAME_MAP:
        logger.error(
            "지원하지 않는 타임프레임: %s (지원: %s)",
            timeframe_key, list(TIMEFRAME_MAP.keys()),
        )
        return

    alpaca_tf, db_tf = TIMEFRAME_MAP[timeframe_key]
    logger.info("Starting OHLCV backfill: %d years, timeframe=%s", years, db_tf)

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

        logger.info("Found %d active tickers", len(symbols))

        # 2. 날짜 범위 설정
        et_tz = timezone('America/New_York')
        end_date = datetime.now(et_tz)
        start_date = end_date - timedelta(days=years * 365)

        logger.info("Date range: %s to %s", start_date.date(), end_date.date())

        total_inserted = 0
        total_skipped = 0
        failed_symbols: list[str] = []

        # 3. 각 종목별로 데이터 수집
        for idx, symbol in enumerate(symbols, 1):
            logger.info("[%d/%d] Processing %s (tf=%s)...", idx, len(symbols), symbol, db_tf)

            try:
                # 3.1. Alpaca에서 데이터 가져오기
                bars = await provider.get_historical_data(
                    symbol,
                    start_date,
                    end_date,
                    timeframe=alpaca_tf,
                )

                if not bars:
                    logger.warning("  ⚠️  No %s data for %s", db_tf, symbol)
                    failed_symbols.append(symbol)
                    continue

                # 3.2. DB에 저장 (개별 bar 단위로 중복 체크)
                inserted_count = 0
                skipped_count = 0

                for bar in bars:
                    # Check if this specific bar already exists
                    existing = await db.execute(
                        select(StockOHLCV)
                        .where(StockOHLCV.symbol == symbol)
                        .where(StockOHLCV.timeframe == db_tf)
                        .where(StockOHLCV.date_time == bar.date_time)
                    )

                    if existing.scalar_one_or_none():
                        skipped_count += 1
                        continue

                    # Insert new bar
                    ohlcv = StockOHLCV(
                        symbol=symbol,
                        date_time=bar.date_time,
                        timeframe=db_tf,
                        open=float(bar.open),
                        high=float(bar.high),
                        low=float(bar.low),
                        close=float(bar.close),
                        volume=float(bar.volume),
                        adj_close=None,
                        vwap=(
                            float(bar.vwap)
                            if hasattr(bar, 'vwap') and bar.vwap is not None
                            else None
                        ),
                        trade_count=(
                            int(bar.trade_count)
                            if hasattr(bar, 'trade_count') and bar.trade_count is not None
                            else None
                        ),
                    )
                    db.add(ohlcv)
                    inserted_count += 1

                await db.commit()
                total_inserted += inserted_count
                total_skipped += skipped_count

                logger.info(
                    "  ✅ %s: %d bars inserted, %d skipped (already exist)",
                    symbol, inserted_count, skipped_count,
                )

                # Rate limiting (Alpaca API 제한)
                await asyncio.sleep(0.3)

            except Exception as e:
                logger.error("  ❌ Failed to process %s: %s", symbol, e)
                failed_symbols.append(symbol)
                await db.rollback()
                continue

    # 4. 결과 요약
    logger.info("=" * 60)
    logger.info("📊 Backfill Summary (timeframe=%s):", db_tf)
    logger.info("  - Total inserted: %d bars", total_inserted)
    logger.info("  - Total skipped: %d bars", total_skipped)
    logger.info("  - Successful: %d symbols", len(symbols) - len(failed_symbols))
    logger.info("  - Failed: %d symbols", len(failed_symbols))

    if failed_symbols:
        logger.warning("  Failed: %s", ', '.join(failed_symbols))

    logger.info("=" * 60)
    logger.info("✅ Backfill complete!")


async def verify_backfill(timeframe_key: str = DEFAULT_TIMEFRAME) -> None:
    """백필 결과 검증.

    Args:
        timeframe_key: 검증할 타임프레임 ('1d', '15m', '1h').
    """
    _, db_tf = TIMEFRAME_MAP.get(timeframe_key, (None, timeframe_key))
    logger.info("Verifying backfill results (timeframe=%s)...", db_tf)

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(StockTicker).where(StockTicker.is_active == True)
        )
        tickers = result.scalars().all()

        total_bars = 0
        symbols_with_data = 0
        symbols_missing = 0

        for ticker in sorted(tickers, key=lambda t: t.symbol):
            symbol = ticker.symbol

            # 타임프레임별 데이터 카운트 (효율적 COUNT 쿼리)
            count_result = await db.execute(
                select(
                    func.count(StockOHLCV.id),
                    func.min(StockOHLCV.date_time),
                    func.max(StockOHLCV.date_time),
                )
                .where(StockOHLCV.symbol == symbol)
                .where(StockOHLCV.timeframe == db_tf)
            )
            row = count_result.one()
            bar_count, min_date, max_date = row

            if bar_count and bar_count > 0:
                total_bars += bar_count
                symbols_with_data += 1
                logger.info(
                    "  %s: %d bars (%s to %s) [%s]",
                    symbol, bar_count,
                    min_date.date() if min_date else 'N/A',
                    max_date.date() if max_date else 'N/A',
                    ticker.sector or 'Unknown',
                )
            else:
                symbols_missing += 1
                logger.warning("  %s: No %s data found!", symbol, db_tf)

        logger.info("=" * 60)
        logger.info("📊 Verification Summary (timeframe=%s):", db_tf)
        logger.info("  - Symbols with data: %d", symbols_with_data)
        logger.info("  - Symbols missing: %d", symbols_missing)
        logger.info("  - Total bars: %d", total_bars)
        if symbols_with_data > 0:
            logger.info("  - Avg bars/symbol: %d", total_bars // symbols_with_data)
        logger.info("=" * 60)


async def main() -> None:
    """메인 실행 함수."""
    import argparse

    parser = argparse.ArgumentParser(description='Backfill OHLCV historical data')
    parser.add_argument(
        '--years',
        type=int,
        default=BACKFILL_YEARS,
        help=f'Number of years to backfill (default: {BACKFILL_YEARS})',
    )
    parser.add_argument(
        '--timeframe',
        type=str,
        default=DEFAULT_TIMEFRAME,
        choices=list(TIMEFRAME_MAP.keys()),
        help=f'Timeframe for bars (default: {DEFAULT_TIMEFRAME})',
    )
    parser.add_argument(
        '--verify',
        action='store_true',
        help='Verify backfill results only (no data collection)',
    )

    args = parser.parse_args()

    et_tz = timezone('America/New_York')
    current_time = datetime.now(et_tz)
    logger.info("Current time (ET): %s", current_time)
    logger.info("Timeframe: %s", args.timeframe)

    if args.verify:
        await verify_backfill(args.timeframe)
    else:
        await backfill_ohlcv(args.years, args.timeframe)
        await verify_backfill(args.timeframe)


if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    asyncio.run(main())
