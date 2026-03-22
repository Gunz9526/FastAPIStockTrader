import logging
from enum import Enum

import pandas as pd

logger = logging.getLogger(__name__)

class MarketRegime(str, Enum):
    """시장 레짐 분류입니다."""
    BULL_TRENDING = "bull_trending"
    BEAR_TRENDING = "bear_trending"
    SIDEWAYS_VOLATILE = "sideways_volatile"
    SIDEWAYS_CALM = "sideways_calm"

class RegimeDetector:
    """
    여러 지표를 사용해 현재 시장 레짐을 감지합니다.
    """

    def __init__(
        self,
        adx_trend_threshold: float = 25.0,  # 일봉 기준 (ADX > 25 = 강한 추세)
        atr_volatility_threshold: float = 0.03,  # 일봉 기준 (3% 일일 변동)
        vix_high_threshold: float = 20.0,  # VIX > 20 = 높은 공포
        vix_extreme_threshold: float = 30.0  # VIX > 30 = 극도 공포
    ):
        self.adx_threshold = adx_trend_threshold
        self.atr_threshold = atr_volatility_threshold
        self.vix_high_threshold = vix_high_threshold
        self.vix_extreme_threshold = vix_extreme_threshold

    def detect_regime(self, df: pd.DataFrame, vix_value: float | None = None) -> MarketRegime:
        """
        Detect market regime from OHLCV data with indicators.
                
        Args:
            df: DataFrame with indicators (adx, sma_50, atr_pct, etc.)
            vix_value: Current VIX (Volatility Index) value (optional but recommended)
        
        Returns:
            MarketRegime enum
        """
        if df.empty or len(df) < 50:
            logger.warning("레짐 감지용 데이터 부족 (%d개 바), SIDEWAYS_CALM으로 기본 설정", len(df))
            return MarketRegime.SIDEWAYS_CALM

        try:
            latest = df.iloc[-1]

            close = float(latest.get('close', 0))
            sma_50 = float(latest.get('sma_50', close))
            adx = float(latest.get('adx', 0))
            atr_pct = float(latest.get('atr_pct', 0))

            # 가격 모멘텀 계산 (10개 바 = 일봉 10거래일)
            if len(df) >= 10:
                price_change_10d = (close - df['close'].iloc[-10]) / df['close'].iloc[-10]
            else:
                price_change_10d = 0

            # 트렌드 방향
            trend_up = close > sma_50
            strong_trend = adx > self.adx_threshold
            high_volatility = atr_pct > self.atr_threshold

            # VIX 기반 변동성 감지 강화
            if vix_value is not None:
                extreme_fear = vix_value > self.vix_extreme_threshold
                high_fear = vix_value > self.vix_high_threshold

                # VIX가 ATR보다 우선순위
                if extreme_fear:
                    high_volatility = True
                    logger.info("VIX 극도 공포 감지: %.2f (임계값: %.2f)", vix_value, self.vix_extreme_threshold)
                elif high_fear:
                    high_volatility = True
                    logger.info("VIX 높은 공포 감지: %.2f (임계값: %.2f)", vix_value, self.vix_high_threshold)
            else:
                logger.debug("VIX 미제공, ATR 기반 변동성 감지 사용")

            # 레짐 분류 로직 (일봉 기준)
            if strong_trend and trend_up and price_change_10d > 0.02:  # 10거래일 내 2% 상승
                regime = MarketRegime.BULL_TRENDING
            elif strong_trend and not trend_up and price_change_10d < -0.02:  # 10거래일 내 2% 하락
                regime = MarketRegime.BEAR_TRENDING
            elif high_volatility:
                regime = MarketRegime.SIDEWAYS_VOLATILE
            else:
                regime = MarketRegime.SIDEWAYS_CALM

            # 주석처리 (train_model에서 수천 번 호출되므로)
            # logger.debug(
            #     "레짐 감지: %s (ADX=%.1f, ATR%%=%.3f, VIX=%s, 트렌드=%s)",
            #     regime.value, adx, atr_pct,
            #     f"{vix_value:.1f}" if vix_value else 'N/A',
            #     'UP' if trend_up else 'DOWN'
            # )

            return regime

        except (ValueError, KeyError, AttributeError) as e:
            logger.error("레짐 감지 오류: %s", str(e), exc_info=True)
            return MarketRegime.SIDEWAYS_CALM


# Regime-specific signal weighting for trading strategy
# Weights must sum to 1.0 for each regime
#
# [Session 33] Sentiment & fundamentals DEACTIVATED (weight=0).
# ML is sole signal source until sentiment/fundamentals pipelines are validated.
# To restore: set sentiment/fundamentals weights back to non-zero values
# (original values preserved in git history).
REGIME_STRATEGY_WEIGHTS: dict[str, dict[str, float]] = {
    'bull_trending': {
        'ml_prediction': 1.0,    # Full ML reliance (sentiment/fundamentals deactivated)
        'sentiment': 0.0,        # Deactivated — reactivate when validated
        'fundamentals': 0.0,     # Deactivated — reactivate when validated
    },
    'bear_trending': {
        'ml_prediction': 1.0,    # Full ML reliance (sentiment/fundamentals deactivated)
        'sentiment': 0.0,        # Deactivated — reactivate when validated
        'fundamentals': 0.0,     # Deactivated — reactivate when validated
    },
    'sideways_volatile': {
        'ml_prediction': 1.0,    # Full ML reliance (sentiment/fundamentals deactivated)
        'sentiment': 0.0,        # Deactivated — reactivate when validated
        'fundamentals': 0.0,     # Deactivated — reactivate when validated
    },
    'sideways_calm': {
        'ml_prediction': 1.0,    # Full ML reliance (sentiment/fundamentals deactivated)
        'sentiment': 0.0,        # Deactivated — reactivate when validated
        'fundamentals': 0.0,     # Deactivated — reactivate when validated
    },
}
