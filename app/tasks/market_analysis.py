import logging
from datetime import timedelta

import pandas as pd

from app.core.database import SessionLocal
from app.repositories.stock_repo_sync import SyncStockRepository
from app.services.momentum_scorer import CrossSectionalMomentum
from app.worker import celery_app

logger = logging.getLogger(__name__)

@celery_app.task(name="app.tasks.market_analysis.analyze_market")
def analyze_market():
    """
    시장 전체 분석 태스크 (동기 버전).
    활성 심볼 전체에 대한 집계 지표를 계산합니다.
    """
    logger.info("=" * 60)
    logger.info("시장 분석 시작")
    logger.info("=" * 60)

    session = SessionLocal()
    try:
        repo = SyncStockRepository(session)

        # Get active symbols
        symbols = repo.get_active_symbols()

        if not symbols:
            logger.warning("분석할 활성 심볼이 없습니다")
            return

        logger.info(f"{len(symbols)}개 심볼 분석 중")

        # Time range: last 30 days
        end_date = pd.Timestamp.now(tz='UTC')
        start_date = end_date - timedelta(days=30)

        analysis_results = {
            'total_symbols': len(symbols),
            'analyzed_symbols': 0,
            'high_momentum': [],
            'low_volatility': [],
            'high_volume': [],
            'avg_return_pct': 0.0,
            'avg_volatility': 0.0
        }

        returns = []
        volatilities = []

        for symbol in symbols:
            try:
                # Get recent daily OHLCV data
                ohlcv = repo.get_ohlcv_range(symbol, start_date, end_date, timeframe='1d')

                if len(ohlcv) < 20:  # 의미 있는 분석을 위해 최소 20 거래일 필요
                    logger.debug(f"{symbol}: 데이터 부족")
                    continue

                # Convert to DataFrame
                df = pd.DataFrame([{
                    'date_time': bar.date_time,
                    'close': bar.close,
                    'volume': bar.volume
                } for bar in ohlcv])
                df.set_index('date_time', inplace=True)
                df.sort_index(inplace=True)

                # Calculate metrics
                daily_returns = df['close'].pct_change().dropna()
                total_return = (df['close'].iloc[-1] / df['close'].iloc[0] - 1) * 100
                volatility = daily_returns.std() * (252 ** 0.5)  # Annualized
                avg_volume = df['volume'].mean()

                returns.append(total_return)
                volatilities.append(volatility)

                # Classify symbols (daily bars)
                # Daily bars: ~22 bars/month * 1 month = ~22 bars
                if total_return > 2:  # >2% gain in 30 days
                    analysis_results['high_momentum'].append(symbol)

                if volatility < 0.15:  # <15% annual volatility
                    analysis_results['low_volatility'].append(symbol)

                if avg_volume > 1_000_000:  # Daily volume threshold
                    analysis_results['high_volume'].append(symbol)

                analysis_results['analyzed_symbols'] += 1

            except Exception as e:
                logger.error(f"Failed to analyze {symbol}: {e}")
                continue

        # Aggregate statistics
        if returns:
            analysis_results['avg_return_pct'] = round(sum(returns) / len(returns), 2)
        if volatilities:
            analysis_results['avg_volatility'] = round(sum(volatilities) / len(volatilities), 2)

        # Log results
        logger.info("=" * 60)
        logger.info("시장 분석 결과")
        logger.info("=" * 60)
        logger.info(f"분석 완료: {analysis_results['analyzed_symbols']}/{analysis_results['total_symbols']}")
        logger.info(f"평균 수익률 (30일): {analysis_results['avg_return_pct']}%")
        logger.info(f"평균 변동성 (연율): {analysis_results['avg_volatility']}")
        logger.info(f"상승 모멘텀 심볼 수: {len(analysis_results['high_momentum'])}개")
        logger.info(f"저변동성 심볼 수: {len(analysis_results['low_volatility'])}개")
        logger.info(f"고거래량 심볼 수: {len(analysis_results['high_volume'])}개")

        if analysis_results['high_momentum']:
            logger.info(f"상위 상승 종목: {', '.join(analysis_results['high_momentum'][:5])}")

        session.commit()
        logger.info("=" * 60)
        logger.info("시장 분석 완료")
        logger.info("=" * 60)

        return analysis_results

    except Exception as e:
        logger.error(f"시장 분석 오류: {e}", exc_info=True)
        session.rollback()
        raise
    finally:
        session.close()


@celery_app.task(
    name="app.tasks.market_analysis.compute_momentum_scores",
    bind=True,
    max_retries=2,
    default_retry_delay=60,
)
def compute_momentum_scores(self):
    """Compute cross-sectional momentum rankings for all active symbols.

    Phase M.1 — Runs post-market after OHLCV collection.
    Stores results in Redis (momentum:scores:{date}, momentum:sectors:{date}).

    Returns:
        dict with ``scored_count`` and ``status``.
    """
    logger.info("=" * 60)
    logger.info("Cross-Sectional Momentum 스코어링 시작")
    logger.info("=" * 60)

    try:
        scorer = CrossSectionalMomentum()
        count = scorer.compute_and_cache()

        logger.info("=" * 60)
        logger.info("Momentum 스코어링 완료: %d개 심볼", count)
        logger.info("=" * 60)

        return {"scored_count": count, "status": "success"}

    except Exception as exc:
        logger.error("Momentum 스코어링 오류: %s", exc, exc_info=True)
        raise self.retry(exc=exc)
