import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass

import pandas as pd

logger = logging.getLogger(__name__)

@dataclass
class Signal:
    """거래 신호: 동작, 강도 및 메타데이터를 포함합니다."""
    action: str  # 'BUY', 'SELL', 'HOLD'
    strength: float  # 0.0 to 1.0
    price: float
    stop_loss: float | None = None
    take_profit: float | None = None
    reason: str = ""

class BaseStrategy(ABC):
    """모든 트레이딩 전략의 기초 클래스입니다."""

    def __init__(self, name: str):
        self.name = name

    @abstractmethod
    def generate_signal(self, df: pd.DataFrame) -> Signal:
        """
        Generate trading signal from OHLCV data with indicators.
        
        Args:
            df: DataFrame with OHLCV and technical indicators
            
        Returns:
            Signal object with action and strength
        """
        pass

    def _get_latest(self, df: pd.DataFrame, column: str, default: float = 0.0) -> float:
        """Safely get latest value from dataframe."""
        if df.empty or column not in df.columns:
            return default
        try:
            return float(df[column].iloc[-1])
        except Exception:
            return default

class MomentumStrategy(BaseStrategy):
    """
    이동평균 교차와 MACD를 사용하는 모멘텀 기반 전략입니다.
    트렌드 시장에서 유효합니다.
    """

    def __init__(self):
        super().__init__("Momentum")

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        if df.empty or len(df) < 50:
            return Signal('HOLD', 0.0, 0.0, reason="데이터 부족")

        try:
            close = self._get_latest(df, 'close')
            sma_20 = self._get_latest(df, 'sma_20')
            sma_50 = self._get_latest(df, 'sma_50')
            macd = self._get_latest(df, 'macd')
            macd_signal = self._get_latest(df, 'macd_signal')
            adx = self._get_latest(df, 'adx')

            # Previous values for crossover detection
            prev_sma_20 = float(df['sma_20'].iloc[-2]) if len(df) > 1 else sma_20
            prev_sma_50 = float(df['sma_50'].iloc[-2]) if len(df) > 1 else sma_50
            prev_macd = float(df['macd'].iloc[-2]) if len(df) > 1 else macd
            prev_macd_signal = float(df['macd_signal'].iloc[-2]) if len(df) > 1 else macd_signal

            # Trend strength (ADX > 25 = strong trend)
            trend_strength = min(adx / 50.0, 1.0) if adx > 0 else 0.0

            # Golden Cross: SMA20 crosses above SMA50
            golden_cross = prev_sma_20 <= prev_sma_50 and sma_20 > sma_50

            # Death Cross: SMA20 crosses below SMA50
            death_cross = prev_sma_20 >= prev_sma_50 and sma_20 < sma_50

            # MACD Crossover
            macd_bullish = prev_macd <= prev_macd_signal and macd > macd_signal
            macd_bearish = prev_macd >= prev_macd_signal and macd < macd_signal

            # Price above/below moving averages
            price_above_ma = close > sma_20 and close > sma_50
            price_below_ma = close < sma_20 and close < sma_50

            # BUY Signal
            if (golden_cross or macd_bullish) and adx > 20:
                strength = 0.6 + (trend_strength * 0.4)
                if price_above_ma:
                    strength += 0.1

                return Signal(
                    action='BUY',
                    strength=min(strength, 1.0),
                    price=close,
                    reason=f"Momentum BUY: Golden={golden_cross}, MACD={macd_bullish}, ADX={adx:.1f}"
                )

            # SELL Signal
            elif (death_cross or macd_bearish) and adx > 20:
                strength = 0.6 + (trend_strength * 0.4)
                if price_below_ma:
                    strength += 0.1

                return Signal(
                    action='SELL',
                    strength=min(strength, 1.0),
                    price=close,
                    reason=f"Momentum SELL: Death={death_cross}, MACD={macd_bearish}, ADX={adx:.1f}"
                )

            else:
                return Signal('HOLD', 0.0, close, reason="모멘텀 신호 없음")

        except Exception as e:
            logger.error(f"모멘텀 전략 오류: {e}", exc_info=True)
            return Signal('HOLD', 0.0, 0.0, reason=f"오류: {e}")

class MeanReversionStrategy(BaseStrategy):
    """
    볼린저 밴드와 RSI를 사용하는 평균회귀 전략입니다.
    박스권(횡보) 시장에 적합합니다.
    """

    def __init__(self):
        super().__init__("MeanReversion")

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        if df.empty or len(df) < 30:
            return Signal('HOLD', 0.0, 0.0, reason="데이터 부족")

        try:
            close = self._get_latest(df, 'close')
            rsi = self._get_latest(df, 'rsi')
            bb_upper = self._get_latest(df, 'bb_upper')
            bb_lower = self._get_latest(df, 'bb_lower')
            bb_middle = self._get_latest(df, 'bb_middle')
            bb_position = self._get_latest(df, 'bb_position')
            adx = self._get_latest(df, 'adx')

            # Mean reversion works best in low trend environments
            if adx > 25:
                return Signal('HOLD', 0.0, close, reason="Strong trend detected, MR disabled")

            # Oversold conditions
            rsi_oversold = rsi < 30
            bb_oversold = bb_position < 0.2  # Close to lower band

            # Overbought conditions
            rsi_overbought = rsi > 70
            bb_overbought = bb_position > 0.8  # Close to upper band

            # BUY Signal (Oversold)
            if rsi_oversold and bb_oversold:
                # Strength based on how oversold
                strength = 0.5 + ((30 - rsi) / 30 * 0.3) + ((0.2 - bb_position) * 0.2)

                return Signal(
                    action='BUY',
                    strength=min(strength, 1.0),
                    price=close,
                    reason=f"Mean Reversion BUY: RSI={rsi:.1f}, BB_pos={bb_position:.2f}"
                )

            # SELL Signal (Overbought)
            elif rsi_overbought and bb_overbought:
                # Strength based on how overbought
                strength = 0.5 + ((rsi - 70) / 30 * 0.3) + ((bb_position - 0.8) * 0.2)

                return Signal(
                    action='SELL',
                    strength=min(strength, 1.0),
                    price=close,
                    reason=f"Mean Reversion SELL: RSI={rsi:.1f}, BB_pos={bb_position:.2f}"
                )

            else:
                return Signal('HOLD', 0.0, close, reason="MR 신호 없음")

        except Exception as e:
            logger.error(f"평균회귀 전략 오류: {e}", exc_info=True)
            return Signal('HOLD', 0.0, 0.0, reason=f"오류: {e}")

class BreakoutStrategy(BaseStrategy):
    """
    ATR과 변동성을 이용한 돌파 전략입니다.
    횡보 구간 이후 강한 움직임을 포착합니다.
    """

    def __init__(self):
        super().__init__("Breakout")

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        if df.empty or len(df) < 30:
            return Signal('HOLD', 0.0, 0.0, reason="데이터 부족")

        try:
            close = self._get_latest(df, 'close')
            high = self._get_latest(df, 'high')
            low = self._get_latest(df, 'low')
            atr = self._get_latest(df, 'atr')
            atr_pct = self._get_latest(df, 'atr_pct')
            volume_ratio = self._get_latest(df, 'volume_ratio')

            # Calculate 20-day high/low
            high_20 = float(df['high'].tail(20).max())
            low_20 = float(df['low'].tail(20).min())

            # Detect breakout
            breakout_high = close > high_20 * 1.01  # 1% above 20-day high
            breakout_low = close < low_20 * 0.99   # 1% below 20-day low

            # Volume confirmation
            high_volume = volume_ratio > 1.5

            # Volatility check (ATR should be reasonable)
            normal_volatility = 0.01 < atr_pct < 0.10

            # BUY on upside breakout
            if breakout_high and high_volume and normal_volatility:
                strength = 0.6
                if volume_ratio > 2.0:
                    strength += 0.2
                if atr_pct < 0.05:  # Low volatility = more confidence
                    strength += 0.2

                return Signal(
                    action='BUY',
                    strength=min(strength, 1.0),
                    price=close,
                    reason=f"Breakout BUY: High={high_20:.2f}, Vol={volume_ratio:.1f}x"
                )

            # SELL on downside breakout
            elif breakout_low and high_volume and normal_volatility:
                strength = 0.6
                if volume_ratio > 2.0:
                    strength += 0.2

                return Signal(
                    action='SELL',
                    strength=min(strength, 1.0),
                    price=close,
                    reason=f"Breakout SELL: Low={low_20:.2f}, Vol={volume_ratio:.1f}x"
                )

            else:
                return Signal('HOLD', 0.0, close, reason="돌파 신호 없음")

        except Exception as e:
            logger.error(f"돌파 전략 오류: {e}", exc_info=True)
            return Signal('HOLD', 0.0, 0.0, reason=f"오류: {e}")

class MLStrategy(BaseStrategy):
    """
    앙상블 모델 예측을 사용하는 머신러닝 기반 전략입니다.
    """

    def __init__(self, predictor_service):
        super().__init__("MLEnsemble")
        self.predictor = predictor_service

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        if df.empty:
            return Signal('HOLD', 0.0, 0.0, reason="데이터 없음")

        try:
            # Get latest features (already normalized from FeatureEngineer)
            latest_features = df.iloc[[-1]]
            close = self._get_latest(df, 'close')

            # ML Prediction (0.0 to 1.0)
            prediction = self.predictor.predict_next(latest_features)

            # Interpret prediction
            if prediction > 0.7:
                return Signal(
                    action='BUY',
                    strength=prediction,
                    price=close,
                    reason=f"ML BUY: Score={prediction:.3f}"
                )
            elif prediction < 0.3:
                return Signal(
                    action='SELL',
                    strength=1.0 - prediction,
                    price=close,
                    reason=f"ML SELL: Score={prediction:.3f}"
                )
            else:
                return Signal('HOLD', 0.0, close, reason=f"ML 중립: {prediction:.3f}")

        except Exception as e:
            logger.error(f"ML 전략 오류: {e}", exc_info=True)
            return Signal('HOLD', 0.0, 0.0, reason=f"오류: {e}")
