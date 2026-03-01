"""Circuit Breaker: 자동 트레이딩 중단 시스템.

이 모듈은 시스템 보호를 위한 Circuit Breaker 패턴을 구현합니다.

트레이딩 중단 조건:
1. 일일 손실 한도 초과 (기본: 3% 또는 $500)
2. API 레이턴시 초과 (기본: 3000ms, 연속 3회)
3. 연속 실패 거래 횟수 초과 (기본: 5회)
4. VIX 극단치 감지 (기본: > 35)
5. 연속 손실 거래 횟수 초과 (기본: 3회, pnl < 0)
6. 일일 거래 횟수 한도 (기본: 20회, 소프트 한도)

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
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from enum import Enum
from threading import Lock

from app.core.cache import cache

logger = logging.getLogger(__name__)

# Lazy import 대상 (초기화 실패 방지)
try:
    from app.core.metrics import circuit_breaker_state, circuit_breaker_triggers
except ImportError:
    circuit_breaker_state = None  # type: ignore[assignment]
    circuit_breaker_triggers = None  # type: ignore[assignment]

try:
    from app.services.discord_notifier import discord_notifier as _discord_notifier
except ImportError:
    _discord_notifier = None  # type: ignore[assignment]


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
        half_open_max_trades: int = 2,             # HALF_OPEN 상태에서 허용 거래 수
        max_consecutive_losses: int = 3,           # 연속 손실 거래 한도
        max_trades_per_day: int = 20               # 일일 거래 횟수 한도
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
            max_consecutive_losses: 연속 손실 거래 한도 (pnl < 0)
            max_trades_per_day: 일일 거래 횟수 소프트 한도
        """
        self.daily_loss_limit_pct = daily_loss_limit_pct
        self.daily_loss_limit_usd = daily_loss_limit_usd
        self.max_api_latency_ms = max_api_latency_ms
        self.max_consecutive_failures = max_consecutive_failures
        self.vix_extreme_threshold = vix_extreme_threshold
        self.recovery_timeout_minutes = recovery_timeout_minutes
        self.half_open_max_trades = half_open_max_trades
        self.max_consecutive_losses = max_consecutive_losses
        self.max_trades_per_day = max_trades_per_day

        # 인메모리 상태 (Redis 백업)
        self._state = CircuitState.CLOSED
        self._opened_at: datetime | None = None
        self._consecutive_failures = 0
        self._consecutive_losses = 0
        self._daily_pnl: dict[date, float] = {}
        self._daily_trade_count: dict[date, int] = {}
        self._api_latencies: list = []
        self._half_open_trade_count = 0

        # 스레드 안전성
        self._lock = Lock()

        # Redis에서 상태 복원
        self._restore_from_redis()

    def _restore_from_redis(self) -> None:
        """Redis에서 상태 복원.

        저장된 state, opened_at, consecutive_failures, consecutive_losses를
        Redis에서 읽어 인메모리 상태를 복원합니다.
        Redis 연결 실패 시 기본값을 유지합니다.
        """
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

            losses = cache.get(f"{self.REDIS_PREFIX}consecutive_losses")
            if losses:
                self._consecutive_losses = int(losses)

            # Prometheus 상태 게이지 동기화
            try:
                if circuit_breaker_state is not None:
                    state_value = {CircuitState.CLOSED: 0, CircuitState.HALF_OPEN: 1, CircuitState.OPEN: 2}
                    circuit_breaker_state.set(state_value.get(self._state, 0))
            except Exception:
                pass

            logger.info("Circuit Breaker 상태 복원: %s", self._state.value)
        except Exception as e:
            logger.warning("Circuit Breaker 상태 복원 실패: %s", str(e))

    def _save_to_redis(self) -> None:
        """상태를 Redis에 저장.

        현재 state, opened_at, consecutive_failures, consecutive_losses를
        Redis에 TTL 24시간으로 저장합니다.
        Redis 연결 실패 시 경고 로그만 출력합니다.
        """
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

            cache.set(
                f"{self.REDIS_PREFIX}consecutive_losses",
                str(self._consecutive_losses),
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
        """OPEN 상태로 전환 (트레이딩 차단).

        Args:
            reason: 차단 사유 (로그, Discord 알림에 사용)
        """
        self._state = CircuitState.OPEN
        self._opened_at = datetime.now()
        self._save_to_redis()
        logger.warning("Circuit Breaker OPEN: %s", reason)

        # Prometheus 메트릭 업데이트 (reason을 카테고리로 제한하여 카디널리티 방지)
        try:
            reason_category = "daily_loss" if "손실 한도" in reason else \
                              "consecutive_failures" if "거래 실패" in reason else \
                              "consecutive_losses" if "손실 거래" in reason else \
                              "api_latency" if "레이턴시" in reason else \
                              "vix_extreme" if "VIX" in reason else "manual"
            if circuit_breaker_triggers is not None:
                circuit_breaker_triggers.labels(reason=reason_category).inc()
            if circuit_breaker_state is not None:
                circuit_breaker_state.set(2)  # OPEN = 2
        except Exception as e:
            logger.warning("Prometheus 메트릭 업데이트 실패: %s", str(e))

        # Discord 알림
        try:
            status = self.get_status()
            if _discord_notifier:
                _discord_notifier.send_warning(
                    "Circuit Breaker",
                    f"🔴 OPEN: {reason}",
                    extra_info={
                        "상태": status.get("state", "unknown"),
                        "일일 손익": f"${status.get('daily_pnl', 0):.2f}",
                        "연속 실패": str(status.get("consecutive_failures", 0)),
                        "연속 손실": str(status.get("consecutive_losses", 0)),
                    }
                )
        except Exception as e:
            logger.warning("Discord 알림 전송 실패: %s", str(e))

    def _transition_to_half_open(self) -> None:
        """HALF_OPEN 상태로 전환 (제한된 테스트).

        recovery_timeout_minutes 경과 후 자동 전환되며,
        half_open_max_trades 만큼 테스트 거래를 허용합니다.
        """
        self._state = CircuitState.HALF_OPEN
        self._half_open_trade_count = 0
        self._save_to_redis()
        logger.info("Circuit Breaker HALF_OPEN: 복구 테스트 시작")

        # Prometheus 상태 게이지 업데이트
        try:
            if circuit_breaker_state is not None:
                circuit_breaker_state.set(1)  # HALF_OPEN = 1
        except Exception as e:
            logger.warning("Prometheus 메트릭 업데이트 실패: %s", str(e))

        # Discord 알림
        try:
            if _discord_notifier:
                _discord_notifier.send_warning(
                    "Circuit Breaker",
                    "🟡 HALF_OPEN: 복구 테스트 시작",
                    extra_info={"허용 거래 수": str(self.half_open_max_trades)}
                )
        except Exception as e:
            logger.warning("Discord 알림 전송 실패: %s", str(e))

    def _transition_to_closed(self) -> None:
        """CLOSED 상태로 전환 (정상 운영).

        모든 실패/손실 카운터를 초기화하고 정상 운영을 재개합니다.
        """
        self._state = CircuitState.CLOSED
        self._opened_at = None
        self._consecutive_failures = 0
        self._consecutive_losses = 0
        self._half_open_trade_count = 0
        self._save_to_redis()
        logger.info("Circuit Breaker CLOSED: 정상 운영 재개")

        # Prometheus 상태 게이지 업데이트
        try:
            if circuit_breaker_state is not None:
                circuit_breaker_state.set(0)  # CLOSED = 0
        except Exception as e:
            logger.warning("Prometheus 메트릭 업데이트 실패: %s", str(e))

        # Discord 알림
        try:
            if _discord_notifier:
                _discord_notifier.send_success(
                    "Circuit Breaker",
                    "🟢 CLOSED: 정상 운영 재개"
                )
        except Exception as e:
            logger.warning("Discord 알림 전송 실패: %s", str(e))

    def can_trade(self, portfolio_value: float | None = None) -> bool:
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

        # 일일 거래 횟수 한도 확인 (소프트 한도 — OPEN 전환 없이 차단)
        today = date.today()
        with self._lock:
            daily_count = self._daily_trade_count.get(today, 0)
        if daily_count >= self.max_trades_per_day:
            logger.warning(
                "일일 거래 횟수 한도 도달: %d/%d — 트레이딩 차단",
                daily_count, self.max_trades_per_day
            )
            return False

        # 일일 손실 한도 확인
        if portfolio_value:
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
            success: 거래 성공 여부 (API 호출 성공/실패)
            pnl: 실현 손익 (USD)
        """
        with self._lock:
            today = date.today()

            # 손익 기록
            if today not in self._daily_pnl:
                self._daily_pnl[today] = 0.0
            self._daily_pnl[today] += pnl

            # 일일 거래 횟수 기록 (성공/실패 무관)
            if today not in self._daily_trade_count:
                self._daily_trade_count[today] = 0
            self._daily_trade_count[today] += 1

            if success:
                self._consecutive_failures = 0

                # 연속 손실 거래 추적 (pnl 기반)
                if pnl < 0:
                    self._consecutive_losses += 1
                    if self._consecutive_losses >= self.max_consecutive_losses:
                        self._transition_to_open(
                            f"연속 {self._consecutive_losses}회 손실 거래"
                        )
                else:
                    self._consecutive_losses = 0

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

    def get_status(self) -> dict:
        """현재 상태 정보 반환.

        Returns:
            상태 딕셔너리 (state, opened_at, consecutive_failures,
            consecutive_losses, daily_pnl, daily_trade_count,
            avg_latency_ms, config)
        """
        today = date.today()
        return {
            "state": self.state.value,
            "opened_at": self._opened_at.isoformat() if self._opened_at else None,
            "consecutive_failures": self._consecutive_failures,
            "consecutive_losses": self._consecutive_losses,
            "daily_pnl": self._daily_pnl.get(today, 0.0),
            "daily_trade_count": self._daily_trade_count.get(today, 0),
            "avg_latency_ms": (
                sum(self._api_latencies) / len(self._api_latencies)
                if self._api_latencies else 0
            ),
            "config": {
                "daily_loss_limit_pct": self.daily_loss_limit_pct,
                "daily_loss_limit_usd": self.daily_loss_limit_usd,
                "max_api_latency_ms": self.max_api_latency_ms,
                "max_consecutive_failures": self.max_consecutive_failures,
                "max_consecutive_losses": self.max_consecutive_losses,
                "max_trades_per_day": self.max_trades_per_day,
                "vix_extreme_threshold": self.vix_extreme_threshold
            }
        }


# 싱글톤 인스턴스
_circuit_breaker: CircuitBreaker | None = None
_circuit_breaker_lock = Lock()


def get_circuit_breaker() -> CircuitBreaker:
    """Circuit Breaker 싱글톤 인스턴스 반환 (thread-safe)."""
    global _circuit_breaker
    if _circuit_breaker is None:
        with _circuit_breaker_lock:
            if _circuit_breaker is None:
                _circuit_breaker = CircuitBreaker()
    return _circuit_breaker
