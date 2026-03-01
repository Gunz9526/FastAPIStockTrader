"""PortfolioOptimizer 클래스에 대한 단위 테스트."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import numpy as np  # type: ignore[import-untyped]
import pandas as pd  # type: ignore[import-untyped]
import pytest  # type: ignore[import-untyped]

from app.services.portfolio_optimizer import PortfolioOptimizer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class FakeBar:
    """OHLCV 바 모의 객체."""

    def __init__(self, close: float, date_time: datetime | None = None):
        self.close = close
        self.date_time = date_time or datetime.now()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def mock_repo() -> MagicMock:
    """PortfolioRepository 모의 객체."""
    return MagicMock()


@pytest.fixture()
def optimizer() -> PortfolioOptimizer:
    """기본 PortfolioOptimizer 인스턴스."""
    return PortfolioOptimizer()


# ---------------------------------------------------------------------------
# TestInit
# ---------------------------------------------------------------------------

class TestInit:
    """PortfolioOptimizer 생성자 테스트."""

    def test_init_defaults(self) -> None:
        """기본 매개변수로 초기화 시 lookback_days=14, min_live_trades=50, cache_ttl=86400."""
        opt = PortfolioOptimizer()
        assert opt.lookback_days == 14
        assert opt.min_live_trades == 50
        assert opt.cache_ttl == 86400


# ---------------------------------------------------------------------------
# TestCorrelationMatrix
# ---------------------------------------------------------------------------

class TestCorrelationMatrix:
    """calculate_correlation_matrix 메서드 테스트."""

    @patch("app.services.portfolio_optimizer.cache")
    def test_correlation_matrix_identity_fallback(
        self,
        mock_cache: MagicMock,
        optimizer: PortfolioOptimizer,
        mock_repo: MagicMock,
    ) -> None:
        """비어 있는 심볼이 2개 미만일 때 단위 행렬(identity matrix) 반환."""
        mock_cache.get.return_value = None

        # 라이브 거래 수가 충분하지 않으면 백테스트 경로 사용
        mock_repo.count_live_trades.return_value = 0
        # 각 심볼에 대해 빈 OHLCV 반환 → 빈 Series → non_empty < 2
        mock_repo.get_ohlcv_range.return_value = []

        symbols = ["AAPL", "MSFT", "GOOG"]
        result = optimizer.calculate_correlation_matrix(mock_repo, symbols)

        expected = pd.DataFrame(
            np.eye(3), index=symbols, columns=symbols
        )
        pd.testing.assert_frame_equal(result, expected)

    @patch("app.services.portfolio_optimizer.cache")
    def test_correlation_matrix_live_data(
        self,
        mock_cache: MagicMock,
        optimizer: PortfolioOptimizer,
        mock_repo: MagicMock,
    ) -> None:
        """충분한 라이브 거래가 있을 때 상관 행렬을 올바르게 계산."""
        mock_cache.get.return_value = None

        # 라이브 거래 수: min_live_trades 이상
        mock_repo.count_live_trades.return_value = 100

        # 심볼당 10건의 거래 데이터 생성
        def _make_trades(base: float) -> list[dict]:
            return [
                {"entry_price": base, "exit_price": base * (1 + 0.01 * i)}
                for i in range(1, 11)
            ]

        mock_repo.get_trade_history.side_effect = lambda sym, *a, **kw: _make_trades(
            {"AAPL": 150.0, "MSFT": 300.0}[sym]
        )

        symbols = ["AAPL", "MSFT"]
        result = optimizer.calculate_correlation_matrix(mock_repo, symbols)

        # 결과는 2x2 DataFrame이어야 함
        assert result.shape == (2, 2)
        # 대각선 값은 1.0
        assert result.loc["AAPL", "AAPL"] == pytest.approx(1.0)
        assert result.loc["MSFT", "MSFT"] == pytest.approx(1.0)
        # cache.set이 호출되었는지 확인
        mock_cache.set.assert_called_once()


# ---------------------------------------------------------------------------
# TestKellyCriterion
# ---------------------------------------------------------------------------

class TestKellyCriterion:
    """kelly_criterion 메서드 테스트."""

    @patch("app.services.portfolio_optimizer.cache")
    def test_kelly_positive(
        self,
        mock_cache: MagicMock,
        optimizer: PortfolioOptimizer,
        mock_repo: MagicMock,
    ) -> None:
        """승률 60%, 평균 수익 2.0, 평균 손실 1.0 → Kelly 안전 비율 0.10.

        b = avg_win / avg_loss = 2.0 / 1.0 = 2.0
        p = 0.6, q = 0.4
        kelly = (b * p - q) / b = (2*0.6 - 0.4) / 2 = 0.4
        kelly_safe = 0.4 * 0.25 = 0.10
        """
        mock_cache.get.return_value = None

        # 10건 거래: 6승(pnl=2.0), 4패(pnl=-1.0)
        trades: list[dict] = (
            [{"entry_price": 100, "exit_price": 102, "pnl": 2.0}] * 6
            + [{"entry_price": 100, "exit_price": 99, "pnl": -1.0}] * 4
        )
        mock_repo.get_trade_history.return_value = trades

        result = optimizer.kelly_criterion(mock_repo, "AAPL")

        assert result == pytest.approx(0.10, abs=1e-6)
        mock_cache.set.assert_called_once()

    @patch("app.services.portfolio_optimizer.cache")
    def test_kelly_insufficient_trades(
        self,
        mock_cache: MagicMock,
        optimizer: PortfolioOptimizer,
        mock_repo: MagicMock,
    ) -> None:
        """거래 이력이 5건 미만이면 보수적 기본값 0.10 반환."""
        mock_cache.get.return_value = None

        # 라이브 거래 3건 + 백테스트도 부족
        mock_repo.get_trade_history.return_value = [
            {"entry_price": 100, "exit_price": 101, "pnl": 1.0}
        ] * 3
        # 백테스트에서도 25개 미만 바 → 빈 리스트
        mock_repo.get_ohlcv_range.return_value = []

        result = optimizer.kelly_criterion(mock_repo, "AAPL")

        assert result == pytest.approx(0.10)

    @patch("app.services.portfolio_optimizer.cache")
    def test_kelly_exception(
        self,
        mock_cache: MagicMock,
        optimizer: PortfolioOptimizer,
        mock_repo: MagicMock,
    ) -> None:
        """repo에서 예외 발생 시 보수적 기본값 0.10 반환."""
        mock_cache.get.return_value = None
        mock_repo.get_trade_history.side_effect = RuntimeError("DB 오류")

        result = optimizer.kelly_criterion(mock_repo, "AAPL")

        assert result == pytest.approx(0.10)


# ---------------------------------------------------------------------------
# TestCalculateVaR
# ---------------------------------------------------------------------------

class TestCalculateVaR:
    """calculate_var 메서드 테스트."""

    @patch("app.services.portfolio_optimizer.cache")
    def test_var_live_data(
        self,
        mock_cache: MagicMock,
        optimizer: PortfolioOptimizer,
        mock_repo: MagicMock,
    ) -> None:
        """충분한 라이브 일일 수익률이 있을 때 음수 VaR 값 반환."""
        mock_cache.get.return_value = None

        # 정규분포 N(0.001, 0.02) 기반 14일 일일 수익률
        rng = np.random.default_rng(42)
        daily_returns = rng.normal(0.001, 0.02, size=14)
        mock_repo.get_daily_pnl.return_value = pd.DataFrame(
            {"daily_return": daily_returns}
        )

        portfolio_value = 100_000.0
        result = optimizer.calculate_var(mock_repo, portfolio_value)

        # VaR는 음수여야 함 (손실)
        assert result < 0
        # 합리적인 범위 내 확인 (예: -10% ~ 0)
        assert result > portfolio_value * -0.10
        mock_cache.set.assert_called_once()

    @patch("app.services.portfolio_optimizer.cache")
    def test_var_exception(
        self,
        mock_cache: MagicMock,
        optimizer: PortfolioOptimizer,
        mock_repo: MagicMock,
    ) -> None:
        """repo에서 예외 발생 시 portfolio_value * -0.03 반환."""
        mock_cache.get.return_value = None
        mock_repo.get_daily_pnl.side_effect = RuntimeError("DB 오류")

        portfolio_value = 100_000.0
        result = optimizer.calculate_var(mock_repo, portfolio_value)

        assert result == pytest.approx(portfolio_value * -0.03)


# ---------------------------------------------------------------------------
# TestOptimizeWeights
# ---------------------------------------------------------------------------

class TestOptimizeWeights:
    """optimize_weights 메서드 테스트."""

    @patch("app.services.portfolio_optimizer.cache")
    def test_optimize_weights_success(
        self,
        mock_cache: MagicMock,
        optimizer: PortfolioOptimizer,
        mock_repo: MagicMock,
    ) -> None:
        """3개 심볼에 유효한 데이터가 있을 때 가중치 합 = 1.0, 각 ≤ 0.30."""
        mock_cache.get.return_value = None

        symbols = ["AAPL", "MSFT", "GOOG"]

        # 상관 행렬 계산을 위해 백테스트 경로 사용
        mock_repo.count_live_trades.return_value = 0

        # 심볼별 30개 바 생성 (백테스트 수익률 + optimize_weights 내부 호출 공용)
        rng = np.random.default_rng(123)
        base_prices = {"AAPL": 150.0, "MSFT": 300.0, "GOOG": 2800.0}

        def _make_bars(symbol: str, *_args, **_kwargs) -> list[FakeBar]:
            base = base_prices.get(symbol, 100.0)
            prices = base * np.cumprod(1 + rng.normal(0.001, 0.01, size=30))
            return [
                FakeBar(close=float(p), date_time=datetime(2026, 1, i + 1))
                for i, p in enumerate(prices)
            ]

        mock_repo.get_ohlcv_range.side_effect = _make_bars

        result = optimizer.optimize_weights(mock_repo, symbols)

        # 모든 심볼에 대한 가중치가 존재해야 함
        assert set(result.keys()) == set(symbols)
        # 가중치 합 ≈ 1.0
        assert sum(result.values()) == pytest.approx(1.0, abs=1e-4)
        # 각 가중치 ≤ 0.30
        for w in result.values():
            assert w <= 0.30 + 1e-6

    @patch("app.services.portfolio_optimizer.cache")
    def test_optimize_weights_fallback(
        self,
        mock_cache: MagicMock,
        optimizer: PortfolioOptimizer,
        mock_repo: MagicMock,
    ) -> None:
        """예외 발생 시 균등 가중치(1/n) 반환."""
        mock_cache.get.return_value = None

        # 상관 행렬 계산에서 예외 발생하도록 설정
        mock_repo.count_live_trades.side_effect = RuntimeError("DB 오류")

        symbols = ["AAPL", "MSFT", "GOOG"]
        result = optimizer.optimize_weights(mock_repo, symbols)

        expected_weight = 1.0 / len(symbols)
        for sym in symbols:
            assert result[sym] == pytest.approx(expected_weight)
