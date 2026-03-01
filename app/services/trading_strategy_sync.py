import logging
from datetime import UTC, datetime

import pandas as pd
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest

from app.core.config import settings
from app.ml.features import FeatureEngineer
from app.ml.predictor import PredictorService
from app.repositories.portfolio_repo import PortfolioRepository
from app.repositories.stock_repo_sync import SyncStockRepository
from app.services.discord_notifier import discord_notifier
from app.services.momentum_scorer import CrossSectionalMomentum
from app.services.portfolio_optimizer import PortfolioOptimizer
from app.services.regime import MarketRegime, RegimeDetector, REGIME_STRATEGY_WEIGHTS
from app.services.risk_manager import RiskManager

from app.domain.schemas.intraday import EntrySignal, ExitSignal

logger = logging.getLogger(__name__)

class SyncTradingStrategy:
    """
    Synchronous Trading Strategy Engine (Production).
    Uses PredictorService for signals and Alpaca API for execution.
    [Phase H] Includes market regime detection for adaptive trading.
    [Phase H.4] Regime-specific trading thresholds for improved bull market handling.
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

        # Phase H.4: Load regime-specific trading configuration
        from app.core.config import REGIME_TRADING_CONFIG
        self.regime_config = REGIME_TRADING_CONFIG

        # Circuit Breaker 초기화 (일일 손실, API 레이턴시, VIX 극단치 모니터링)
        try:
            from app.services.circuit_breaker import get_circuit_breaker
            self.circuit_breaker = get_circuit_breaker()
            logger.info("Circuit Breaker 초기화됨")
        except (ImportError, Exception) as e:
            logger.warning("Circuit Breaker 초기화 실패: %s", str(e))
            self.circuit_breaker = None

        try:
            from app.services.fundamental_provider import get_fundamental_provider
            from app.services.sentiment_analyzer import get_sentiment_analyzer
            self.sentiment_analyzer = get_sentiment_analyzer()
            self.fundamental_provider = get_fundamental_provider()
            logger.info("감성 및 펀더멘털 분석기 초기화됨")
        except (ImportError, AttributeError, ValueError) as e:
            logger.warning("Phase F analyzers 초기화 실패: %s", str(e))
            self.sentiment_analyzer = None
            self.fundamental_provider = None

        self.optimizer = PortfolioOptimizer(lookback_days=14, min_live_trades=50)
        self.max_positions = 5  # Max 5 concurrent positions
        self.multi_position_mode = True  # Enable multi-position trading

        # Signal weights are now dynamic per regime (see REGIME_STRATEGY_WEIGHTS)
        # Fallback weights used when regime is unknown
        self._default_weights = {
            'ml_prediction': 0.75,
            'sentiment': 0.15,
            'fundamentals': 0.10,
        }

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
        self.current_regime 업데이트.
        
        Redis 캐시 우선 확인 (5분 TTL) → 없으면 새로 계산
        """
        try:
            # Redis 캐시에서 먼저 확인
            from app.core.cache import cache
            cached_regime = cache.get("market:regime")
            if cached_regime:
                try:
                    self.current_regime = MarketRegime(cached_regime)
                    logger.debug("캐시된 레짐 사용: %s", cached_regime.upper())
                    return
                except ValueError:
                    logger.warning("잘못된 캐시 레짐 값: %s", cached_regime)
        except Exception as e:
            logger.debug("Regime 캐시 확인 실패: %s", e)

        # 캐시 없음 → 새로 계산
        try:
            # 레짐 감지용 SPY 데이터 가져오기
            end_date = pd.Timestamp.now(tz='UTC')
            start_date = end_date - pd.Timedelta(days=90)  # SMA50 + ADX를 위한 90일

            spy_data = self.repo.get_ohlcv_range('SPY', start_date, end_date, timeframe='1d')

            if len(spy_data) < 50:
                logger.warning("레짐 감지를 위한 SPY 일봉 데이터 부족")
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
                logger.warning("SPY 특성 생성 실패: 레짐 감지 불가")
                return

            vix_value = None
            try:
                from app.core.cache import cache
                vix_cached = cache.get("vix:latest")
                if vix_cached:
                    vix_value = float(vix_cached)
                    logger.info("캐시에서 VIX 값 가져오기: %.2f", vix_value)
            except (ImportError, ValueError, TypeError) as e:
                logger.debug("캐시에서 VIX 가져오기 실패: %s", str(e))

            # VIX 강화로 레짐 감지
            regime = self.regime_detector.detect_regime(features_df, vix_value=vix_value)
            self.current_regime = regime

            # Redis에 캐시 (1시간 TTL - 일봉 기준 충분)
            try:
                from app.core.cache import cache
                cache.set("market:regime", regime.value, ttl_seconds=3600)
                logger.info("시장 레짐 감지 및 캐시: %s", regime.value.upper())
            except Exception as cache_err:
                logger.warning("Regime 캐시 실패: %s", cache_err)
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

            # 2. Get recent daily OHLCV data
            end_date = pd.Timestamp.now(tz='UTC')
            start_date = end_date - pd.Timedelta(days=365)  # 1년 일봉 데이터

            # Use daily timeframe
            ohlcv = self.repo.get_ohlcv_range(symbol, start_date, end_date, timeframe='1d')

            # 최소 50개 일봉 필요 (약 2.5개월 거래일)
            if len(ohlcv) < 50:
                logger.debug("%s: 일봉 데이터 부족 (%d bars, 50+ 필요)", symbol, len(ohlcv))
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
            # Determine effective prediction regime (handle fallback BEFORE scaling)
            current_features = features_df.iloc[[-1]]
            regime_key = self.current_regime.value if self.current_regime else 'sideways_calm'
            prediction_regime = self.current_regime

            # Check for regime fallback (e.g., bull_trending uses sideways_calm model)
            config = self.regime_config.get(regime_key, {})
            fallback_regime = config.get('fallback_to_regime')

            if fallback_regime:
                try:
                    prediction_regime = MarketRegime(fallback_regime)
                    logger.info(
                        "%s: %s regime fallback to %s model (better performance)",
                        symbol, regime_key, fallback_regime
                    )
                except ValueError:
                    logger.warning("Invalid fallback regime: %s", fallback_regime)

            # Scale using the MODEL's regime scaler (not current regime if fallback)
            effective_regime = prediction_regime.value if prediction_regime else 'sideways_calm'
            scaled_features = self.feature_engineer.extract_feature_vector(
                current_features, fit_scaler=False, feature_set="base",
                scaler_suffix=effective_regime
            )

            # Predict next daily class (UP/NEUTRAL/DOWN)
            predicted_class, confidence, probabilities = self.predictor.predict_class(
                scaled_features, regime=prediction_regime
            )

            # 6. Execute Strategy (regime-adjusted classification)
            self._execute_trade_logic(
                symbol, predicted_class, confidence, probabilities, df.iloc[-1]['close']
            )

        except Exception as e:
            logger.error(f"{symbol} 처리 중 오류: {e}")

    def _execute_trade_logic(
        self,
        symbol: str,
        predicted_class: int,
        confidence: float,
        probabilities: dict[str, float],
        current_price: float,
    ):
        """
        Execute trade based on classification prediction + sentiment + fundamentals.

        Phase H.4: Uses regime-specific confidence_threshold from REGIME_TRADING_CONFIG.
        Classification: 0=DOWN, 1=NEUTRAL, 2=UP.
        """
        # Phase H.4: Get regime-specific configuration
        regime_key = self.current_regime.value if self.current_regime else 'sideways_calm'
        config = self.regime_config.get(regime_key, self.regime_config['sideways_calm'])

        # Check if trading is enabled for this regime
        if not config.get('enabled', True):
            logger.info("%s [%s] 거래 비활성화 - 레짐 설정에 따라 스킵", symbol, regime_key)
            return

        # Get regime-specific thresholds
        confidence_threshold = config['confidence_threshold']
        position_scale = config['position_scale']
        min_profit_required = config.get('min_profit_required', 0.015)  # Default 1.5%
        min_hold_days = config.get('min_hold_days', 1)

        # sentiment와 fundamentals 가져오기
        sentiment_score, fundamentals = self._get_phase_f_signals(symbol)

        # 가중치 기반 신뢰도 조정
        adjusted_confidence = self._calculate_adjusted_confidence(
            predicted_class=predicted_class,
            confidence=confidence,
            probabilities=probabilities,
            sentiment_score=sentiment_score,
            fundamentals=fundamentals,
        )

        class_names = {0: "DOWN", 1: "NEUTRAL", 2: "UP"}
        logger.info(
            "%s [1d][%s] 예측: %s(%.1f%%) | 조정신뢰도: %.1f%% | 감성: %.2f | "
            "가격: %s | 신뢰임계값: %.0f%% | 포지션스케일: %.0f%%",
            symbol, regime_key, class_names.get(predicted_class, "?"),
            confidence * 100, adjusted_confidence * 100,
            sentiment_score, current_price,
            confidence_threshold * 100, position_scale * 100,
        )

        if self.api is None:
            logger.warning("Alpaca API 미초기화 — 주문 생략")
            return

        # Classification-based trading logic
        if predicted_class == 2 and adjusted_confidence >= confidence_threshold:
            # BUY 신호: UP class with sufficient confidence
            can_enter, reason = self.risk_manager.can_enter_position(symbol)
            if not can_enter:
                logger.info("%s BUY 차단: %s", symbol, reason)
                return

            logger.info(
                "%s BUY 허용: %s (신뢰도: %.1f%%)",
                symbol, reason, adjusted_confidence * 100,
            )
            self._place_order(symbol, "buy", "limit", current_price, position_scale)

        elif predicted_class == 0 and adjusted_confidence >= confidence_threshold:
            # SELL 신호: DOWN class with sufficient confidence
            if self._has_position(symbol):
                active_position = self.repo.get_active_position(symbol)
                if active_position:
                    entry_price = active_position.entry_price
                    profit_pct = (current_price - entry_price) / entry_price

                    # Phase H.4: Check regime-specific minimum profit
                    if profit_pct < min_profit_required and profit_pct > -0.03:
                        logger.info(
                            "%s SELL 차단: 최소수익 미달 (현재: %.2f%%, 필요: %.2f%%) [%s]",
                            symbol, profit_pct * 100, min_profit_required * 100, regime_key,
                        )
                        return

                    # Check RiskManager rules (hold time, etc.)
                    can_exit, reason = self.risk_manager.can_exit_position(
                        symbol=symbol,
                        entry_price=entry_price,
                        current_price=current_price,
                        entry_time=active_position.entry_time,
                        hold_multiplier=float(min_hold_days),
                    )

                    if not can_exit:
                        logger.info("%s SELL 차단: %s", symbol, reason)
                        return

                    logger.info(
                        "%s SELL 허용: %s (수익: %.2f%%)",
                        symbol, reason, profit_pct * 100,
                    )

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
            # 캐시에서 sentiment 가져오기 (1시간 TTL)
            if self.sentiment_analyzer:
                sentiment_score = self.sentiment_analyzer.get_cached_sentiment(symbol)
                if sentiment_score is None:
                    sentiment_score = 0.0  # 데이터 없으면 중립값
        except (AttributeError, ValueError, TypeError) as e:
            logger.debug("%s sentiment 가져오기 실패: %s", symbol, str(e))

        try:
            # Fundamentals 가져오기 (yfinance LRU 캐시)
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

    def _calculate_adjusted_confidence(
        self,
        predicted_class: int,
        confidence: float,
        probabilities: dict[str, float],
        sentiment_score: float,
        fundamentals: dict,
    ) -> float:
        """
        Calculate adjusted confidence for classification-based trading.

        Adjusts the raw prediction confidence using sentiment and fundamentals
        signals, weighted by regime-specific REGIME_STRATEGY_WEIGHTS.

        For BUY (class=2/UP):
            - Positive sentiment → boost confidence
            - Undervalued fundamentals → boost confidence
        For SELL (class=0/DOWN):
            - Negative sentiment → boost confidence
            - Overvalued fundamentals → boost confidence
        For NEUTRAL (class=1):
            - No adjustment (stay neutral)

        Args:
            predicted_class: 0=DOWN, 1=NEUTRAL, 2=UP
            confidence: Raw model confidence (0.0 - 1.0)
            probabilities: Class probability dict {"DOWN": p, "NEUTRAL": p, "UP": p}
            sentiment_score: Sentiment score (-1.0 to +1.0)
            fundamentals: Fundamentals dict with pe_ratio, pb_ratio, overvalued keys

        Returns:
            Adjusted confidence value (0.0 - 1.0)
        """
        # Resolve regime-specific weights
        regime_key = (
            self.current_regime.value
            if isinstance(self.current_regime, MarketRegime)
            else str(self.current_regime)
        )
        weights = REGIME_STRATEGY_WEIGHTS.get(regime_key, self._default_weights)

        ml_weight: float = weights['ml_prediction']
        sentiment_weight: float = weights['sentiment']
        fundamentals_weight: float = weights['fundamentals']

        logger.debug(
            "Confidence weights [regime=%s]: ML=%.2f, Sentiment=%.2f, Fundamentals=%.2f",
            regime_key, ml_weight, sentiment_weight, fundamentals_weight,
        )

        # --- 1. ML confidence (base) ---
        ml_component: float = confidence

        # --- 2. Sentiment adjustment (-1.0 to +1.0) → confidence modifier ---
        # For UP prediction: positive sentiment boosts, negative dampens
        # For DOWN prediction: negative sentiment boosts, positive dampens
        if predicted_class == 2:  # UP
            sentiment_modifier = sentiment_score * 0.1  # ±10% max adjustment
        elif predicted_class == 0:  # DOWN
            sentiment_modifier = -sentiment_score * 0.1  # Reversed
        else:  # NEUTRAL
            sentiment_modifier = 0.0

        sentiment_component: float = max(0.0, min(1.0, 0.5 + sentiment_modifier))

        # --- 3. Fundamentals adjustment ---
        pe_ratio: float = fundamentals.get('pe_ratio', 15.0)
        if predicted_class == 2:  # UP — penalize overvalued, boost undervalued
            if pe_ratio > 40:
                fund_modifier = -0.1 * min((pe_ratio - 15) / 50, 1.0)
            elif pe_ratio < 10:
                fund_modifier = 0.1 * min((15 - pe_ratio) / 15, 1.0)
            else:
                fund_modifier = 0.05 * (15 - pe_ratio) / 15
        elif predicted_class == 0:  # DOWN — boost overvalued, penalize undervalued
            if pe_ratio > 40:
                fund_modifier = 0.1 * min((pe_ratio - 15) / 50, 1.0)
            elif pe_ratio < 10:
                fund_modifier = -0.1 * min((15 - pe_ratio) / 15, 1.0)
            else:
                fund_modifier = -0.05 * (15 - pe_ratio) / 15
        else:
            fund_modifier = 0.0

        fundamentals_component: float = max(0.0, min(1.0, 0.5 + fund_modifier))

        # --- 4. Weighted combination ---
        adjusted: float = (
            ml_component * ml_weight
            + sentiment_component * sentiment_weight
            + fundamentals_component * fundamentals_weight
        )

        # Clamp to [0, 1]
        return max(0.0, min(1.0, adjusted))

    def _has_position(self, symbol: str) -> bool:
        """Check if position exists for symbol."""
        try:
            self.api.get_open_position(symbol)
            return True
        except Exception:
            return False

    def _place_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        price: float,
        position_scale: float = 1.0
    ):
        """
        Place real order via Alpaca API using alpaca-py.

        Concurrency Control:
            - Redis distributed lock prevents duplicate orders from parallel workers
            - DB pessimistic lock (with_for_update) prevents dirty reads during position update

        Circuit Breaker Integration:
            - track_api_call: API 레이턴시 추적
            - record_trade_result: 거래 결과 기록 (성공/실패, 손익)

        Args:
            symbol: Stock symbol
            side: 'buy' or 'sell'
            order_type: 'market' or 'limit'
            price: Current price (used for limit orders and buying power check)
            position_scale: Position size multiplier (0.0-1.0), Phase H.4
        """
        # Import distributed lock for trading operations
        from app.core.distributed_lock import get_trading_lock

        # Acquire distributed lock (30s TTL) to prevent race conditions
        with get_trading_lock(symbol, ttl_seconds=30) as lock:
            if not lock.acquired:
                logger.warning("%s 락 획득 실패 - 주문 건너뛰기", symbol)
                return

            order_success = False
            realized_pnl = 0.0

            try:
                if side == "buy":
                    # Dynamic position sizing using RiskManager (ATR-based + portfolio risk)
                    account = self.api.get_account()
                    buying_power = float(account.buying_power)
                    portfolio_value = float(account.portfolio_value)

                    # Get ATR from latest features for volatility-based sizing
                    atr = None
                    try:
                        end_date = pd.Timestamp.now(tz='UTC')
                        start_date = end_date - pd.Timedelta(days=30)
                        ohlcv = self.repo.get_ohlcv_range(symbol, start_date, end_date, timeframe='1d')
                        if len(ohlcv) >= 14:
                            df_temp = pd.DataFrame([{
                                'high': bar.high, 'low': bar.low, 'close': bar.close
                            } for bar in ohlcv])
                            import talib
                            atr_values = talib.ATR(
                                df_temp['high'].values,
                                df_temp['low'].values,
                                df_temp['close'].values,
                                timeperiod=14
                            )
                            atr = float(atr_values[-1]) if not pd.isna(atr_values[-1]) else None
                    except Exception as e:
                        logger.debug("%s ATR 계산 실패: %s", symbol, e)

                    allowed, qty = self.risk_manager.calculate_position_size(
                        symbol=symbol,
                        price=price,
                        buying_power=buying_power,
                        atr=atr,
                        portfolio_value=portfolio_value
                    )

                    if not allowed or qty < 1:
                        logger.warning("%s 포지션 사이징 실패 (qty=%d)", symbol, qty)
                        return

                    # Apply regime position scale (0.0-1.0)
                    qty = max(1, int(qty * position_scale))

                    # Final buying power check
                    if buying_power < price * qty:
                        logger.warning("%s 매수 가능 금액 부족", symbol)
                        return

                    logger.info(
                        "%s 포지션 사이즈: %d주 @ $%.2f (ATR=%.2f, 스케일=%.0f%%)",
                        symbol, qty, price, atr or 0.0, position_scale * 100
                    )
                else:
                    # For SELL: use existing position quantity
                    active_pos = self.repo.get_active_position(symbol)
                    qty = active_pos.quantity if active_pos else 1

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

                # 주문 제출 - Circuit Breaker로 API 레이턴시 추적
                if self.circuit_breaker:
                    with self.circuit_breaker.track_api_call():
                        order = self.api.submit_order(order_data=order_data)
                else:
                    order = self.api.submit_order(order_data=order_data)

                logger.info("주문 실행: %s %s (ID: %s)", side.upper(), symbol, order.id)
                order_success = True

                # Discord 거래 알림 전송
                discord_notifier.send_trade_alert(
                    action=side.upper(),
                    symbol=symbol,
                    qty=qty,
                    price=price,
                    extra_info={
                        "Order ID": str(order.id),
                        "Type": order_type,
                        "Regime": self.current_regime.value if self.current_regime else "N/A"
                    }
                )

                # Phase I.1: DB에 포지션 진입/종료 기록
                # Use pessimistic lock for position updates
                if side == "buy":
                    entry_time = datetime.now(UTC)  # timezone-aware
                    # Record current market regime
                    regime_str = self.current_regime.value if self.current_regime else None
                    self.repo.record_position_entry(symbol, price, qty, entry_time, regime=regime_str)
                    self.risk_manager.record_position_entry(symbol, entry_time)
                    self.db.commit()
                elif side == "sell":
                    # Use with_for_update for safe position update
                    active_position = self.repo.get_active_position_for_update(symbol)
                    if active_position:
                        # 손익 계산
                        realized_pnl = (price - active_position.entry_price) * active_position.quantity
                        try:
                            self.repo.update_position_exit(active_position.id, price)
                            self.risk_manager.record_position_exit(symbol)
                            self.db.commit()
                        except Exception as db_err:
                            logger.critical(
                                "CRITICAL: Alpaca SELL 주문 성공했으나 DB 업데이트 실패! "
                                "수동 조정 필요: %s SELL (order_id=%s, position_id=%s, error=%s)",
                                symbol, str(order.id), str(active_position.id), str(db_err)
                            )
                            self.db.rollback()
                            raise

            except Exception as e:
                logger.error("%s 주문 실패: %s", symbol, str(e), exc_info=True)
                order_success = False
            finally:
                # Circuit Breaker에 거래 결과 기록
                if self.circuit_breaker:
                    self.circuit_breaker.record_trade_result(order_success, realized_pnl)


    def _get_cached_signal(self, symbol: str, regime: str):
        """Retrieve cached daily signal from Redis (Phase L.1).

        Args:
            symbol: Stock ticker.
            regime: Market regime string.

        Returns:
            ``CachedSignal`` if cached, ``None`` on miss or error.
        """
        try:
            from app.services.signal_cache import daily_signal_cache

            return daily_signal_cache.get_signal(symbol, regime)
        except Exception:
            logger.debug("Signal cache lookup failed for %s", symbol)
            return None

    def process_portfolio(self, symbols: list[str]):
        """
        Process multiple symbols for multi-position portfolio trading (Phase I.2).
        
        Strategy:
        1. Circuit Breaker 확인 (일일 손실, VIX 극단치)
        2. Detect market regime
        3. Get current active positions (Alpaca API - Source of Truth)
        4. Calculate Kelly position sizes for each symbol
        5. Select uncorrelated symbols (max 5 positions)
        6. Execute BUY/SELL orders based on signals and portfolio optimization
        
        Args:
            symbols: List of symbols to analyze
        """
        try:
            logger.info("%d개 심볼로 포트폴리오 처리 중", len(symbols))

            # 0. Circuit Breaker 확인
            if self.circuit_breaker:
                # 포트폴리오 가치 조회
                try:
                    account = self.api.get_account()
                    portfolio_value = float(account.portfolio_value)
                except Exception:
                    portfolio_value = None

                if not self.circuit_breaker.can_trade(portfolio_value):
                    status = self.circuit_breaker.get_status()
                    logger.warning(
                        "Circuit Breaker 활성화 - 트레이딩 중단 (상태: %s, 일일손익: $%.2f)",
                        status['state'], status['daily_pnl']
                    )
                    return

            # 0b. 일일 거래 한도 확인
            if not self.risk_manager.can_trade_today():
                logger.warning("일일 거래 한도 도달 — 트레이딩 중단")
                return

            # 1. 시장 레짐 감지
            self.detect_market_regime()

            # 2. 현재 활성 포지션 가져오기 (Alpaca API - Source of Truth)
            try:
                alpaca_positions = self.api.get_all_positions()
                active_positions = [
                    {
                        'symbol': pos.symbol,
                        'qty': int(pos.qty),
                        'entry_price': float(pos.avg_entry_price),
                        'current_price': float(pos.current_price),
                        'unrealized_pl': float(pos.unrealized_pl)
                    }
                    for pos in alpaca_positions
                ]
                active_symbols = {pos['symbol'] for pos in active_positions}
                logger.info("Alpaca 활성 포지션: %s", list(active_symbols))
            except Exception as e:
                logger.error("Alpaca 포지션 조회 실패: %s", e, exc_info=True)
                active_positions = []
                active_symbols = set()

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
            signals = {}  # {symbol: {'class': int, 'confidence': float, 'probs': dict, 'kelly': float, 'price': float}}

            for symbol in symbols:
                try:
                    # Get prediction signal
                    end_date = pd.Timestamp.now(tz='UTC')
                    start_date = end_date - pd.Timedelta(days=365)

                    ohlcv = self.repo.get_ohlcv_range(symbol, start_date, end_date, timeframe='1d')
                    if len(ohlcv) < 50:
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

                    # Check signal cache first (L.1 optimization)
                    regime_suffix = self.current_regime.value if self.current_regime else 'sideways_calm'
                    cached_signal = self._get_cached_signal(symbol, regime_suffix)

                    if cached_signal is not None:
                        pred_class = cached_signal.predicted_class
                        pred_conf = cached_signal.confidence
                        pred_probs = cached_signal.probabilities
                        logger.debug(
                            "%s: 캐시된 signal 사용 (class=%d, conf=%.2f)",
                            symbol, pred_class, pred_conf,
                        )
                    else:
                        features_df = self.feature_engineer.create_features(df)
                        if features_df.empty:
                            continue

                        latest_features = features_df.iloc[[-1]]
                        # Use base feature set (26 features) matching training pipeline
                        X_norm = self.feature_engineer.extract_feature_vector(
                            latest_features, feature_set="base",
                            scaler_suffix=regime_suffix
                        )

                        # Get classification prediction with regime awareness
                        pred_class, pred_conf, pred_probs = self.predictor.predict_class(
                            X_norm, regime=self.current_regime
                        )

                    # Calculate Kelly position size
                    kelly_size = self.optimizer.kelly_criterion(
                        self.portfolio_repo,
                        symbol,
                        use_live_data=True
                    )

                    current_price = df['close'].iloc[-1]
                    class_names = {0: "DOWN", 1: "NEUTRAL", 2: "UP"}

                    signals[symbol] = {
                        'class': pred_class,
                        'confidence': pred_conf,
                        'probs': pred_probs,
                        'kelly': kelly_size,
                        'price': current_price,
                    }

                    logger.info(
                        "  %s: 예측=%s(%.1f%%), Kelly=%.2f%%, 가격=$%.2f",
                        symbol, class_names.get(pred_class, "?"),
                        pred_conf * 100, kelly_size * 100, current_price,
                    )

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
            for symbol in signals:
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
        signals: dict[str, dict],
        corr_matrix: pd.DataFrame,
        active_symbols: set,
        max_new_positions: int
    ) -> list[str]:
        """
        Select symbols with low correlation to existing positions.
        
        Strategy:
        - Prioritize symbols with UP class prediction and high confidence
        - Filter by momentum percentile >= 0.50 (Phase M.1)
        - Avoid symbols highly correlated with active positions (corr > 0.7)
        - Use momentum rank as tiebreaker when confidence is similar
        - Limit to max_new_positions
        """
        # Phase M.1: Load cached momentum scores
        momentum_lookup: dict[str, float] = {}
        try:
            cached_scores = CrossSectionalMomentum.get_cached_scores()
            momentum_lookup = {
                s.symbol: s.universe_percentile_rank for s in cached_scores
            }
            if momentum_lookup:
                logger.info(
                    "Momentum scores loaded: %d symbols (top: %s)",
                    len(momentum_lookup),
                    ", ".join(
                        s.symbol for s in sorted(
                            cached_scores,
                            key=lambda x: x.composite_score,
                            reverse=True,
                        )[:3]
                    ),
                )
        except Exception:
            logger.warning("Momentum scores unavailable — falling back to confidence only")

        candidates = []

        for symbol, data in signals.items():
            if symbol in active_symbols:
                continue

            # Only consider UP predictions with reasonable confidence
            if data.get('class') != 2 or data.get('confidence', 0) < 0.35:
                continue

            # Phase M.1: Skip weak momentum (bottom half of universe)
            # Graceful degradation: if no momentum data, allow all symbols
            mom_rank = momentum_lookup.get(symbol, 0.5)
            if momentum_lookup and mom_rank < 0.50:
                logger.debug(
                    "%s skipped: momentum percentile %.2f < 0.50",
                    symbol,
                    mom_rank,
                )
                continue

            # Check correlation with active positions
            max_corr = 0.0
            for active_sym in active_symbols:
                if active_sym in corr_matrix.index and symbol in corr_matrix.columns:
                    corr = abs(corr_matrix.loc[active_sym, symbol])
                    max_corr = max(max_corr, corr)

            candidates.append({
                'symbol': symbol,
                'confidence': data.get('confidence', 0),
                'max_corr': max_corr,
                'kelly': data['kelly'],
                'momentum_rank': mom_rank,
            })

        # Sort by confidence (primary) + momentum rank (secondary tiebreaker)
        candidates.sort(
            key=lambda x: (x['confidence'], x['momentum_rank']),
            reverse=True,
        )

        # Filter by correlation threshold and select top N
        selected = []
        for cand in candidates:
            if cand['max_corr'] < 0.7:  # Low correlation
                selected.append(cand['symbol'])
                if len(selected) >= max_new_positions:
                    break

        return selected

    def _process_buy_signal(self, symbol: str, signal_data: dict, portfolio_value: float):
        """Process BUY signal with Kelly position sizing.

        Concurrency Control:
            - Redis distributed lock prevents duplicate BUY orders from parallel Celery workers
        """
        from app.core.distributed_lock import get_trading_lock

        with get_trading_lock(symbol, ttl_seconds=30) as lock:
            if not lock.acquired:
                logger.warning("%s BUY 락 획득 실패 - 건너뛰기", symbol)
                return

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
                if self.circuit_breaker:
                    with self.circuit_breaker.track_api_call():
                        order = self.api.submit_order(order_data=order_data)
                else:
                    order = self.api.submit_order(order_data=order_data)

                logger.info("주문 실행: BUY %s (ID: %s)", symbol, order.id)

                # DB에 기록
                entry_time = datetime.now(UTC)  # timezone-aware
                regime_str = self.current_regime.value if self.current_regime else None
                self.repo.record_position_entry(symbol, current_price, qty, entry_time, regime=regime_str)
                self.risk_manager.record_position_entry(symbol, entry_time)

                # Set initial stop prices
                db_position = self.repo.get_active_position(symbol)
                if db_position:
                    initial_stop_loss = current_price * 0.95  # 5% stop loss
                    initial_trailing = current_price * 0.985  # 1.5% trailing
                    initial_take_profit = current_price * 1.10  # 10% take profit
                    self.repo.update_position_stops(
                        db_position.id,
                        trailing_stop_price=initial_trailing,
                        stop_loss_price=initial_stop_loss,
                        take_profit_price=initial_take_profit,
                    )

                self.session.commit()

                # 일일 거래 기록 (Redis 영속화)
                self.risk_manager.record_trade(
                    symbol, "BUY", current_price, qty,
                )

                # Discord 알림
                try:
                    discord_notifier.send_trade_alert(
                        action="BUY",
                        symbol=symbol,
                        qty=qty,
                        price=current_price,
                        extra_info={
                            "Order ID": str(order.id),
                            "Type": "PORTFOLIO_BUY",
                            "Regime": self.current_regime.value if self.current_regime else "N/A",
                        },
                    )
                except Exception:
                    logger.debug("Discord notification failed", exc_info=True)

            except Exception as e:
                logger.error("%s BUY 주문 실패: %s", symbol, str(e), exc_info=True)

    def _process_sell_signal(self, symbol: str, signal_data: dict):
        """Process SELL signal with regime-aware defense checks (classification-based)."""
        try:
            # 활성 포지션 가져오기
            active_position = self.repo.get_active_position(symbol)
            if not active_position:
                return

            current_price = signal_data['price']
            entry_price = active_position.entry_price
            pred_class = signal_data.get('class', 1)  # default NEUTRAL
            pred_conf = signal_data.get('confidence', 0.0)

            # 손익 계산
            pnl_pct = (current_price - entry_price) / entry_price

            # 1. 강제 청산 조건 (방어 규칙 무시)
            force_exit = False
            force_reason = ""

            # 1-1. 레짐 기반 강제 청산
            if self.current_regime == MarketRegime.BEAR_TRENDING:
                force_exit = True
                force_reason = f"REGIME_FORCE: BEAR_TRENDING (손익: {pnl_pct:.2%})"

            # 1-2. 손절 강제 청산 (-3% 이하)
            elif pnl_pct <= -0.03:
                force_exit = True
                force_reason = f"STOP_LOSS: {pnl_pct:.2%} <= -3.0%"

            # 1-3. 강한 DOWN 신호 (class=0 + 높은 신뢰도 >= 0.6)
            elif pred_class == 0 and pred_conf >= 0.6:
                force_exit = True
                force_reason = f"STRONG_SELL: class=DOWN, confidence={pred_conf:.2f}"

            # 1-4. SIDEWAYS_VOLATILE에서 빠른 익절 (변동성 높으므로)
            elif self.current_regime == MarketRegime.SIDEWAYS_VOLATILE and pnl_pct >= 0.02:
                force_exit = True
                force_reason = f"REGIME_TAKE_PROFIT: VOLATILE + {pnl_pct:.2%}"

            if force_exit:
                logger.info("%s 강제 SELL: %s", symbol, force_reason)
                self._execute_sell_order(symbol, active_position, current_price, force_reason)
                return

            # 2. 일반 매도 조건 (방어 규칙 적용)
            can_exit, defense_reason = self.risk_manager.can_exit_position(
                symbol=symbol,
                entry_price=entry_price,
                current_price=current_price,
                entry_time=active_position.entry_time
            )

            # 2-1. 명확한 매도 신호 확인 (DOWN class + confidence >= 0.45)
            has_sell_signal = pred_class == 0 and pred_conf >= 0.45

            if not can_exit and not has_sell_signal:
                # 방어 차단 + 약한 신호 → SELL 차단
                logger.info(
                    "%s SELL 차단: %s (class=%d, conf=%.2f)",
                    symbol, defense_reason, pred_class, pred_conf,
                )
                return

            # 2-2. 방어 허용 또는 명확한 신호 → SELL 실행
            if can_exit:
                sell_reason = f"DEFENSE_OK: {defense_reason}"
            else:
                sell_reason = f"STRONG_DOWN: conf={pred_conf:.2f}"
            pnl_pct = ((current_price - entry_price) / entry_price) if entry_price else 0.0
            logger.info("%s SELL 허용: %s (손익: %.2f%%)", symbol, sell_reason, pnl_pct * 100)
            self._execute_sell_order(symbol, active_position, current_price, sell_reason)

        except Exception as e:
            logger.error("%s SELL 주문 실패: %s", symbol, str(e), exc_info=True)

    def _execute_sell_order(self, symbol: str, position, current_price: float, reason: str):
        """Execute SELL order (extracted for reuse).

        Concurrency Control:
            - Redis distributed lock prevents duplicate SELL orders from parallel Celery workers
            - Re-reads position with FOR UPDATE to ensure consistency
        """
        from app.core.distributed_lock import get_trading_lock

        with get_trading_lock(symbol, ttl_seconds=30) as lock:
            if not lock.acquired:
                logger.warning("%s SELL 락 획득 실패 - 건너뛰기", symbol)
                return

            try:
                # Re-read position with FOR UPDATE to prevent dirty reads
                locked_pos = self.repo.get_active_position_for_update(symbol)
                if locked_pos is None:
                    logger.warning("%s: 포지션 이미 닫힘 - SELL 건너뛰기", symbol)
                    return

                qty = locked_pos.quantity
                order_data = MarketOrderRequest(
                    symbol=symbol,
                    qty=qty,
                    side=OrderSide.SELL,
                    time_in_force=TimeInForce.DAY
                )
                if self.circuit_breaker:
                    with self.circuit_breaker.track_api_call():
                        order = self.api.submit_order(order_data=order_data)
                else:
                    order = self.api.submit_order(order_data=order_data)

                logger.info("주문 실행: SELL %s x%d @ $%.2f (ID: %s, 이유: %s)",
                           symbol, qty, current_price, order.id, reason)

                # DB 업데이트
                try:
                    self.repo.update_position_exit(locked_pos.id, current_price)
                    self.risk_manager.record_position_exit(symbol)
                    self.session.commit()

                    # 일일 거래 기록 (Redis 영속화)
                    realized_pnl = (current_price - locked_pos.entry_price) * qty
                    self.risk_manager.record_trade(
                        symbol, "SELL", current_price, qty,
                        realized_pl=realized_pnl,
                    )

                    # Discord 알림
                    try:
                        discord_notifier.send_trade_alert(
                            action="SELL",
                            symbol=symbol,
                            qty=qty,
                            price=current_price,
                            extra_info={
                                "Order ID": str(order.id),
                                "Type": "PORTFOLIO_SELL",
                                "Reason": reason,
                            },
                        )
                    except Exception:
                        logger.debug("Discord notification failed", exc_info=True)

                except Exception as db_err:
                    logger.critical(
                        "CRITICAL: Alpaca SELL 주문 성공했으나 DB 업데이트 실패! "
                        "수동 조정 필요: %s SELL %d주 @ $%.2f (order_id=%s, position_id=%s, error=%s)",
                        symbol, qty, current_price, str(order.id), str(locked_pos.id), str(db_err)
                    )
                    self.session.rollback()
                    raise

            except Exception as e:
                logger.error("%s SELL 주문 실행 실패: %s", symbol, str(e))
                raise

    # ------------------------------------------------------------------
    # Phase L.2c: Intraday Execution Integration
    # ------------------------------------------------------------------

    def process_intraday_cycle(self, symbols: list[str]) -> dict:
        """Process one 15-min intraday entry/exit cycle (Phase L.2c).

        Orchestrates:
            1. Create ``DualTimeframeOrchestrator``
            2. Get Alpaca active positions
            3. EXIT phase — ``check_exit`` for each held position
            4. ENTRY phase — ``scan_entries`` for non-held symbols
            5. Execute trades via ``_process_intraday_entry`` / ``_process_intraday_exit``

        Args:
            symbols: All active symbols to scan.

        Returns:
            Summary dict with keys: status, entries, exits, skipped, errors.
        """
        # Feature flag gate
        if not settings.DUAL_TIMEFRAME_ENABLED:
            return {
                "status": "disabled",
                "entries": 0,
                "exits": 0,
                "skipped": 0,
                "errors": 0,
            }

        entry_count = 0
        exit_count = 0
        error_count = 0
        skipped_count = 0

        try:
            # Lazy import to avoid circular dependency
            from app.services.dual_timeframe import DualTimeframeOrchestrator

            orchestrator = DualTimeframeOrchestrator(self.session)

            # Refresh market regime
            self.detect_market_regime()
            regime_str = self.current_regime.value if self.current_regime else "sideways_calm"

            # Get Alpaca positions (source of truth)
            try:
                alpaca_positions = self.api.get_all_positions()
            except Exception as e:
                logger.error("INTRADAY_CYCLE: Alpaca 포지션 조회 실패: %s", e)
                return {"status": "error", "entries": 0, "exits": 0, "skipped": 0, "errors": 1}

            active_symbols = {pos.symbol for pos in alpaca_positions}

            # Get account value for position sizing
            try:
                account = self.api.get_account()
                portfolio_value = float(account.portfolio_value)
            except Exception as e:
                logger.error("INTRADAY_CYCLE: Alpaca 계좌 조회 실패: %s", e)
                return {"status": "error", "entries": 0, "exits": 0, "skipped": 0, "errors": 1}

            # ── EXIT PHASE ──────────────────────────────────────────
            for pos in alpaca_positions:
                try:
                    current_price = float(pos.current_price)

                    # Determine trailing stop from DB position record
                    db_pos = self.repo.get_active_position(pos.symbol)
                    if db_pos is not None and db_pos.trailing_stop_price is not None:
                        trailing_stop = db_pos.trailing_stop_price
                    elif db_pos is not None:
                        trailing_stop = db_pos.entry_price * 0.985  # default 1.5%
                    else:
                        trailing_stop = float(pos.avg_entry_price) * 0.985

                    exit_signal = orchestrator.check_exit(
                        pos.symbol, regime_str, current_price, trailing_stop,
                    )
                    if exit_signal is not None:
                        success = self._process_intraday_exit(exit_signal)
                        if success:
                            exit_count += 1
                        else:
                            error_count += 1
                except Exception as e:
                    logger.error("INTRADAY_CYCLE: %s EXIT 처리 실패: %s", pos.symbol, e)
                    error_count += 1

            # ── ENTRY PHASE ─────────────────────────────────────────
            candidate_symbols = [s for s in symbols if s not in active_symbols]
            available_slots = self.max_positions - len(alpaca_positions) + exit_count

            if available_slots <= 0:
                skipped_count = len(candidate_symbols)
                logger.info(
                    "INTRADAY_CYCLE: 가용 슬롯 없음 (%d/%d), 진입 건너뛰기",
                    len(alpaca_positions) - exit_count, self.max_positions,
                )
            else:
                try:
                    entry_signals = orchestrator.scan_entries(candidate_symbols, regime_str)
                except Exception as e:
                    logger.error("INTRADAY_CYCLE: scan_entries 실패: %s", e)
                    entry_signals = []
                    error_count += 1

                for entry in entry_signals[:available_slots]:
                    try:
                        success = self._process_intraday_entry(entry, portfolio_value)
                        if success:
                            entry_count += 1
                        else:
                            skipped_count += 1
                    except Exception as e:
                        logger.error("INTRADAY_CYCLE: %s ENTRY 처리 실패: %s", entry.symbol, e)
                        error_count += 1

        except Exception as e:
            logger.error("INTRADAY_CYCLE: 전체 사이클 오류: %s", e, exc_info=True)
            error_count += 1

        summary = {
            "status": "success",
            "entries": entry_count,
            "exits": exit_count,
            "skipped": skipped_count,
            "errors": error_count,
        }
        logger.info("INTRADAY_CYCLE 완료: %s", summary)
        return summary

    def _process_intraday_entry(self, entry: EntrySignal, portfolio_value: float) -> bool:
        """Execute BUY order from intraday EntrySignal (Phase L.2c).

        Uses Kelly position sizing, distributed lock, and risk-manager checks.
        Similar to ``_process_buy_signal`` but accepts an ``EntrySignal``
        instead of a raw dict.

        Args:
            entry: Validated EntrySignal from DualTimeframeOrchestrator.
            portfolio_value: Current portfolio value for position sizing.

        Returns:
            True if order was submitted, False otherwise.
        """
        from datetime import timedelta

        from app.core.distributed_lock import get_trading_lock

        with get_trading_lock(entry.symbol, ttl_seconds=30) as lock:
            if not lock.acquired:
                logger.warning("INTRADAY_ENTRY: %s BUY 락 획득 실패 - 건너뛰기", entry.symbol)
                return False

            try:
                # Risk-manager cooldown / limit check
                can_enter, reason = self.risk_manager.can_enter_position(entry.symbol)
                if not can_enter:
                    logger.info("INTRADAY_ENTRY: %s BUY 차단: %s", entry.symbol, reason)
                    return False

                # Get current price from latest 15min bar (fallback to daily)
                now = datetime.now(UTC)
                bars = self.repo.get_ohlcv_range(
                    entry.symbol, now - timedelta(hours=2), now, timeframe='15m',
                )
                if bars:
                    current_price = bars[-1].close
                else:
                    bars = self.repo.get_ohlcv_range(
                        entry.symbol, now - timedelta(days=3), now, timeframe='1d',
                    )
                    if not bars:
                        logger.warning(
                            "INTRADAY_ENTRY: %s 가격 데이터 없음, 진입 건너뛰기", entry.symbol,
                        )
                        return False
                    current_price = bars[-1].close

                # Kelly-based position sizing with regime scale
                position_scale = self.regime_config.get(
                    entry.regime, {},
                ).get("position_scale", 0.5)
                kelly_size = self.optimizer.kelly_criterion(
                    self.portfolio_repo, entry.symbol, use_live_data=True,
                )
                adjusted_size = kelly_size * position_scale
                position_value = portfolio_value * adjusted_size
                qty = int(position_value / current_price)

                if qty < 1:
                    logger.info(
                        "INTRADAY_ENTRY: %s BUY 건너뛰기 — Kelly 크기 너무 작음 (%.2f%%)",
                        entry.symbol, adjusted_size * 100,
                    )
                    return False

                # Submit market order via Alpaca
                order_data = MarketOrderRequest(
                    symbol=entry.symbol,
                    qty=qty,
                    side=OrderSide.BUY,
                    time_in_force=TimeInForce.DAY,
                )
                if self.circuit_breaker:
                    with self.circuit_breaker.track_api_call():
                        order = self.api.submit_order(order_data=order_data)
                else:
                    order = self.api.submit_order(order_data=order_data)

                logger.info(
                    "INTRADAY_ENTRY: %s BUY %d주 @ ~$%.2f (reason: %s, order: %s)",
                    entry.symbol, qty, current_price, entry.reason, order.id,
                )

                # Record in DB
                entry_time = datetime.now(UTC)
                self.repo.record_position_entry(
                    entry.symbol, current_price, qty, entry_time, regime=entry.regime,
                )
                self.risk_manager.record_position_entry(entry.symbol, entry_time)

                # Set initial stop prices
                db_position = self.repo.get_active_position(entry.symbol)
                if db_position:
                    initial_stop_loss = current_price * 0.95  # 5% stop loss
                    initial_trailing = current_price * 0.985  # 1.5% trailing
                    initial_take_profit = current_price * 1.10  # 10% take profit
                    self.repo.update_position_stops(
                        db_position.id,
                        trailing_stop_price=initial_trailing,
                        stop_loss_price=initial_stop_loss,
                        take_profit_price=initial_take_profit,
                    )

                self.session.commit()

                # 일일 거래 기록 (Redis 영속화)
                self.risk_manager.record_trade(
                    entry.symbol, "BUY", current_price, qty,
                )

                # Discord notification (best-effort)
                try:
                    discord_notifier.send_trade_alert(
                        action="BUY",
                        symbol=entry.symbol,
                        qty=qty,
                        price=current_price,
                        extra_info={"reason": entry.reason, "type": "INTRADAY_ENTRY"},
                    )
                except Exception:
                    logger.debug("Discord notification failed", exc_info=True)

                return True

            except Exception as e:
                logger.error("INTRADAY_ENTRY: %s BUY 주문 실패: %s", entry.symbol, e)
                return False

    def _process_intraday_exit(self, exit_signal: ExitSignal) -> bool:
        """Execute SELL order from intraday ExitSignal (Phase L.2c).

        Reuses ``_execute_sell_order`` for Alpaca execution.

        Args:
            exit_signal: Validated ExitSignal from DualTimeframeOrchestrator.

        Returns:
            True if order was submitted, False otherwise.
        """
        try:
            position = self.repo.get_active_position(exit_signal.symbol)
            if position is None:
                logger.warning(
                    "INTRADAY_EXIT: %s DB 포지션 없음 — 건너뛰기", exit_signal.symbol,
                )
                return False

            # Determine current price from exit signal or latest bar
            if exit_signal.current_price is not None:
                current_price = exit_signal.current_price
            else:
                from datetime import timedelta

                now = datetime.now(UTC)
                bars = self.repo.get_ohlcv_range(
                    exit_signal.symbol, now - timedelta(hours=2), now, timeframe='15m',
                )
                if bars:
                    current_price = bars[-1].close
                else:
                    logger.warning(
                        "INTRADAY_EXIT: %s 가격 데이터 없음, 엔트리 가격 사용",
                        exit_signal.symbol,
                    )
                    current_price = position.entry_price

            reason = f"INTRADAY_EXIT ({exit_signal.exit_reason}): {exit_signal.reason}"
            self._execute_sell_order(exit_signal.symbol, position, current_price, reason)

            # Discord notification (best-effort)
            try:
                discord_notifier.send_warning(
                    "INTRADAY_EXIT",
                    f"{exit_signal.symbol} SELL | {reason}",
                )
            except Exception:
                logger.debug("Discord notification failed", exc_info=True)

            return True

        except Exception as e:
            logger.error("INTRADAY_EXIT: %s SELL 처리 실패: %s", exit_signal.symbol, e)
            return False
