"""Daily Signal Cache 테스트 — CachedSignal schema 및 DailySignalCache 동작 검증.

Tests:
    - CachedSignal / DailySignalSummary Pydantic 모델 유효성
    - DailySignalCache Redis 키 포맷
    - DailySignalCache CRUD 동작 (Redis mock)
    - DailySignalCache summary 집계
    - Cache disabled 상태 동작
"""
from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from app.domain.schemas.signal import CachedSignal, DailySignalSummary
from app.services.signal_cache import DailySignalCache, _KEY_PREFIX

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def sample_signal() -> CachedSignal:
    """테스트용 CachedSignal 인스턴스를 반환합니다."""
    return CachedSignal(
        symbol="AAPL",
        predicted_class=2,
        confidence=0.85,
        probabilities={"DOWN": 0.05, "NEUTRAL": 0.10, "UP": 0.85},
        regime="sideways_calm",
        generated_at=datetime(2026, 2, 27, 17, 30, 0),
        model_version="v1.0",
    )


@pytest.fixture()
def sample_signals() -> list[CachedSignal]:
    """여러 종목의 CachedSignal 리스트를 반환합니다."""
    return [
        CachedSignal(
            symbol="AAPL",
            predicted_class=2,
            confidence=0.85,
            probabilities={"DOWN": 0.05, "NEUTRAL": 0.10, "UP": 0.85},
            regime="sideways_calm",
            generated_at=datetime(2026, 2, 27, 17, 30, 0),
        ),
        CachedSignal(
            symbol="MSFT",
            predicted_class=1,
            confidence=0.60,
            probabilities={"DOWN": 0.15, "NEUTRAL": 0.60, "UP": 0.25},
            regime="sideways_calm",
            generated_at=datetime(2026, 2, 27, 17, 31, 0),
        ),
        CachedSignal(
            symbol="TSLA",
            predicted_class=0,
            confidence=0.70,
            probabilities={"DOWN": 0.70, "NEUTRAL": 0.20, "UP": 0.10},
            regime="sideways_calm",
            generated_at=datetime(2026, 2, 27, 17, 32, 0),
        ),
    ]


@pytest.fixture()
def mock_cache() -> MagicMock:
    """Redis CacheService를 대체하는 in-memory mock을 반환합니다."""
    store: dict[str, str] = {}

    mock = MagicMock()
    mock.enabled = True

    # redis_client mock
    redis_mock = MagicMock()

    def _setex(key: str, ttl: int, value: str) -> None:
        store[key] = value

    def _get(key: str) -> str | None:
        return store.get(key)

    def _keys(pattern: str) -> list[str]:
        """간이 glob 매칭: 'prefix:*' 또는 'prefix:*:regime' 형태를 지원."""
        import fnmatch

        return [k for k in store if fnmatch.fnmatch(k, pattern)]

    def _delete(*keys: str) -> int:
        count = 0
        for k in keys:
            if k in store:
                del store[k]
                count += 1
        return count

    redis_mock.setex.side_effect = _setex
    redis_mock.get.side_effect = _get
    redis_mock.keys.side_effect = _keys
    redis_mock.delete.side_effect = _delete

    mock.redis_client = redis_mock

    # CacheService.set / .get also used by set_signal / get_signal
    def _cache_set(key: str, value: object, ttl_seconds: int = 3600) -> None:
        store[key] = json.dumps(value, default=str)

    def _cache_get(key: str) -> object | None:
        raw = store.get(key)
        if raw is None:
            return None
        return json.loads(raw)

    mock.set.side_effect = _cache_set
    mock.get.side_effect = _cache_get

    # Expose internal store for assertions
    mock._store = store
    return mock


# ===========================================================================
# TestCachedSignalSchema
# ===========================================================================


class TestCachedSignalSchema:
    """CachedSignal Pydantic 모델 유효성 검증 테스트."""

    def test_valid_signal_creation(self, sample_signal: CachedSignal) -> None:
        """유효한 파라미터로 CachedSignal 인스턴스가 올바르게 생성되는지 확인합니다."""
        assert sample_signal.symbol == "AAPL"
        assert sample_signal.predicted_class == 2
        assert sample_signal.confidence == 0.85
        assert sample_signal.regime == "sideways_calm"
        assert sample_signal.model_version == "v1.0"
        assert isinstance(sample_signal.generated_at, datetime)

    def test_class_name_property(self) -> None:
        """predicted_class 값에 따라 class_name 프로퍼티가 올바른 문자열을 반환하는지 확인합니다."""
        mapping = {0: "DOWN", 1: "NEUTRAL", 2: "UP"}
        for cls_id, expected_name in mapping.items():
            sig = CachedSignal(
                symbol="TEST",
                predicted_class=cls_id,
                confidence=0.5,
                probabilities={"DOWN": 0.33, "NEUTRAL": 0.34, "UP": 0.33},
                regime="test",
            )
            assert sig.class_name == expected_name, (
                f"predicted_class={cls_id} → expected '{expected_name}', got '{sig.class_name}'"
            )

    def test_invalid_class_rejected(self) -> None:
        """predicted_class가 허용 범위(0~2)를 벗어나면 ValidationError가 발생하는지 확인합니다."""
        with pytest.raises(ValidationError):
            CachedSignal(
                symbol="BAD",
                predicted_class=5,
                confidence=0.5,
                probabilities={"DOWN": 0.33, "NEUTRAL": 0.34, "UP": 0.33},
                regime="test",
            )

    def test_probabilities_stored_correctly(self, sample_signal: CachedSignal) -> None:
        """probabilities 딕셔너리가 올바르게 저장되는지 확인합니다."""
        assert set(sample_signal.probabilities.keys()) == {"DOWN", "NEUTRAL", "UP"}
        assert abs(sum(sample_signal.probabilities.values()) - 1.0) < 1e-9


# ===========================================================================
# TestDailySignalSummary
# ===========================================================================


class TestDailySignalSummary:
    """DailySignalSummary Pydantic 모델 검증 테스트."""

    def test_empty_summary(self) -> None:
        """기본값으로 생성된 DailySignalSummary가 빈 상태를 나타내는지 확인합니다."""
        summary = DailySignalSummary()
        assert summary.total_signals == 0
        assert summary.up_count == 0
        assert summary.neutral_count == 0
        assert summary.down_count == 0
        assert summary.avg_confidence == 0.0
        assert summary.signals == []
        assert summary.generated_at is None

    def test_summary_with_signals(self, sample_signals: list[CachedSignal]) -> None:
        """시그널 리스트가 포함된 DailySignalSummary의 필드들이 올바른지 확인합니다."""
        summary = DailySignalSummary(
            total_signals=3,
            regime="sideways_calm",
            up_count=1,
            neutral_count=1,
            down_count=1,
            avg_confidence=round((0.85 + 0.60 + 0.70) / 3, 4),
            generated_at=datetime(2026, 2, 27, 17, 32, 0),
            signals=sample_signals,
        )
        assert summary.total_signals == 3
        assert summary.up_count == 1
        assert summary.neutral_count == 1
        assert summary.down_count == 1
        assert len(summary.signals) == 3
        assert summary.regime == "sideways_calm"


# ===========================================================================
# TestDailySignalCacheKeyFormat
# ===========================================================================


class TestDailySignalCacheKeyFormat:
    """DailySignalCache._make_key 키 포맷 검증 테스트."""

    def test_make_key_format(self) -> None:
        """표준 심볼에 대해 올바른 Redis 키 포맷이 생성되는지 확인합니다."""
        key = DailySignalCache._make_key("AAPL", "sideways_calm")
        assert key == f"{_KEY_PREFIX}:AAPL:sideways_calm"

    def test_make_key_special_characters(self) -> None:
        """특수 문자가 포함된 심볼(BRK.B)에 대해서도 키가 올바르게 생성되는지 확인합니다."""
        key = DailySignalCache._make_key("BRK.B", "bullish")
        assert key == f"{_KEY_PREFIX}:BRK.B:bullish"
        assert "BRK.B" in key


# ===========================================================================
# TestDailySignalCacheOperations
# ===========================================================================


class TestDailySignalCacheOperations:
    """DailySignalCache CRUD 동작 검증 테스트 (Redis mock 사용)."""

    @patch("app.services.signal_cache.cache")
    def test_set_and_get_signal(
        self, patched_cache: MagicMock, mock_cache: MagicMock, sample_signal: CachedSignal,
    ) -> None:
        """set_signal 후 get_signal로 동일한 시그널을 조회할 수 있는지 확인합니다."""
        patched_cache.__dict__.update(mock_cache.__dict__)
        patched_cache.set = mock_cache.set
        patched_cache.get = mock_cache.get

        svc = DailySignalCache()
        svc._cache = patched_cache

        svc.set_signal(sample_signal)
        result = svc.get_signal("AAPL", "sideways_calm")

        assert result is not None
        assert result.symbol == "AAPL"
        assert result.predicted_class == 2
        assert result.confidence == 0.85

    @patch("app.services.signal_cache.cache")
    def test_get_signal_cache_miss(self, patched_cache: MagicMock, mock_cache: MagicMock) -> None:
        """캐시에 존재하지 않는 키를 조회하면 None을 반환하는지 확인합니다."""
        patched_cache.get = mock_cache.get

        svc = DailySignalCache()
        svc._cache = patched_cache

        result = svc.get_signal("NONEXIST", "unknown_regime")
        assert result is None

    @patch("app.services.signal_cache.cache")
    def test_get_all_signals_empty(self, patched_cache: MagicMock, mock_cache: MagicMock) -> None:
        """캐시가 비어있을 때 get_all_signals가 빈 리스트를 반환하는지 확인합니다."""
        patched_cache.enabled = True
        patched_cache.redis_client = mock_cache.redis_client

        svc = DailySignalCache()
        svc._cache = patched_cache

        result = svc.get_all_signals()
        assert result == []

    @patch("app.services.signal_cache.cache")
    def test_set_signals_bulk(
        self, patched_cache: MagicMock, mock_cache: MagicMock, sample_signals: list[CachedSignal],
    ) -> None:
        """set_signals_bulk로 여러 시그널을 저장하고 저장 개수를 확인합니다."""
        patched_cache.set = mock_cache.set
        patched_cache.get = mock_cache.get

        svc = DailySignalCache()
        svc._cache = patched_cache

        count = svc.set_signals_bulk(sample_signals)
        assert count == 3

        # 개별 조회 확인
        aapl = svc.get_signal("AAPL", "sideways_calm")
        assert aapl is not None
        assert aapl.symbol == "AAPL"

    @patch("app.services.signal_cache.cache")
    def test_invalidate_all(
        self, patched_cache: MagicMock, mock_cache: MagicMock, sample_signals: list[CachedSignal],
    ) -> None:
        """invalidate_all이 모든 시그널 캐시를 삭제하는지 확인합니다."""
        patched_cache.enabled = True
        patched_cache.set = mock_cache.set
        patched_cache.redis_client = mock_cache.redis_client

        svc = DailySignalCache()
        svc._cache = patched_cache

        # 시그널 캐싱
        for sig in sample_signals:
            svc.set_signal(sig)

        deleted = svc.invalidate_all()
        assert deleted == 3

        # 삭제 후 조회
        remaining = svc.get_all_signals()
        assert remaining == []

    @patch("app.services.signal_cache.cache")
    def test_invalidate_symbol(
        self, patched_cache: MagicMock, mock_cache: MagicMock, sample_signals: list[CachedSignal],
    ) -> None:
        """invalidate_symbol이 특정 심볼의 캐시만 삭제하는지 확인합니다."""
        patched_cache.enabled = True
        patched_cache.set = mock_cache.set
        patched_cache.get = mock_cache.get
        patched_cache.redis_client = mock_cache.redis_client

        svc = DailySignalCache()
        svc._cache = patched_cache

        for sig in sample_signals:
            svc.set_signal(sig)

        deleted = svc.invalidate_symbol("AAPL")
        assert deleted == 1

        # AAPL은 삭제, MSFT/TSLA는 남아있어야 함
        assert svc.get_signal("AAPL", "sideways_calm") is None
        assert svc.get_signal("MSFT", "sideways_calm") is not None


# ===========================================================================
# TestDailySignalCacheSummary
# ===========================================================================


class TestDailySignalCacheSummary:
    """DailySignalCache.get_summary 집계 로직 검증 테스트."""

    @patch("app.services.signal_cache.cache")
    def test_get_summary_with_signals(
        self, patched_cache: MagicMock, mock_cache: MagicMock, sample_signals: list[CachedSignal],
    ) -> None:
        """시그널이 캐시된 상태에서 get_summary가 올바른 집계를 반환하는지 확인합니다."""
        patched_cache.enabled = True
        patched_cache.set = mock_cache.set
        patched_cache.redis_client = mock_cache.redis_client

        svc = DailySignalCache()
        svc._cache = patched_cache

        for sig in sample_signals:
            svc.set_signal(sig)

        summary = svc.get_summary()
        assert summary.total_signals == 3
        assert summary.up_count == 1
        assert summary.neutral_count == 1
        assert summary.down_count == 1
        expected_avg = round((0.85 + 0.60 + 0.70) / 3, 4)
        assert summary.avg_confidence == expected_avg

    @patch("app.services.signal_cache.cache")
    def test_get_summary_empty(self, patched_cache: MagicMock, mock_cache: MagicMock) -> None:
        """캐시가 비어있을 때 get_summary가 기본 DailySignalSummary를 반환하는지 확인합니다."""
        patched_cache.enabled = True
        patched_cache.redis_client = mock_cache.redis_client

        svc = DailySignalCache()
        svc._cache = patched_cache

        summary = svc.get_summary()
        assert summary.total_signals == 0
        assert summary.signals == []
        assert summary.regime == "all"


# ===========================================================================
# TestCacheDisabled
# ===========================================================================


class TestCacheDisabled:
    """Cache가 비활성화된 상태의 동작 검증 테스트."""

    @patch("app.services.signal_cache.cache")
    def test_get_all_returns_empty(self, patched_cache: MagicMock) -> None:
        """cache.enabled=False일 때 get_all_signals가 빈 리스트를 반환하는지 확인합니다."""
        patched_cache.enabled = False

        svc = DailySignalCache()
        svc._cache = patched_cache

        result = svc.get_all_signals()
        assert result == []

    @patch("app.services.signal_cache.cache")
    def test_invalidate_returns_zero(self, patched_cache: MagicMock) -> None:
        """cache.enabled=False일 때 invalidate_all이 0을 반환하는지 확인합니다."""
        patched_cache.enabled = False

        svc = DailySignalCache()
        svc._cache = patched_cache

        result = svc.invalidate_all()
        assert result == 0
