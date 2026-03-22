"""FeatureEngineer 유닛 테스트.

``app.ml.features.FeatureEngineer`` 클래스의 속성, 기술 지표 계산,
피처 벡터 추출, 스케일러 영속성 등을 검증한다.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import numpy as np  # type: ignore[import-untyped]
import pandas as pd  # type: ignore[import-untyped]
import pytest  # type: ignore[import-untyped]
from sklearn.preprocessing import StandardScaler  # type: ignore[import-untyped]

if TYPE_CHECKING:
    from app.ml.features import FeatureEngineer


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def ohlcv_df() -> pd.DataFrame:
    """100행 OHLCV 테스트 데이터."""
    rng = np.random.default_rng(42)
    n = 100
    return pd.DataFrame(
        {
            "close": 100 + np.cumsum(rng.normal(0, 1, n)),
            "high": 101 + np.cumsum(rng.normal(0, 1, n)),
            "low": 99 + np.cumsum(rng.normal(0, 1, n)),
            "volume": rng.integers(1000, 10000, size=n).astype(float),
            "symbol": "AAPL",
        }
    )


@pytest.fixture()
def fe() -> FeatureEngineer:
    """``FeatureEngineer`` 인스턴스 (스케일러 파일 없이 생성)."""
    with patch("app.ml.features.os.path.exists", return_value=False):
        from app.ml.features import FeatureEngineer

        return FeatureEngineer()


# ---------------------------------------------------------------------------
# Helpers — talib mock 설정
# ---------------------------------------------------------------------------

def _configure_talib_mock(mock_talib: MagicMock, n: int = 100) -> None:
    """talib mock 함수들에 적절한 반환값을 설정한다."""
    mock_talib.RSI.return_value = np.full(n, 50.0)
    mock_talib.MACD.return_value = (np.zeros(n), np.zeros(n), np.zeros(n))
    mock_talib.BBANDS.return_value = (
        np.ones(n) * 110,
        np.ones(n) * 100,
        np.ones(n) * 90,
    )
    mock_talib.SMA.return_value = np.full(n, 100.0)
    mock_talib.EMA.return_value = np.full(n, 100.0)
    mock_talib.ATR.return_value = np.full(n, 2.0)
    mock_talib.ADX.return_value = np.full(n, 25.0)
    mock_talib.STOCH.return_value = (np.full(n, 50.0), np.full(n, 50.0))
    mock_talib.OBV.return_value = np.cumsum(np.ones(n))
    mock_talib.ROC.return_value = np.zeros(n)
    mock_talib.MOM.return_value = np.zeros(n)
    mock_talib.MAX.return_value = np.full(n, 105.0)
    mock_talib.MIN.return_value = np.full(n, 95.0)
    mock_talib.LINEARREG_SLOPE.return_value = np.zeros(n)
    mock_talib.WILLR.return_value = np.full(n, -50.0)
    mock_talib.MFI.return_value = np.full(n, 50.0)


# ---------------------------------------------------------------------------
# TestFeatureColumnProperties
# ---------------------------------------------------------------------------


class TestFeatureColumnProperties:
    """feature_columns 프로퍼티 반환값 검증."""

    def test_feature_columns_returns_31(self, fe: FeatureEngineer) -> None:
        """``feature_columns`` 는 31개 피처를 반환해야 한다."""
        cols = fe.feature_columns
        assert len(cols) == 31
        assert "sector_id" in cols

    def test_base_feature_columns_returns_26(self, fe: FeatureEngineer) -> None:
        """``base_feature_columns`` 는 26개 피처를 반환해야 한다."""
        cols = fe.base_feature_columns
        assert len(cols) == 26
        assert "sector_id" in cols

    def test_core_feature_columns_returns_21(self, fe: FeatureEngineer) -> None:
        """``core_feature_columns`` 는 21개 피처를 반환해야 한다."""
        cols = fe.core_feature_columns
        assert len(cols) == 21
        assert "sector_id" in cols

    def test_legacy_feature_columns_returns_25(self, fe: FeatureEngineer) -> None:
        """``legacy_feature_columns`` 는 25개 피처를 반환해야 한다."""
        cols = fe.legacy_feature_columns
        assert len(cols) == 25
        assert "sector_id" in cols


# ---------------------------------------------------------------------------
# TestAddTechnicalIndicators
# ---------------------------------------------------------------------------


class TestAddTechnicalIndicators:
    """``add_technical_indicators`` 메서드 검증."""

    @patch("app.ml.features.get_sector_id", return_value=0)
    @patch("app.ml.features.talib")
    def test_add_indicators_basic(
        self,
        mock_talib: MagicMock,
        _mock_sector: MagicMock,
        fe: FeatureEngineer,
        ohlcv_df: pd.DataFrame,
    ) -> None:
        """유효한 OHLCV 데이터(100행)로 기술 지표 컬럼이 추가되는지 확인."""
        _configure_talib_mock(mock_talib, n=len(ohlcv_df))

        result = fe.add_technical_indicators(ohlcv_df)

        assert not result.empty
        expected_cols = [
            "rsi",
            "macd",
            "macd_signal",
            "macd_hist",
            "bb_width",
            "bb_position",
            "sma_20",
            "sma_50",
            "ema_12",
            "ema_26",
            "atr",
            "atr_pct",
            "adx",
            "stoch_k",
            "stoch_d",
            "volume_ratio",
            "roc",
            "mom",
            "sector_id",
        ]
        for col in expected_cols:
            assert col in result.columns, f"'{col}' 컬럼이 결과 DataFrame 에 없음"

    @patch("app.ml.features.talib")
    def test_add_indicators_empty_df(
        self, mock_talib: MagicMock, fe: FeatureEngineer
    ) -> None:
        """빈 DataFrame 입력 시 빈 DataFrame 을 반환해야 한다."""
        result = fe.add_technical_indicators(pd.DataFrame())

        assert result.empty
        mock_talib.RSI.assert_not_called()

    @patch("app.ml.features.talib")
    def test_add_indicators_insufficient_data(
        self, mock_talib: MagicMock, fe: FeatureEngineer
    ) -> None:
        """30행 미만 데이터 입력 시 빈 DataFrame 을 반환해야 한다."""
        small_df = pd.DataFrame(
            {
                "close": np.arange(20, dtype=float),
                "high": np.arange(20, dtype=float) + 1,
                "low": np.arange(20, dtype=float) - 1,
                "volume": np.ones(20) * 1000,
            }
        )

        result = fe.add_technical_indicators(small_df)

        assert result.empty
        mock_talib.RSI.assert_not_called()


# ---------------------------------------------------------------------------
# TestExtractFeatureVector
# ---------------------------------------------------------------------------


class TestExtractFeatureVector:
    """``extract_feature_vector`` 메서드 검증."""

    @patch("app.ml.features.joblib")
    @patch("app.ml.features.os.makedirs")
    def test_extract_feature_vector_base_set(
        self,
        _mock_makedirs: MagicMock,
        _mock_joblib: MagicMock,
        fe: FeatureEngineer,
    ) -> None:
        """feature_set='base' 로 추출하면 26개 컬럼이 반환되어야 한다."""
        cols = fe.base_feature_columns
        rng = np.random.default_rng(0)
        df = pd.DataFrame(rng.standard_normal((50, len(cols))), columns=cols)

        result = fe.extract_feature_vector(
            df, fit_scaler=True, feature_set="base"
        )

        assert not result.empty
        assert result.shape[1] == 26
        assert list(result.columns) == cols

    def test_extract_feature_vector_empty_df(
        self, fe: FeatureEngineer
    ) -> None:
        """빈 DataFrame 입력 시 빈 DataFrame 을 반환해야 한다."""
        result = fe.extract_feature_vector(pd.DataFrame(), feature_set="base")

        assert result.empty

    @patch("app.ml.features.joblib")
    @patch("app.ml.features.os.makedirs")
    def test_extract_feature_vector_relative_volume_passthrough(
        self,
        _mock_makedirs: MagicMock,
        _mock_joblib: MagicMock,
        fe: FeatureEngineer,
    ) -> None:
        """``relative_volume`` 이 이미 DataFrame에 존재하면 그대로 전달되어야 한다."""
        cols = fe.core_feature_columns
        rng = np.random.default_rng(1)
        df = pd.DataFrame(rng.standard_normal((50, len(cols))), columns=cols)
        # relative_volume 컬럼이 이미 존재하는 경우
        df["relative_volume"] = 0.75

        result = fe.extract_feature_vector(
            df,
            fit_scaler=True,
            feature_set="core",
        )

        assert not result.empty
        # relative_volume 컬럼이 결과에 포함되어 있어야 한다.
        assert "relative_volume" in result.columns


# ---------------------------------------------------------------------------
# TestGetLatestFeatures
# ---------------------------------------------------------------------------


class TestGetLatestFeatures:
    """``get_latest_features`` 메서드 검증."""

    def test_get_latest_features(self, fe: FeatureEngineer) -> None:
        """마지막 행 1개만 반환해야 한다."""
        df = pd.DataFrame(
            {"a": [1, 2, 3], "b": [4, 5, 6]}, index=[10, 20, 30]
        )

        result = fe.get_latest_features(df)

        assert len(result) == 1
        assert result.index[0] == 30
        assert result["a"].iloc[0] == 3

    def test_get_latest_features_empty(self, fe: FeatureEngineer) -> None:
        """빈 DataFrame 입력 시 빈 DataFrame 을 반환해야 한다."""
        result = fe.get_latest_features(pd.DataFrame())
        assert result.empty


# ---------------------------------------------------------------------------
# TestScalerPersistence
# ---------------------------------------------------------------------------


class TestScalerPersistence:
    """``_load_or_create_scaler`` 스케일러 로딩/생성 검증."""

    @patch("app.ml.features.os.path.exists", return_value=False)
    def test_scaler_load_or_create_new(
        self, _mock_exists: MagicMock
    ) -> None:
        """스케일러 파일이 없으면 새 ``StandardScaler`` 를 생성해야 한다."""
        from app.ml.features import FeatureEngineer

        fe = FeatureEngineer()

        assert isinstance(fe.scaler, StandardScaler)

    @patch("app.ml.features.joblib.load")
    @patch("app.ml.features.os.path.exists", return_value=True)
    def test_scaler_load_existing(
        self,
        _mock_exists: MagicMock,
        mock_load: MagicMock,
    ) -> None:
        """스케일러 파일이 존재하면 ``joblib.load`` 로 로드해야 한다."""
        from app.ml.features import FeatureEngineer

        saved_scaler = StandardScaler()
        mock_load.return_value = saved_scaler

        fe = FeatureEngineer()

        mock_load.assert_called_once()
        assert fe.scaler is saved_scaler

