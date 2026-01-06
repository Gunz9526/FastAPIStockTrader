import logging
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from app.core.config import settings
from datetime import datetime
from app.core.database import SessionLocal
from app.repositories.stock_repo_sync import SyncStockRepository
from app.repositories.portfolio_repo import PortfolioRepository
from app.ml.predictor import PredictorService
from app.ml.features import FeatureEngineer
from app.services.regime import RegimeDetector, MarketRegime
from app.services.portfolio_optimizer import PortfolioOptimizer
import pandas as pd
from typing import List, Dict

logger = logging.getLogger(__name__)

class SyncTradingStrategy:
    """
    Synchronous Trading Strategy Engine (Production).
    Uses PredictorService for signals and Alpaca API for execution.
    [Phase H] Includes market regime detection for adaptive trading.
    """
    
    def __init__(self, session):
        self.session = session
        self.repo = SyncStockRepository(session)
        self.portfolio_repo = PortfolioRepository(session)  # Phase I.2
        self.predictor = PredictorService()
        self.feature_engineer = FeatureEngineer()
        self.regime_detector = RegimeDetector()  # Phase H: Regime detection
        self.current_regime = MarketRegime.SIDEWAYS_CALM  # Default
        
        # Phase I.2: Portfolio optimization
        self.optimizer = PortfolioOptimizer(lookback_days=14, min_live_trades=50)
        self.max_positions = 5  # Max 5 concurrent positions
        self.multi_position_mode = True  # Enable multi-position trading
        
        # Initialize Alpaca API
        try:
            # Determine if using paper trading based on URL
            is_paper = 'paper' in settings.ALPACA_TRADING_URL.lower()
            self.api = TradingClient(
                api_key=settings.ALPACA_API_KEY,
                secret_key=settings.ALPACA_SECRET_KEY,
                paper=is_paper
            )
            logger.info("Alpaca API connected (Paper: {is_paper})")
        except Exception as e:
            logger.error(f"Failed to connect to Alpaca: {e}")
            self.api = None
    
    def detect_market_regime(self):
        """
        Detect current market regime using SPY data.
        Updates self.current_regime.
        """
        try:
            # Fetch SPY data for regime detection
            end_date = pd.Timestamp.now(tz='UTC')
            start_date = end_date - pd.Timedelta(days=90)  # 90 days for SMA50 + ADX
            
            spy_data = self.repo.get_ohlcv_range('SPY', start_date, end_date, timeframe='15m')
            
            if len(spy_data) < 100:
                logger.warning("Insufficient SPY data for regime detection")
                return
            
            # Convert to DataFrame
            df = pd.DataFrame([{
                'date_time': bar.date_time,
                'open': bar.open,
                'high': bar.high,
                'low': bar.low,
                'close': bar.close,
                'volume': bar.volume
            } for bar in spy_data])
            df.set_index('date_time', inplace=True)
            df.sort_index(inplace=True)
            
            # Generate features (needed for ADX, SMA, ATR)
            features_df = self.feature_engineer.create_features(df)
            
            if features_df.empty:
                logger.warning("Failed to generate SPY features for regime detection")
                return
            
            # Detect regime
            regime = self.regime_detector.detect_regime(features_df)
            self.current_regime = regime
            
            logger.info(f"Market Regime: {regime.value.upper()}")
            
        except Exception as e:
            logger.error(f"Regime detection error: {e}", exc_info=True)
            self.current_regime = MarketRegime.SIDEWAYS_CALM  # Safe default
        
    def process_symbol(self, symbol: str):
        """Analyze symbol and execute trade if signal is strong."""
        try:
            # 0. Detect market regime first (if not already detected)
            if not hasattr(self, 'current_regime'):
                self.detect_market_regime()
            
            # 1. Check if market is open (optional, skipped for now to allow pre-market scans)
            
            # 2. Get recent 15-minute data (changed from daily)
            end_date = pd.Timestamp.now(tz='UTC')
            start_date = end_date - pd.Timedelta(days=30)  # ~30 days of 15m data = ~750 bars
            
            # Use 15-minute timeframe
            ohlcv = self.repo.get_ohlcv_range(symbol, start_date, end_date, timeframe='15m')
            
            # Require at least 500 bars (roughly 5 trading days of 15m data)
            if len(ohlcv) < 500:
                logger.debug(f"{symbol}: Insufficient 15m data ({len(ohlcv)} bars, need 500+)")
                return
            
            # 3. DataFrame conversion with VWAP support
            df = pd.DataFrame([{
                'date_time': bar.date_time,
                'open': bar.open,
                'high': bar.high,
                'low': bar.low,
                'close': bar.close,
                'volume': bar.volume,
                'vwap': bar.vwap if hasattr(bar, 'vwap') and bar.vwap else None,
                'symbol': symbol  # For sector feature
            } for bar in ohlcv])
            df.set_index('date_time', inplace=True)
            df.sort_index(inplace=True)
            
            # 4. Generate Features (includes VWAP distance if available)
            features_df = self.feature_engineer.create_features(df)
            if features_df.empty:
                return

            # 5. Predict (using regime-aware model if available)
            current_features = features_df.iloc[[-1]]
            scaled_features = self.feature_engineer.extract_feature_vector(
                current_features, fit_scaler=False
            )
            
            # Predict next 15-minute return
            prediction = self.predictor.predict_next(scaled_features, regime=self.current_regime)
            
            # 6. Execute Strategy (regime-adjusted)
            self._execute_trade_logic(symbol, prediction, df.iloc[-1]['close'])
            
        except Exception as e:
            logger.error(f"Error processing {symbol}: {e}")

    def _execute_trade_logic(self, symbol: str, prediction: float, current_price: float):
        """
        Execute trade based on prediction.
        Thresholds adjusted for 15-minute timeframe (smaller moves expected).
        """
        # Thresholds: Expected 15m return > 0.2% for BUY, < -0.2% for SELL
        # (More frequent trading with 15m data)
        buy_threshold = 0.002  
        sell_threshold = -0.002
        
        logger.info(f"{symbol} [15m] Pred: {prediction:.5f} | Price: {current_price}")
        
        if self.api is None:
            logger.warning("Alpaca API not initialized, skipping trade")
            return

        if prediction > buy_threshold:
            # BUY SIGNAL
            # Phase I.1: Check cooldown period
            can_enter, reason = self.risk_manager.can_enter_position(symbol)
            if not can_enter:
                logger.info(f"{symbol} BUY blocked: {reason}")
                return
            
            logger.info(f"{symbol} BUY allowed: {reason}")
            self._place_order(symbol, "buy", "limit", current_price)
            
        elif prediction < sell_threshold:
            # SELL SIGNAL
            # Check if we hold the position first
            if self._has_position(symbol):
                # Phase I.1: Check exit conditions
                active_position = self.repo.get_active_position(symbol)
                if active_position:
                    can_exit, reason = self.risk_manager.can_exit_position(
                        symbol=symbol,
                        entry_price=active_position.entry_price,
                        current_price=current_price,
                        entry_time=active_position.entry_time
                    )
                    
                    if not can_exit:
                        logger.info(f"{symbol} SELL blocked: {reason}")
                        return
                    
                    logger.info(f"{symbol} SELL allowed: {reason}")
                
                self._place_order(symbol, "sell", "market", current_price)
            else:
                logger.debug(f"{symbol}: Skip SELL (No position)")

    def _has_position(self, symbol: str) -> bool:
        """Check if position exists for symbol."""
        try:
            self.api.get_open_position(symbol)
            return True
        except Exception:
            return False

    def _place_order(self, symbol: str, side: str, order_type: str, price: float):
        """
        Place real order via Alpaca API using alpaca-py.
        
        Args:
            symbol: Stock symbol
            side: 'buy' or 'sell'
            order_type: 'market' or 'limit'
            price: Current price (used for limit orders and buying power check)
        """
        try:
            qty = 1  # Fixed quantity for safety
            
            if side == "buy":
                # Check buying power
                account = self.api.get_account()
                if float(account.buying_power) < price * qty:
                    logger.warning(f"Insufficient buying power for {symbol}")
                    return

            # Create order request object
            order_side = OrderSide.BUY if side == "buy" else OrderSide.SELL
            
            if order_type == 'market':
                order_data = MarketOrderRequest(
                    symbol=symbol,
                    qty=qty,
                    side=order_side,
                    time_in_force=TimeInForce.DAY
                )
            else:
                order_data = LimitOrderRequest(
                    symbol=symbol,
                    qty=qty,
                    side=order_side,
                    time_in_force=TimeInForce.DAY,
                    limit_price=price
                )
            
            # Submit order
            order = self.api.submit_order(order_data=order_data)
            
            logger.info(f"ORDER PLACED: {side.upper()} {symbol} (ID: {order.id})")
            
            # Phase I.1: Record position entry/exit in DB
            if side == "buy":
                from datetime import datetime
                entry_time = datetime.now()
                self.repo.record_position_entry(symbol, price, qty, entry_time)
                self.risk_manager.record_position_entry(symbol, entry_time)
                self.db.commit()
            elif side == "sell":
                active_position = self.repo.get_active_position(symbol)
                if active_position:
                    self.repo.update_position_exit(active_position.id, price)
                    self.risk_manager.record_position_exit(symbol)
                    self.db.commit()
            
        except Exception as e:
            logger.error(f"❌ Order failed for {symbol}: {e}", exc_info=True) 
    
    def process_portfolio(self, symbols: List[str]):
        """
        Process multiple symbols for multi-position portfolio trading (Phase I.2).
        
        Strategy:
        1. Detect market regime
        2. Get current active positions
        3. Calculate Kelly position sizes for each symbol
        4. Select uncorrelated symbols (max 5 positions)
        5. Execute BUY/SELL orders based on signals and portfolio optimization
        
        Args:
            symbols: List of symbols to analyze
        """
        try:
            logger.info(f"Processing portfolio with {len(symbols)} symbols")
            
            # 1. Detect market regime
            self.detect_market_regime()
            
            # 2. Get current active positions
            active_positions = self.portfolio_repo.get_all_active_positions()
            active_symbols = {pos['symbol'] for pos in active_positions}
            
            logger.info(f"Active positions: {len(active_positions)} / {self.max_positions}")
            
            # 3. Get account info
            account = self.api.get_account()
            portfolio_value = float(account.portfolio_value)
            buying_power = float(account.buying_power)
            
            # 4. Calculate correlation matrix and Kelly sizes
            corr_matrix = self.optimizer.calculate_correlation_matrix(
                self.portfolio_repo, 
                symbols, 
                use_live_data=True
            )
            
            # 5. Analyze each symbol
            signals = {}  # {symbol: {'signal': float, 'kelly': float, 'price': float}}
            
            for symbol in symbols:
                try:
                    # Get prediction signal
                    end_date = pd.Timestamp.now(tz='UTC')
                    start_date = end_date - pd.Timedelta(days=30)
                    
                    ohlcv = self.repo.get_ohlcv_range(symbol, start_date, end_date, timeframe='15m')
                    if len(ohlcv) < 500:
                        continue
                    
                    df = pd.DataFrame([{
                        'date_time': bar.date_time,
                        'open': bar.open,
                        'high': bar.high,
                        'low': bar.low,
                        'close': bar.close,
                        'volume': bar.volume,
                        'symbol': symbol
                    } for bar in ohlcv])
                    df.set_index('date_time', inplace=True)
                    
                    features_df = self.feature_engineer.create_features(df)
                    if features_df.empty:
                        continue
                    
                    latest_features = features_df.iloc[[-1]]
                    X_norm = self.feature_engineer.extract_feature_vector(latest_features)
                    
                    # Get prediction with regime awareness
                    prediction = self.predictor.predict_next(X_norm, regime=self.current_regime)
                    
                    # Calculate Kelly position size
                    kelly_size = self.optimizer.kelly_criterion(
                        self.portfolio_repo,
                        symbol,
                        use_live_data=True
                    )
                    
                    current_price = df['close'].iloc[-1]
                    
                    signals[symbol] = {
                        'signal': prediction,
                        'kelly': kelly_size,
                        'price': current_price
                    }
                    
                    logger.info(f"  {symbol}: Signal={prediction:.5f}, Kelly={kelly_size:.2%}, Price=${current_price:.2f}")
                    
                except Exception as e:
                    logger.error(f"Failed to analyze {symbol}: {e}")
                    continue
            
            # 6. Select uncorrelated symbols for portfolio
            selected_symbols = self._select_uncorrelated_symbols(
                signals, 
                corr_matrix, 
                active_symbols,
                max_new_positions=self.max_positions - len(active_positions)
            )
            
            logger.info(f"Selected symbols for action: {selected_symbols}")
            
            # 7. Execute trades
            for symbol in signals.keys():
                signal_data = signals[symbol]
                
                if symbol in active_symbols:
                    # Already have position - check for SELL
                    self._process_sell_signal(symbol, signal_data)
                elif symbol in selected_symbols and len(active_positions) < self.max_positions:
                    # New position - check for BUY
                    self._process_buy_signal(symbol, signal_data, portfolio_value)
            
            logger.info("Portfolio processing complete")
            
        except Exception as e:
            logger.error(f"Portfolio processing error: {e}", exc_info=True)
    
    def _select_uncorrelated_symbols(
        self,
        signals: Dict[str, Dict],
        corr_matrix: pd.DataFrame,
        active_symbols: set,
        max_new_positions: int
    ) -> List[str]:
        """
        Select symbols with low correlation to existing positions.
        
        Strategy:
        - Prioritize symbols with strong BUY signals (signal > 0.005)
        - Avoid symbols highly correlated with active positions (corr > 0.7)
        - Limit to max_new_positions
        """
        candidates = []
        
        for symbol, data in signals.items():
            if symbol in active_symbols:
                continue
            
            if data['signal'] <= 0.005:  # Weak signal
                continue
            
            # Check correlation with active positions
            max_corr = 0.0
            for active_sym in active_symbols:
                if active_sym in corr_matrix.index and symbol in corr_matrix.columns:
                    corr = abs(corr_matrix.loc[active_sym, symbol])
                    max_corr = max(max_corr, corr)
            
            candidates.append({
                'symbol': symbol,
                'signal': data['signal'],
                'max_corr': max_corr,
                'kelly': data['kelly']
            })
        
        # Sort by signal strength (descending)
        candidates.sort(key=lambda x: x['signal'], reverse=True)
        
        # Filter by correlation threshold and select top N
        selected = []
        for cand in candidates:
            if cand['max_corr'] < 0.7:  # Low correlation
                selected.append(cand['symbol'])
                if len(selected) >= max_new_positions:
                    break
        
        return selected
    
    def _process_buy_signal(self, symbol: str, signal_data: Dict, portfolio_value: float):
        """Process BUY signal with Kelly position sizing."""
        try:
            # Check cooldown
            can_enter, reason = self.risk_manager.can_enter_position(symbol)
            if not can_enter:
                logger.info(f"{symbol} BUY blocked: {reason}")
                return
            
            # Calculate position size using Kelly
            kelly_fraction = signal_data['kelly']
            position_value = portfolio_value * kelly_fraction
            current_price = signal_data['price']
            qty = int(position_value / current_price)
            
            if qty < 1:
                logger.info(f"{symbol} BUY skipped: Kelly size too small ({kelly_fraction:.2%})")
                return
            
            logger.info(f"{symbol} BUY: {qty} shares @ ${current_price:.2f} (Kelly: {kelly_fraction:.2%})")
            
            # Place order using alpaca-py
            order_data = MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY
            )
            order = self.api.submit_order(order_data=order_data)
            
            logger.info(f"ORDER PLACED: BUY {symbol} (ID: {order.id})")
            
            # Record in DB
            entry_time = datetime.now()
            self.repo.record_position_entry(symbol, current_price, qty, entry_time)
            self.risk_manager.record_position_entry(symbol, entry_time)
            self.session.commit()
            
        except Exception as e:
            logger.error(f"BUY order failed for {symbol}: {e}")
    
    def _process_sell_signal(self, symbol: str, signal_data: Dict):
        """Process SELL signal with defense checks."""
        try:
            # Get active position
            active_position = self.repo.get_active_position(symbol)
            if not active_position:
                return
            
            current_price = signal_data['price']
            
            # Check exit conditions (Phase I.1 defense)
            can_exit, reason = self.risk_manager.can_exit_position(
                symbol=symbol,
                entry_price=active_position.entry_price,
                current_price=current_price,
                entry_time=active_position.entry_time
            )
            
            if not can_exit and signal_data['signal'] >= -0.002:
                # Weak SELL signal + defense blocked
                logger.info(f"{symbol} SELL blocked: {reason}")
                return
            
            logger.info(f"{symbol} SELL allowed: {reason}")
            
            # Place order using alpaca-py
            qty = active_position.quantity
            order_data = MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY
            )
            order = self.api.submit_order(order_data=order_data)
            
            logger.info(f"ORDER PLACED: SELL {symbol} (ID: {order.id})")
            
            # Update DB
            self.repo.update_position_exit(active_position.id, current_price)
            self.risk_manager.record_position_exit(symbol)
            self.session.commit()
            
        except Exception as e:
            logger.error(f"SELL order failed for {symbol}: {e}") 
