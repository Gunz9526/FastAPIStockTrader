import logging

from app.core.database import SessionLocal
from app.services.discord_notifier import notify_on_failure
from app.worker import celery_app

logger = logging.getLogger(__name__)

@celery_app.task(
    name="app.tasks.trading.execute_market_scan",
    bind=True,
    max_retries=3,
    autoretry_for=(Exception,),
    retry_backoff=60,
    retry_backoff_max=600,
)
@notify_on_failure("execute_market_scan")
def execute_market_scan(self):
    """
    멀티 포지션 포트폴리오 전략으로 시장 스캔을 실행합니다 (Phase I.2).

    워크플로우:
    1. DB에서 활성 심볼 조회
    2. 시장 레짐 감지 (SPY 기반)
    3. 멀티 포지션 포트폴리오 처리 (최대 5개 동시)
    4. 상관관계 및 신호 기반 자동 선택
    """
    logger.info("멀티 포지션 시장 스캔 시작...")

    session = SessionLocal()
    try:
        from app.repositories.stock_repo_sync import SyncStockRepository
        from app.services.trading_strategy_sync import SyncTradingStrategy

        strategy = SyncTradingStrategy(session)
        repo = SyncStockRepository(session)

        # DB에서 활성 심볼 조회 (동적, 하드코딩 없음)
        symbols = repo.get_active_symbols()
        logger.info("후보 심볼 수: %d개", len(symbols))

        # 멀티 포지션 모드 활성화
        if strategy.multi_position_mode:
            logger.info("멀티 포지션 모드 활성화")

            # 포트폴리오 처리 (최대 5개 포지션)
            strategy.process_portfolio(symbols)
        else:
            logger.info("단일 포지션 모드")

            # 레거시 동작: 순차적 단일 포지션
            for symbol in symbols[:5]:  # 안전을 위해 5개로 제한
                strategy.process_symbol(symbol)

        session.commit()
        logger.info("시장 스캔 완료")

    except Exception as e:
        logger.error("시장 스캔 오류: %s", str(e), exc_info=True)
        session.rollback()
        raise
    finally:
        session.close()


@celery_app.task(
    name="app.tasks.trading.update_trailing_stops",
    bind=True,
    max_retries=3,
    autoretry_for=(Exception,),
    retry_backoff=30,
    retry_backoff_max=300,
)
@notify_on_failure("update_trailing_stops")
def update_trailing_stops(self):
    """
    트레일링 스톱 업데이트 (동기 버전).

    각 활성 PositionTracking에 대해:
    1. 최신 일봉 가격 조회
    2. ATR 기반 트레일링 스톱 갱신 (상승만 허용)
    3. 종료 조건 확인 (SL/TP/Trailing Stop)
    4. 트리거 시 포지션 exit 처리
    """
    session = SessionLocal()
    try:
        from datetime import UTC, datetime, timedelta

        from app.repositories.stock_repo_sync import SyncStockRepository
        from app.services.risk_manager import RiskManager

        repo = SyncStockRepository(session)

        # 활성 포지션 가져오기 (exit_time IS NULL)
        positions = repo.get_all_active_positions()

        if not positions:
            logger.debug("트레일링 스톱: 활성 포지션 없음 - 스킵")
            return

        logger.info("트레일링 스톱: %d개 포지션 확인 중...", len(positions))

        try:
            import numpy as np
            import talib
            _has_talib = True
        except ImportError:
            _has_talib = False
            logger.warning("talib 미설치 - 고정 비율 트레일링 스톱 사용")

        risk = RiskManager()
        now = datetime.now(UTC)
        updated_count = 0
        exit_count = 0

        for pos in positions:
            try:
                # 1a. 최근 30 거래일 OHLCV 데이터 조회 (ATR 계산용)
                start_time = now - timedelta(days=45)
                bars = repo.get_ohlcv_range(pos.symbol, start_time, now, timeframe='1d')

                if not bars:
                    logger.warning(
                        "트레일링 스톱: %s - 가격 데이터 없음, 스킵", pos.symbol
                    )
                    continue

                # 1b. 최신 바에서 현재 가격 추출
                latest_bar = bars[-1]
                current_price = latest_bar.close

                # 1c. ATR 계산 (최근 30 bars)
                atr_value = None
                if _has_talib and len(bars) >= 14:
                    highs = np.array([b.high for b in bars[-30:]], dtype=np.float64)
                    lows = np.array([b.low for b in bars[-30:]], dtype=np.float64)
                    closes = np.array([b.close for b in bars[-30:]], dtype=np.float64)
                    atr_arr = talib.ATR(highs, lows, closes, timeperiod=14)
                    if len(atr_arr) > 0 and not np.isnan(atr_arr[-1]):
                        atr_value = float(atr_arr[-1])

                # 1d. 트레일링 스톱 업데이트
                current_trailing = pos.trailing_stop_price
                if current_trailing is None:
                    # 초기 트레일링 스톱: 진입가 기준으로 설정
                    if atr_value:
                        current_trailing = pos.entry_price - (atr_value * risk.trailing_stop_atr_mult)
                    else:
                        current_trailing = pos.entry_price * 0.985

                new_trailing = risk.update_trailing_stop(
                    entry_price=pos.entry_price,
                    current_price=current_price,
                    current_trailing_stop=current_trailing,
                    atr=atr_value,
                )

                # 1e. 종료 조건 확인
                stop_loss = pos.stop_loss_price or (pos.entry_price * 0.95)
                take_profit = pos.take_profit_price or (pos.entry_price * 1.10)

                should_exit, reason = risk.check_exit_conditions(
                    entry_price=pos.entry_price,
                    current_price=current_price,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                    trailing_stop=new_trailing,
                )

                # 1f. 종료 트리거 시 포지션 exit 처리
                if should_exit:
                    realized_pl = (current_price - pos.entry_price) * pos.quantity
                    repo.update_position_exit(pos.id, current_price)
                    risk.record_position_exit(pos.symbol)

                    # 일일 거래 기록 (Redis 영속화)
                    risk.record_trade(
                        pos.symbol, "SELL", current_price, pos.quantity,
                        realized_pl=realized_pl,
                    )

                    # Discord 알림 (best-effort)
                    try:
                        from app.services.discord_notifier import discord_notifier
                        discord_notifier.send_trade_alert(
                            action="SELL",
                            symbol=pos.symbol,
                            qty=pos.quantity,
                            price=current_price,
                            extra_info={
                                "Type": "TRAILING_STOP_EXIT",
                                "Reason": reason,
                            },
                            pnl_amount=realized_pl,
                            pnl_pct=((current_price - pos.entry_price) / pos.entry_price) if pos.entry_price else None,
                            hold_duration_hours=((datetime.now(UTC) - pos.entry_time).total_seconds() / 3600) if hasattr(pos, 'entry_time') and pos.entry_time else None,
                        )
                    except Exception:
                        logger.debug("Discord notification failed for trailing stop exit", exc_info=True)

                    logger.info(
                        "포지션 종료: %s | 사유: %s | 실현 P&L: $%.2f",
                        pos.symbol, reason, realized_pl,
                    )
                    exit_count += 1
                else:
                    # 1g. 트레일링 스톱 가격 저장
                    repo.update_position_stops(pos.id, trailing_stop_price=new_trailing)

                updated_count += 1

            except Exception:
                logger.error(
                    "트레일링 스톱 처리 실패: %s", pos.symbol, exc_info=True
                )
                continue

        logger.info(
            "트레일링 스톱 결과: %d개 업데이트, %d개 종료 플래그",
            updated_count, exit_count,
        )

        session.commit()
        logger.info("트레일링 스톱 확인 완료")

    except Exception as e:
        logger.error("트레일링 스톱 오류: %s", str(e), exc_info=True)
        session.rollback()
        raise
    finally:
        session.close()


@celery_app.task(
    name="app.tasks.trading.generate_daily_signals",
    bind=True,
    max_retries=2,
    autoretry_for=(Exception,),
    retry_backoff=120,
    retry_backoff_max=600,
)
@notify_on_failure("generate_daily_signals")
def generate_daily_signals(self):
    """Post-market 일일 ML 예측 생성 및 Redis 캐시 저장.

    워크플로우:
        1. SPY 기반 시장 regime 감지
        2. 전 활성 심볼에 대해 feature 생성 → 스케일링 → 예측
        3. 결과를 ``DailySignalCache``에 저장 (24h TTL)
        4. Discord 알림으로 요약 전송

    스케줄: 17:30 ET (월-금), ``collect_daily_ohlcv`` (17:00 ET) 이후 실행.
    """
    from datetime import datetime

    from pytz import timezone

    et_tz = timezone("America/New_York")
    current_time = datetime.now(et_tz)

    if current_time.weekday() > 4:
        logger.info("주말 — 일일 signal 생성 건너뜀")
        return {"status": "skipped", "reason": "weekend"}

    logger.info("===== 일일 ML Signal 생성 시작 =====")

    session = SessionLocal()
    try:
        import pandas as pd

        from app.domain.schemas.signal import CachedSignal
        from app.ml.features import FeatureEngineer
        from app.ml.predictor import PredictorService
        from app.repositories.stock_repo_sync import SyncStockRepository
        from app.services.regime import RegimeDetector
        from app.services.signal_cache import daily_signal_cache

        repo = SyncStockRepository(session)
        predictor = PredictorService()
        feature_engineer = FeatureEngineer()

        # 1. Regime 감지
        regime_detector = RegimeDetector()
        end_date = pd.Timestamp.now(tz="UTC")
        start_date = end_date - pd.Timedelta(days=90)
        spy_data = repo.get_ohlcv_range("SPY", start_date, end_date, timeframe="1d")

        if spy_data and len(spy_data) >= 50:
            spy_df = pd.DataFrame(
                [
                    {
                        "date_time": bar.date_time,
                        "open": bar.open,
                        "high": bar.high,
                        "low": bar.low,
                        "close": bar.close,
                        "volume": bar.volume,
                    }
                    for bar in spy_data
                ]
            )
            spy_df.set_index("date_time", inplace=True)
            spy_df.sort_index(inplace=True)

            # Generate features (needed for ADX, SMA, ATR)
            spy_features = feature_engineer.create_features(spy_df)

            if spy_features.empty:
                from app.services.regime import MarketRegime

                current_regime = MarketRegime.SIDEWAYS_CALM
                logger.warning("SPY 특성 생성 실패 — 기본 regime(sideways_calm) 사용")
            else:
                # Load VIX from Redis cache
                vix_value = None
                try:
                    from app.core.cache import cache

                    vix_cached = cache.get("vix:latest")
                    if vix_cached:
                        vix_value = float(vix_cached)
                        logger.info("캐시에서 VIX 값 로드: %.2f", vix_value)
                except Exception:
                    logger.debug("VIX 캐시 로드 실패 — VIX 없이 regime 감지")

                current_regime = regime_detector.detect_regime(
                    spy_features, vix_value=vix_value
                )
        else:
            from app.services.regime import MarketRegime

            current_regime = MarketRegime.SIDEWAYS_CALM
            logger.warning("SPY 데이터 부족 — 기본 regime(sideways_calm) 사용")

        regime_str = current_regime.value
        logger.info("현재 시장 regime: %s", regime_str)

        # 2. Regime fallback 확인
        from app.core.config import REGIME_TRADING_CONFIG

        config = REGIME_TRADING_CONFIG.get(regime_str, {})
        fallback_regime_str = config.get("fallback_to_regime")

        prediction_regime = current_regime
        if fallback_regime_str:
            try:
                from app.services.regime import MarketRegime

                prediction_regime = MarketRegime(fallback_regime_str)
                logger.info(
                    "Regime fallback: %s → %s (예측용)",
                    regime_str,
                    fallback_regime_str,
                )
            except ValueError:
                logger.warning("잘못된 fallback regime: %s", fallback_regime_str)

        effective_regime = prediction_regime.value

        # 3. 활성 심볼 조회
        symbols = repo.get_active_symbols()
        if not symbols:
            logger.warning("활성 심볼 없음 — signal 생성 스킵")
            return {"status": "no_symbols"}

        logger.info("%d개 심볼에 대해 signal 생성 중...", len(symbols))

        # 4. 이전 캐시 무효화
        daily_signal_cache.invalidate_all()

        # 5. 각 심볼별 예측
        signals: list[CachedSignal] = []
        error_count = 0

        for symbol in symbols:
            try:
                ohlcv = repo.get_ohlcv_range(
                    symbol, end_date - pd.Timedelta(days=365), end_date, timeframe="1d"
                )
                if len(ohlcv) < 50:
                    logger.debug(
                        "%s: 데이터 부족 (%d bars < 50), 스킵", symbol, len(ohlcv)
                    )
                    continue

                df = pd.DataFrame(
                    [
                        {
                            "date_time": bar.date_time,
                            "open": bar.open,
                            "high": bar.high,
                            "low": bar.low,
                            "close": bar.close,
                            "volume": bar.volume,
                            "symbol": symbol,
                        }
                        for bar in ohlcv
                    ]
                )
                df.set_index("date_time", inplace=True)
                df.sort_index(inplace=True)

                features_df = feature_engineer.create_features(df)
                if features_df.empty:
                    continue

                latest = features_df.iloc[[-1]]
                scaled = feature_engineer.extract_feature_vector(
                    latest,
                    fit_scaler=False,
                    feature_set="base",
                    scaler_suffix=effective_regime,
                )

                pred_class, confidence, probs = predictor.predict_class(
                    scaled, regime=prediction_regime
                )

                sig = CachedSignal(
                    symbol=symbol,
                    predicted_class=pred_class,
                    confidence=confidence,
                    probabilities=probs,
                    regime=regime_str,
                    generated_at=datetime.now(),
                )
                signals.append(sig)

            except Exception:
                logger.error("%s signal 생성 실패", symbol, exc_info=True)
                error_count += 1

        # 6. Redis 저장
        cached_count = daily_signal_cache.set_signals_bulk(signals)

        # 7. 요약 통계
        up_count = sum(1 for s in signals if s.predicted_class == 2)
        neutral_count = sum(1 for s in signals if s.predicted_class == 1)
        down_count = sum(1 for s in signals if s.predicted_class == 0)
        avg_conf = (
            sum(s.confidence for s in signals) / len(signals) if signals else 0.0
        )

        summary = (
            f"📊 일일 ML Signal 생성 완료\n"
            f"Regime: {regime_str}\n"
            f"총 {cached_count}개 signal 캐시됨 (에러: {error_count})\n"
            f"UP: {up_count} | NEUTRAL: {neutral_count} | DOWN: {down_count}\n"
            f"평균 confidence: {avg_conf:.1%}"
        )
        logger.info(summary)

        # 8. Discord 알림
        try:
            from app.services.discord_notifier import discord_notifier

            discord_notifier.send_success("daily_signals", summary)
        except Exception:
            logger.debug("Discord 알림 전송 실패 (무시)")

        return {
            "status": "success",
            "regime": regime_str,
            "signals_cached": cached_count,
            "errors": error_count,
            "up": up_count,
            "neutral": neutral_count,
            "down": down_count,
            "avg_confidence": round(avg_conf, 4),
        }

    except Exception as e:
        logger.error("일일 signal 생성 오류: %s", str(e), exc_info=True)
        session.rollback()
        raise
    finally:
        session.close()


@celery_app.task(
    name="app.tasks.trading.execute_intraday_entries",
    bind=True,
    max_retries=2,
    autoretry_for=(Exception,),
    retry_backoff=30,
    retry_backoff_max=300,
)
@notify_on_failure("execute_intraday_entries")
def execute_intraday_entries(self):
    """15min intraday entry/exit cycle (Phase L.2c).

    Scans for entry (RSI/MACD + daily ML UP) and exit (trailing stop,
    daily DOWN, signal expired) conditions using ``DualTimeframeOrchestrator``.

    Workflow:
        1. Feature flag gate (``DUAL_TIMEFRAME_ENABLED``)
        2. Market hours guard (9:30–16:00 ET)
        3. Delegate to ``SyncTradingStrategy.process_intraday_cycle()``

    Schedule: ``*/15`` (9–15h ET, Mon–Fri), same cadence as 15min OHLCV collection.
    """
    from app.core.config import settings as app_settings

    # 1. Feature flag gate
    if not app_settings.DUAL_TIMEFRAME_ENABLED:
        logger.debug("execute_intraday_entries: DUAL_TIMEFRAME_ENABLED=False — 건너뛰기")
        return {"status": "disabled"}

    # 2. Market hours guard
    from app.services.intraday_features import is_market_hours

    if not is_market_hours():
        logger.debug("execute_intraday_entries: 장외 시간 — 건너뛰기")
        return {"status": "outside_hours"}

    logger.info("===== Intraday entry/exit 사이클 시작 =====")

    session = SessionLocal()
    try:
        from app.repositories.stock_repo_sync import SyncStockRepository
        from app.services.trading_strategy_sync import SyncTradingStrategy

        strategy = SyncTradingStrategy(session)
        repo = SyncStockRepository(session)

        # Get active symbols from DB
        symbols = repo.get_active_symbols()
        if not symbols:
            logger.warning("execute_intraday_entries: 활성 심볼 없음")
            return {"status": "no_symbols"}

        logger.info("execute_intraday_entries: %d개 심볼 스캔", len(symbols))

        # Delegate to strategy
        result = strategy.process_intraday_cycle(symbols)

        session.commit()
        logger.info("===== Intraday 사이클 완료: %s =====", result)
        return result

    except Exception as e:
        logger.error("execute_intraday_entries 오류: %s", str(e), exc_info=True)
        session.rollback()
        raise
    finally:
        session.close()


@celery_app.task(
    name="app.tasks.trading.send_end_of_day_summary",
    bind=True,
    max_retries=2,
    autoretry_for=(Exception,),
    retry_backoff=30,
    retry_backoff_max=300,
)
@notify_on_failure("send_end_of_day_summary")
def send_end_of_day_summary(self):
    """Post-market daily summary sent to Discord (Phase R.3).

    Workflow:
        1. Weekend/holiday guard
        2. Query Alpaca account for portfolio value + daily P&L
        3. Get active positions and find top/worst performers
        4. Read Redis trade count for today
        5. Detect current market regime
        6. Send structured summary via Discord webhook

    Schedule: 16:05 ET (Mon-Fri), 5 minutes after market close.
    """
    from datetime import date, datetime

    from pytz import timezone

    et_tz = timezone("America/New_York")
    current_time = datetime.now(et_tz)

    # Weekend guard
    if current_time.weekday() > 4:
        logger.info("주말 — 일일 요약 건너뛰기")
        return {"status": "skipped", "reason": "weekend"}

    logger.info("===== 장마감 일일 요약 생성 시작 =====")

    try:
        from alpaca.trading.client import TradingClient

        from app.core.cache import cache
        from app.core.config import settings
        from app.services.discord_notifier import discord_notifier

        # 1. Alpaca account info
        is_paper = "paper" in settings.ALPACA_TRADING_URL.lower()
        api = TradingClient(
            api_key=settings.ALPACA_API_KEY,
            secret_key=settings.ALPACA_SECRET_KEY,
            paper=is_paper,
        )

        account = api.get_account()
        portfolio_value = float(account.portfolio_value)

        # Daily P&L from equity change
        last_equity = float(account.last_equity)
        daily_pnl = portfolio_value - last_equity
        daily_pnl_pct = (daily_pnl / last_equity) if last_equity > 0 else 0.0

        # 2. Active positions
        positions = api.get_all_positions()
        total_positions = len(positions)

        # Find top/worst performers
        top_performer = None
        worst_performer = None
        if positions:
            best_pos = max(positions, key=lambda p: float(p.unrealized_plpc))
            worst_pos = min(positions, key=lambda p: float(p.unrealized_plpc))
            top_performer = f"{best_pos.symbol} ({float(best_pos.unrealized_plpc):+.2%})"
            worst_performer = f"{worst_pos.symbol} ({float(worst_pos.unrealized_plpc):+.2%})"

        # 3. Redis trade count
        today_str = date.today().isoformat()
        trades_today = 0
        try:
            raw = cache.get(f"risk:daily_trades:{today_str}")
            if raw is not None:
                trades_today = int(raw)
        except Exception:
            logger.debug("Redis trade count read failed")

        # 4. Current regime
        regime = None
        try:
            raw_regime = cache.get("regime:current")
            if raw_regime:
                regime = str(raw_regime)
        except Exception:
            logger.debug("Redis regime read failed")

        # 5. Send Discord summary
        success = discord_notifier.send_daily_summary(
            portfolio_value=portfolio_value,
            daily_pnl=daily_pnl,
            daily_pnl_pct=daily_pnl_pct,
            total_positions=total_positions,
            trades_today=trades_today,
            regime=regime,
            top_performer=top_performer,
            worst_performer=worst_performer,
        )

        logger.info(
            "일일 요약 전송 %s: 포트폴리오=$%s, P&L=$%s (%s), 포지션=%d, 거래=%d",
            "성공" if success else "실패",
            f"{portfolio_value:,.2f}",
            f"{daily_pnl:+,.2f}",
            f"{daily_pnl_pct:+.2%}",
            total_positions,
            trades_today,
        )

        return {
            "status": "success",
            "portfolio_value": portfolio_value,
            "daily_pnl": daily_pnl,
            "trades_today": trades_today,
        }

    except Exception as e:
        logger.error("일일 요약 생성 오류: %s", str(e), exc_info=True)
        raise
