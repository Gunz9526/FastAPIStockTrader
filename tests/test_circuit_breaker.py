"""Circuit Breaker 테스트.

Circuit Breaker 상태 전환, 거래 결과 기록, API 레이턴시 추적 테스트.
K.1 Enhancement: 연속 손실, 일일 거래 한도, Discord/Prometheus 통합 테스트.
"""
from unittest.mock import MagicMock, patch

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


# ──────────────────────────────────────────────────────────────────────
# K.1 Enhancement Tests
# ──────────────────────────────────────────────────────────────────────


class TestConsecutiveLosses:
    """연속 손실 거래 한도 테스트 (max_consecutive_losses)."""

    def setup_method(self):
        """각 테스트 전 초기화."""
        with patch('app.services.circuit_breaker.cache'):
            self.breaker = CircuitBreaker(max_consecutive_losses=3)

    def test_3_consecutive_losses_triggers_open(self):
        """3회 연속 손실(pnl < 0) 시 OPEN 상태로 전환.

        Consecutive loss trigger — 3 losses in a row transitions to OPEN.
        """
        # Arrange & Act: 3회 연속 손실 거래
        self.breaker.record_trade_result(success=True, pnl=-50.0)
        self.breaker.record_trade_result(success=True, pnl=-30.0)
        self.breaker.record_trade_result(success=True, pnl=-10.0)

        # Assert
        assert self.breaker.state == CircuitState.OPEN
        assert self.breaker._consecutive_losses >= 3

    def test_loss_then_profit_resets_counter(self):
        """손실 후 이익 거래 시 연속 손실 카운터 초기화.

        A profitable trade resets the consecutive loss counter to 0.
        """
        # Arrange: 2회 손실
        self.breaker.record_trade_result(success=True, pnl=-50.0)
        self.breaker.record_trade_result(success=True, pnl=-30.0)
        assert self.breaker._consecutive_losses == 2

        # Act: 1회 이익
        self.breaker.record_trade_result(success=True, pnl=100.0)

        # Assert
        assert self.breaker._consecutive_losses == 0

    def test_2_losses_then_profit_stays_closed(self):
        """2회 손실 후 이익 → CLOSED 유지.

        Two losses followed by a profit should keep the breaker CLOSED.
        """
        # Arrange & Act
        self.breaker.record_trade_result(success=True, pnl=-50.0)
        self.breaker.record_trade_result(success=True, pnl=-30.0)
        self.breaker.record_trade_result(success=True, pnl=10.0)

        # Assert
        assert self.breaker.state == CircuitState.CLOSED
        assert self.breaker._consecutive_losses == 0

    def test_custom_max_consecutive_losses(self):
        """사용자 정의 max_consecutive_losses 적용.

        Custom threshold of 5 should only trigger after 5 consecutive losses.
        """
        # Arrange
        with patch('app.services.circuit_breaker.cache'):
            breaker = CircuitBreaker(max_consecutive_losses=5)

        # Act: 4회 손실 — 아직 CLOSED
        for _ in range(4):
            breaker.record_trade_result(success=True, pnl=-20.0)
        assert breaker.state == CircuitState.CLOSED

        # Act: 5번째 손실 → OPEN
        breaker.record_trade_result(success=True, pnl=-20.0)

        # Assert
        assert breaker.state == CircuitState.OPEN


class TestDailyTradeLimit:
    """일일 거래 횟수 한도 테스트 (max_trades_per_day)."""

    def setup_method(self):
        """각 테스트 전 초기화."""
        with patch('app.services.circuit_breaker.cache'):
            self.breaker = CircuitBreaker(max_trades_per_day=20)

    def test_blocks_at_max_trades_per_day(self):
        """일일 거래 20회 도달 시 can_trade()가 False 반환.

        After 20 trades in a day, can_trade() returns False (soft block).
        """
        # Arrange: 20회 거래 기록
        for _ in range(20):
            self.breaker.record_trade_result(success=True, pnl=5.0)

        # Act & Assert
        assert self.breaker.can_trade() is False

    def test_state_remains_closed_on_trade_limit(self):
        """거래 횟수 한도 도달 시 상태는 CLOSED 유지 (소프트 한도).

        Trade count limit is a soft block — state stays CLOSED.
        """
        # Arrange: 20회 거래 기록
        for _ in range(20):
            self.breaker.record_trade_result(success=True, pnl=5.0)

        # Act: can_trade는 False이지만 상태는 CLOSED
        can = self.breaker.can_trade()

        # Assert
        assert can is False
        assert self.breaker._state == CircuitState.CLOSED

    def test_trade_count_includes_failures(self):
        """실패 거래도 일일 거래 카운트에 포함.

        Both success and failure trades increment the daily trade counter.
        """
        # Arrange
        with patch('app.services.circuit_breaker.cache'):
            breaker = CircuitBreaker(
                max_trades_per_day=5,
                max_consecutive_failures=100  # 실패로 OPEN 안 되게
            )

        # Act: 성공 2회 + 실패 3회 = 5회
        breaker.record_trade_result(success=True, pnl=10.0)
        breaker.record_trade_result(success=True, pnl=10.0)
        breaker.record_trade_result(success=False, pnl=0.0)
        breaker.record_trade_result(success=False, pnl=0.0)
        breaker.record_trade_result(success=False, pnl=0.0)

        # Assert
        assert breaker.can_trade() is False
        assert breaker._state == CircuitState.CLOSED


class TestDiscordNotifications:
    """Discord 알림 통합 테스트."""

    def setup_method(self):
        """각 테스트 전 초기화."""
        with patch('app.services.circuit_breaker.cache'):
            self.breaker = CircuitBreaker()

    @patch('app.services.circuit_breaker._discord_notifier')
    def test_discord_warning_on_open(self, mock_discord):
        """OPEN 전환 시 Discord send_warning 호출.

        Transitioning to OPEN calls discord send_warning with reason.
        """
        # Act
        self.breaker.force_open("테스트 사유")

        # Assert
        mock_discord.send_warning.assert_called_once()
        call_args = mock_discord.send_warning.call_args
        assert "Circuit Breaker" in call_args[0][0]
        assert "OPEN" in call_args[0][1]

    @patch('app.services.circuit_breaker._discord_notifier')
    def test_discord_success_on_closed(self, mock_discord):
        """CLOSED 전환 시 Discord send_success 호출.

        Transitioning to CLOSED calls discord send_success.
        """
        # Arrange: OPEN 상태로 만들기
        self.breaker.force_open("테스트")
        mock_discord.reset_mock()

        # Act
        self.breaker.force_close()

        # Assert
        mock_discord.send_success.assert_called_once()
        call_args = mock_discord.send_success.call_args
        assert "Circuit Breaker" in call_args[0][0]
        assert "CLOSED" in call_args[0][1]

    @patch('app.services.circuit_breaker._discord_notifier')
    def test_discord_warning_on_half_open(self, mock_discord):
        """HALF_OPEN 전환 시 Discord send_warning 호출.

        Transitioning to HALF_OPEN calls discord send_warning.
        """
        # Act: _transition_to_half_open 직접 호출
        self.breaker._transition_to_half_open()

        # Assert
        mock_discord.send_warning.assert_called_once()
        call_args = mock_discord.send_warning.call_args
        assert "HALF_OPEN" in call_args[0][1]

    @patch('app.services.circuit_breaker._discord_notifier')
    def test_discord_failure_doesnt_break_transition(self, mock_discord):
        """Discord 전송 실패 시에도 상태 전환은 정상 수행.

        Discord failure must not prevent state transition.
        """
        # Arrange: Discord가 예외 발생
        mock_discord.send_warning.side_effect = Exception("Discord 연결 실패")

        # Act
        self.breaker.force_open("테스트 차단")

        # Assert: 상태 전환은 정상
        assert self.breaker.state == CircuitState.OPEN


class TestPrometheusMetrics:
    """Prometheus 메트릭 통합 테스트."""

    def setup_method(self):
        """각 테스트 전 초기화."""
        with patch('app.services.circuit_breaker.cache'):
            self.breaker = CircuitBreaker()

    @patch('app.services.circuit_breaker._discord_notifier', new=None)
    @patch('app.services.circuit_breaker.circuit_breaker_state')
    @patch('app.services.circuit_breaker.circuit_breaker_triggers')
    def test_counter_incremented_on_open(self, mock_triggers, mock_state):
        """OPEN 전환 시 circuit_breaker_triggers 카운터 증가.

        Prometheus counter is incremented with reason label on OPEN transition.
        """
        # Arrange
        mock_label = MagicMock()
        mock_triggers.labels.return_value = mock_label

        # Act
        self.breaker.force_open("수동 차단")

        # Assert
        mock_triggers.labels.assert_called_once_with(reason="manual")
        mock_label.inc.assert_called_once()

    @patch('app.services.circuit_breaker._discord_notifier', new=None)
    @patch('app.services.circuit_breaker.circuit_breaker_state')
    @patch('app.services.circuit_breaker.circuit_breaker_triggers')
    def test_gauge_set_on_state_transitions(self, mock_triggers, mock_state):
        """상태 전환 시 Prometheus 게이지 올바르게 설정.

        Gauge values: CLOSED=0, HALF_OPEN=1, OPEN=2.
        """
        # Arrange
        mock_triggers.labels.return_value = MagicMock()

        # Act: OPEN (2)
        self.breaker._transition_to_open("테스트")
        mock_state.set.assert_called_with(2)

        # Act: HALF_OPEN (1)
        mock_state.reset_mock()
        self.breaker._transition_to_half_open()
        mock_state.set.assert_called_with(1)

        # Act: CLOSED (0)
        mock_state.reset_mock()
        self.breaker._transition_to_closed()
        mock_state.set.assert_called_with(0)

    @patch('app.services.circuit_breaker._discord_notifier', new=None)
    @patch('app.services.circuit_breaker.circuit_breaker_state')
    @patch('app.services.circuit_breaker.circuit_breaker_triggers')
    def test_reason_categorized_correctly(self, mock_triggers, mock_state):
        """_transition_to_open reason이 올바른 카테고리로 분류.

        Reason strings are mapped to low-cardinality label categories.
        """
        mock_label = MagicMock()
        mock_triggers.labels.return_value = mock_label

        test_cases = [
            ("일일 손실 한도 초과: $-300", "daily_loss"),
            ("연속 5회 거래 실패", "consecutive_failures"),
            ("연속 3회 손실 거래", "consecutive_losses"),
            ("API 레이턴시 연속 초과", "api_latency"),
            ("VIX 극단치: 42.0", "vix_extreme"),
            ("수동 차단", "manual"),
        ]

        for reason, expected_category in test_cases:
            mock_triggers.reset_mock()
            mock_triggers.labels.return_value = mock_label

            self.breaker._transition_to_open(reason)
            mock_triggers.labels.assert_called_with(reason=expected_category)

            # 다시 CLOSED로 복원
            self.breaker._transition_to_closed()


class TestEnhancedStatus:
    """강화된 get_status() 테스트."""

    def setup_method(self):
        """각 테스트 전 초기화."""
        with patch('app.services.circuit_breaker.cache'):
            self.breaker = CircuitBreaker(
                max_consecutive_losses=3,
                max_trades_per_day=20,
            )

    def test_status_includes_new_fields(self):
        """get_status()가 consecutive_losses, daily_trade_count 포함.

        Enhanced status response includes new K.1 fields.
        """
        # Arrange: 손실 2회 기록
        self.breaker.record_trade_result(success=True, pnl=-10.0)
        self.breaker.record_trade_result(success=True, pnl=-20.0)

        # Act
        status = self.breaker.get_status()

        # Assert
        assert "consecutive_losses" in status
        assert status["consecutive_losses"] == 2
        assert "daily_trade_count" in status
        assert status["daily_trade_count"] == 2

    def test_status_config_includes_new_limits(self):
        """config에 max_consecutive_losses, max_trades_per_day 포함.

        Status config section includes the new limit parameters.
        """
        # Act
        status = self.breaker.get_status()
        config = status["config"]

        # Assert
        assert "max_consecutive_losses" in config
        assert config["max_consecutive_losses"] == 3
        assert "max_trades_per_day" in config
        assert config["max_trades_per_day"] == 20
