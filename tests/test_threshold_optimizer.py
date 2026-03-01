"""Adaptive Threshold Optimizer 단위 테스트.

``app.ml.threshold_optimizer`` 모듈의 ``ThresholdResult``, 순수 헬퍼 함수,
``AdaptiveThresholdOptimizer`` 의 최적화·저장·로드 메서드, 그리고
``run_threshold_optimization`` 진입점에 대한 포괄적 테스트를 제공한다.

optuna/catboost 의존 메서드는 ``unittest.mock`` 으로 mock 처리하여
빠르고 결정적인 테스트를 보장한다.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np  # type: ignore[import-untyped]
import pandas as pd  # type: ignore[import-untyped]
import pytest  # type: ignore[import-untyped]

from app.ml.threshold_optimizer import (
    AdaptiveThresholdOptimizer,
    ThresholdResult,
    _CONFIDENCE_RANGE,
    _THETA_RANGE,
    _class_distribution,
    _composite_score,
    _label_returns,
    _min_class_fraction,
    run_threshold_optimization,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def sample_result() -> ThresholdResult:
    """완전한 ThresholdResult 인스턴스 fixture."""
    return ThresholdResult(
        regime="bull_trending",
        optimal_theta=0.007,
        optimal_confidence=0.50,
        theta_composite_score=0.85,
        confidence_trade_score=0.72,
        n_samples=500,
        theta_search_range=_THETA_RANGE,
        confidence_search_range=_CONFIDENCE_RANGE,
        class_distribution={0: 100, 1: 300, 2: 100},
    )


@pytest.fixture()
def optimizer(tmp_path: Path) -> AdaptiveThresholdOptimizer:
    """임시 디렉토리를 사용하는 AdaptiveThresholdOptimizer fixture."""
    return AdaptiveThresholdOptimizer(output_dir=str(tmp_path))


@pytest.fixture()
def two_regime_results() -> dict[str, ThresholdResult]:
    """2개 레짐 결과 dict fixture."""
    return {
        "bull_trending": ThresholdResult(
            regime="bull_trending",
            optimal_theta=0.006,
            optimal_confidence=0.45,
            theta_composite_score=0.88,
            confidence_trade_score=0.70,
            n_samples=400,
            theta_search_range=_THETA_RANGE,
            confidence_search_range=_CONFIDENCE_RANGE,
            class_distribution={0: 80, 1: 240, 2: 80},
        ),
        "bear_trending": ThresholdResult(
            regime="bear_trending",
            optimal_theta=0.010,
            optimal_confidence=0.55,
            theta_composite_score=0.75,
            confidence_trade_score=0.60,
            n_samples=300,
            theta_search_range=_THETA_RANGE,
            confidence_search_range=_CONFIDENCE_RANGE,
            class_distribution={0: 120, 1: 100, 2: 80},
        ),
    }


# =========================================================================
# TestThresholdResult
# =========================================================================


class TestThresholdResult:
    """ThresholdResult 데이터클래스 단위 테스트."""

    def test_dataclass_creation(self, sample_result: ThresholdResult) -> None:
        """모든 필드를 지정해 생성하면 값이 올바르게 저장된다."""
        assert sample_result.regime == "bull_trending"
        assert sample_result.optimal_theta == 0.007
        assert sample_result.optimal_confidence == 0.50
        assert sample_result.theta_composite_score == 0.85
        assert sample_result.confidence_trade_score == 0.72
        assert sample_result.n_samples == 500
        assert sample_result.theta_search_range == _THETA_RANGE
        assert sample_result.confidence_search_range == _CONFIDENCE_RANGE
        assert sample_result.class_distribution == {0: 100, 1: 300, 2: 100}

    def test_dataclass_asdict(self, sample_result: ThresholdResult) -> None:
        """dataclasses.asdict() 로 dict 변환이 올바르게 동작한다."""
        d = dataclasses.asdict(sample_result)
        assert isinstance(d, dict)
        assert d["regime"] == "bull_trending"
        assert d["optimal_theta"] == 0.007
        assert d["class_distribution"] == {0: 100, 1: 300, 2: 100}

    def test_default_values_absent(self) -> None:
        """모든 필드가 필수값이므로 인자 없이 생성하면 TypeError 발생."""
        with pytest.raises(TypeError):
            ThresholdResult()  # type: ignore[call-arg]


# =========================================================================
# TestCompositeScore
# =========================================================================


class TestCompositeScore:
    """_composite_score 순수 함수 단위 테스트."""

    def test_perfect_predictions(self) -> None:
        """모든 예측이 정확하면 composite ≈ 1.0 이어야 한다."""
        y_true = np.array([0, 1, 2, 0, 1, 2])
        y_pred = np.array([0, 1, 2, 0, 1, 2])
        composite, acc, f1, class_balance, min_recall = _composite_score(
            y_true, y_pred,
        )
        assert acc == pytest.approx(1.0)
        assert f1 == pytest.approx(1.0)
        assert class_balance == pytest.approx(1.0)
        assert min_recall == pytest.approx(1.0)
        assert composite == pytest.approx(1.0)

    def test_all_wrong_predictions(self) -> None:
        """모든 예측이 틀리면 composite 가 낮아야 한다."""
        y_true = np.array([0, 0, 0, 1, 1, 1, 2, 2, 2])
        # 체계적으로 전부 오답
        y_pred = np.array([1, 1, 1, 2, 2, 2, 0, 0, 0])
        composite, acc, f1, class_balance, min_recall = _composite_score(
            y_true, y_pred,
        )
        assert acc == pytest.approx(0.0)
        assert min_recall == pytest.approx(0.0)
        assert composite < 0.3

    def test_single_class_only(self) -> None:
        """하나의 클래스만 예측하면 class_balance ≈ 0.333 이다."""
        y_true = np.array([0, 1, 2, 0, 1, 2])
        y_pred = np.array([1, 1, 1, 1, 1, 1])
        composite, acc, f1, class_balance, min_recall = _composite_score(
            y_true, y_pred,
        )
        assert class_balance == pytest.approx(1.0 / 3.0, abs=0.01)

    def test_return_tuple_structure(self) -> None:
        """반환값이 5개 요소의 tuple 이어야 한다."""
        y_true = np.array([0, 1, 2])
        y_pred = np.array([0, 1, 2])
        result = _composite_score(y_true, y_pred)
        assert isinstance(result, tuple)
        assert len(result) == 5


# =========================================================================
# TestLabelReturns
# =========================================================================


class TestLabelReturns:
    """_label_returns 순수 함수 단위 테스트."""

    def test_basic_labeling(self) -> None:
        """기본 라벨링: returns=[0.01, -0.01, 0.001], theta=0.005 → [2, 0, 1]."""
        returns = np.array([0.01, -0.01, 0.001])
        labels = _label_returns(returns, theta=0.005)
        np.testing.assert_array_equal(labels, [2, 0, 1])

    def test_zero_theta(self) -> None:
        """theta=0 이면 양수는 UP(2), 음수는 DOWN(0), 0.0 은 NEUTRAL(1)."""
        returns = np.array([0.05, -0.03, 0.0])
        labels = _label_returns(returns, theta=0.0)
        # 0.05 > 0 → UP, -0.03 < 0 → DOWN, 0.0은 > 0도 < 0도 아니므로 NEUTRAL
        np.testing.assert_array_equal(labels, [2, 0, 1])

    def test_large_theta(self) -> None:
        """theta=1.0 (매우 큼) 이면 거의 모든 값이 NEUTRAL(1)."""
        returns = np.array([0.01, -0.01, 0.05, -0.05, 0.0])
        labels = _label_returns(returns, theta=1.0)
        np.testing.assert_array_equal(labels, [1, 1, 1, 1, 1])


# =========================================================================
# TestClassDistribution
# =========================================================================


class TestClassDistribution:
    """_class_distribution 순수 함수 단위 테스트."""

    def test_balanced_distribution(self) -> None:
        """각 클래스가 동일 개수인 경우 정확히 세어야 한다."""
        y = np.array([0, 1, 2, 0, 1, 2])
        dist = _class_distribution(y)
        assert dist == {0: 2, 1: 2, 2: 2}

    def test_empty_array(self) -> None:
        """빈 배열 → {0: 0, 1: 0, 2: 0}."""
        y = np.array([], dtype=int)
        dist = _class_distribution(y)
        assert dist == {0: 0, 1: 0, 2: 0}


# =========================================================================
# TestMinClassFraction
# =========================================================================


class TestMinClassFraction:
    """_min_class_fraction 순수 함수 단위 테스트."""

    def test_balanced(self) -> None:
        """균형 데이터에서 최소 클래스 비율 ≈ 1/3."""
        y = np.array([0, 1, 2, 0, 1, 2, 0, 1, 2])
        frac = _min_class_fraction(y)
        assert frac == pytest.approx(1.0 / 3.0)

    def test_empty(self) -> None:
        """빈 배열이면 0.0 을 반환해야 한다."""
        y = np.array([], dtype=int)
        assert _min_class_fraction(y) == 0.0


# =========================================================================
# TestOptimizeClassificationThreshold
# =========================================================================


class TestOptimizeClassificationThreshold:
    """optimize_classification_threshold 메서드 단위 테스트 (optuna/catboost mock)."""

    @pytest.fixture()
    def _mock_study(self) -> MagicMock:
        """best_params 와 best_value 를 갖는 mock Optuna Study."""
        study = MagicMock()
        study.best_params = {"theta": 0.008}
        study.best_value = 0.82
        return study

    def test_basic_optimization(
        self,
        tmp_path: Path,
        _mock_study: MagicMock,
    ) -> None:
        """mock optuna study 로 반환 dict 의 키를 검증한다."""
        optimizer = AdaptiveThresholdOptimizer(output_dir=str(tmp_path))
        returns = np.random.default_rng(42).normal(0, 0.01, size=200)
        X = pd.DataFrame(
            np.random.default_rng(42).standard_normal((200, 5)),
            columns=[f"f{i}" for i in range(5)],
        )

        with patch("app.ml.threshold_optimizer.optuna") as mock_optuna:
            mock_optuna.create_study.return_value = _mock_study
            mock_optuna.Trial = MagicMock()
            mock_optuna.samplers.TPESampler.return_value = MagicMock()
            mock_optuna.pruners.MedianPruner.return_value = MagicMock()

            result = optimizer.optimize_classification_threshold(
                returns=returns, X=X, regime="bull_trending",
            )

        assert "theta" in result
        assert "composite_score" in result
        assert "class_distribution" in result
        assert result["theta"] == 0.008
        assert result["composite_score"] == 0.82

    def test_with_class_weights(
        self,
        tmp_path: Path,
        _mock_study: MagicMock,
    ) -> None:
        """class_weights 를 전달해도 정상 동작해야 한다."""
        optimizer = AdaptiveThresholdOptimizer(output_dir=str(tmp_path))
        returns = np.random.default_rng(7).normal(0, 0.01, size=150)
        X = pd.DataFrame(
            np.random.default_rng(7).standard_normal((150, 3)),
            columns=["a", "b", "c"],
        )
        class_weights = {0: 1.5, 1: 1.0, 2: 1.5}

        with patch("app.ml.threshold_optimizer.optuna") as mock_optuna:
            mock_optuna.create_study.return_value = _mock_study
            mock_optuna.samplers.TPESampler.return_value = MagicMock()
            mock_optuna.pruners.MedianPruner.return_value = MagicMock()

            result = optimizer.optimize_classification_threshold(
                returns=returns,
                X=X,
                regime="bear_trending",
                class_weights=class_weights,
            )

        assert result["theta"] == 0.008
        # study.optimize 가 호출되었는지 검증
        _mock_study.optimize.assert_called_once()


# =========================================================================
# TestOptimizeConfidenceThreshold
# =========================================================================


class TestOptimizeConfidenceThreshold:
    """optimize_confidence_threshold 메서드 단위 테스트 (optuna mock)."""

    @pytest.fixture()
    def _mock_model(self) -> MagicMock:
        """predict / predict_proba 인터페이스를 갖는 mock 모델."""
        model = MagicMock()
        n = 100
        rng = np.random.default_rng(99)
        proba = rng.dirichlet([1, 1, 1], size=n)
        preds = proba.argmax(axis=1)
        model.predict_proba.return_value = proba
        model.predict.return_value = preds
        return model

    def test_basic_confidence_optimization(
        self,
        tmp_path: Path,
        _mock_model: MagicMock,
    ) -> None:
        """mock optuna study 로 반환 dict 키를 검증한다."""
        optimizer = AdaptiveThresholdOptimizer(output_dir=str(tmp_path))
        n = 100
        rng = np.random.default_rng(99)
        X = pd.DataFrame(rng.standard_normal((n, 4)), columns=list("abcd"))
        y = rng.integers(0, 3, size=n)

        mock_study = MagicMock()
        mock_study.best_params = {"confidence": 0.50}
        mock_study.best_value = 0.65

        with patch("app.ml.threshold_optimizer.optuna") as mock_optuna:
            mock_optuna.create_study.return_value = mock_study
            mock_optuna.samplers.TPESampler.return_value = MagicMock()

            result = optimizer.optimize_confidence_threshold(
                model=_mock_model, X=X, y=y, regime="sideways_volatile",
            )

        assert "confidence" in result
        assert "trade_score" in result
        assert "acted_accuracy" in result
        assert "coverage" in result

    def test_zero_coverage(self, tmp_path: Path) -> None:
        """모든 예측의 신뢰도가 임계값 미만이면 trade_score=0."""
        optimizer = AdaptiveThresholdOptimizer(output_dir=str(tmp_path))
        n = 50
        rng = np.random.default_rng(10)
        X = pd.DataFrame(rng.standard_normal((n, 3)), columns=list("xyz"))
        y = np.ones(n, dtype=int)  # 전부 NEUTRAL

        # 모든 max_proba < 0.35 이 되도록 균일 분포 확률
        proba = np.full((n, 3), 1.0 / 3.0)
        model = MagicMock()
        model.predict_proba.return_value = proba
        model.predict.return_value = np.ones(n, dtype=int)  # 전부 NEUTRAL(1) → acted_mask 없음

        mock_study = MagicMock()
        mock_study.best_params = {"confidence": 0.60}
        mock_study.best_value = 0.0

        with patch("app.ml.threshold_optimizer.optuna") as mock_optuna:
            mock_optuna.create_study.return_value = mock_study
            mock_optuna.samplers.TPESampler.return_value = MagicMock()

            result = optimizer.optimize_confidence_threshold(
                model=model, X=X, y=y, regime="sideways_calm",
            )

        # pred_classes 가 전부 1(NEUTRAL) 이므로 acted_mask 는 전부 False
        assert result["trade_score"] == 0.0
        assert result["coverage"] == 0.0


# =========================================================================
# TestSaveLoadThresholds
# =========================================================================


class TestSaveLoadThresholds:
    """save_thresholds / load_thresholds 라운드트립 테스트."""

    def test_save_and_load_roundtrip(
        self,
        optimizer: AdaptiveThresholdOptimizer,
        two_regime_results: dict[str, ThresholdResult],
        tmp_path: Path,
    ) -> None:
        """저장 후 로드하면 동일한 데이터가 복원되어야 한다."""
        saved_path = optimizer.save_thresholds(two_regime_results)
        assert saved_path.exists()

        loaded = optimizer.load_thresholds()
        assert set(loaded.keys()) == set(two_regime_results.keys())

        for regime in two_regime_results:
            orig = two_regime_results[regime]
            restored = loaded[regime]
            assert restored.regime == orig.regime
            assert restored.optimal_theta == pytest.approx(orig.optimal_theta)
            assert restored.optimal_confidence == pytest.approx(
                orig.optimal_confidence,
            )
            assert restored.theta_composite_score == pytest.approx(
                orig.theta_composite_score,
            )
            assert restored.confidence_trade_score == pytest.approx(
                orig.confidence_trade_score,
            )
            assert restored.n_samples == orig.n_samples
            assert restored.class_distribution == orig.class_distribution

    def test_load_missing_file(self, tmp_path: Path) -> None:
        """존재하지 않는 파일 로드 시 FileNotFoundError 가 발생해야 한다."""
        optimizer = AdaptiveThresholdOptimizer(output_dir=str(tmp_path))
        with pytest.raises(FileNotFoundError):
            optimizer.load_thresholds()

    def test_save_creates_directory(self, tmp_path: Path) -> None:
        """저장 경로의 디렉토리가 자동 생성되어야 한다."""
        nested = tmp_path / "deep" / "nested" / "dir"
        target = nested / "thresholds.json"
        assert not nested.exists()

        optimizer = AdaptiveThresholdOptimizer(output_dir=str(tmp_path))
        result = ThresholdResult(
            regime="sideways_calm",
            optimal_theta=0.004,
            optimal_confidence=0.40,
            theta_composite_score=0.70,
            confidence_trade_score=0.50,
            n_samples=100,
            theta_search_range=_THETA_RANGE,
            confidence_search_range=_CONFIDENCE_RANGE,
            class_distribution={0: 30, 1: 40, 2: 30},
        )
        saved = optimizer.save_thresholds(
            {"sideways_calm": result}, path=target,
        )
        assert saved == target
        assert target.exists()

        # JSON 파싱 가능 여부 검증
        with open(target, encoding="utf-8") as fp:
            data = json.load(fp)
        assert "regimes" in data
        assert "sideways_calm" in data["regimes"]


# =========================================================================
# TestRunThresholdOptimization
# =========================================================================


class TestRunThresholdOptimization:
    """run_threshold_optimization 진입점 단위 테스트."""

    def test_loads_existing_file(self, tmp_path: Path) -> None:
        """기존 파일이 있으면 로드하여 반환해야 한다."""
        expected = {
            "bull_trending": ThresholdResult(
                regime="bull_trending",
                optimal_theta=0.006,
                optimal_confidence=0.45,
                theta_composite_score=0.88,
                confidence_trade_score=0.70,
                n_samples=400,
                theta_search_range=_THETA_RANGE,
                confidence_search_range=_CONFIDENCE_RANGE,
                class_distribution={0: 80, 1: 240, 2: 80},
            ),
        }
        with patch(
            "app.ml.threshold_optimizer.AdaptiveThresholdOptimizer.load_thresholds",
            return_value=expected,
        ):
            result = run_threshold_optimization(output_dir=str(tmp_path))
        assert result == expected

    def test_returns_empty_on_no_file(self, tmp_path: Path) -> None:
        """파일이 없으면 빈 dict 를 반환해야 한다."""
        result = run_threshold_optimization(output_dir=str(tmp_path))
        assert result == {}
