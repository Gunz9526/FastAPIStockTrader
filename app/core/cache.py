import json
import logging
from typing import Any

import redis

from app.core.config import settings

logger = logging.getLogger(__name__)

class CacheService:
    """
    Redis-based caching service for performance optimization.
    """

    def __init__(self):
        try:
            # Convert Pydantic RedisDsn to string
            redis_url = str(settings.REDIS_URL) if settings.REDIS_URL else "redis://redis:6379/0"

            self.redis_client = redis.from_url(
                redis_url,
                decode_responses=True,
                socket_connect_timeout=5
            )
            # Test connection
            self.redis_client.ping()
            logger.info("Redis cache connected successfully")
            self.enabled = True
        except Exception as e:
            logger.warning(f"Redis cache disabled: {e}")
            self.enabled = False
            self.redis_client = None

    def _make_key(self, prefix: str, *args) -> str:
        """Generate cache key."""
        return f"{prefix}:" + ":".join(str(arg) for arg in args)

    def get(self, key: str) -> Any | None:
        """Get value from cache."""
        if not self.enabled:
            return None

        try:
            value = self.redis_client.get(key)
            if value:
                return json.loads(value)
            return None
        except Exception as e:
            logger.error(f"Cache get error: {e}")
            return None

    def set(self, key: str, value: Any, ttl_seconds: int = 3600):
        """Set value in cache with TTL."""
        if not self.enabled:
            return

        try:
            self.redis_client.setex(
                key,
                ttl_seconds,
                json.dumps(value, default=str)
            )
        except Exception as e:
            logger.error(f"Cache set error: {e}")

    def delete(self, key: str):
        """Delete key from cache."""
        if not self.enabled:
            return

        try:
            self.redis_client.delete(key)
        except Exception as e:
            logger.error(f"Cache delete error: {e}")

    def clear_pattern(self, pattern: str):
        """Delete all keys matching pattern."""
        if not self.enabled:
            return

        try:
            keys = self.redis_client.keys(pattern)
            if keys:
                self.redis_client.delete(*keys)
                logger.info(f"Cleared {len(keys)} keys matching {pattern}")
        except Exception as e:
            logger.error(f"Cache clear error: {e}")

    # Specific cache methods

    def get_ohlcv(self, symbol: str, days: int) -> list[dict] | None:
        """Get cached OHLCV data."""
        key = self._make_key("ohlcv", symbol, days)
        return self.get(key)

    def set_ohlcv(self, symbol: str, days: int, data: list[dict], ttl: int = 3600):
        """Cache OHLCV data (1 hour TTL)."""
        key = self._make_key("ohlcv", symbol, days)
        self.set(key, data, ttl)

    def get_account_info(self) -> dict | None:
        """Get cached account info."""
        key = "account:info"
        return self.get(key)

    def set_account_info(self, data: dict, ttl: int = 30):
        """Cache account info (30 seconds TTL)."""
        key = "account:info"
        self.set(key, data, ttl)

    def get_position(self, symbol: str) -> dict | None:
        """Get cached position."""
        key = self._make_key("position", symbol)
        return self.get(key)

    def set_position(self, symbol: str, data: dict, ttl: int = 60):
        """Cache position (1 minute TTL)."""
        key = self._make_key("position", symbol)
        self.set(key, data, ttl)

    def invalidate_symbol(self, symbol: str):
        """Invalidate all cache for a symbol."""
        self.clear_pattern(f"*:{symbol}:*")
        self.clear_pattern(f"position:{symbol}")

# Global cache instance
cache = CacheService()
