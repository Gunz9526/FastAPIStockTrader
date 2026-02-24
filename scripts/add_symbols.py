"""
종목 추가 스크립트
SPY, QQQ 등 ETF 및 추가 종목을 stock_ticker 테이블에 추가

총 60개 종목: 11 GICS 섹터 + ETF 2종
섹터 분포는 S&P 500 시가총액 비중에 근사하게 설계
최종 업데이트: 2026-02-24
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import logging

from sqlalchemy import select

from app.core.database import SessionLocal
from app.domain.models.stock import StockTicker

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────
# 추가할 종목 목록 (symbol, name, market, sector)
# sector 값은 app/ml/sector_map.py SECTOR_TO_ID 키와 반드시 일치해야 함
# 총 60개: ETF 2 + Technology 12 + Communication Services 4
#   + Consumer Cyclical 6 + Consumer Defensive 5 + Financial Services 6
#   + Healthcare 6 + Energy 4 + Industrials 6 + Basic Materials 3
#   + Real Estate 3 + Utilities 3
# ──────────────────────────────────────────────────────────────────────
SYMBOLS_TO_ADD = [
    # ── Market Index ETF (2) ─────────────────────────────────────────
    ('SPY', 'SPDR S&P 500 ETF Trust', 'ARCA', 'Market Index'),
    ('QQQ', 'Invesco QQQ Trust', 'NASDAQ', 'Market Index'),

    # ── Technology (12) ──────────────────────────────────────────────
    ('AAPL', 'Apple Inc', 'NASDAQ', 'Technology'),
    ('MSFT', 'Microsoft Corp', 'NASDAQ', 'Technology'),
    ('NVDA', 'NVIDIA Corp', 'NASDAQ', 'Technology'),
    ('AMD', 'Advanced Micro Devices Inc', 'NASDAQ', 'Technology'),
    ('CRM', 'Salesforce Inc', 'NYSE', 'Technology'),
    ('ADBE', 'Adobe Inc', 'NASDAQ', 'Technology'),
    ('AVGO', 'Broadcom Inc', 'NASDAQ', 'Technology'),
    ('ORCL', 'Oracle Corp', 'NYSE', 'Technology'),
    ('INTC', 'Intel Corp', 'NASDAQ', 'Technology'),
    ('CSCO', 'Cisco Systems Inc', 'NASDAQ', 'Technology'),
    ('QCOM', 'Qualcomm Inc', 'NASDAQ', 'Technology'),
    ('NOW', 'ServiceNow Inc', 'NYSE', 'Technology'),

    # ── Communication Services (4) ───────────────────────────────────
    ('GOOGL', 'Alphabet Inc Class A', 'NASDAQ', 'Communication Services'),
    ('META', 'Meta Platforms Inc', 'NASDAQ', 'Communication Services'),
    ('NFLX', 'Netflix Inc', 'NASDAQ', 'Communication Services'),
    ('DIS', 'Walt Disney Co', 'NYSE', 'Communication Services'),

    # ── Consumer Cyclical (6) ────────────────────────────────────────
    ('AMZN', 'Amazon.com Inc', 'NASDAQ', 'Consumer Cyclical'),
    ('TSLA', 'Tesla Inc', 'NASDAQ', 'Consumer Cyclical'),
    ('HD', 'Home Depot Inc', 'NYSE', 'Consumer Cyclical'),
    ('MCD', "McDonald's Corp", 'NYSE', 'Consumer Cyclical'),
    ('NKE', 'Nike Inc', 'NYSE', 'Consumer Cyclical'),
    ('SBUX', 'Starbucks Corp', 'NASDAQ', 'Consumer Cyclical'),

    # ── Consumer Defensive (5) ───────────────────────────────────────
    ('WMT', 'Walmart Inc', 'NYSE', 'Consumer Defensive'),
    ('PG', 'Procter & Gamble Co', 'NYSE', 'Consumer Defensive'),
    ('KO', 'Coca-Cola Co', 'NYSE', 'Consumer Defensive'),
    ('PEP', 'PepsiCo Inc', 'NASDAQ', 'Consumer Defensive'),
    ('COST', 'Costco Wholesale Corp', 'NASDAQ', 'Consumer Defensive'),

    # ── Financial Services (6) ───────────────────────────────────────
    ('JPM', 'JPMorgan Chase & Co', 'NYSE', 'Financial Services'),
    ('V', 'Visa Inc', 'NYSE', 'Financial Services'),
    ('MA', 'Mastercard Inc', 'NYSE', 'Financial Services'),
    ('BAC', 'Bank of America Corp', 'NYSE', 'Financial Services'),
    ('GS', 'Goldman Sachs Group Inc', 'NYSE', 'Financial Services'),
    ('BLK', 'BlackRock Inc', 'NYSE', 'Financial Services'),

    # ── Healthcare (6) ───────────────────────────────────────────────
    ('JNJ', 'Johnson & Johnson', 'NYSE', 'Healthcare'),
    ('UNH', 'UnitedHealth Group Inc', 'NYSE', 'Healthcare'),
    ('LLY', 'Eli Lilly and Co', 'NYSE', 'Healthcare'),
    ('PFE', 'Pfizer Inc', 'NYSE', 'Healthcare'),
    ('ABT', 'Abbott Laboratories', 'NYSE', 'Healthcare'),
    ('TMO', 'Thermo Fisher Scientific Inc', 'NYSE', 'Healthcare'),

    # ── Energy (4) ───────────────────────────────────────────────────
    ('XOM', 'Exxon Mobil Corp', 'NYSE', 'Energy'),
    ('CVX', 'Chevron Corp', 'NYSE', 'Energy'),
    ('COP', 'ConocoPhillips', 'NYSE', 'Energy'),
    ('SLB', 'SLB Ltd', 'NYSE', 'Energy'),

    # ── Industrials (6) ──────────────────────────────────────────────
    ('HON', 'Honeywell International Inc', 'NASDAQ', 'Industrials'),
    ('CAT', 'Caterpillar Inc', 'NYSE', 'Industrials'),
    ('UNP', 'Union Pacific Corp', 'NYSE', 'Industrials'),
    ('GE', 'GE Aerospace', 'NYSE', 'Industrials'),
    ('RTX', 'RTX Corp', 'NYSE', 'Industrials'),
    ('BA', 'Boeing Co', 'NYSE', 'Industrials'),

    # ── Basic Materials (3) ──────────────────────────────────────────
    ('LIN', 'Linde plc', 'NASDAQ', 'Basic Materials'),
    ('APD', 'Air Products and Chemicals Inc', 'NYSE', 'Basic Materials'),
    ('SHW', 'Sherwin-Williams Co', 'NYSE', 'Basic Materials'),

    # ── Real Estate (3) ──────────────────────────────────────────────
    ('AMT', 'American Tower Corp', 'NYSE', 'Real Estate'),
    ('PLD', 'Prologis Inc', 'NYSE', 'Real Estate'),
    ('CCI', 'Crown Castle Inc', 'NYSE', 'Real Estate'),

    # ── Utilities (3) ────────────────────────────────────────────────
    ('NEE', 'NextEra Energy Inc', 'NYSE', 'Utilities'),
    ('DUK', 'Duke Energy Corp', 'NYSE', 'Utilities'),
    ('SO', 'Southern Co', 'NYSE', 'Utilities'),
]



def add_symbols():
    """종목을 stock_ticker 테이블에 추가"""
    session = SessionLocal()
    try:
        added_count = 0
        updated_count = 0
        skipped_count = 0

        for symbol, name, market, sector in SYMBOLS_TO_ADD:
            # 기존 종목 확인
            stmt = select(StockTicker).where(StockTicker.symbol == symbol)
            existing = session.execute(stmt).scalar_one_or_none()

            if existing:
                # 이미 존재하는 경우 is_active 및 섹터 업데이트
                changed = False
                if not existing.is_active:
                    existing.is_active = True
                    changed = True
                if existing.sector != sector:
                    logger.info(f"{symbol} 섹터 업데이트: {existing.sector} → {sector}")
                    existing.sector = sector
                    changed = True
                if changed:
                    logger.info(f"{symbol} 업데이트됨 ({name})")
                    updated_count += 1
                else:
                    logger.info(f"{symbol} 이미 활성화 상태 ({name})")
                    skipped_count += 1
            else:
                # 새로 추가
                new_ticker = StockTicker(
                    symbol=symbol,
                    name=name,
                    market=market,
                    sector=sector,
                    is_active=True
                )
                session.add(new_ticker)
                logger.info(f"{symbol} 추가됨 ({name} - {sector})")
                added_count += 1

        session.commit()

        logger.info("=" * 60)
        logger.info("종목 추가 완료:")
        logger.info(f"  - 신규 추가: {added_count}개")
        logger.info(f"  - 업데이트: {updated_count}개")
        logger.info(f"  - 이미 활성: {skipped_count}개")
        logger.info(f"  - 총: {added_count + updated_count + skipped_count}개")
        logger.info("=" * 60)

        # 전체 활성 종목 확인
        stmt = select(StockTicker).where(StockTicker.is_active == True)
        active_tickers = session.execute(stmt).scalars().all()

        logger.info(f"\n현재 활성 종목 ({len(active_tickers)}개):")

        # 섹터별 그룹 출력
        sector_groups: dict[str, list[str]] = {}
        for ticker in sorted(active_tickers, key=lambda x: x.symbol):
            sector_name = ticker.sector or 'Unknown'
            sector_groups.setdefault(sector_name, []).append(ticker.symbol)

        for sector_name, syms in sorted(sector_groups.items()):
            logger.info(f"  [{sector_name}] ({len(syms)}개): {', '.join(syms)}")

    except Exception as e:
        logger.error(f"종목 추가 실패: {e}", exc_info=True)
        session.rollback()
        raise
    finally:
        session.close()


if __name__ == "__main__":
    logger.info("종목 추가 스크립트 시작...")
    add_symbols()
    logger.info("\n다음 단계:")
    logger.info("1. 일봉 백필: python scripts/backfill_ohlcv.py --years 2 --timeframe 1d")
    logger.info("2. 학습 실행: Celery train_models 태스크 트리거")
