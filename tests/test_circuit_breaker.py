"""Circuit Breaker 테스트.

Circuit Breaker 상태 전환, 거래 결과 기록, API 레이턴시 추적 테스트.
"""
from unittest.mock import patch

from app.services.circuit_breaker import CircuitBreaker, CircuitState, get_circuit_breaker


class TestCircuitBreaker:
    """Circuit Breaker 단위 테스트."""

    def setup_method(self):
        """각 테스트 전 초기화."""
        # Redis mock으로 격리된 인스턴스 생성
        with patch('app.services.circuit_breaker.cache'):
            self.breaker = CircuitBreaker()

    def test_initial_state_is_closed(self):
        """초기 상태는 CLOSED여야 함."""
        assert self.breaker.state == CircuitState.CLOSED

    def test_can_trade_when_closed(self):
        """CLOSED 상태에서 트레이딩 허용."""
        assert self.breaker.can_trade() is True

    def test_transition_to_open_on_consecutive_failures(self):
        """연속 실패 시 OPEN 상태로 전환."""
        # 5회 연속 실패 기록
        for _ in range(5):
            self.breaker.record_trade_result(success=False, pnl=0.0)

        assert self.breaker.state == CircuitState.OPEN
        assert self.breaker.can_trade() is False

    def test_transition_to_open_on_daily_loss_limit(self):
        """일일 손실 한도 초과 시 OPEN 상태로 전환."""
        portfolio_value = 10000.0
        # 3% 손실 ($300) 기록
        self.breaker.record_trade_result(success=True, pnl=-350.0)

        # 손실 한도 확인
        can_trade = self.breaker.can_trade(portfolio_value=portfolio_value)
        assert can_trade is False
        assert self.breaker.state == CircuitState.OPEN

    def test_record_trade_resets_failures_on_success(self):
        """성공 거래 시 연속 실패 카운터 초기화."""
        # 3회 실패
        for _ in range(3):
            self.breaker.record_trade_result(success=False, pnl=0.0)

        assert self.breaker._consecutive_failures == 3

        # 1회 성공
        self.breaker.record_trade_result(success=True, pnl=100.0)

        assert self.breaker._consecutive_failures == 0

    def test_force_open_and_close(self):
        """수동 차단/해제 테스트."""
        # 수동 차단
        self.breaker.force_open("테스트 차단")
        assert self.breaker.state == CircuitState.OPEN
        assert self.breaker.can_trade() is False

        # 수동 해제
        self.breaker.force_close()
        assert self.breaker.state == CircuitState.CLOSED
        assert self.breaker.can_trade() is True

    def test_get_status_returns_correct_format(self):
        """get_status가 올바른 형식 반환."""
        status = self.breaker.get_status()

        assert "state" in status
        assert "opened_at" in status
        assert "consecutive_failures" in status
        assert "daily_pnl" in status
        assert "avg_latency_ms" in status
        assert "config" in status

    def test_track_api_call_records_latency(self):
        """track_api_call이 레이턴시 기록."""
        import time

        with self.breaker.track_api_call():
            time.sleep(0.01)  # 10ms

        assert len(self.breaker._api_latencies) == 1
        assert self.breaker._api_latencies[0] >= 10  # 최소 10ms


class TestCircuitBreakerSingleton:
    """싱글톤 패턴 테스트."""

    def test_get_circuit_breaker_returns_same_instance(self):
        """get_circuit_breaker가 동일 인스턴스 반환."""
        with patch('app.services.circuit_breaker.cache'):
            breaker1 = get_circuit_breaker()
            breaker2 = get_circuit_breaker()

            # 참조 동일성 확인 (싱글톤)
            assert breaker1 is breaker2


class TestCircuitBreakerVIX:
    """VIX 기반 차단 테스트."""

    def test_blocks_on_extreme_vix(self):
        """VIX 극단치 감지 시 차단."""
        with patch('app.services.circuit_breaker.cache') as mock_cache:
            breaker = CircuitBreaker(vix_extreme_threshold=35.0)

            # VIX 40으로 설정
            mock_cache.get.return_value = "40.0"

            can_trade = breaker.can_trade()

            assert can_trade is False
            assert breaker.state == CircuitState.OPEN
