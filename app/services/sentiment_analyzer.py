"""
감성 분석 서비스
Phase F.1: 뉴스 감성(Gemini API)과 Redis 캐싱 통합
"""
import json
import logging
import os
from datetime import datetime

import redis

# Gemini API import (using new google-genai SDK)
try:
    from google import genai
    GEMINI_AVAILABLE = True
except ImportError:
    GEMINI_AVAILABLE = False

from app.core.config import settings

logger = logging.getLogger(__name__)


class SentimentAnalyzer:
    """
    Gemini API와 Redis 캐싱을 이용한 감성 분석기입니다.

    감성 점수 범위: -1.0 (매우 부정) ~ +1.0 (매우 긍정)
    - [-1.0, -0.6]: 강력 매도
    - [-0.6, -0.3]: 약한 매도
    - [-0.3, +0.3]: 중립
    - [+0.3, +0.6]: 약한 매수
    - [+0.6, +1.0]: 강력 매수

    캐시 TTL: 1시간 (감성은 자주 변동)
    """

    def __init__(self, redis_client: redis.Redis | None = None):
        self.redis_client = redis_client or self._init_redis()
        self._init_gemini()
        self.cache_ttl = 3600  # 1 hour

    def _init_redis(self) -> redis.Redis | None:
        """Redis 연결 초기화 (shared pool 사용)"""
        try:
            from app.core.cache import get_shared_redis
            client = get_shared_redis()
            client.ping()
            logger.info("감성 캐싱용 Redis 연결됨")
            return client
        except Exception as e:
            logger.warning("Redis 연결 실패: %s. 캐시 사용 중지.", e)
            return None

    def _init_gemini(self):
        """Gemini API 초기화 (google-genai SDK)"""
        if not GEMINI_AVAILABLE:
            logger.warning("google-genai 미설치: 감성 분석 비활성화")
            self.gemini_client = None
            return

        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            logger.warning("GEMINI_API_KEY 미설정: 감성 분석 비활성화")
            self.gemini_client = None
            return

        try:
            self.gemini_client = genai.Client(api_key=api_key)
            logger.info("Gemini API 초기화됨 (감성 분석용)")
        except Exception as e:
            logger.error(f"Gemini API 초기화 실패: {e}")
            self.gemini_client = None

    def get_cache_key(self, symbol: str) -> str:
        """Generate Redis cache key"""
        return f"sentiment:{symbol}"

    def get_cached_sentiment(self, symbol: str) -> float | None:
        """Retrieve cached sentiment score"""
        if not self.redis_client:
            return None

        try:
            cached = self.redis_client.get(self.get_cache_key(symbol))
            if cached:
                data = json.loads(cached)
                logger.debug(f"캐시 HIT {symbol}: {data['score']}")
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
            logger.debug(f"캐시 저장 {symbol}: {score}")
        except Exception as e:
            logger.warning(f"캐시 저장 실패 {symbol}: {e}")

    def analyze_news(self, symbol: str, news_text: str) -> float:
        """
        Analyze news text using Gemini API (google-genai SDK).
        
        Args:
            symbol: Stock symbol (e.g., 'AAPL')
            news_text: News articles or headlines (aggregated)
        
        Returns:
            Sentiment score from -1.0 to +1.0
        """
        if not self.gemini_client:
            logger.warning("Gemini client not available. Returning neutral sentiment.")
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
            # Use google-genai client API
            response = self.gemini_client.models.generate_content(
                model='gemini-2.5-flash-lite',
                contents=prompt
            )
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

            logger.info(f"감성 점수 {symbol}: {score} | 사유: {result.get('reasoning', 'N/A')}")
            return score

        except Exception as e:
            logger.error(f"Gemini API 호출 실패 {symbol}: {e}")
            return 0.0

    def get_sentiment_score(self, symbol: str, news_text: str | None = None, force_refresh: bool = False) -> float:
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
            logger.warning(f"{symbol}에 대한 뉴스 텍스트 없음 — 중립(0.0) 반환")
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
        if current_regime == "bull_trending":
            if raw_score > 0:
                adjusted = raw_score * 1.3
            else:
                adjusted = raw_score * 0.7
        elif current_regime == "bear_trending":
            if raw_score < 0:
                adjusted = raw_score * 1.3
            else:
                adjusted = raw_score * 0.7
        else:  # sideways_volatile or sideways_calm
            adjusted = raw_score

        # Clamp to valid range
        adjusted = max(-1.0, min(1.0, adjusted))

        logger.debug(f"{symbol} 감성: raw={raw_score:.2f}, regime={current_regime}, adjusted={adjusted:.2f}")
        return adjusted

    def analyze_news_batch(self, news_data: dict[str, str]) -> dict[str, float]:
        """
        Analyze news for MULTIPLE symbols in ONE Gemini API call.
        
        Args:
            news_data: Dictionary of {symbol: news_text}
        
        Returns:
            Dictionary of {symbol: sentiment_score}
        """
        if not self.gemini_client:
            logger.warning("Gemini client not available. Returning neutral sentiment for all.")
            return {symbol: 0.0 for symbol in news_data}

        if not news_data:
            return {}

        # Construct batch prompt
        batch_prompt = """You are a financial sentiment analyst. Analyze news for multiple stocks.

For each stock, provide a sentiment score from -1.0 (extremely negative) to +1.0 (extremely positive).

Stocks and news:
"""

        for symbol, news in news_data.items():
            # Limit news to 500 chars per symbol to avoid token overflow
            truncated_news = news[:500] if len(news) > 500 else news
            batch_prompt += f"\n[{symbol}]\n{truncated_news}\n"

        batch_prompt += """
Response format (JSON only, no markdown):
{
    "AAPL": {"score": 0.75, "reasoning": "brief reason"},
    "MSFT": {"score": 0.45, "reasoning": "brief reason"},
    ...
}
"""

        try:
            response = self.gemini_client.models.generate_content(
                model='gemini-2.5-flash-lite',  # Stable model with better quota
                contents=batch_prompt
            )
            result_text = response.text.strip()

            # Parse JSON response
            if result_text.startswith("```json"):
                result_text = result_text.replace("```json", "").replace("```", "").strip()
            elif result_text.startswith("```"):
                result_text = result_text.replace("```", "").strip()

            results = json.loads(result_text)

            # Extract scores
            scores = {}
            for symbol in news_data:
                if symbol in results:
                    score = float(results[symbol].get('score', 0.0))
                    score = max(-1.0, min(1.0, score))  # Clamp
                    scores[symbol] = score
                    logger.info(f"배치 감성 {symbol}: {score} | {results[symbol].get('reasoning', 'N/A')}")
                else:
                    logger.warning(f"{symbol} not in batch response, using neutral")
                    scores[symbol] = 0.0

            logger.info(f"배치 감성 분석 완료: {len(scores)}개 심볼, 1회 API 호출")
            return scores

        except Exception as e:
            logger.error(f"배치 Gemini API 호출 실패: {e}")
            # Fallback to neutral for all
            return {symbol: 0.0 for symbol in news_data}


# Singleton instance (optional)
_sentiment_analyzer_instance = None

def get_sentiment_analyzer() -> SentimentAnalyzer:
    """Get singleton SentimentAnalyzer instance"""
    global _sentiment_analyzer_instance
    if _sentiment_analyzer_instance is None:
        _sentiment_analyzer_instance = SentimentAnalyzer()
    return _sentiment_analyzer_instance
