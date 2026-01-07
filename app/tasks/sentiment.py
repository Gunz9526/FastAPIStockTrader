"""
Sentiment Analysis Celery Tasks
Phase F.1: Automated sentiment analysis updates
"""

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
    Update sentiment scores for active symbols.
    
    Schedule: Hourly (00:00, 01:00, ..., 23:00)
    
    Args:
        symbol: Specific symbol to update (if None, update all active symbols)
    
    Notes:
    - Fetches latest news from external API (e.g., NewsAPI, Alpha Vantage)
    - Analyzes sentiment using Gemini API
    - Caches results in Redis with 1-hour TTL
    - Used by trading strategy for signal adjustment
    """
    logger.info(f"Starting sentiment update task (symbol={symbol})")
    
    try:
        # Get active symbols
        with get_sync_session() as db:
            repo = SyncStockRepository(db)
            
            if symbol:
                symbols = [symbol]
            else:
                symbols = repo.get_active_symbols()
            
            logger.info(f"Updating sentiment for {len(symbols)} symbols")
        
        # Get sentiment analyzer
        analyzer = get_sentiment_analyzer()
        
        # Update each symbol
        updated_count = 0
        for sym in symbols:
            try:
                # TODO: Fetch news from external API
                news_text = _fetch_news_for_symbol(sym)
                
                if news_text:
                    # Analyze and cache sentiment
                    score = analyzer.get_sentiment_score(sym, news_text, force_refresh=True)
                    logger.info(f"Updated sentiment for {sym}: {score:.3f}")
                    updated_count += 1
                else:
                    logger.debug(f"No news found for {sym}")
            
            except Exception as e:
                logger.error(f"Failed to update sentiment for {sym}: {e}")
                continue
        
        logger.info(f"Sentiment update completed: {updated_count}/{len(symbols)} symbols updated")
        return {
            'status': 'success',
            'updated_count': updated_count,
            'total_symbols': len(symbols),
            'timestamp': datetime.now().isoformat()
        }
    
    except Exception as e:
        logger.error(f"Sentiment update task failed: {e}", exc_info=True)
        # Retry with exponential backoff
        raise self.retry(exc=e, countdown=60 * (2 ** self.request.retries))


def _fetch_news_for_symbol(symbol: str) -> str:
    """
    Fetch latest news for a symbol from Finnhub using official Python client.
    
    Integration: Finnhub Python Client (https://github.com/finnhub-stock-api/finnhub-python)
    - Free tier: 60 API calls/minute (sufficient for hourly updates)
    - Production: $59/month (Professional plan)
    - Quality: Premium financial news (Reuters, Bloomberg, WSJ, etc.)
    
    Args:
        symbol: Stock symbol (e.g., 'AAPL', 'MSFT')
    
    Returns:
        Aggregated news text (headlines + summaries) or empty string
    """
    import os
    import finnhub
    from datetime import datetime, timedelta
    
    # Check for Finnhub API key
    api_key = os.getenv("FINNHUB_API_KEY")
    
    if not api_key:
        logger.warning(f"⚠️ FINNHUB_API_KEY not set. Skipping news fetch for {symbol}")
        return ""
    
    try:
        # Initialize Finnhub client
        finnhub_client = finnhub.Client(api_key=api_key)
        
        # Time range: last 24 hours
        today = datetime.now()
        yesterday = today - timedelta(days=1)
        
        # Fetch company news using official client
        articles = finnhub_client.company_news(
            symbol=symbol,
            _from=yesterday.strftime('%Y-%m-%d'),
            to=today.strftime('%Y-%m-%d')
        )
        
        # Filter out empty or invalid articles
        if not articles or not isinstance(articles, list):
            logger.debug(f"No news found for {symbol}")
            return ""
        
        # Limit to top 10 articles (sorted by datetime)
        articles = sorted(articles, key=lambda x: x.get('datetime', 0), reverse=True)[:10]
        
        if not articles:
            logger.debug(f"No valid news articles for {symbol}")
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
            logger.debug(f"No meaningful content in news for {symbol}")
            return ""
        
        sources = set([a.get('source', 'Unknown') for a in articles])
        logger.info(f"✅ Fetched {len(articles)} Finnhub articles for {symbol} (sources: {sources})")
        return news_text.strip()
    
    except finnhub.FinnhubAPIException as e:
        logger.error(f"Finnhub API error for {symbol}: {e}")
        return ""
    
    except Exception as e:
        logger.error(f"Unexpected error fetching Finnhub news for {symbol}: {e}")
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
    logger.info("Starting stale sentiment cache cleanup")
    
    try:
        analyzer = get_sentiment_analyzer()
        
        if not analyzer.redis_client:
            logger.warning("Redis not available. Skipping cache cleanup.")
            return {'status': 'skipped', 'reason': 'Redis not available'}
        
        # Get all sentiment keys
        keys = analyzer.redis_client.keys("sentiment:*")
        
        if not keys:
            logger.info("No sentiment cache entries found")
            return {'status': 'success', 'deleted_count': 0}
        
        # Delete expired keys (TTL-based cleanup is automatic, but we can force it)
        deleted_count = 0
        for key in keys:
            ttl = analyzer.redis_client.ttl(key)
            if ttl == -1:  # No expiration set (shouldn't happen)
                analyzer.redis_client.delete(key)
                deleted_count += 1
        
        logger.info(f"Sentiment cache cleanup completed: {deleted_count} keys deleted")
        return {
            'status': 'success',
            'deleted_count': deleted_count,
            'total_keys': len(keys)
        }
    
    except Exception as e:
        logger.error(f"Sentiment cache cleanup failed: {e}", exc_info=True)
        raise
