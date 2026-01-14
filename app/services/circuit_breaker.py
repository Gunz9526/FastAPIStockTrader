"""Circuit Breaker: 자동 트레이딩 중단 시스템.

이 모듈은 시스템 보호를 위한 Circuit Breaker 패턴을 구현합니다.

트레이딩 중단 조건:
1. 일일 손실 한도 초과 (기본: 3% 또는 $500)
2. API 레이턴시 초과 (기본: 3000ms)
3. 연속 실패 거래 횟수 초과 (기본: 5회)
4. VIX 극단치 감지 (기본: > 35)

상태:
- CLOSED: 정상 운영 (트레이딩 허용)
- OPEN: 차단됨 (트레이딩 금지)
- HALF_OPEN: 테스트 중 (제한된 트레이딩)

사용법:
    from app.services.circuit_breaker import CircuitBreaker, get_circuit_breaker
    
    breaker = get_circuit_breaker()
    
    # 트레이딩 전 확인
    if not breaker.can_trade():
        logger.warning("Circuit Breaker 활성화 - 트레이딩 중단")
        return
    
    # API 호출 래핑
    with breaker.track_api_call():
        response = alpaca_api.submit_order(...)
"""
import logging
import time
from enum import Enum
from datetime import datetime, date, timedelta
from typing import Optional, Dict
from contextlib import contextmanager
from threading import Lock

from app.core.cache import cache

logger = logging.getLogger(__name__)


class CircuitState(str, Enum):
    """Circuit Breaker 상태."""
    CLOSED = "closed"       # 정상 운영
    OPEN = "open"           # 차단됨
    HALF_OPEN = "half_open" # 테스트 중


class CircuitBreaker:
    """
    트레이딩 시스템용 Circuit Breaker.
    
    일일 손실, API 레이턴시, 연속 실패를 모니터링하고
    임계값 초과 시 자동으로 트레이딩을 중단합니다.
    """
    
    # Redis 키 접두사
    REDIS_PREFIX = "circuit_breaker:"
    
    def __init__(
        self,
        daily_loss_limit_pct: float = 0.03,        # 일일 손실 한도 (포트폴리오의 3%)
        daily_loss_limit_usd: float = 500.0,       # 일일 손실 한도 ($500)
        max_api_latency_ms: int = 3000,            # API 레이턴시 한도 (3초)
        max_consecutive_failures: int = 5,         # 연속 실패 한도
        vix_extreme_threshold: float = 35.0,       # VIX 극단치 임계값
        recovery_timeout_minutes: int = 30,        # 자동 복구 시간 (분)
        half_open_max_trades: int = 2              # HALF_OPEN 상태에서 허용 거래 수
    ):
        """
        Circuit Breaker 초기화.
        
        Args:
            daily_loss_limit_pct: 일일 손실 한도 (포트폴리오 비율)
            daily_loss_limit_usd: 일일 손실 한도 (USD)
            max_api_latency_ms: API 레이턴시 한도 (밀리초)
            max_consecutive_failures: 연속 실패 거래 한도
            vix_extreme_threshold: VIX 극단치 임계값
            recovery_timeout_minutes: OPEN -> HALF_OPEN 전환 시간
            half_open_max_trades: HALF_OPEN 상태 허용 거래 수
        """
        self.daily_loss_limit_pct = daily_loss_limit_pct
        self.daily_loss_limit_usd = daily_loss_limit_usd
        self.max_api_latency_ms = max_api_latency_ms
        self.max_consecutive_failures = max_consecutive_failures
        self.vix_extreme_threshold = vix_extreme_threshold
        self.recovery_timeout_minutes = recovery_timeout_minutes
        self.half_open_max_trades = half_open_max_trades
        
        # 인메모리 상태 (Redis 백업)
        self._state = CircuitState.CLOSED
        self._opened_at: Optional[datetime] = None
        self._consecutive_failures = 0
        self._daily_pnl: Dict[date, float] = {}
        self._api_latencies: list = []
        self._half_open_trade_count = 0
        
        # 스레드 안전성
        self._lock = Lock()
        
        # Redis에서 상태 복원
        self._restore_from_redis()
    
    def _restore_from_redis(self) -> None:
        """Redis에서 상태 복원."""
        try:
            state = cache.get(f"{self.REDIS_PREFIX}state")
            if state:
                self._state = CircuitState(state)
            
            opened_at = cache.get(f"{self.REDIS_PREFIX}opened_at")
            if opened_at:
                self._opened_at = datetime.fromisoformat(opened_at)
            
            failures = cache.get(f"{self.REDIS_PREFIX}consecutive_failures")
            if failures:
                self._consecutive_failures = int(failures)
                
            logger.info("Circuit Breaker 상태 복원: %s", self._state.value)
        except Exception as e:
            logger.warning("Circuit Breaker 상태 복원 실패: %s", str(e))
    
    def _save_to_redis(self) -> None:
        """상태를 Redis에 저장."""
        try:
            cache.set(f"{self.REDIS_PREFIX}state", self._state.value, ttl_seconds=86400)
            
            if self._opened_at:
                cache.set(
                    f"{self.REDIS_PREFIX}opened_at", 
                    self._opened_at.isoformat(), 
                    ttl_seconds=86400
                )
            
            cache.set(
                f"{self.REDIS_PREFIX}consecutive_failures", 
                str(self._consecutive_failures), 
                ttl_seconds=86400
            )
        except Exception as e:
            logger.warning("Circuit Breaker 상태 저장 실패: %s", str(e))
    
    @property
    def state(self) -> CircuitState:
        """현재 상태 반환 (자동 복구 확인 포함)."""
        with self._lock:
            # OPEN 상태에서 자동 복구 확인
            if self._state == CircuitState.OPEN and self._opened_at:
                elapsed = datetime.now() - self._opened_at
                if elapsed > timedelta(minutes=self.recovery_timeout_minutes):
                    self._transition_to_half_open()
            
            return self._state
    
    def _transition_to_open(self, reason: str) -> None:
        """OPEN 상태로 전환 (트레이딩 차단)."""
        self._state = CircuitState.OPEN
        self._opened_at = datetime.now()
        self._save_to_redis()
        logger.warning("Circuit Breaker OPEN: %s", reason)
    
    def _transition_to_half_open(self) -> None:
        """HALF_OPEN 상태로 전환 (제한된 테스트)."""
        self._state = CircuitState.HALF_OPEN
        self._half_open_trade_count = 0
        self._save_to_redis()
        logger.info("Circuit Breaker HALF_OPEN: 복구 테스트 시작")
    
    def _transition_to_closed(self) -> None:
        """CLOSED 상태로 전환 (정상 운영)."""
        self._state = CircuitState.CLOSED
        self._opened_at = None
        self._consecutive_failures = 0
        self._half_open_trade_count = 0
        self._save_to_redis()
        logger.info("Circuit Breaker CLOSED: 정상 운영 재개")
    
    def can_trade(self, portfolio_value: Optional[float] = None) -> bool:
        """
        트레이딩 가능 여부 확인.
        
        Args:
            portfolio_value: 현재 포트폴리오 가치 (손실률 계산용)
        
        Returns:
            트레이딩 가능 시 True
        """
        current_state = self.state  # 자동 복구 확인 포함
        
        if current_state == CircuitState.OPEN:
            return False
        
        if current_state == CircuitState.HALF_OPEN:
            with self._lock:
                if self._half_open_trade_count >= self.half_open_max_trades:
                    return False
        
        # 일일 손실 한도 확인
        if portfolio_value:
            today = date.today()
            daily_loss = self._daily_pnl.get(today, 0.0)
            loss_pct = abs(daily_loss) / portfolio_value if portfolio_value > 0 else 0
            
            if daily_loss < 0 and (
                loss_pct >= self.daily_loss_limit_pct or 
                abs(daily_loss) >= self.daily_loss_limit_usd
            ):
                with self._lock:
                    self._transition_to_open(
                        f"일일 손실 한도 초과: ${daily_loss:.2f} ({loss_pct:.1%})"
                    )
                return False
        
        # VIX 극단치 확인
        try:
            vix_cached = cache.get("vix:latest")
            if vix_cached:
                vix_value = float(vix_cached)
                if vix_value > self.vix_extreme_threshold:
                    with self._lock:
                        self._transition_to_open(f"VIX 극단치: {vix_value:.1f}")
                    return False
        except (ValueError, TypeError):
            pass
        
        return True
    
    def record_trade_result(self, success: bool, pnl: float = 0.0) -> None:
        """
        거래 결과 기록.
        
        Args:
            success: 거래 성공 여부
            pnl: 실현 손익 (USD)
        """
        with self._lock:
            today = date.today()
            
            # 손익 기록
            if today not in self._daily_pnl:
                self._daily_pnl[today] = 0.0
            self._daily_pnl[today] += pnl
            
            if success:
                self._consecutive_failures = 0
                
                # HALF_OPEN 상태에서 성공 시 CLOSED로 전환
                if self._state == CircuitState.HALF_OPEN:
                    self._half_open_trade_count += 1
                    if self._half_open_trade_count >= self.half_open_max_trades:
                        self._transition_to_closed()
            else:
                self._consecutive_failures += 1
                
                # 연속 실패 한도 초과 확인
                if self._consecutive_failures >= self.max_consecutive_failures:
                    self._transition_to_open(
                        f"연속 {self._consecutive_failures}회 거래 실패"
                    )
            
            self._save_to_redis()
    
    @contextmanager
    def track_api_call(self):
        """
        API 호출 레이턴시 추적 컨텍스트 매니저.
        
        사용법:
            with breaker.track_api_call():
                response = api.call()
        """
        start_time = time.time()
        try:
            yield
        finally:
            elapsed_ms = (time.time() - start_time) * 1000
            self._record_latency(elapsed_ms)
    
    def _record_latency(self, latency_ms: float) -> None:
        """API 레이턴시 기록."""
        with self._lock:
            # 최근 10개 레이턴시만 유지
            self._api_latencies.append(latency_ms)
            if len(self._api_latencies) > 10:
                self._api_latencies.pop(0)
            
            # 레이턴시 한도 초과 확인
            if latency_ms > self.max_api_latency_ms:
                logger.warning("API 레이턴시 초과: %.0fms > %dms", latency_ms, self.max_api_latency_ms)
                
                # 연속 3회 초과 시 차단
                recent_high = sum(1 for l in self._api_latencies[-3:] if l > self.max_api_latency_ms)
                if recent_high >= 3:
                    self._transition_to_open(f"API 레이턴시 연속 초과: {latency_ms:.0f}ms")
    
    def force_open(self, reason: str = "수동 차단") -> None:
        """수동으로 Circuit Breaker 활성화."""
        with self._lock:
            self._transition_to_open(reason)
    
    def force_close(self) -> None:
        """수동으로 Circuit Breaker 해제."""
        with self._lock:
            self._transition_to_closed()
    
    def get_status(self) -> Dict:
        """현재 상태 정보 반환."""
        return {
            "state": self.state.value,
            "opened_at": self._opened_at.isoformat() if self._opened_at else None,
            "consecutive_failures": self._consecutive_failures,
            "daily_pnl": self._daily_pnl.get(date.today(), 0.0),
            "avg_latency_ms": (
                sum(self._api_latencies) / len(self._api_latencies) 
                if self._api_latencies else 0
            ),
            "config": {
                "daily_loss_limit_pct": self.daily_loss_limit_pct,
                "daily_loss_limit_usd": self.daily_loss_limit_usd,
                "max_api_latency_ms": self.max_api_latency_ms,
                "max_consecutive_failures": self.max_consecutive_failures,
                "vix_extreme_threshold": self.vix_extreme_threshold
            }
        }


# 싱글톤 인스턴스
_circuit_breaker: Optional[CircuitBreaker] = None


def get_circuit_breaker() -> CircuitBreaker:
    """Circuit Breaker 싱글톤 인스턴스 반환."""
    global _circuit_breaker
    if _circuit_breaker is None:
        _circuit_breaker = CircuitBreaker()
    return _circuit_breaker
