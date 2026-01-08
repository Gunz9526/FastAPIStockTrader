import logging
from app.worker import celery_app
from datetime import datetime
from app.core.database import SessionLocal
from app.repositories.stock_repo_sync import SyncStockRepository

logger = logging.getLogger(__name__)

@celery_app.task(name="app.tasks.trading.execute_market_scan")
def execute_market_scan():
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
        from app.services.trading_strategy_sync import SyncTradingStrategy
        from app.repositories.stock_repo_sync import SyncStockRepository
        
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


@celery_app.task(name="app.tasks.trading.update_trailing_stops")
def update_trailing_stops():
    """
    트레일링 스톱 업데이트 (동기 버전).
    """
    logger.info("트레일링 스톱 업데이트 중 (동기)...")
    
    session = SessionLocal()
    try:
        from sqlalchemy import select
        from app.domain.models.stock import Position, PositionStatus
        
        # 오픈 포지션 가져오기
        stmt = select(Position).where(Position.status == PositionStatus.OPEN.value)
        result = session.execute(stmt)
        positions = list(result.scalars().all())
        
        if not positions:
            logger.info("오픈 포지션 없음")
            return
        
        logger.info("%d개 포지션 업데이트 중", len(positions))
        
        # TODO: 동기 가격 조회 및 트레일링 스톱 로직 구현 필요
        logger.warning("트레일링 스톱 업데이트는 일시 비활성화되어 있습니다 - 동기 리팩토링 필요")
        
        session.commit()
        logger.info("트레일링 스톱 확인 완료")
        
    except Exception as e:
        logger.error("트레일링 스톱 오류: %s", str(e), exc_info=True)
        session.rollback()
        raise
    finally:
        session.close()
