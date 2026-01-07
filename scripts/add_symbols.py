"""
종목 추가 스크립트
SPY, QQQ 등 ETF 및 추가 종목을 stock_ticker 테이블에 추가
"""

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.core.database import SessionLocal
from app.domain.models.stock import StockTicker
from sqlalchemy import select
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 추가할 종목 목록 (심볼, 이름, 섹터)
SYMBOLS_TO_ADD = [
    # ETF - 필수 (시장 지수 및 레짐 감지용)
    ('SPY', 'SPDR S&P 500 ETF Trust', 'ARCA', 'ETF'),
    ('QQQ', 'Invesco QQQ Trust', 'NASDAQ', 'ETF'),

    # 대형주 - Technology
    ('AMD', 'Advanced Micro Devices Inc', 'NASDAQ', 'Technology'),
    ('CRM', 'Salesforce Inc', 'NYSE', 'Technology'),
    ('AMZN', 'Amazon.com Inc', 'NASDAQ', 'Technology'),
    ('ADBE', 'Adobe Inc', 'NASDAQ', 'Technology'),
    
    # 대형주 - Consumer
    ('WMT', 'Walmart Inc', 'NYSE', 'Consumer Defensive'),
    ('PG', 'Procter & Gamble Co', 'NYSE', 'Consumer Defensive'),
    
    # 대형주 - Healthcare
    ('JNJ', 'Johnson & Johnson', 'NYSE', 'Healthcare'),
    ('UNH', 'UnitedHealth Group Inc', 'NYSE', 'Healthcare'),
    
    # 대형주 - Finance
    ('JPM', 'JPMorgan Chase & Co', 'NYSE', 'Financial Services'),
    ('V', 'Visa Inc', 'NYSE', 'Financial Services'),
    
    # 대형주 - Energy
    ('XOM', 'Exxon Mobil Corporation', 'NYSE', 'Energy'),

    # 대형주 - Industrials
    ('HON', 'Honeywell International Inc', 'NASDAQ', 'Industrials'),
    ('CAT', 'Caterpillar Inc', 'NYSE', 'Industrials'),
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
                # 이미 존재하는 경우 is_active만 업데이트
                if not existing.is_active:
                    existing.is_active = True
                    logger.info(f"{symbol} 활성화됨 ({name})")
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
        logger.info(f"종목 추가 완료:")
        logger.info(f"  - 신규 추가: {added_count}개")
        logger.info(f"  - 활성화: {updated_count}개")
        logger.info(f"  - 이미 활성: {skipped_count}개")
        logger.info(f"  - 총: {added_count + updated_count + skipped_count}개")
        logger.info("=" * 60)
        
        # 전체 활성 종목 확인
        stmt = select(StockTicker).where(StockTicker.is_active == True)
        active_tickers = session.execute(stmt).scalars().all()
        
        logger.info(f"\n현재 활성 종목 ({len(active_tickers)}개):")
        for ticker in sorted(active_tickers, key=lambda x: x.symbol):
            logger.info(f"  - {ticker.symbol}: {ticker.name} ({ticker.sector})")
        
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
    logger.info("1. 새 종목 백필: python scripts/backfill_ohlcv.py --days 90")
    logger.info("2. Docker 재시작: docker-compose restart")
    logger.info("3. 학습 재실행: 다음 일요일 자동 실행 또는 수동 트리거")
