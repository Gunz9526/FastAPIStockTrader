"""SHAP Feature Importance Analyzer 단위 테스트.

``app.ml.shap_analyzer`` 모듈의 ``SHAPResult``, ``SHAPFeatureSelector``,
``run_shap_analysis`` 등 주요 클래스 및 함수에 대한 포괄적 테스트를 제공한다.

shap 패키지가 설치되지 않은 환경에서도 모든 테스트가 실행될 수 있도록
``shap`` 모듈을 항상 mock 처리한다.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np  # type: ignore[import-untyped]
import pandas as pd  # type: ignore[import-untyped]
import pytest  # type: ignore[import-untyped]

from app.ml.shap_analyzer import (
    SHAPFeatureSelector,
    SHAPResult,
    _PROTECTED_FEATURES,
    run_shap_analysis,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_SAMPLE_FEATURES: list[str] = [
    "rsi", "macd", "bb_width", "sector_id", "volume_ratio",
    "atr_pct", "adx", "momentum_5",
]
"""테스트용 축소 피처 목록 (8개)."""


@pytest.fixture()
def sample_features() -> list[str]:
    """축소된 피처 이름 목록 fixture."""
    return list(_SAMPLE_FEATURES)


@pytest.fixture()
def sample_global_importance() -> dict[str, float]:
    """글로벌 중요도 dict fixture (내림차순 아님)."""
    return {
        "rsi": 0.15,
        "macd": 0.08,
        "bb_width": 0.04,
        "sector_id": 0.002,
        "volume_ratio": 0.003,
        "atr_pct": 0.12,
        "adx": 0.009,
        "momentum_5": 0.05,
    }


@pytest.fixture()
def sample_per_class_importance() -> dict[str, dict[str, float]]:
    """클래스별 중요도 dict fixture."""
    return {
        "DOWN": {f: 0.01 * (i + 1) for i, f in enumerate(_SAMPLE_FEATURES)},
        "NEUTRAL": {f: 0.005 * (i + 1) for i, f in enumerate(_SAMPLE_FEATURES)},
        "UP": {f: 0.02 * (i + 1) for i, f in enumerate(_SAMPLE_FEATURES)},
    }


@pytest.fixture()
def sample_shap_summary() -> dict[str, dict[str, float]]:
    """SHAP directional summary fixture."""
    return {
        f: {"mean": 0.01 * i, "std": 0.005, "min": -0.1, "max": 0.2}
        for i, f in enumerate(_SAMPLE_FEATURES)
    }


@pytest.fixture()
def sample_result(
    sample_features: list[str],
    sample_global_importance: dict[str, float],
    sample_per_class_importance: dict[str, dict[str, float]],
    sample_shap_summary: dict[str, dict[str, float]],
) -> SHAPResult:
    """완전한 SHAPResult 인스턴스 fixture."""
    return SHAPResult(
        feature_names=sample_features,
        global_importance=sample_global_importance,
        per_class_importance=sample_per_class_importance,
        shap_values_summary=sample_shap_summary,
        n_samples=100,
        regime="bull_trending",
    )


@pytest.fixture()
def selector(tmp_path: Path) -> SHAPFeatureSelector:
    """SHAPFeatureSelector 인스턴스 fixture (임시 디렉토리 사용)."""
    return SHAPFeatureSelector(
        model_artifacts_path=str(tmp_path),
        shap_sample_size=50,
    )


@pytest.fixture()
def sample_dataframe() -> pd.DataFrame:
    """테스트용 DataFrame (200행, 8칼럼)."""
    rng = np.random.default_rng(42)
    data = {f: rng.standard_normal(200) for f in _SAMPLE_FEATURES}
    data["sector_id"] = rng.integers(0, 10, size=200).astype(float)
    return pd.DataFrame(data)


# ---------------------------------------------------------------------------
# 1. TestSHAPResult — 데이터 컨테이너 검증
# ---------------------------------------------------------------------------

class TestSHAPResult:
    """SHAPResult 데이터클래스의 생성, 직렬화, 엣지케이스를 검증한다."""

    def test_shap_result_creation(self, sample_result: SHAPResult) -> None:
        """SHAPResult 를 유효 데이터로 생성하면 모든 필드에 접근 가능해야 한다."""
        assert sample_result.feature_names == _SAMPLE_FEATURES
        assert isinstance(sample_result.global_importance, dict)
        assert len(sample_result.global_importance) == len(_SAMPLE_FEATURES)
        assert isinstance(sample_result.per_class_importance, dict)
        assert set(sample_result.per_class_importance.keys()) == {"DOWN", "NEUTRAL", "UP"}
        assert isinstance(sample_result.shap_values_summary, dict)
        assert sample_result.n_samples == 100
        assert sample_result.regime == "bull_trending"

    def test_shap_result_to_dict(self, sample_result: SHAPResult) -> None:
        """dataclasses.asdict 로 직렬화하면 JSON-serializable dict가 반환되어야 한다."""
        d = dataclasses.asdict(sample_result)

        # 최상위 키 확인
        expected_keys = {
            "feature_names", "global_importance", "per_class_importance",
            "shap_values_summary", "n_samples", "regime",
        }
        assert set(d.keys()) == expected_keys

        # JSON 직렬화 가능 여부
        serialized = json.dumps(d)
        assert isinstance(serialized, str)

        # 라운드트립 값 검증
        restored = json.loads(serialized)
        assert restored["regime"] == "bull_trending"
        assert restored["n_samples"] == 100
        assert len(restored["feature_names"]) == len(_SAMPLE_FEATURES)

    def test_shap_result_empty_features(self) -> None:
        """피처 목록이 빈 경우에도 유효한 (비어 있는) 결과가 생성되어야 한다."""
        result = SHAPResult(
            feature_names=[],
            global_importance={},
            per_class_importance={"DOWN": {}, "NEUTRAL": {}, "UP": {}},
            shap_values_summary={},
            n_samples=0,
            regime="unknown",
        )
        assert result.feature_names == []
        assert result.global_importance == {}
        assert result.n_samples == 0

        d = dataclasses.asdict(result)
        assert d["feature_names"] == []
        assert json.dumps(d)  # 직렬화 가능


# ---------------------------------------------------------------------------
# 2. TestSelectFeatures — 피처 선택 로직
# ---------------------------------------------------------------------------

class TestSelectFeatures:
    """SHAPFeatureSelector.select_features 메서드의 임계값·top-k 로직을 검증한다."""

    def test_select_above_threshold(
        self, selector: SHAPFeatureSelector, sample_result: SHAPResult,
    ) -> None:
        """global_importance >= 0.01 인 피처만 선택되어야 한다."""
        selected = selector.select_features(sample_result, min_importance=0.01)

        for feat in selected:
            assert sample_result.global_importance[feat] >= 0.01

        # 실제로 0.01 이상인 피처들: rsi(0.15), atr_pct(0.12), macd(0.08),
        # momentum_5(0.05), bb_width(0.04)
        expected_above = {
            f for f, v in sample_result.global_importance.items() if v >= 0.01
        }
        assert set(selected) == expected_above

    def test_select_below_threshold_excluded(
        self, selector: SHAPFeatureSelector, sample_result: SHAPResult,
    ) -> None:
        """global_importance < min_importance 인 피처는 제외되어야 한다."""
        selected = selector.select_features(sample_result, min_importance=0.01)

        below = {
            f for f, v in sample_result.global_importance.items() if v < 0.01
        }
        for feat in below:
            assert feat not in selected

    def test_select_top_k(
        self, selector: SHAPFeatureSelector, sample_result: SHAPResult,
    ) -> None:
        """top_k=3 이면 최대 3개 피처가 반환되어야 한다."""
        selected = selector.select_features(
            sample_result, min_importance=0.0, top_k=3,
        )
        assert len(selected) == 3

        # 내림차순 정렬 확인 — 상위 3개는 rsi, atr_pct, macd
        ranked = sorted(
            sample_result.global_importance.items(),
            key=lambda kv: kv[1], reverse=True,
        )
        expected_top3 = [f for f, _ in ranked[:3]]
        assert selected == expected_top3

    def test_select_sector_id_can_be_excluded(
        self, selector: SHAPFeatureSelector, sample_result: SHAPResult,
    ) -> None:
        """select_features 는 sector_id 를 보호하지 않으므로 임계값 미달 시 제외된다.

        Note: 보호(protection)는 get_removal_candidates 에서만 적용된다.
        """
        # sector_id importance = 0.002, threshold = 0.01 → 제외
        selected = selector.select_features(sample_result, min_importance=0.01)
        assert "sector_id" not in selected

    def test_select_empty_result(self, selector: SHAPFeatureSelector) -> None:
        """모든 피처 중요도가 0이면 빈 리스트가 반환되어야 한다."""
        result = SHAPResult(
            feature_names=["a", "b", "c"],
            global_importance={"a": 0.0, "b": 0.0, "c": 0.0},
            per_class_importance={},
            shap_values_summary={},
            n_samples=10,
            regime="test",
        )
        selected = selector.select_features(result, min_importance=0.01)
        assert selected == []


# ---------------------------------------------------------------------------
# 3. TestRemovalCandidates — 제거 후보 피처 검증
# ---------------------------------------------------------------------------

class TestRemovalCandidates:
    """SHAPFeatureSelector.get_removal_candidates 메서드를 검증한다."""

    def test_removal_below_threshold(
        self, selector: SHAPFeatureSelector, sample_result: SHAPResult,
    ) -> None:
        """threshold(0.005) 미만 피처가 제거 후보에 포함되어야 한다."""
        candidates = selector.get_removal_candidates(sample_result, threshold=0.005)

        # sector_id(0.002)는 보호, volume_ratio(0.003)만 해당
        assert "volume_ratio" in candidates

    def test_removal_sector_id_never_included(
        self, selector: SHAPFeatureSelector, sample_result: SHAPResult,
    ) -> None:
        """sector_id 는 중요도가 0 이더라도 제거 후보에 포함되지 않아야 한다."""
        # sector_id importance = 0.002 → threshold=1.0 이면 모든 피처 포함 가능하지만
        # _PROTECTED_FEATURES 에 들어 있으므로 제외
        candidates = selector.get_removal_candidates(sample_result, threshold=1.0)
        assert "sector_id" not in candidates
        assert "sector_id" in _PROTECTED_FEATURES

    def test_removal_all_above_threshold(
        self, selector: SHAPFeatureSelector,
    ) -> None:
        """모든 피처가 threshold 이상이면 빈 리스트가 반환되어야 한다."""
        result = SHAPResult(
            feature_names=["rsi", "macd"],
            global_importance={"rsi": 0.1, "macd": 0.05},
            per_class_importance={},
            shap_values_summary={},
            n_samples=50,
            regime="test",
        )
        candidates = selector.get_removal_candidates(result, threshold=0.005)
        assert candidates == []

    def test_removal_custom_threshold(
        self, selector: SHAPFeatureSelector, sample_result: SHAPResult,
    ) -> None:
        """threshold 값에 따라 제거 후보가 달라져야 한다."""
        # threshold=0.01 → sector_id(0.002, 보호), volume_ratio(0.003), adx(0.009)
        candidates_low = selector.get_removal_candidates(sample_result, threshold=0.01)
        # threshold=0.10 → 더 많은 피처 포함
        candidates_high = selector.get_removal_candidates(sample_result, threshold=0.10)

        assert len(candidates_high) > len(candidates_low)

        # adx(0.009) — threshold=0.01 에서 포함
        assert "adx" in candidates_low
        # bb_width(0.04) — threshold=0.10 에서 포함
        assert "bb_width" in candidates_high


# ---------------------------------------------------------------------------
# 4. TestNormaliseShapOutput — SHAP 출력 정규화
# ---------------------------------------------------------------------------

class TestNormaliseShapOutput:
    """SHAPFeatureSelector._normalise_shap_output 의 다양한 입력 형식 처리를 검증한다."""

    N_SAMPLES = 10
    N_FEATURES = 5
    N_CLASSES = 3

    def test_list_of_arrays_3_classes(self) -> None:
        """list[ndarray] 형식 (클래스별 배열) → (n, f, c) 3D 출력."""
        raw = [
            np.ones((self.N_SAMPLES, self.N_FEATURES)) * (c + 1)
            for c in range(self.N_CLASSES)
        ]
        result = SHAPFeatureSelector._normalise_shap_output(
            raw, self.N_FEATURES, self.N_CLASSES,
        )
        assert result.shape == (self.N_SAMPLES, self.N_FEATURES, self.N_CLASSES)
        # 클래스 0 → 값 1.0, 클래스 2 → 값 3.0
        np.testing.assert_allclose(result[:, :, 0], 1.0)
        np.testing.assert_allclose(result[:, :, 2], 3.0)

    def test_3d_array_passthrough(self) -> None:
        """이미 (n, f, c) 형태의 3D 배열이면 그대로 반환되어야 한다."""
        raw = np.random.default_rng(0).standard_normal(
            (self.N_SAMPLES, self.N_FEATURES, self.N_CLASSES),
        )
        result = SHAPFeatureSelector._normalise_shap_output(
            raw, self.N_FEATURES, self.N_CLASSES,
        )
        assert result.shape == (self.N_SAMPLES, self.N_FEATURES, self.N_CLASSES)
        np.testing.assert_array_equal(result, raw)

    def test_2d_broadcast(self) -> None:
        """(n, f) 2D 배열 → n_classes 만큼 복제하여 (n, f, c) 로 확장해야 한다."""
        raw = np.ones((self.N_SAMPLES, self.N_FEATURES)) * 0.5
        result = SHAPFeatureSelector._normalise_shap_output(
            raw, self.N_FEATURES, self.N_CLASSES,
        )
        assert result.shape == (self.N_SAMPLES, self.N_FEATURES, self.N_CLASSES)
        # 모든 클래스 슬라이스가 동일해야 한다
        for c in range(self.N_CLASSES):
            np.testing.assert_allclose(result[:, :, c], 0.5)

    def test_list_wrong_length_padded(self) -> None:
        """list 길이가 n_classes와 다르면 잘라내거나 0으로 패딩해야 한다."""
        # 2개만 주어졌을 때 → padding
        raw_short = [
            np.ones((self.N_SAMPLES, self.N_FEATURES)) for _ in range(2)
        ]
        result = SHAPFeatureSelector._normalise_shap_output(
            raw_short, self.N_FEATURES, self.N_CLASSES,
        )
        assert result.shape == (self.N_SAMPLES, self.N_FEATURES, self.N_CLASSES)
        # 3번째 클래스는 0으로 패딩
        np.testing.assert_allclose(result[:, :, 2], 0.0)

        # 4개 주어졌을 때 → truncation
        raw_long = [
            np.ones((self.N_SAMPLES, self.N_FEATURES)) * (c + 1)
            for c in range(4)
        ]
        result2 = SHAPFeatureSelector._normalise_shap_output(
            raw_long, self.N_FEATURES, self.N_CLASSES,
        )
        assert result2.shape == (self.N_SAMPLES, self.N_FEATURES, self.N_CLASSES)
        # 4번째 클래스(인덱스 3)는 제거됨 — 클래스 2의 값 = 3.0
        np.testing.assert_allclose(result2[:, :, 2], 3.0)

    def test_unexpected_shape_zeros(self) -> None:
        """예상치 못한 형태의 배열이면 0으로 채워진 올바른 shape를 반환해야 한다."""
        raw = np.ones((self.N_SAMPLES,))  # 1-D → 예상 외 형태
        result = SHAPFeatureSelector._normalise_shap_output(
            raw, self.N_FEATURES, self.N_CLASSES,
        )
        assert result.shape == (self.N_SAMPLES, self.N_FEATURES, self.N_CLASSES)
        np.testing.assert_allclose(result, 0.0)


# ---------------------------------------------------------------------------
# 5. TestSubsample — 서브샘플링 로직
# ---------------------------------------------------------------------------

class TestSubsample:
    """SHAPFeatureSelector._subsample 의 random/stratified 샘플링을 검증한다."""

    def test_no_subsample_small_data(self, selector: SHAPFeatureSelector) -> None:
        """데이터 크기가 n 이하이면 원본을 그대로 반환해야 한다."""
        small_df = pd.DataFrame({"a": range(10), "b": range(10)})
        result = selector._subsample(small_df, n=50)
        pd.testing.assert_frame_equal(result, small_df)

    def test_subsample_random(self, selector: SHAPFeatureSelector) -> None:
        """데이터 크기가 n 초과이고 y 없으면 정확히 n개 행을 랜덤 반환해야 한다."""
        big_df = pd.DataFrame({"a": range(200), "b": range(200)})
        result = selector._subsample(big_df, n=50)
        assert len(result) == 50
        # 원본 인덱스의 부분집합이어야 한다
        assert set(result.index).issubset(set(big_df.index))

    def test_subsample_with_y_stratified(
        self, selector: SHAPFeatureSelector,
    ) -> None:
        """y 가 주어지면 stratified sampling 이 시도되어야 한다."""
        rng = np.random.default_rng(42)
        big_df = pd.DataFrame({"a": rng.standard_normal(300)})
        y = pd.Series(rng.choice([0, 1, 2], size=300))

        with patch(
            "app.ml.shap_analyzer.train_test_split",
            side_effect=self._fake_train_test_split,
        ) as mock_split:
            # _subsample imports train_test_split within the function,
            # so we patch where it would be after the import
            pass

        # 직접 sklearn의 train_test_split을 mock 하기 위해
        # _subsample 내부의 from import 을 모킹
        with patch(
            "sklearn.model_selection.train_test_split",
        ) as mock_split:
            mock_split.return_value = (
                big_df.iloc[:250],   # X_train (unused)
                big_df.iloc[:50],    # X_test (= sample)
                y.iloc[:250],        # y_train (unused)
                y.iloc[:50],         # y_test (unused)
            )
            result = selector._subsample(big_df, n=50, y=y)

        assert len(result) == 50
        mock_split.assert_called_once()
        # stratify 인자가 전달되었는지 확인
        _, kwargs = mock_split.call_args
        assert kwargs.get("stratify") is not None

    @staticmethod
    def _fake_train_test_split(*args, **kwargs):  # noqa: ANN002, ANN003
        """Fallback mock (사용되지 않음)."""
        raise AssertionError("should not reach here")


# ---------------------------------------------------------------------------
# 6. TestReportGeneration — 리포트 생성 및 저장
# ---------------------------------------------------------------------------

class TestReportGeneration:
    """save_report / generate_text_report 의 출력 형식과 파일 생성을 검증한다."""

    def test_generate_text_report(
        self, selector: SHAPFeatureSelector, sample_result: SHAPResult,
    ) -> None:
        """text report 에 레짐 이름과 피처 정보가 포함되어야 한다."""
        results = {"bull_trending": sample_result}
        text = selector.generate_text_report(results)

        assert isinstance(text, str)
        assert len(text) > 0
        assert "Bull Trending" in text  # 레짐 이름 (title-case)
        assert "SHAP Feature Importance Report" in text
        assert "rsi" in text
        assert "Samples used:" in text

    def test_save_report_creates_files(
        self, tmp_path: Path, sample_result: SHAPResult,
    ) -> None:
        """save_report 가 JSON 과 TXT 파일을 모두 생성해야 한다."""
        sel = SHAPFeatureSelector(model_artifacts_path=str(tmp_path))
        results = {"bull_trending": sample_result}

        json_path_str = sel.save_report(results, output_dir=str(tmp_path))

        json_path = Path(json_path_str)
        txt_path = tmp_path / "shap_feature_analysis.txt"

        assert json_path.exists()
        assert txt_path.exists()
        assert json_path.name == "shap_feature_analysis.json"

    def test_save_report_json_valid(
        self, tmp_path: Path, sample_result: SHAPResult,
    ) -> None:
        """저장된 JSON 파일이 파싱 가능하고 예상 키를 포함해야 한다."""
        sel = SHAPFeatureSelector(model_artifacts_path=str(tmp_path))
        results = {"bull_trending": sample_result}

        json_path_str = sel.save_report(results, output_dir=str(tmp_path))

        with open(json_path_str, encoding="utf-8") as f:
            data = json.load(f)

        assert "analysis_date" in data
        assert "regimes" in data
        assert "bull_trending" in data["regimes"]

        regime_data = data["regimes"]["bull_trending"]
        assert "n_samples" in regime_data
        assert "global_importance" in regime_data
        assert "per_class_importance" in regime_data
        assert "shap_values_summary" in regime_data
        assert "top_10" in regime_data
        assert "removal_candidates" in regime_data
        assert regime_data["n_samples"] == 100


# ---------------------------------------------------------------------------
# 7. TestAnalyzeModel — SHAP 분석 실행
# ---------------------------------------------------------------------------

class TestAnalyzeModel:
    """SHAPFeatureSelector.analyze_model 의 SHAP TreeExplainer 호출을 검증한다."""

    @staticmethod
    def _build_mock_voting_clf(n_features: int, n_classes: int = 3) -> MagicMock:
        """mock VotingClassifier (named_estimators_ 포함) 생성."""
        mock_clf = MagicMock()
        mock_clf.named_estimators_ = {
            "cat": MagicMock(),
            "lgbm": MagicMock(),
            "xgb": MagicMock(),
        }
        mock_clf.weights = [0.4, 0.35, 0.25]
        return mock_clf

    def test_analyze_model_mock_shap(
        self,
        selector: SHAPFeatureSelector,
        sample_features: list[str],
        sample_dataframe: pd.DataFrame,
    ) -> None:
        """mock shap.TreeExplainer 로 analyze_model 이 유효한 SHAPResult 를 반환해야 한다."""
        n_features = len(sample_features)
        n_classes = 3

        mock_voting = self._build_mock_voting_clf(n_features, n_classes)

        # mock shap 모듈 구성
        mock_shap = MagicMock()
        mock_explainer_instance = MagicMock()

        # TreeExplainer.shap_values → list[ndarray] (클래스별)
        def _mock_shap_values(X: pd.DataFrame) -> list[np.ndarray]:
            n = len(X)
            rng = np.random.default_rng(7)
            return [rng.standard_normal((n, n_features)) for _ in range(n_classes)]

        mock_explainer_instance.shap_values = _mock_shap_values
        mock_shap.TreeExplainer.return_value = mock_explainer_instance

        # _import_shap 를 mock 하여 mock_shap 반환
        with patch.object(
            SHAPFeatureSelector, "_import_shap", return_value=mock_shap,
        ):
            X_subset = pd.DataFrame(sample_dataframe[sample_features])
            result = selector.analyze_model(
                mock_voting, X_subset, sample_features, regime="bull_trending",
            )

        assert isinstance(result, SHAPResult)
        assert result.regime == "bull_trending"
        assert result.feature_names == sample_features
        assert len(result.global_importance) == n_features
        assert set(result.per_class_importance.keys()) == {"DOWN", "NEUTRAL", "UP"}
        assert result.n_samples <= selector.shap_sample_size

        # 중요도 값 비-음수 확인
        for imp in result.global_importance.values():
            assert imp >= 0.0

    def test_analyze_model_shap_not_installed(
        self,
        selector: SHAPFeatureSelector,
        sample_features: list[str],
        sample_dataframe: pd.DataFrame,
    ) -> None:
        """shap 패키지 미설치 시 ImportError 가 발생해야 한다."""
        mock_voting = self._build_mock_voting_clf(len(sample_features))

        with (
            patch.object(
                SHAPFeatureSelector,
                "_import_shap",
                side_effect=ImportError("shap 패키지가 설치되어 있지 않습니다"),
            ),
            pytest.raises(ImportError, match="shap"),
        ):
            selector.analyze_model(
                mock_voting,
                pd.DataFrame(sample_dataframe[sample_features]),
                sample_features,
            )


# ---------------------------------------------------------------------------
# 8. TestRunShapAnalysis — 편의 함수
# ---------------------------------------------------------------------------

class TestRunShapAnalysis:
    """run_shap_analysis 함수의 모델 파일 미존재 시 동작을 검증한다."""

    def test_run_shap_analysis_no_models(self, tmp_path: Path) -> None:
        """모델 파일이 없으면 빈 결과 dict 가 반환되어야 한다."""
        results = run_shap_analysis(
            feature_names=["rsi", "macd", "sector_id"],
            regimes=["bull_trending", "bear_trending"],
            output_dir=str(tmp_path),
        )
        assert isinstance(results, dict)
        assert len(results) == 0
