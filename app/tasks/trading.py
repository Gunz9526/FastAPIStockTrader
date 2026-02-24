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

    각 오픈 포지션에 대해:
    1. 최신 일봉 가격 조회
    2. ATR 기반 트레일링 스톱 갱신 (상승만 허용)
    3. 종료 조건 확인 (SL/TP/Trailing Stop)
    4. 트리거 시 포지션 CLOSED 처리
    """
    session = SessionLocal()
    try:
        from sqlalchemy import select

        from app.domain.models.stock import Position, PositionStatus

        # 오픈 포지션 가져오기
        stmt = select(Position).where(Position.status == PositionStatus.OPEN.value)
        result = session.execute(stmt)
        positions = list(result.scalars().all())

        if not positions:
            # 포지션 없으면 조용히 종료 (INFO 로그 불필요)
            logger.debug("트레일링 스톱: 오픈 포지션 없음 - 스킵")
            return

        logger.info("트레일링 스톱: %d개 포지션 확인 중...", len(positions))

        from datetime import UTC, datetime, timedelta

        from app.repositories.stock_repo_sync import SyncStockRepository
        from app.services.risk_manager import RiskManager

        try:
            import numpy as np
            import talib
            _has_talib = True
        except ImportError:
            _has_talib = False
            logger.warning("talib 미설치 - 고정 비율 트레일링 스톱 사용")

        repo = SyncStockRepository(session)
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

                # 1f. 종료 트리거 시 포지션 상태 업데이트
                if should_exit:
                    pos.status = PositionStatus.CLOSED.value
                    pos.exit_price = current_price
                    pos.exit_time = now
                    pos.realized_pl = (current_price - pos.entry_price) * pos.current_qty
                    logger.info(
                        "포지션 종료: %s | 사유: %s | 실현 P&L: $%.2f",
                        pos.symbol, reason, pos.realized_pl,
                    )
                    exit_count += 1

                # 1g-h. 포지션 레코드 업데이트
                pos.trailing_stop_price = new_trailing
                pos.current_price = current_price
                pos.unrealized_pl = (current_price - pos.entry_price) * pos.current_qty
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
