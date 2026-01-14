"""Distributed Lock using Redis.

This module provides a synchronous Redis-based distributed lock for preventing
race conditions in trading operations. Uses SET NX (Not eXists) with TTL.

Why Synchronous Redis (not async)?
- Celery workers run synchronously (--pool=solo)
- Trading logic in trading_strategy_sync.py is synchronous
- Sync redis-py client is simpler and sufficient for this use case
- < 1ms response time is adequate for locking operations

Why NOT Pub/Sub?
- Pub/Sub is for broadcasting messages to multiple subscribers
- Trading needs mutual exclusion (locking), not message distribution
- SET NX EX pattern provides atomic lock acquire/release

Usage:
    from app.core.distributed_lock import DistributedLock

    # Context manager (recommended)
    with DistributedLock(f"trading:{symbol}", ttl_seconds=30) as lock:
        if lock.acquired:
            # Critical section - only one worker can be here
            execute_trade(symbol)
        else:
            logger.warning("Could not acquire lock, skipping trade")

    # Manual acquire/release
    lock = DistributedLock(f"trading:{symbol}")
    if lock.acquire():
        try:
            execute_trade(symbol)
        finally:
            lock.release()
"""
import logging
import time
from typing import Optional
from redis import Redis
from app.core.config import settings

logger = logging.getLogger(__name__)


class DistributedLock:
    """
    Redis-based distributed lock with automatic TTL expiration.
    
    Attributes:
        key: Lock key in Redis (e.g., "trading:AAPL:lock")
        ttl_seconds: Time-to-live for the lock (auto-release)
        acquired: Whether the lock was successfully acquired
    
    Thread Safety:
        This lock is designed for distributed systems (multiple containers).
        For same-process threading, use threading.Lock instead.
    """
    
    # Prefix for all lock keys
    LOCK_PREFIX = "lock:"
    
    def __init__(
        self,
        key: str,
        ttl_seconds: int = 30,
        retry_times: int = 3,
        retry_delay: float = 0.1
    ):
        """
        Initialize distributed lock.
        
        Args:
            key: Unique identifier for the resource being locked
            ttl_seconds: Lock expiration time (prevents deadlocks)
            retry_times: Number of acquire attempts before giving up
            retry_delay: Delay between retry attempts (seconds)
        """
        self.key = f"{self.LOCK_PREFIX}{key}"
        self.ttl_seconds = ttl_seconds
        self.retry_times = retry_times
        self.retry_delay = retry_delay
        self.acquired = False
        self._lock_value: Optional[str] = None
        
        # Initialize Redis connection
        # Using sync redis-py (not aioredis) because Celery workers are sync
        self._redis: Optional[Redis] = None
        self._connect()
    
    def _connect(self) -> None:
        """Establish Redis connection."""
        try:
            redis_url = str(settings.REDIS_URL)
            self._redis = Redis.from_url(redis_url, decode_responses=True)
            # Test connection
            self._redis.ping()
        except Exception as e:
            logger.error("Redis connection failed: %s", str(e))
            self._redis = None
    
    def acquire(self) -> bool:
        """
        Attempt to acquire the lock.
        
        Returns:
            True if lock was acquired, False otherwise.
            
        Notes:
            Uses SET NX (Not eXists) with EX (Expire) for atomic operation.
            This prevents race conditions during lock acquisition.
        """
        if self._redis is None:
            logger.warning("Redis not available, proceeding without lock")
            return True  # Fail-open: allow operation if Redis is down
        
        # Generate unique lock value (for safe release)
        import uuid
        self._lock_value = str(uuid.uuid4())
        
        for attempt in range(self.retry_times):
            try:
                # SET key value NX EX ttl
                # NX: Only set if key does not exist
                # EX: Set expiration in seconds
                result = self._redis.set(
                    self.key,
                    self._lock_value,
                    nx=True,
                    ex=self.ttl_seconds
                )
                
                if result:
                    self.acquired = True
                    logger.debug("Lock acquired: %s (attempt %d)", self.key, attempt + 1)
                    return True
                
                # Lock held by another process, retry
                if attempt < self.retry_times - 1:
                    time.sleep(self.retry_delay)
                    
            except Exception as e:
                logger.error("Lock acquire error: %s", str(e))
                break
        
        logger.warning("Failed to acquire lock: %s after %d attempts", self.key, self.retry_times)
        return False
    
    def release(self) -> bool:
        """
        Release the lock if we own it.
        
        Returns:
            True if lock was released, False otherwise.
            
        Notes:
            Only releases if the lock value matches (prevents releasing
            a lock that was acquired by another process after TTL expiry).
        """
        if self._redis is None or not self.acquired:
            return False
        
        try:
            # Lua script for atomic check-and-delete
            # Ensures we only delete if we own the lock
            lua_script = """
            if redis.call("get", KEYS[1]) == ARGV[1] then
                return redis.call("del", KEYS[1])
            else
                return 0
            end
            """
            result = self._redis.eval(lua_script, 1, self.key, self._lock_value)
            
            if result:
                logger.debug("Lock released: %s", self.key)
                self.acquired = False
                return True
            else:
                logger.warning("Lock release failed (not owner): %s", self.key)
                return False
                
        except Exception as e:
            logger.error("Lock release error: %s", str(e))
            return False
    
    def __enter__(self) -> "DistributedLock":
        """Context manager entry."""
        self.acquire()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context manager exit - always release lock."""
        if self.acquired:
            self.release()


def get_trading_lock(symbol: str, ttl_seconds: int = 30) -> DistributedLock:
    """
    Factory function for trading operation locks.
    
    Args:
        symbol: Stock symbol (e.g., "AAPL")
        ttl_seconds: Lock expiration time
        
    Returns:
        DistributedLock instance for the symbol
        
    Usage:
        with get_trading_lock("AAPL") as lock:
            if lock.acquired:
                execute_trade("AAPL")
    """
    return DistributedLock(f"trading:{symbol}", ttl_seconds=ttl_seconds)
