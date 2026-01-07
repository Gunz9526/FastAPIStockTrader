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
from app.services.risk_manager import RiskManager
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
        self.db = session  # RiskManager에서 사용
        self.repo = SyncStockRepository(session)
        self.portfolio_repo = PortfolioRepository(session)
        self.predictor = PredictorService()
        self.feature_engineer = FeatureEngineer()
        self.regime_detector = RegimeDetector()  # 레짐 감지
        self.current_regime = MarketRegime.SIDEWAYS_CALM  # 기본값
        self.risk_manager = RiskManager()  # Risk Management 활성화
        
        try:
            from app.services.sentiment_analyzer import get_sentiment_analyzer
            from app.services.fundamental_provider import get_fundamental_provider
            self.sentiment_analyzer = get_sentiment_analyzer()
            self.fundamental_provider = get_fundamental_provider()
            logger.info("Sentiment & Fundamentals analyzers initialized")
        except (ImportError, AttributeError, ValueError) as e:
            logger.warning("Phase F analyzers 초기화 실패: %s", str(e))
            self.sentiment_analyzer = None
            self.fundamental_provider = None
        
        self.optimizer = PortfolioOptimizer(lookback_days=14, min_live_trades=50)
        self.max_positions = 5  # Max 5 concurrent positions
        self.multi_position_mode = True  # Enable multi-position trading
        
        self.sentiment_weight = 0.15  # Sentiment 영향도 15%
        self.fundamentals_weight = 0.10  # Fundamentals 영향도 10%
        self.ml_prediction_weight = 0.75  # ML 예측 영향도 75%
        
        # Alpaca API 초기화
        try:
            # URL 기반 페이퍼 트레이딩 판단
            is_paper = 'paper' in settings.ALPACA_TRADING_URL.lower()
            self.api = TradingClient(
                api_key=settings.ALPACA_API_KEY,
                secret_key=settings.ALPACA_SECRET_KEY,
                paper=is_paper
            )
            logger.info("Alpaca API 연결 성공 (페이퍼: %s)", is_paper)
        except Exception as e:
            logger.error("Alpaca 연결 실패: %s", str(e))
            self.api = None
    
    def detect_market_regime(self):
        """
        SPY 데이터를 사용하여 현재 시장 레짐 감지.
        Phase F.3: VIX 통합으로 강화.
        self.current_regime 업데이트.
        """
        try:
            # 레짐 감지용 SPY 데이터 가져오기
            end_date = pd.Timestamp.now(tz='UTC')
            start_date = end_date - pd.Timedelta(days=90)  # SMA50 + ADX를 위한 90일
            
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
            
            # Phase F.3: Redis 캠시에서 VIX 값 가져오기
            vix_value = None
            try:
                from app.core.cache import cache
                vix_cached = cache.get("vix:latest")
                if vix_cached:
                    vix_value = float(vix_cached)
                    logger.info("캠시에서 VIX 값 가져오기: %.2f", vix_value)
            except (ImportError, ValueError, TypeError) as e:
                logger.debug("캠시에서 VIX 가져오기 실패: %s", str(e))
            
            # VIX 강화로 레짐 감지
            regime = self.regime_detector.detect_regime(features_df, vix_value=vix_value)
            self.current_regime = regime
            
            logger.info("시장 레짐: %s", regime.value.upper())
            
        except (ValueError, KeyError, AttributeError) as e:
            logger.error("레짐 감지 오류: %s", str(e), exc_info=True)
            self.current_regime = MarketRegime.SIDEWAYS_CALM  # 안전 기본값
        
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
            
            # 최소 500개 바 필요 (약 5 거래일분 15분봉 데이터)
            if len(ohlcv) < 500:
                logger.debug("%s: 15분봉 데이터 부족 (%d bars, 500+ 필요)", symbol, len(ohlcv))
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
        Execute trade based on prediction + sentiment + fundamentals.
        Phase F: Adaptive signal adjustment with auto-weighted factors.
        """
        # Phase F: sentiment와 fundamentals 가져오기
        sentiment_score, fundamentals = self._get_phase_f_signals(symbol)
        
        # Phase F: 가중치 기반 예측 조정
        adjusted_prediction = self._calculate_adjusted_signal(
            ml_prediction=prediction,
            sentiment_score=sentiment_score,
            fundamentals=fundamentals
        )
        
        # 임계값: 15분봉 예상 수익률 BUY > 0.2%, SELL < -0.2%
        buy_threshold = 0.002  
        sell_threshold = -0.002
        
        logger.info(
            "%s [15m] ML: %.5f | Sentiment: %.2f | 조정: %.5f | 가격: %s",
            symbol, prediction, sentiment_score, adjusted_prediction, current_price
        )
        
        if self.api is None:
            logger.warning("Alpaca API not initialized, skipping trade")
            return

        # 조정된 예측 사용
        if adjusted_prediction > buy_threshold:
            # BUY 신호
            can_enter, reason = self.risk_manager.can_enter_position(symbol)
            if not can_enter:
                logger.info("%s BUY 차단: %s", symbol, reason)
                return
            
            logger.info("%s BUY 허용: %s", symbol, reason)
            self._place_order(symbol, "buy", "limit", current_price)
            
        elif adjusted_prediction < sell_threshold:
            # SELL 신호
            # 먼저 포지션 보유 여부 확인
            if self._has_position(symbol):
                active_position = self.repo.get_active_position(symbol)
                if active_position:
                    can_exit, reason = self.risk_manager.can_exit_position(
                        symbol=symbol,
                        entry_price=active_position.entry_price,
                        current_price=current_price,
                        entry_time=active_position.entry_time
                    )
                    
                    if not can_exit:
                        logger.info("%s SELL 차단: %s", symbol, reason)
                        return
                    
                    logger.info("%s SELL 허용: %s", symbol, reason)
                
                self._place_order(symbol, "sell", "market", current_price)
            else:
                logger.debug("%s: SELL 건너뛰기 (포지션 없음)", symbol)

    def _get_phase_f_signals(self, symbol: str) -> tuple:
        """
        Get sentiment and fundamentals signals for a symbol.
        
        Returns:
            tuple: (sentiment_score, fundamentals_dict)
        """
        # 기본값 (중립)
        sentiment_score = 0.0
        fundamentals = {'pe_ratio': 15.0, 'pb_ratio': 3.0, 'overvalued': False}
        
        try:
            # 캠시에서 sentiment 가져오기 (1시간 TTL)
            if self.sentiment_analyzer:
                sentiment_score = self.sentiment_analyzer.get_sentiment_from_cache(symbol)
                if sentiment_score is None:
                    sentiment_score = 0.0  # 데이터 없으면 중립값
        except (AttributeError, ValueError, TypeError) as e:
            logger.debug("%s sentiment 가져오기 실패: %s", symbol, str(e))
        
        try:
            # Fundamentals 가져오기 (yfinance LRU 캠시)
            if self.fundamental_provider:
                fund_data = self.fundamental_provider.get_fundamentals(symbol)
                if fund_data:
                    fundamentals = {
                        'pe_ratio': fund_data.get('pe_ratio', 15.0),
                        'pb_ratio': fund_data.get('pb_ratio', 3.0),
                        'overvalued': fund_data.get('pe_ratio', 15.0) > 40  # PE > 40 = 고평가
                    }
        except (AttributeError, ValueError, TypeError) as e:
            logger.debug("%s fundamentals 가져오기 실패: %s", symbol, str(e))
        
        return sentiment_score, fundamentals
    
    def _calculate_adjusted_signal(self, ml_prediction: float, sentiment_score: float, fundamentals: dict) -> float:
        """
        Calculate adjusted trading signal with adaptive weights.
        
        Formula:
            Adjusted = (ML * 0.75) + (Sentiment * 0.15) + (Fundamentals * 0.10)
        
        Args:
            ml_prediction: Model prediction (-0.05 to +0.05 range)
            sentiment_score: Sentiment score (-1.0 to +1.0)
            fundamentals: Fundamentals dict with PE, PB ratios
        
        Returns:
            Adjusted prediction value
        """
        # Sentiment adjustment: -1.0 (극도 부정) to +1.0 (극도 긍정)
        # Scale to same magnitude as ML prediction
        sentiment_adjustment = sentiment_score * 0.005  # Max ±0.005
        
        # Fundamentals adjustment: Overvalued penalty
        fundamentals_adjustment = 0.0
        if fundamentals['overvalued']:
            fundamentals_adjustment = -0.003  # Penalty for overvalued stocks
        elif fundamentals['pe_ratio'] < 10:  # Undervalued
            fundamentals_adjustment = 0.002  # Bonus for undervalued stocks
        
        # Weighted combination
        adjusted = (
            ml_prediction * self.ml_prediction_weight +
            sentiment_adjustment * self.sentiment_weight +
            fundamentals_adjustment * self.fundamentals_weight
        )
        
        return adjusted
    
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
                # 매수 가능 금액 확인
                account = self.api.get_account()
                if float(account.buying_power) < price * qty:
                    logger.warning("%s 매수 가능 금액 부족", symbol)
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
            
            # 주문 제출
            order = self.api.submit_order(order_data=order_data)
            
            logger.info("주문 실행: %s %s (ID: %s)", side.upper(), symbol, order.id)
            
            # Phase I.1: DB에 포지션 진입/종료 기록
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
            logger.error("%s 주문 실패: %s", symbol, str(e), exc_info=True) 
    
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
            logger.info("%d개 심볼로 포트폴리오 처리 중", len(symbols))
            
            # 1. 시장 레짐 감지
            self.detect_market_regime()
            
            # 2. 현재 활성 포지션 가져오기
            active_positions = self.portfolio_repo.get_all_active_positions()
            active_symbols = {pos['symbol'] for pos in active_positions}
            
            logger.info("활성 포지션: %d / %d", len(active_positions), self.max_positions)
            
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
                    
                    logger.info("  %s: 신호=%.5f, Kelly=%.2f%%, 가격=$%.2f", symbol, prediction, kelly_size * 100, current_price)
                    
                except (ValueError, KeyError, AttributeError) as e:
                    logger.error("%s 분석 실패: %s", symbol, str(e))
                    continue
            
            # 6. 비상관 심볼 선택
            selected_symbols = self._select_uncorrelated_symbols(
                signals, 
                corr_matrix, 
                active_symbols,
                max_new_positions=self.max_positions - len(active_positions)
            )
            
            logger.info("선택된 심볼: %s", selected_symbols)
            
            # 7. 거래 실행
            for symbol in signals.keys():
                signal_data = signals[symbol]
                
                if symbol in active_symbols:
                    # 이미 포지션 보유 - SELL 확인
                    self._process_sell_signal(symbol, signal_data)
                elif symbol in selected_symbols and len(active_positions) < self.max_positions:
                    # 신규 포지션 - BUY 확인
                    self._process_buy_signal(symbol, signal_data, portfolio_value)
            
            logger.info("포트폴리오 처리 완료")
            
        except (ValueError, KeyError, AttributeError) as e:
            logger.error("포트폴리오 처리 오류: %s", str(e), exc_info=True)
    
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
            # 쿨다운 확인
            can_enter, reason = self.risk_manager.can_enter_position(symbol)
            if not can_enter:
                logger.info("%s BUY 차단: %s", symbol, reason)
                return
            
            # Kelly 기반 포지션 크기 계산
            kelly_fraction = signal_data['kelly']
            position_value = portfolio_value * kelly_fraction
            current_price = signal_data['price']
            qty = int(position_value / current_price)
            
            if qty < 1:
                logger.info("%s BUY 건너뛰기: Kelly 크기 너무 작음 (%.2f%%)", symbol, kelly_fraction * 100)
                return
            
            logger.info("%s BUY: %d주 @ $%.2f (Kelly: %.2f%%)", symbol, qty, current_price, kelly_fraction * 100)
            
            # alpaca-py로 주문
            order_data = MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY
            )
            order = self.api.submit_order(order_data=order_data)
            
            logger.info("주문 실행: BUY %s (ID: %s)", symbol, order.id)
            
            # DB에 기록
            entry_time = datetime.now()
            self.repo.record_position_entry(symbol, current_price, qty, entry_time)
            self.risk_manager.record_position_entry(symbol, entry_time)
            self.session.commit()
            
        except (ValueError, AttributeError, TypeError) as e:
            logger.error("%s BUY 주문 실패: %s", symbol, str(e))
    
    def _process_sell_signal(self, symbol: str, signal_data: Dict):
        """Process SELL signal with defense checks."""
        try:
            # 활성 포지션 가져오기
            active_position = self.repo.get_active_position(symbol)
            if not active_position:
                return
            
            current_price = signal_data['price']
            
            # 종료 조건 확인 (Phase I.1 방어)
            can_exit, reason = self.risk_manager.can_exit_position(
                symbol=symbol,
                entry_price=active_position.entry_price,
                current_price=current_price,
                entry_time=active_position.entry_time
            )
            
            if not can_exit and signal_data['signal'] >= -0.002:
                # 약한 SELL 신호 + 방어 차단
                logger.info("%s SELL 차단: %s", symbol, reason)
                return
            
            logger.info("%s SELL 허용: %s", symbol, reason)
            
            # alpaca-py로 주문
            qty = active_position.quantity
            order_data = MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY
            )
            order = self.api.submit_order(order_data=order_data)
            
            logger.info("주문 실행: SELL %s (ID: %s)", symbol, order.id)
            
            # DB 업데이트
            self.repo.update_position_exit(active_position.id, current_price)
            self.risk_manager.record_position_exit(symbol)
            self.session.commit()
            
        except (ValueError, AttributeError, TypeError) as e:
            logger.error("%s SELL 주문 실패: %s", symbol, str(e)) 
