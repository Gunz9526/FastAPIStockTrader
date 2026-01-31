import logging
from datetime import datetime

import yfinance as yf

from app.core.database import SessionLocal
from app.worker import celery_app

logger = logging.getLogger(__name__)

@celery_app.task(name="app.tasks.data_tasks.collect_fundamentals")
def collect_fundamentals():
    """
    yfinance에서 기본 재무데이터 수집 (동기 버전).
    `stock_fundamentals` 테이블을 업데이트합니다.
    """
    logger.info("재무지표 수집 시작 (동기)")

    session = SessionLocal()
    try:
        from sqlalchemy import select

        from app.domain.models.stock import StockFundamentals, StockTicker

        # Get active symbols
        stmt = select(StockTicker.symbol).where(StockTicker.is_active == True)
        result = session.execute(stmt)
        symbols = [row[0] for row in result]

        if not symbols:
            logger.warning("수집할 심볼이 없습니다")
            return

        logger.info(f"총 {len(symbols)}개 심볼의 재무지표를 수집합니다")

        success_count = 0
        error_count = 0

        for symbol in symbols:
            try:
                # Fetch from yfinance
                ticker = yf.Ticker(symbol)
                info = ticker.info

                if not info:
                    logger.warning(f"No info for {symbol}")
                    continue

                # Extract key metrics matching StockFundamentals model
                fundamentals_data = {
                    'symbol': symbol,
                    'market_cap': info.get('marketCap'),
                    'per': info.get('trailingPE'),
                    'pbr': info.get('priceToBook'),
                    'roe': info.get('returnOnEquity'),
                    'sector': info.get('sector'),
                    'date': datetime.utcnow()
                }

                # Upsert (update or insert)
                existing = session.execute(
                    select(StockFundamentals).where(StockFundamentals.symbol == symbol)
                ).scalar_one_or_none()

                if existing:
                    # Update
                    for key, value in fundamentals_data.items():
                        if key != 'symbol':
                            setattr(existing, key, value)
                else:
                    # Insert
                    new_fundamental = StockFundamentals(**fundamentals_data)
                    session.add(new_fundamental)

                success_count += 1
                logger.debug(f"{symbol} 재무지표 업데이트됨")

            except Exception as e:
                error_count += 1
                logger.error(f"{symbol} 처리 실패: {e}")
                continue

        session.commit()
        logger.info(f"재무지표 수집 완료: 성공 {success_count}건, 오류 {error_count}건")

        return {
            'success': success_count,
            'errors': error_count,
            'total': len(symbols)
        }

    except Exception as e:
        logger.error(f"재무지표 수집 오류: {e}", exc_info=True)
        session.rollback()
        raise
    finally:
        session.close()
