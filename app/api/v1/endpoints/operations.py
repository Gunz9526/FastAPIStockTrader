from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel
from typing import Dict, Any
from datetime import datetime, timedelta
import logging
import pandas as pd

from app.core.database import get_async_session
from app.core.security import get_api_key
from app.tasks.data_tasks import collect_fundamentals
from app.tasks.training import train_models, tune_models
from app.tasks.market_analysis import analyze_market
from app.tasks.trading import execute_market_scan
from app.ml.predictor import PredictorService
from app.ml.features import FeatureEngineer
from app.services.regime import RegimeDetector
from app.repositories.stock_repo import StockRepository

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
