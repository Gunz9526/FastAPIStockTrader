import logging
import os

import joblib
import pandas as pd
import talib
from sklearn.preprocessing import StandardScaler

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
            # Prevent divide-by-zero: add epsilon to denominator
            band_range = upper - lower
            df['bb_position'] = (close - lower) / (band_range + 1e-8)  # Safe division

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

            # 12. Trade count intensity (if trade_count data available)
            # Higher trade count indicates higher market participation and liquidity
            if 'trade_count' in df.columns and df['trade_count'].notna().any():
                # Normalize by rolling average
                trade_count_ma = df['trade_count'].rolling(window=20, min_periods=1).mean()
                df['trade_intensity'] = df['trade_count'] / (trade_count_ma + 1e-8)
            else:
                # If trade_count not available, use neutral value
                df['trade_intensity'] = 1.0

            # 13. Momentum features (Phase H.4 - Bull Regime Enhancement)
            # These features are optimized for trending market conditions
            df['momentum_5'] = close / talib.SMA(close, timeperiod=5) - 1  # 5-bar momentum
            df['momentum_10'] = close / talib.SMA(close, timeperiod=10) - 1  # 10-bar momentum

            # RSI momentum: direction of RSI change (positive = strengthening)
            rsi_values = df['rsi'].values
            df['rsi_momentum'] = pd.Series(rsi_values).diff(5).fillna(0).values

            # Trend strength: normalized trend measure using EMA spread
            ema_spread = df['ema_12'] - df['ema_26']
            df['trend_strength'] = ema_spread / (df['atr'] + 1e-8)

            # Price position in 20-bar high-low range (0=low, 1=high)
            high_20 = talib.MAX(high, timeperiod=20)
            low_20 = talib.MIN(low, timeperiod=20)
            df['price_position'] = (close - low_20) / (high_20 - low_20 + 1e-8)

            # Breakout flag: 1 if close > 20-bar high, else 0
            df['breakout_flag'] = (close > talib.MAX(high, timeperiod=20)).astype(float)

            # 14. Sentiment features (Phase F.1)
            # NOTE: Sentiment is fetched separately and passed as additional context
            # We'll add it during extract_feature_vector if available

            # 15. Fundamental features (Phase F.2)
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
        sentiment_score: float | None = None,
        fundamental_data: dict[str, float] | None = None,
        feature_set: str = "legacy"
    ) -> pd.DataFrame:
        """
        Extract and normalize feature vector for ML model.
        
        Args:
            df: DataFrame with technical indicators
            fit_scaler: If True, fit scaler on this data (for training)
            market_avg_volume: Average volume across all symbols (for relative volume)
            sentiment_score: Sentiment score from -1.0 to +1.0 (Phase F.1)
            fundamental_data: Dict with 'pe_ratio', 'pb_ratio', 'roe', etc. (Phase F.2)
            feature_set: Which feature set to use:
                - "legacy": 25 features (existing model compatibility)
                - "core": 21 features (no Phase F, no momentum)
                - "base": 27 features (with momentum, no Phase F)
                - "full": 32 features (all features including Phase F)
        
        Returns:
            DataFrame with normalized features ready for model
        """
        if df.empty:
            return pd.DataFrame()

        # DataFrame copy 명시적 생성 (SettingWithCopyWarning 방지)
        df = df.copy()

        try:
            # 시장 대비 거래량 피처 추가
            if market_avg_volume is not None and 'volume' in df.columns:
                df.loc[:, 'relative_volume'] = df['volume'] / market_avg_volume
            else:
                df.loc[:, 'relative_volume'] = 1.0  # 시장 데이터 없으면 중립값

            # Phase F features - add only when needed by feature_set
            include_phase_f = feature_set in ("legacy", "full")
            if include_phase_f:
                # Sentiment 피처 추가 (Phase F.1)
                if sentiment_score is not None:
                    df.loc[:, 'sentiment_score'] = sentiment_score
                else:
                    df.loc[:, 'sentiment_score'] = 0.0

                # Fundamentals 피처 추가 (Phase F.2)
                if fundamental_data is not None:
                    df.loc[:, 'pe_ratio'] = fundamental_data.get('pe_ratio', 15.0)
                    df.loc[:, 'pb_ratio'] = fundamental_data.get('pb_ratio', 3.0)
                    df.loc[:, 'roe'] = fundamental_data.get('roe', 0.10)
                    df.loc[:, 'beta'] = fundamental_data.get('beta', 1.0)
                else:
                    df.loc[:, 'pe_ratio'] = 15.0
                    df.loc[:, 'pb_ratio'] = 3.0
                    df.loc[:, 'roe'] = 0.10
                    df.loc[:, 'beta'] = 1.0

            # Select features based on feature_set
            if feature_set == "legacy":
                feature_cols = self.legacy_feature_columns.copy()
            elif feature_set == "core":
                feature_cols = self.core_feature_columns.copy()
            elif feature_set == "base":
                feature_cols = self.base_feature_columns.copy()
            elif feature_set == "full":
                feature_cols = self.feature_columns.copy()
            else:
                logger.warning(f"Unknown feature_set '{feature_set}', using legacy")
                feature_cols = self.legacy_feature_columns.copy()

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
                    # Scaler 저장
                    os.makedirs(os.path.dirname(self.scaler_path), mode=0o777, exist_ok=True)
                    joblib.dump(self.scaler, self.scaler_path)
                    logger.info("Scaler fitted and saved to %s", self.scaler_path)
                else:
                    X_numeric_scaled = self.scaler.transform(X[numeric_features])

                # CRITICAL: Build DataFrame in EXACT order of available_features
                # This ensures feature names AND order match training
                X_normalized = pd.DataFrame(index=X.index)
                
                # Create dict for fast lookup
                numeric_dict = dict(zip(numeric_features, X_numeric_scaled.T))
                
                # Add features in available_features order
                for feat in available_features:
                    if feat in numeric_features:
                        X_normalized[feat] = numeric_dict[feat]
                    else:  # categorical (sector_id)
                        X_normalized[feat] = X[feat].values
            else:
                X_normalized = X

            # CRITICAL: Verify DataFrame format and column order
            assert isinstance(X_normalized, pd.DataFrame), "extract_feature_vector must return DataFrame"
            assert list(X_normalized.columns) == available_features, (
                f"Column mismatch: got {list(X_normalized.columns)} but expected {available_features}"
            )
            logger.debug(f"Extracted features (count={len(available_features)}): {list(X_normalized.columns)}")
            
            return X_normalized

        except (ValueError, KeyError, TypeError) as e:
            logger.error("Error extracting features: %s", str(e), exc_info=True)
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
    def legacy_feature_columns(self) -> list:
        """
        Return legacy feature columns (25 features) for compatibility with existing models.
        
        IMPORTANT: Existing models were trained with 25 features BEFORE Phase H.4 momentum
        features were added. This list matches the original training data structure.
        
        After retraining with new features, switch to base_feature_columns (27 features).
        
        Returns:
            List of 25 legacy feature names (no momentum features, includes Phase F)
        """
        return [
            # Core technical indicators (17)
            'rsi', 'macd', 'macd_signal', 'macd_hist',
            'bb_width', 'bb_position',
            'sma_20', 'sma_50', 'ema_12', 'ema_26',
            'atr_pct', 'adx',
            'stoch_k', 'stoch_d',
            'volume_ratio', 'roc', 'mom',
            # Cross-sectional features (2)
            'sector_id', 'relative_volume',
            # VWAP & liquidity (2)
            'vwap_distance', 'trade_intensity',
            # Phase F features (4) - these were included in legacy training
            'sentiment_score',
            'pe_ratio', 'pb_ratio', 'roe',
        ]

    @property
    def core_feature_columns(self) -> list:
        """
        Return core technical features only (21 features).
        
        Excludes: Phase F (sentiment/fundamentals) and Phase H.4 (momentum).
        Use for training/prediction when sentiment and fundamentals should be
        used as trading reference only, not as ML model inputs.
        
        Returns:
            List of 21 core technical indicator feature names
        """
        return [
            # Core technical indicators (17)
            'rsi', 'macd', 'macd_signal', 'macd_hist',
            'bb_width', 'bb_position',
            'sma_20', 'sma_50', 'ema_12', 'ema_26',
            'atr_pct', 'adx',
            'stoch_k', 'stoch_d',
            'volume_ratio', 'roc', 'mom',
            # Cross-sectional features (2)
            'sector_id', 'relative_volume',
            # VWAP & liquidity (2)
            'vwap_distance', 'trade_intensity',
        ]

    @property
    def base_feature_columns(self) -> list:
        """
        Return base technical indicator features only (for training on historical data).

        This excludes Phase F features (sentiment, fundamentals) which are not available
        in historical OHLCV data. Use this for model training on historical data.

        Returns:
            List of 27 base technical indicator feature names (including Phase H.4 momentum)
        """
        return [
            # Core technical indicators (17)
            'rsi', 'macd', 'macd_signal', 'macd_hist',
            'bb_width', 'bb_position',
            'sma_20', 'sma_50', 'ema_12', 'ema_26',
            'atr_pct', 'adx',
            'stoch_k', 'stoch_d',
            'volume_ratio', 'roc', 'mom',
            # Cross-sectional features (2)
            'sector_id', 'relative_volume',
            # VWAP & liquidity (2)
            'vwap_distance', 'trade_intensity',
            # Phase H.4: Momentum features for bull regime (6)
            'momentum_5', 'momentum_10',  # Price momentum
            'rsi_momentum',  # RSI trend direction
            'trend_strength',  # Normalized trend measure
            'price_position',  # Position in 20-bar range
            'breakout_flag',  # Breakout detection
        ]

    @property
    def feature_columns(self) -> list:
        """
        Return full feature list including Phase F enhancements.
        
        This includes all base features plus sentiment and fundamentals.
        Use this for live prediction when Phase F features are available.
        
        Returns:
            List of 25 feature names (20 base + 5 Phase F)
        """
        return self.base_feature_columns + [
            'sentiment_score',  # Sentiment feature (Phase F.1)
            'pe_ratio', 'pb_ratio', 'roe', 'beta'  # Fundamental features (Phase F.2)
        ]

    def add_sentiment_and_fundamentals(
        self,
        df: pd.DataFrame,
        symbol: str,
        news_text: str | None = None,
        current_regime: str | None = None
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

            logger.info("%s sentiment added: %.3f", symbol, sentiment_score)

        df['sentiment_score'] = sentiment_score

        # Phase F.2: Add fundamental metrics
        if self.fundamental_provider:
            fundamentals = self.fundamental_provider.get_fundamentals(symbol)
            df['pe_ratio'] = fundamentals.get('pe_ratio', 15.0)
            df['pb_ratio'] = fundamentals.get('pb_ratio', 3.0)
            df['roe'] = fundamentals.get('roe', 0.10)
            df['beta'] = fundamentals.get('beta', 1.0)

            logger.info("%s fundamentals added: PE=%.2f", symbol, df['pe_ratio'].iloc[0])
        else:
            # 기본값
            df['pe_ratio'] = 15.0
            df['pb_ratio'] = 3.0
            df['roe'] = 0.10
            df['beta'] = 1.0

        return df
