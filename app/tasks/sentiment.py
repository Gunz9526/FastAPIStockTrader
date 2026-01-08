from app.worker import celery_app
from celery.utils.log import get_task_logger
from app.core.database import get_sync_session
from app.repositories.stock_repo_sync import SyncStockRepository
from app.services.sentiment_analyzer import get_sentiment_analyzer
from datetime import datetime, timedelta
import logging

logger = get_task_logger(__name__)


@celery_app.task(name="app.tasks.sentiment.update_sentiment_scores", bind=True, max_retries=3)
def update_sentiment_scores(self, symbol: str = None):
    """
    활성 심볼의 감성 점수를 업데이트합니다.

    스케줄: 매시 정각 실행

    Args:
        symbol: 특정 심볼만 업데이트할 경우 심볼 문자열 (None이면 모든 활성 심볼)

    Notes:
    - 외부 뉴스 API에서 최신 기사 수집
    - Gemini(또는 내부 분석기)를 사용해 감성 분석
    - 결과를 Redis에 1시간 TTL로 캐시
    - 트레이딩 전략의 신호 조정에 사용됨
    """
    logger.info(f"감성 업데이트 태스크 시작 (symbol={symbol})")
    
    try:
        # Get active symbols
        with get_sync_session() as db:
            repo = SyncStockRepository(db)
            
            if symbol:
                symbols = [symbol]
            else:
                symbols = repo.get_active_symbols()
            
            logger.info(f"감성 업데이트 대상 심볼 수: {len(symbols)}개")
        
        # Get sentiment analyzer
        analyzer = get_sentiment_analyzer()
        
        # Batch collection: Fetch news for all symbols first
        news_batch = {}
        for sym in symbols:
            try:
                news_text = _fetch_news_for_symbol(sym)
                if news_text:
                    news_batch[sym] = news_text
            except Exception as e:
                logger.error(f"{sym} 뉴스 수집 실패: {e}")
        
        if not news_batch:
            logger.warning("수집된 뉴스 없음")
            return {
                'status': 'warning',
                'message': 'No news collected',
                'updated_count': 0,
                'total_symbols': len(symbols)
            }
        
        # BATCH sentiment analysis (ONE Gemini API call for all symbols)
        logger.info(f"배치 감성 분석 시작: {len(news_batch)}개 심볼")
        sentiment_scores = analyzer.analyze_news_batch(news_batch)
        
        # Cache results
        updated_count = 0
        for sym, score in sentiment_scores.items():
            try:
                analyzer.cache_sentiment(sym, score)
                updated_count += 1
            except Exception as e:
                logger.error(f"{sym} 캐시 저장 실패: {e}")
        
        logger.info(f"감성 업데이트 완료: {updated_count}/{len(symbols)} 심볼, 1회 API 호출")
        return {
            'status': 'success',
            'updated_count': updated_count,
            'total_symbols': len(symbols),
            'api_calls': 1,  # Batch processing = 1 call
            'timestamp': datetime.now().isoformat()
        }
    
    except Exception as e:
        logger.error(f"감성 업데이트 태스크 실패: {e}", exc_info=True)
        # Retry with exponential backoff
        raise self.retry(exc=e, countdown=60 * (2 ** self.request.retries))


def _fetch_news_for_symbol(symbol: str) -> str:
    """
    Finnhub 공식 클라이언트를 사용해 심볼의 최신 뉴스를 가져옵니다.

    Integration: Finnhub Python Client

    Args:
        symbol: 주식 심볼 (예: 'AAPL', 'MSFT')

    Returns:
        헤드라인과 요약을 합친 문자열 또는 빈 문자열
    """
    import os
    import finnhub
    from datetime import datetime, timedelta
    
    # Check for Finnhub API key
    api_key = os.getenv("FINNHUB_API_KEY")
    
    if not api_key:
        logger.warning(f"FINNHUB_API_KEY가 설정되어 있지 않습니다. {symbol} 뉴스 수집 건너뜁니다.")
        return ""
    
    try:
        # Finnhub 클라이언트 초기화
        finnhub_client = finnhub.Client(api_key=api_key)
        
        # 기간: 최근 24시간
        today = datetime.now()
        yesterday = today - timedelta(days=1)
        
        # Fetch company news using official client
        articles = finnhub_client.company_news(
            symbol=symbol,
            _from=yesterday.strftime('%Y-%m-%d'),
            to=today.strftime('%Y-%m-%d')
        )
        
        # 비어있거나 잘못된 기사 필터링
        if not articles or not isinstance(articles, list):
            logger.debug(f"{symbol}: 뉴스 없음 또는 형식 오류")
            return ""
        
        # Limit to top 10 articles (sorted by datetime)
        articles = sorted(articles, key=lambda x: x.get('datetime', 0), reverse=True)[:10]
        
        if not articles:
            logger.debug(f"{symbol}: 유효한 기사 없음")
            return ""
        
        # Aggregate headlines and summaries
        news_texts = []
        for article in articles:
            headline = article.get('headline', '').strip()
            summary = article.get('summary', '').strip()
            
            # Combine headline and summary
            if headline:
                news_texts.append(headline)
            if summary:
                news_texts.append(summary)
        
        news_text = " ".join(news_texts)
        
        if not news_text:
            logger.debug(f"{symbol}: 의미 있는 뉴스 내용 없음")
            return ""
        
        sources = set([a.get('source', 'Unknown') for a in articles])
        logger.info(f"Finnhub에서 {symbol} 기사 {len(articles)}건을 가져왔습니다 (출처: {sources})")
        return news_text.strip()
    
    except finnhub.FinnhubAPIException as e:
        logger.error(f"Finnhub API 오류 ({symbol}): {e}")
        return ""
    
    except Exception as e:
        logger.error(f"{symbol} 뉴스 수집 중 예외 발생: {e}")
        return ""


@celery_app.task(name="app.tasks.sentiment.clear_stale_sentiment_cache", bind=True)
def clear_stale_sentiment_cache(self):
    """
    Clear stale sentiment cache entries.
    
    Schedule: Daily at 00:00
    
    Notes:
    - Redis TTL handles automatic expiration
    - This task is optional for manual cleanup
    """
    logger.info("오래된 감성 캐시 정리 시작")
    
    try:
        analyzer = get_sentiment_analyzer()
        
        if not analyzer.redis_client:
            logger.warning("Redis가 사용 불가합니다. 캐시 정리 건너뜁니다.")
            return {'status': 'skipped', 'reason': 'Redis not available'}
        
        # Get all sentiment keys
        keys = analyzer.redis_client.keys("sentiment:*")
        
        if not keys:
            logger.info("감성 캐시 항목이 없습니다")
            return {'status': 'success', 'deleted_count': 0}
        
        # Delete expired keys (TTL-based cleanup is automatic, but we can force it)
        deleted_count = 0
        for key in keys:
            ttl = analyzer.redis_client.ttl(key)
            if ttl == -1:  # No expiration set (shouldn't happen)
                analyzer.redis_client.delete(key)
                deleted_count += 1
        
        logger.info(f"감성 캐시 정리 완료: 삭제된 키 수 {deleted_count}")
        return {
            'status': 'success',
            'deleted_count': deleted_count,
            'total_keys': len(keys)
        }
    
    except Exception as e:
        logger.error(f"감성 캐시 정리 실패: {e}", exc_info=True)
        raise
