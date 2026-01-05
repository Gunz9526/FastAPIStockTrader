from enum import Enum
import pandas as pd
import logging
from typing import Dict, Optional

logger = logging.getLogger(__name__)

class MarketRegime(str, Enum):
    """Market regime classification."""
    BULL_TRENDING = "bull_trending"
    BEAR_TRENDING = "bear_trending"
    SIDEWAYS_VOLATILE = "sideways_volatile"
    SIDEWAYS_CALM = "sideways_calm"

class RegimeDetector:
    """
    Detect current market regime using multiple indicators.
    
    Phase F.3: Enhanced with VIX (Volatility Index) integration
    """
    
    def __init__(
        self,
        adx_trend_threshold: float = 25.0,
        atr_volatility_threshold: float = 0.03,
        vix_high_threshold: float = 20.0,  # VIX > 20 = high fear
        vix_extreme_threshold: float = 30.0  # VIX > 30 = extreme fear
    ):
        self.adx_threshold = adx_trend_threshold
        self.atr_threshold = atr_volatility_threshold
        self.vix_high_threshold = vix_high_threshold
        self.vix_extreme_threshold = vix_extreme_threshold
    
    def detect_regime(self, df: pd.DataFrame, vix_value: Optional[float] = None) -> MarketRegime:
        """
        Detect market regime from OHLCV data with indicators.
        
        Phase F.3: Enhanced with VIX integration for improved volatility detection
        
        Args:
            df: DataFrame with indicators (adx, sma_50, atr_pct, etc.)
            vix_value: Current VIX (Volatility Index) value (optional but recommended)
        
        Returns:
            MarketRegime enum
        """
        if df.empty or len(df) < 50:
            logger.warning("Insufficient data for regime detection, defaulting to SIDEWAYS_CALM")
            return MarketRegime.SIDEWAYS_CALM
        
        try:
            latest = df.iloc[-1]
            
            close = float(latest.get('close', 0))
            sma_50 = float(latest.get('sma_50', close))
            adx = float(latest.get('adx', 0))
            atr_pct = float(latest.get('atr_pct', 0))
            
            # Calculate price momentum (10-day)
            if len(df) >= 10:
                price_change_10d = (close - df['close'].iloc[-10]) / df['close'].iloc[-10]
            else:
                price_change_10d = 0
            
            # Trend direction
            trend_up = close > sma_50
            strong_trend = adx > self.adx_threshold
            high_volatility = atr_pct > self.atr_threshold
            
            # Phase F.3: VIX-enhanced volatility detection
            if vix_value is not None:
                extreme_fear = vix_value > self.vix_extreme_threshold
                high_fear = vix_value > self.vix_high_threshold
                
                # VIX overrides ATR for volatility classification
                if extreme_fear:
                    high_volatility = True
                    logger.info(f"VIX extreme fear detected: {vix_value:.2f} (threshold: {self.vix_extreme_threshold})")
                elif high_fear:
                    high_volatility = True
                    logger.info(f"VIX high fear detected: {vix_value:.2f} (threshold: {self.vix_high_threshold})")
            else:
                logger.debug("VIX not provided, using ATR-only volatility detection")
            
            # Regime classification logic
            if strong_trend and trend_up and price_change_10d > 0.02:
                regime = MarketRegime.BULL_TRENDING
            elif strong_trend and not trend_up and price_change_10d < -0.02:
                regime = MarketRegime.BEAR_TRENDING
            elif high_volatility:
                regime = MarketRegime.SIDEWAYS_VOLATILE
            else:
                regime = MarketRegime.SIDEWAYS_CALM
            
            logger.info(
                f"Regime detected: {regime.value} "
                f"(ADX={adx:.1f}, ATR%={atr_pct:.3f}, VIX={vix_value if vix_value else 'N/A'}, "
                f"Trend={'UP' if trend_up else 'DOWN'})"
            )
            
            return regime
            
        except Exception as e:
            logger.error(f"Error detecting regime: {e}", exc_info=True)
            return MarketRegime.SIDEWAYS_CALM

# Regime-specific strategy weights
REGIME_STRATEGY_WEIGHTS: Dict[MarketRegime, Dict[str, float]] = {
    MarketRegime.BULL_TRENDING: {
        'Momentum': 0.5,
        'MeanReversion': 0.1,
        'Breakout': 0.3,
        'MLEnsemble': 0.1
    },
    MarketRegime.BEAR_TRENDING: {
        'Momentum': 0.4,
        'MeanReversion': 0.2,
        'Breakout': 0.1,
        'MLEnsemble': 0.3
    },
    MarketRegime.SIDEWAYS_VOLATILE: {
        'Momentum': 0.1,
        'MeanReversion': 0.4,
        'Breakout': 0.4,
        'MLEnsemble': 0.1
    },
    MarketRegime.SIDEWAYS_CALM: {
        'Momentum': 0.1,
        'MeanReversion': 0.6,
        'Breakout': 0.1,
        'MLEnsemble': 0.2
    }
}

# Regime-specific risk parameters
REGIME_RISK_PARAMS: Dict[MarketRegime, Dict[str, float]] = {
    MarketRegime.BULL_TRENDING: {
        'max_position_pct': 0.15,
        'stop_loss_mult': 2.5,
        'take_profit_mult': 4.0,
        'trailing_stop_mult': 2.0
    },
    MarketRegime.BEAR_TRENDING: {
        'max_position_pct': 0.05,
        'stop_loss_mult': 1.5,
        'take_profit_mult': 2.5,
        'trailing_stop_mult': 1.2
    },
    MarketRegime.SIDEWAYS_VOLATILE: {
        'max_position_pct': 0.08,
        'stop_loss_mult': 1.8,
        'take_profit_mult': 2.8,
        'trailing_stop_mult': 1.5
    },
    MarketRegime.SIDEWAYS_CALM: {
        'max_position_pct': 0.12,
        'stop_loss_mult': 2.0,
        'take_profit_mult': 3.0,
        'trailing_stop_mult': 1.5
    }
}

def get_regime_strategy_weights(regime: MarketRegime) -> Dict[str, float]:
    """Get strategy weights for current regime."""
    return REGIME_STRATEGY_WEIGHTS.get(regime, REGIME_STRATEGY_WEIGHTS[MarketRegime.SIDEWAYS_CALM])

def get_regime_risk_params(regime: MarketRegime) -> Dict[str, float]:
    """Get risk parameters for current regime."""
    return REGIME_RISK_PARAMS.get(regime, REGIME_RISK_PARAMS[MarketRegime.SIDEWAYS_CALM])
