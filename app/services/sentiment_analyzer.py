"""
Sentiment Analysis Service
Phase F.1: Integrate news sentiment with Gemini API and Redis caching
"""
import os
import logging
from typing import Dict, Optional, List
from datetime import datetime, timedelta
import redis
import json

# Gemini API import (fallback to openai-compatible interface)
try:
    import google.generativeai as genai
    GEMINI_AVAILABLE = True
except ImportError:
    GEMINI_AVAILABLE = False

from app.core.config import settings

logger = logging.getLogger(__name__)


class SentimentAnalyzer:
    """
    Sentiment analysis using Gemini API with Redis caching.
    
    Sentiment scores range from -1.0 (극도 부정) to +1.0 (극도 긍정)
    - [-1.0, -0.6]: 강력 매도 신호
    - [-0.6, -0.3]: 약한 매도 신호
    - [-0.3, +0.3]: 중립
    - [+0.3, +0.6]: 약한 매수 신호
    - [+0.6, +1.0]: 강력 매수 신호
    
    Cache TTL: 1 hour (sentiment changes frequently)
    """
    
    def __init__(self, redis_client: Optional[redis.Redis] = None):
        self.redis_client = redis_client or self._init_redis()
        self._init_gemini()
        self.cache_ttl = 3600  # 1 hour
    
    def _init_redis(self) -> redis.Redis:
        """Initialize Redis connection"""
        try:
            client = redis.Redis(
                host=getattr(settings, 'REDIS_HOST', 'localhost'),
                port=getattr(settings, 'REDIS_PORT', 6379),
                db=getattr(settings, 'REDIS_DB', 0),
                decode_responses=True
            )
            client.ping()
            logger.info("Redis connected for sentiment caching")
            return client
        except Exception as e:
            logger.warning(f"Redis connection failed: {e}. Caching disabled.")
            return None
    
    def _init_gemini(self):
        """Initialize Gemini API"""
        if not GEMINI_AVAILABLE:
            logger.warning("google-generativeai not installed. Sentiment analysis disabled.")
            self.gemini_model = None
            return
        
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            logger.warning("GEMINI_API_KEY not set. Sentiment analysis disabled.")
            self.gemini_model = None
            return
        
        try:
            genai.configure(api_key=api_key)
            self.gemini_model = genai.GenerativeModel('gemini-pro')
            logger.info("Gemini API initialized for sentiment analysis")
        except Exception as e:
            logger.error(f"Gemini API initialization failed: {e}")
            self.gemini_model = None
    
    def get_cache_key(self, symbol: str) -> str:
        """Generate Redis cache key"""
        return f"sentiment:{symbol}"
    
    def get_cached_sentiment(self, symbol: str) -> Optional[float]:
        """Retrieve cached sentiment score"""
        if not self.redis_client:
            return None
        
        try:
            cached = self.redis_client.get(self.get_cache_key(symbol))
            if cached:
                data = json.loads(cached)
                logger.debug(f"Cache HIT for {symbol}: {data['score']}")
                return data['score']
        except Exception as e:
            logger.warning(f"Cache retrieval failed for {symbol}: {e}")
        
        return None
    
    def cache_sentiment(self, symbol: str, score: float):
        """Store sentiment score in cache"""
        if not self.redis_client:
            return
        
        try:
            data = {
                'score': score,
                'timestamp': datetime.now().isoformat(),
                'symbol': symbol
            }
            self.redis_client.setex(
                self.get_cache_key(symbol),
                self.cache_ttl,
                json.dumps(data)
            )
            logger.debug(f"Cached sentiment for {symbol}: {score}")
        except Exception as e:
            logger.warning(f"Cache storage failed for {symbol}: {e}")
    
    def analyze_news(self, symbol: str, news_text: str) -> float:
        """
        Analyze news text using Gemini API.
        
        Args:
            symbol: Stock symbol (e.g., 'AAPL')
            news_text: News articles or headlines (aggregated)
        
        Returns:
            Sentiment score from -1.0 to +1.0
        """
        if not self.gemini_model:
            logger.warning("Gemini model not available. Returning neutral sentiment.")
            return 0.0
        
        # Construct prompt for Gemini
        prompt = f"""
You are a financial sentiment analyst. Analyze the following news about {symbol} stock.

News:
{news_text}

Task:
1. Determine the overall sentiment (positive, negative, or neutral)
2. Provide a numerical score from -1.0 (extremely negative) to +1.0 (extremely positive)
3. Consider financial impact, market reaction potential, and business fundamentals

Response format (JSON only):
{{"score": <float between -1.0 and 1.0>, "reasoning": "<brief explanation>"}}
"""
        
        try:
            response = self.gemini_model.generate_content(prompt)
            result_text = response.text.strip()
            
            # Parse JSON response
            # Sometimes Gemini wraps JSON in markdown code blocks
            if result_text.startswith("```json"):
                result_text = result_text.replace("```json", "").replace("```", "").strip()
            elif result_text.startswith("```"):
                result_text = result_text.replace("```", "").strip()
            
            result = json.loads(result_text)
            score = float(result.get('score', 0.0))
            
            # Clamp score to valid range
            score = max(-1.0, min(1.0, score))
            
            logger.info(f"Sentiment for {symbol}: {score} | Reasoning: {result.get('reasoning', 'N/A')}")
            return score
        
        except Exception as e:
            logger.error(f"Gemini API call failed for {symbol}: {e}")
            return 0.0
    
    def get_sentiment_score(self, symbol: str, news_text: Optional[str] = None, force_refresh: bool = False) -> float:
        """
        Main entry point for sentiment analysis with caching.
        
        Args:
            symbol: Stock symbol
            news_text: News text to analyze (if None, fetch from API or use cached)
            force_refresh: Bypass cache and force new analysis
        
        Returns:
            Sentiment score from -1.0 to +1.0
        """
        # Check cache first
        if not force_refresh:
            cached_score = self.get_cached_sentiment(symbol)
            if cached_score is not None:
                return cached_score
        
        # If no news text provided, cannot analyze
        if not news_text:
            logger.warning(f"No news text provided for {symbol}. Returning neutral sentiment.")
            return 0.0
        
        # Analyze and cache
        score = self.analyze_news(symbol, news_text)
        self.cache_sentiment(symbol, score)
        
        return score
    
    def get_regime_weighted_sentiment(
        self, 
        symbol: str, 
        raw_score: float, 
        current_regime: str
    ) -> float:
        """
        Apply regime-specific weighting to sentiment scores.
        
        In BULL_TRENDING: Amplify positive sentiment (1.3x), dampen negative (0.7x)
        In BEAR: Amplify negative sentiment (1.3x), dampen positive (0.7x)
        In SIDEWAYS: Use raw sentiment as-is
        
        Args:
            symbol: Stock symbol
            raw_score: Raw sentiment score from Gemini
            current_regime: Current market regime (BULL_TRENDING, BEAR, SIDEWAYS_VOLATILE, SIDEWAYS_CALM)
        
        Returns:
            Regime-adjusted sentiment score
        """
        if current_regime == "BULL_TRENDING":
            if raw_score > 0:
                adjusted = raw_score * 1.3
            else:
                adjusted = raw_score * 0.7
        elif current_regime == "BEAR":
            if raw_score < 0:
                adjusted = raw_score * 1.3
            else:
                adjusted = raw_score * 0.7
        else:  # SIDEWAYS_VOLATILE or SIDEWAYS_CALM
            adjusted = raw_score
        
        # Clamp to valid range
        adjusted = max(-1.0, min(1.0, adjusted))
        
        logger.debug(f"{symbol} sentiment: raw={raw_score:.2f}, regime={current_regime}, adjusted={adjusted:.2f}")
        return adjusted
    
    def get_batch_sentiment(self, symbols: List[str], news_data: Dict[str, str]) -> Dict[str, float]:
        """
        Get sentiment scores for multiple symbols efficiently.
        
        Args:
            symbols: List of stock symbols
            news_data: Dict mapping symbol -> news text
        
        Returns:
            Dict mapping symbol -> sentiment score
        """
        results = {}
        
        for symbol in symbols:
            news_text = news_data.get(symbol)
            score = self.get_sentiment_score(symbol, news_text)
            results[symbol] = score
        
        return results


# Singleton instance (optional)
_sentiment_analyzer_instance = None

def get_sentiment_analyzer() -> SentimentAnalyzer:
    """Get singleton SentimentAnalyzer instance"""
    global _sentiment_analyzer_instance
    if _sentiment_analyzer_instance is None:
        _sentiment_analyzer_instance = SentimentAnalyzer()
    return _sentiment_analyzer_instance
