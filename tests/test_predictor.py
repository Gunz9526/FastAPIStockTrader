from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

import numpy as np  # type: ignore[import-untyped]
import pandas as pd  # type: ignore[import-untyped]
import pytest  # type: ignore[import-untyped]

from app.ml.models import EnsembleClassifierWrapper, EnsembleWrapper
from app.ml.predictor import PredictorService
from app.services.regime import MarketRegime

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def sample_features() -> pd.DataFrame:
    """테스트용 단일 행 피처 DataFrame을 반환합니다."""
    return pd.DataFrame({"feat_a": [0.5], "feat_b": [1.2], "feat_c": [-0.3]})


@pytest.fixture()
def training_data() -> tuple[pd.DataFrame, pd.Series]:
    """테스트용 학습 데이터 (X, y) 튜플을 반환합니다."""
    X = pd.DataFrame(
        {
            "feat_a": np.random.rand(100),
            "feat_b": np.random.rand(100),
            "feat_c": np.random.rand(100),
        }
    )
    y = pd.Series(np.random.choice([0, 1, 2], size=100))
    return X, y


# ===========================================================================
# TestPredictorSingleton
# ===========================================================================


class TestPredictorSingleton:
    """PredictorService 싱글턴 패턴 검증 테스트."""

    def setup_method(self) -> None:
        """각 테스트 전 싱글턴 상태 초기화."""
        PredictorService._instance = None
        PredictorService._models = {}

    @patch("app.ml.predictor.os.path.exists", return_value=False)
    def test_singleton_returns_same_instance(self, _mock_exists: MagicMock) -> None:
        """두 번 생성해도 동일한 인스턴스를 반환하는지 확인합니다."""
        svc_a = PredictorService()
        svc_b = PredictorService()

        assert svc_a is svc_b

    @patch("app.ml.predictor.os.path.exists", return_value=False)
    def test_singleton_thread_safe(self, _mock_exists: MagicMock) -> None:
        """멀티스레드 환경에서도 동일 인스턴스를 반환하는지 확인합니다."""
        instances: list[PredictorService] = []

        def _create() -> None:
            instances.append(PredictorService())

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(_create) for _ in range(20)]
            for f in futures:
                f.result()

        assert len(set(id(i) for i in instances)) == 1


# ===========================================================================
# TestPredictClass
# ===========================================================================


class TestPredictClass:
    """predict_class() 메서드 테스트."""

    NEUTRAL_RESULT = (1, 0.33, {"DOWN": 0.33, "NEUTRAL": 0.34, "UP": 0.33})

    def setup_method(self) -> None:
        PredictorService._instance = None
        PredictorService._models = {}

    @patch("app.ml.predictor.os.path.exists", return_value=False)
    def test_predict_class_returns_valid_tuple(
        self, _mock_exists: MagicMock, sample_features: pd.DataFrame
    ) -> None:
        """반환값이 (int, float, dict) 형식인지 확인합니다."""
        svc = PredictorService()
        result = svc.predict_class(sample_features, MarketRegime.SIDEWAYS_CALM)

        assert isinstance(result, tuple) and len(result) == 3
        pred_cls, confidence, probs = result
        assert isinstance(pred_cls, int)
        assert isinstance(confidence, float)
        assert isinstance(probs, dict)
        assert set(probs.keys()) == {"DOWN", "NEUTRAL", "UP"}

    @patch("app.ml.predictor.os.path.exists", return_value=False)
    def test_predict_class_no_model_returns_neutral(
        self, _mock_exists: MagicMock, sample_features: pd.DataFrame
    ) -> None:
        """어떤 regime 모델도 없을 때 중립 결과 (1, 0.33, ...) 를 반환하는지 확인합니다."""
        svc = PredictorService()
        result = svc.predict_class(sample_features, MarketRegime.BULL_TRENDING)

        assert result == self.NEUTRAL_RESULT

    @patch("app.ml.predictor.os.path.exists", return_value=False)
    def test_predict_class_fallback_chain(
        self, _mock_exists: MagicMock, sample_features: pd.DataFrame
    ) -> None:
        """요청 regime 모델이 없을 때 fallback chain 으로 대체 모델을 사용하는지 확인합니다."""
        svc = PredictorService()

        # sideways_calm 모델만 로드 → sideways_volatile 요청 시 calm 으로 fallback
        mock_model = MagicMock(spec=EnsembleClassifierWrapper)
        mock_model.predict_proba.return_value = np.array([[0.2, 0.5, 0.3]])

        with svc._lock:
            svc._models[MarketRegime.SIDEWAYS_CALM] = mock_model

        pred_cls, confidence, probs = svc.predict_class(
            sample_features, MarketRegime.SIDEWAYS_VOLATILE
        )

        assert pred_cls == 1  # NEUTRAL (argmax of [0.2, 0.5, 0.3])
        assert confidence == pytest.approx(0.5, abs=1e-6)
        mock_model.predict_proba.assert_called_once()

    @patch("app.ml.predictor.os.path.exists", return_value=False)
    def test_predict_class_with_classifier(
        self, _mock_exists: MagicMock, sample_features: pd.DataFrame
    ) -> None:
        """EnsembleClassifierWrapper 모델을 통한 분류 결과가 올바른지 확인합니다."""
        svc = PredictorService()

        mock_model = MagicMock(spec=EnsembleClassifierWrapper)
        mock_model.predict_proba.return_value = np.array([[0.1, 0.3, 0.6]])

        with svc._lock:
            svc._models[MarketRegime.BULL_TRENDING] = mock_model

        pred_cls, confidence, probs = svc.predict_class(
            sample_features, MarketRegime.BULL_TRENDING
        )

        assert pred_cls == 2  # UP (argmax)
        assert confidence == pytest.approx(0.6, abs=1e-6)
        assert probs["DOWN"] == pytest.approx(0.1, abs=1e-6)
        assert probs["NEUTRAL"] == pytest.approx(0.3, abs=1e-6)
        assert probs["UP"] == pytest.approx(0.6, abs=1e-6)
        mock_model.predict_proba.assert_called_once()

    @patch("app.ml.predictor.os.path.exists", return_value=False)
    def test_predict_class_with_legacy_regressor(
        self, _mock_exists: MagicMock, sample_features: pd.DataFrame
    ) -> None:
        """레거시 EnsembleWrapper(회귀) 모델 사용 시 임계값 기반 분류를 확인합니다."""
        svc = PredictorService()

        # prediction > 0.001 → UP (class 2)
        mock_model = MagicMock(spec=EnsembleWrapper)
        mock_model.predict.return_value = np.array([0.05])

        with svc._lock:
            svc._models[MarketRegime.BEAR_TRENDING] = mock_model

        pred_cls, confidence, probs = svc.predict_class(
            sample_features, MarketRegime.BEAR_TRENDING
        )
        assert pred_cls == 2
        assert confidence == pytest.approx(0.6, abs=1e-6)
        assert probs["UP"] == pytest.approx(0.6, abs=1e-6)

        # prediction < -0.001 → DOWN (class 0)
        mock_model.predict.return_value = np.array([-0.05])
        pred_cls2, _, probs2 = svc.predict_class(
            sample_features, MarketRegime.BEAR_TRENDING
        )
        assert pred_cls2 == 0
        assert probs2["DOWN"] == pytest.approx(0.6, abs=1e-6)

    @patch("app.ml.predictor.os.path.exists", return_value=False)
    def test_predict_class_exception_returns_neutral(
        self, _mock_exists: MagicMock, sample_features: pd.DataFrame
    ) -> None:
        """모델이 예외를 발생시키면 중립 결과를 반환하는지 확인합니다."""
        svc = PredictorService()

        mock_model = MagicMock(spec=EnsembleClassifierWrapper)
        mock_model.predict_proba.side_effect = RuntimeError("boom")

        with svc._lock:
            svc._models[MarketRegime.SIDEWAYS_CALM] = mock_model

        result = svc.predict_class(sample_features, MarketRegime.SIDEWAYS_CALM)
        assert result == self.NEUTRAL_RESULT


# ===========================================================================
# TestPredictNext
# ===========================================================================


class TestPredictNext:
    """predict_next() 메서드 테스트."""

    def setup_method(self) -> None:
        PredictorService._instance = None
        PredictorService._models = {}

    @patch("app.ml.predictor.os.path.exists", return_value=False)
    def test_predict_next_returns_float(
        self, _mock_exists: MagicMock, sample_features: pd.DataFrame
    ) -> None:
        """반환값이 float 타입인지 확인합니다."""
        svc = PredictorService()

        mock_model = MagicMock(spec=EnsembleClassifierWrapper)
        mock_model.predict.return_value = np.array([0.72])

        with svc._lock:
            svc._models[MarketRegime.SIDEWAYS_CALM] = mock_model

        result = svc.predict_next(sample_features, MarketRegime.SIDEWAYS_CALM)
        assert isinstance(result, float)
        assert result == pytest.approx(0.72, abs=1e-6)

    @patch("app.ml.predictor.os.path.exists", return_value=False)
    def test_predict_next_no_model_returns_half(
        self, _mock_exists: MagicMock, sample_features: pd.DataFrame
    ) -> None:
        """모델이 없을 때 0.5를 반환하는지 확인합니다."""
        svc = PredictorService()
        result = svc.predict_next(sample_features, MarketRegime.BULL_TRENDING)
        assert result == 0.5

    @patch("app.ml.predictor.os.path.exists", return_value=False)
    def test_predict_next_exception_returns_half(
        self, _mock_exists: MagicMock, sample_features: pd.DataFrame
    ) -> None:
        """예측 중 예외 발생 시 0.5를 반환하는지 확인합니다."""
        svc = PredictorService()

        mock_model = MagicMock(spec=EnsembleClassifierWrapper)
        mock_model.predict.side_effect = ValueError("unexpected")

        with svc._lock:
            svc._models[MarketRegime.SIDEWAYS_VOLATILE] = mock_model

        result = svc.predict_next(sample_features, MarketRegime.SIDEWAYS_VOLATILE)
        assert result == 0.5


# ===========================================================================
# TestReloadModels
# ===========================================================================


class TestReloadModels:
    """reload_models() 메서드 테스트."""

    def setup_method(self) -> None:
        PredictorService._instance = None
        PredictorService._models = {}

    @patch("app.ml.predictor.EnsembleClassifierWrapper")
    @patch("app.ml.predictor.os.path.exists")
    def test_reload_models_from_disk(
        self,
        mock_exists: MagicMock,
        mock_clf_cls: MagicMock,
    ) -> None:
        """디스크에 classifier 파일이 존재하면 모델을 로드하는지 확인합니다."""
        # 초기 생성 시에는 파일 없음
        mock_exists.return_value = False
        svc = PredictorService()
        assert len(svc._models) == 0

        # reload 시에는 classifier 파일이 존재
        classifier_filenames = set(svc._classifier_map.values())

        def _exists_side_effect(path: str) -> bool:
            return any(path.endswith(fn) for fn in classifier_filenames)

        mock_exists.side_effect = _exists_side_effect

        mock_instance = MagicMock(spec=EnsembleClassifierWrapper)
        mock_clf_cls.return_value = mock_instance

        svc.reload_models()

        assert len(svc._models) == len(MarketRegime)
        for regime in MarketRegime:
            assert svc._models[regime] is mock_instance

    @patch("app.ml.predictor.os.path.exists", return_value=False)
    def test_reload_models_empty_dir(self, _mock_exists: MagicMock) -> None:
        """모델 파일이 없으면 빈 dict가 되는지 확인합니다."""
        svc = PredictorService()
        svc.reload_models()

        assert svc._models == {}


# ===========================================================================
# TestRetrain
# ===========================================================================


class TestRetrain:
    """retrain() 메서드 테스트."""

    def setup_method(self) -> None:
        PredictorService._instance = None
        PredictorService._models = {}

    @patch("app.ml.predictor.os.makedirs")
    @patch("app.ml.predictor.EnsembleClassifierWrapper")
    @patch("app.ml.predictor.os.path.exists", return_value=False)
    def test_retrain_success(
        self,
        _mock_exists: MagicMock,
        mock_clf_cls: MagicMock,
        _mock_makedirs: MagicMock,
        training_data: tuple[pd.DataFrame, pd.Series],
    ) -> None:
        """retrain 성공 시 True를 반환하고 모델이 교체되는지 확인합니다."""
        svc = PredictorService()
        X, y = training_data

        mock_model = MagicMock(spec=EnsembleClassifierWrapper)
        mock_clf_cls.return_value = mock_model

        regime = MarketRegime.BULL_TRENDING
        result = svc.retrain(X, y, regime=regime)

        assert result is True
        mock_model.train.assert_called_once_with(X, y)
        mock_model.save.assert_called_once()
        assert svc._models[regime] is mock_model

    @patch("app.ml.predictor.EnsembleClassifierWrapper")
    @patch("app.ml.predictor.os.path.exists", return_value=False)
    def test_retrain_failure(
        self,
        _mock_exists: MagicMock,
        mock_clf_cls: MagicMock,
        training_data: tuple[pd.DataFrame, pd.Series],
    ) -> None:
        """retrain 중 예외 발생 시 False를 반환하는지 확인합니다."""
        svc = PredictorService()
        X, y = training_data

        mock_clf_cls.return_value.train.side_effect = RuntimeError("train failed")

        result = svc.retrain(X, y, regime=MarketRegime.BEAR_TRENDING)

        assert result is False
        assert MarketRegime.BEAR_TRENDING not in svc._models


# ===========================================================================
# TestGetModelInfo
# ===========================================================================


class TestGetModelInfo:
    """get_model_info() 메서드 테스트."""

    def setup_method(self) -> None:
        PredictorService._instance = None
        PredictorService._models = {}

    @patch("app.ml.predictor.os.path.exists", return_value=False)
    def test_get_model_info(self, _mock_exists: MagicMock) -> None:
        """로드된 모델 메타데이터를 올바른 형식으로 반환하는지 확인합니다."""
        svc = PredictorService()

        mock_model = MagicMock(spec=EnsembleClassifierWrapper)
        mock_model.metadata = {"trained_at": "2026-02-26", "n_samples": 5000}

        with svc._lock:
            svc._models[MarketRegime.BULL_TRENDING] = mock_model

        info = svc.get_model_info()

        assert MarketRegime.BULL_TRENDING.value in info
        entry = info[MarketRegime.BULL_TRENDING.value]
        assert entry["loaded"] is True
        assert entry["metadata"]["trained_at"] == "2026-02-26"
        assert entry["metadata"]["n_samples"] == 5000

        # 로드되지 않은 regime은 결과에 미포함
        assert MarketRegime.BEAR_TRENDING.value not in info
