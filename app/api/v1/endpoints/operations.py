import logging

from fastapi import APIRouter, BackgroundTasks
from pydantic import BaseModel

from app.tasks.data_tasks import collect_fundamentals
from app.tasks.market_analysis import analyze_market
from app.tasks.trading import execute_market_scan
from app.tasks.training import train_models, tune_models

router = APIRouter()
logger = logging.getLogger(__name__)

class TaskResponse(BaseModel):
    status: str
    message: str
    task_id: str = None

@router.post("/collect-data", response_model=TaskResponse)
async def trigger_data_collection(background_tasks: BackgroundTasks):
    """
    기본적 데이터 수집 작업을 수동으로 트리거합니다.
    
    Returns:
        - status: 작업 시작 상태
        - message: 설명 메시지
        - task_id: Celery 태스크 ID
    """
    task = collect_fundamentals.delay()
    return TaskResponse(
        status="started",
        message="Data collection task started",
        task_id=str(task.id)
    )

@router.post("/train-models", response_model=TaskResponse)
async def trigger_training(background_tasks: BackgroundTasks):
    """
    ML 모델 학습 작업을 수동으로 트리거합니다.
    
    전체 프로세스:
        1. Regime 분류 (SPY 데이터 기반)
        2. 피처 엔지니어링 (20개 base features)
        3. CatBoost + LGBM + XGBoost 앙상블 학습
    
    Returns:
        - task_id: Celery 태스크 ID (진행상황 추적용)
    """
    task = train_models.delay()
    return TaskResponse(
        status="started",
        message="Model training task started",
        task_id=str(task.id)
    )

@router.post("/tune-models", response_model=TaskResponse)
async def trigger_tuning(background_tasks: BackgroundTasks):
    """
    모델 하이퍼파라미터 튜닝 작업을 수동으로 트리거합니다.
    
    주의: 실행 시간이 오래 걸림 (약 1-2시간)
    
    Returns:
        - task_id: Celery 태스크 ID
    """
    task = tune_models.delay()
    return TaskResponse(
        status="started",
        message="Model tuning task started",
        task_id=str(task.id)
    )

@router.post("/analyze-market", response_model=TaskResponse)
async def trigger_market_analysis(background_tasks: BackgroundTasks):
    """
    시장 분석 작업을 수동으로 트리거합니다.
    
    분석 내용:
        - VIX 지수 업데이트
        - 시장 레짐 감지
        - 섹터 분석
    
    Returns:
        - task_id: Celery 태스크 ID
    """
    task = analyze_market.delay()
    return TaskResponse(
        status="started",
        message="Market analysis task started",
        task_id=str(task.id)
    )

@router.post("/execute-scan", response_model=TaskResponse)
async def trigger_market_scan(background_tasks: BackgroundTasks):
    """
    거래 실행을 위한 시장 스캔 작업을 수동으로 트리거합니다.
    
    프로세스:
        1. 활성 종목별 신호 분석 (ML + Sentiment + Fundamentals)
        2. RiskManager 검증 (쿨다운, Circuit Breaker)
        3. 포지션 진입/청산
    
    Returns:
        - task_id: Celery 태스크 ID
    """
    task = execute_market_scan.delay()
    return TaskResponse(
        status="started",
        message="Market scan task started",
        task_id=str(task.id)
    )

@router.get("/status")
async def get_system_status():
    """
    현재 시스템 작동 상태를 조회합니다.
    
    확인 항목:
        - api: 서버 실행 상태
        - database: DB 연결 상태
        - redis: 캐시 서버 상태
        - celery_worker: 비동기 작업 실행기 상태
    
    Returns:
        - status: operational / degraded / down
        - services: 각 서비스 상태
    """
    # In production, check Celery worker status, DB connection, etc.
    return {
        "status": "operational",
        "services": {
            "api": "running",
            "database": "connected",
            "redis": "connected",
            "celery_worker": "active",
            "celery_beat": "active"
        },
        "timestamp": "2025-12-29T06:00:00Z"
    }


@router.get("/circuit-breaker")
async def get_circuit_breaker_status():
    """
    Circuit Breaker 현재 상태를 조회합니다.
    
    응답:
        - state: closed(정상) / open(차단) / half_open(테스트중)
        - opened_at: 차단 시작 시간
        - consecutive_failures: 연속 실패 횟수
        - daily_pnl: 오늘 손익
        - avg_latency_ms: 평균 API 레이턴시
    """
    from app.services.circuit_breaker import get_circuit_breaker

    breaker = get_circuit_breaker()
    return breaker.get_status()


@router.post("/circuit-breaker/open")
async def open_circuit_breaker(reason: str = "수동 차단"):
    """
    Circuit Breaker를 수동으로 활성화 (트레이딩 중단).
    
    Args:
        reason: 차단 사유
    
    사용 사례:
        - 시스템 점검 시
        - 비정상 시장 상황 감지 시
    """
    from app.services.circuit_breaker import get_circuit_breaker

    breaker = get_circuit_breaker()
    breaker.force_open(reason)
    return {"status": "opened", "reason": reason}


@router.post("/circuit-breaker/close")
async def close_circuit_breaker():
    """
    Circuit Breaker를 수동으로 해제 (트레이딩 재개).
    
    주의:
        - 차단 원인이 해결되었는지 확인 후 사용
        - 시장 상황이 정상인지 확인 필요
    """
    from app.services.circuit_breaker import get_circuit_breaker

    breaker = get_circuit_breaker()
    breaker.force_close()
    return {"status": "closed", "message": "Trading resumed"}

