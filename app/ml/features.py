import pandas as pd
import numpy as np
import talib
import logging
from sklearn.preprocessing import StandardScaler
import joblib
import os
from typing import Optional, Dict
from app.ml.sector_map import get_sector_id

logger = logging.getLogger(__name__)

class FeatureEngineer:
    """
    Production-grade feature engineering using TA-Lib.
    Generates normalized feature vectors for ML models.
    
    Phase F.1-F.2 Extensions:
    - Sentiment analysis integration (Gemini API + Redis)
    - Fundamental metrics (P/E, P/B, ROE via yfinance)
    """
    
    def __init__(
        self, 
        scaler_path: str = "/app/model_artifacts/feature_scaler.pkl",
        sentiment_analyzer = None,
        fundamental_provider = None
    ):
        self.scaler_path = scaler_path
        self.scaler = self._load_or_create_scaler()
        self.feature_names = None
        
        # Phase F.1: Sentiment analyzer (lazy loading)
        self._sentiment_analyzer = sentiment_analyzer
        
        # Phase F.2: Fundamental data provider (lazy loading)
        self._fundamental_provider = fundamental_provider
    
    def _load_or_create_scaler(self):
        """Load existing scaler or create new one."""
        if os.path.exists(self.scaler_path):
            try:
                return joblib.load(self.scaler_path)
            except Exception as e:
                logger.warning(f"Failed to load scaler: {e}. Creating new one.")
        return StandardScaler()
    
    @property
    def sentiment_analyzer(self):
        """Lazy load sentiment analyzer"""
        if self._sentiment_analyzer is None:
            try:
                from app.services.sentiment_analyzer import get_sentiment_analyzer
                self._sentiment_analyzer = get_sentiment_analyzer()
            except Exception as e:
                logger.warning(f"Sentiment analyzer initialization failed: {e}")
                self._sentiment_analyzer = None
        return self._sentiment_analyzer
    
    @property
    def fundamental_provider(self):
        """Lazy load fundamental data provider"""
        if self._fundamental_provider is None:
            try:
                from app.services.fundamental_provider import get_fundamental_provider
                self._fundamental_provider = get_fundamental_provider()
            except Exception as e:
                logger.warning(f"Fundamental provider initialization failed: {e}")
                self._fundamental_provider = None
        return self._fundamental_provider
    
    def add_technical_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Adds RSI, MACD, BBANDS, SMA, EMA, ATR to the dataframe.
        Expects 'close', 'high', 'low', 'volume' columns.
        Also adds 'symbol' column for sector feature.
        Returns dataframe with technical indicators.
        """
        if df.empty or len(df) < 30:
            logger.warning("Insufficient data for indicator calculation")
            return pd.DataFrame()
            
        try:
            df = df.copy()
            
            # Ensure float types
            close = df['close'].astype(float).values
            high = df['high'].astype(float).values
            low = df['low'].astype(float).values
            volume = df['volume'].astype(float).values
            
            # 1. RSI
            df['rsi'] = talib.RSI(close, timeperiod=14)
            
            # 2. MACD
            macd, macdsignal, macdhist = talib.MACD(close, fastperiod=12, slowperiod=26, signalperiod=9)
            df['macd'] = macd
            df['macd_signal'] = macdsignal
            df['macd_hist'] = macdhist
            
            # 3. Bollinger Bands
            upper, middle, lower = talib.BBANDS(close, timeperiod=20, nbdevup=2, nbdevdn=2, matype=0)
            df['bb_upper'] = upper
            df['bb_middle'] = middle
            df['bb_lower'] = lower
            df['bb_width'] = (upper - lower) / middle  # Normalized width
            df['bb_position'] = (close - lower) / (upper - lower)  # Position within bands
            
            # 4. Moving Averages
            df['sma_20'] = talib.SMA(close, timeperiod=20)
            df['sma_50'] = talib.SMA(close, timeperiod=50)
            df['ema_12'] = talib.EMA(close, timeperiod=12)
            df['ema_26'] = talib.EMA(close, timeperiod=26)
            
            # 5. ATR (Volatility)
            df['atr'] = talib.ATR(high, low, close, timeperiod=14)
            df['atr_pct'] = df['atr'] / close  # Normalized ATR
            
            # 6. ADX (Trend Strength)
            df['adx'] = talib.ADX(high, low, close, timeperiod=14)
            
            # 7. Stochastic
            slowk, slowd = talib.STOCH(high, low, close, fastk_period=14, slowk_period=3, slowd_period=3)
            df['stoch_k'] = slowk
            df['stoch_d'] = slowd
            
            # 8. Volume indicators
            df['obv'] = talib.OBV(close, volume)
            df['volume_sma'] = talib.SMA(volume, timeperiod=20)
            df['volume_ratio'] = volume / df['volume_sma']
            
            # 9. Price momentum
            df['roc'] = talib.ROC(close, timeperiod=10)  # Rate of Change
            df['mom'] = talib.MOM(close, timeperiod=10)  # Momentum
            
            # 10. Sector feature (categorical)
            if 'symbol' in df.columns:
                # Get the first symbol value (assumes all rows are same symbol)
                symbol = df['symbol'].iloc[0]
                df['sector_id'] = get_sector_id(symbol)
            else:
                logger.warning("No 'symbol' column found - cannot add sector feature")
                df['sector_id'] = 5  # Unknown sector
            
            # 11. VWAP-based features (if VWAP data available)
            if 'vwap' in df.columns and df['vwap'].notna().any():
                # VWAP distance: how far is price from VWAP (institutional benchmark)
                df['vwap_distance'] = (close - df['vwap']) / df['vwap']
            else:
                # If VWAP not available, use neutral value
                df['vwap_distance'] = 0.0
            
            # 12. Sentiment features (Phase F.1)
            # NOTE: Sentiment is fetched separately and passed as additional context
            # We'll add it during extract_feature_vector if available
            
            # 13. Fundamental features (Phase F.2)
            # NOTE: Fundamentals are fetched separately and passed as additional context
            # We'll add them during extract_feature_vector if available
            
            # Drop NaNs
            df.dropna(inplace=True)
            
            return df

        except Exception as e:
            logger.error(f"Error computing features: {e}", exc_info=True)
            return pd.DataFrame()
    
    def extract_feature_vector(
        self, 
        df: pd.DataFrame, 
        fit_scaler: bool = False, 
        market_avg_volume: float = None,
        sentiment_score: Optional[float] = None,
        fundamental_data: Optional[Dict[str, float]] = None
    ) -> pd.DataFrame:
        """
        Extract and normalize feature vector for ML model.
        
        Args:
            df: DataFrame with technical indicators
            fit_scaler: If True, fit scaler on this data (for training)
            market_avg_volume: Average volume across all symbols (for relative volume)
            sentiment_score: Sentiment score from -1.0 to +1.0 (Phase F.1)
            fundamental_data: Dict with 'pe_ratio', 'pb_ratio', 'roe', etc. (Phase F.2)
        
        Returns:
            DataFrame with normalized features ready for model
        """
        if df.empty:
            return pd.DataFrame()
        
        try:
            # Add market-relative volume feature
            if market_avg_volume is not None and 'volume' in df.columns:
                df['relative_volume'] = df['volume'] / market_avg_volume
            else:
                df['relative_volume'] = 1.0  # Neutral if no market data
            
            # Add sentiment feature (Phase F.1)
            if sentiment_score is not None:
                df['sentiment_score'] = sentiment_score
            else:
                df['sentiment_score'] = 0.0  # Neutral if no sentiment data
            
            # Add fundamental features (Phase F.2)
            if fundamental_data is not None:
                df['pe_ratio'] = fundamental_data.get('pe_ratio', 15.0)  # Market average default
                df['pb_ratio'] = fundamental_data.get('pb_ratio', 3.0)
                df['roe'] = fundamental_data.get('roe', 0.10)  # 10% default
                df['beta'] = fundamental_data.get('beta', 1.0)  # Market beta
            else:
                # Use market-average defaults if no fundamental data
                df['pe_ratio'] = 15.0
                df['pb_ratio'] = 3.0
                df['roe'] = 0.10
                df['beta'] = 1.0
            
            # Select features for model (exclude OHLCV and date)
            feature_cols = [
                'rsi', 'macd', 'macd_signal', 'macd_hist',
                'bb_width', 'bb_position',
                'sma_20', 'sma_50', 'ema_12', 'ema_26',
                'atr_pct', 'adx',
                'stoch_k', 'stoch_d',
                'volume_ratio', 'roc', 'mom',
                'sector_id', 'relative_volume',  # Cross-sectional features
                'vwap_distance',  # VWAP feature (Phase G)
                'sentiment_score',  # Sentiment feature (Phase F.1)
                'pe_ratio', 'pb_ratio', 'roe', 'beta'  # Fundamental features (Phase F.2)
            ]
            
            # Filter available columns
            available_features = [col for col in feature_cols if col in df.columns]
            
            if not available_features:
                logger.error("No features available for extraction")
                return pd.DataFrame()
            
            X = df[available_features].copy()
            
            # Store feature names
            self.feature_names = available_features
            
            # Normalize (exclude categorical sector_id)
            numeric_features = [f for f in available_features if f != 'sector_id']
            categorical_features = [f for f in available_features if f == 'sector_id']
            
            if numeric_features:
                if fit_scaler:
                    X_numeric_scaled = self.scaler.fit_transform(X[numeric_features])
                    # Save scaler
                    os.makedirs(os.path.dirname(self.scaler_path), mode=0o777, exist_ok=True)
                    joblib.dump(self.scaler, self.scaler_path)
                    logger.info(f"Scaler fitted and saved to {self.scaler_path}")
                else:
                    X_numeric_scaled = self.scaler.transform(X[numeric_features])
                
                # Convert back to DataFrame
                X_normalized = pd.DataFrame(X_numeric_scaled, columns=numeric_features, index=X.index)
                
                # Add categorical features (not scaled)
                for cat_feat in categorical_features:
                    X_normalized[cat_feat] = X[cat_feat].values
            else:
                X_normalized = X
            
            return X_normalized
            
        except Exception as e:
            logger.error(f"Error extracting features: {e}", exc_info=True)
            return pd.DataFrame()
    
    def get_latest_features(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Get the latest row of features (for live prediction).
        
        Returns:
            DataFrame with single row of features
        """
        if df.empty:
            return pd.DataFrame()
        
        return df.iloc[[-1]]
    
    def create_features(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Convenience method: adds technical indicators and returns DataFrame.
        Alias for add_technical_indicators for API compatibility.
        
        Args:
            df: DataFrame with OHLCV data
            
        Returns:
            DataFrame with indicators (not scaled)
        """
        return self.add_technical_indicators(df)
    
    @property
    def feature_columns(self) -> list:
        """Return list of feature column names."""
        return [
            'rsi', 'macd', 'macd_signal', 'macd_hist',
            'bb_width', 'bb_position',
            'sma_20', 'sma_50', 'ema_12', 'ema_26',
            'atr_pct', 'adx',
            'stoch_k', 'stoch_d',
            'volume_ratio', 'roc', 'mom',
            'sector_id', 'relative_volume',  # Cross-sectional features
            'vwap_distance',  # VWAP feature (Phase G)
            'sentiment_score',  # Sentiment feature (Phase F.1)
            'pe_ratio', 'pb_ratio', 'roe', 'beta'  # Fundamental features (Phase F.2)
        ]
    
    def add_sentiment_and_fundamentals(
        self, 
        df: pd.DataFrame, 
        symbol: str,
        news_text: Optional[str] = None,
        current_regime: Optional[str] = None
    ) -> pd.DataFrame:
        """
        Add sentiment and fundamental features to DataFrame.
        
        Phase F.1-F.2: Convenience method for fetching and adding context features.
        
        Args:
            df: DataFrame with technical indicators
            symbol: Stock symbol
            news_text: News text for sentiment analysis
            current_regime: Current market regime (for regime-weighted sentiment)
        
        Returns:
            DataFrame with sentiment and fundamental features added
        """
        if df.empty:
            return df
        
        df = df.copy()
        
        # Phase F.1: Add sentiment score
        sentiment_score = 0.0
        if self.sentiment_analyzer and news_text:
            raw_sentiment = self.sentiment_analyzer.get_sentiment_score(symbol, news_text)
            if current_regime:
                sentiment_score = self.sentiment_analyzer.get_regime_weighted_sentiment(
                    symbol, raw_sentiment, current_regime
                )
            else:
                sentiment_score = raw_sentiment
            
            logger.info(f"{symbol} sentiment added: {sentiment_score:.3f}")
        
        df['sentiment_score'] = sentiment_score
        
        # Phase F.2: Add fundamental metrics
        if self.fundamental_provider:
            fundamentals = self.fundamental_provider.get_fundamentals(symbol)
            df['pe_ratio'] = fundamentals.get('pe_ratio', 15.0)
            df['pb_ratio'] = fundamentals.get('pb_ratio', 3.0)
            df['roe'] = fundamentals.get('roe', 0.10)
            df['beta'] = fundamentals.get('beta', 1.0)
            
            logger.info(f"{symbol} fundamentals added: PE={df['pe_ratio'].iloc[0]:.2f}")
        else:
            # Defaults
            df['pe_ratio'] = 15.0
            df['pb_ratio'] = 3.0
            df['roe'] = 0.10
            df['beta'] = 1.0
        
        return df
