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
    Manually trigger fundamental data collection.
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
    Manually trigger model training.
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
    Manually trigger model hyperparameter tuning.
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
    Manually trigger market analysis.
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
    Manually trigger market scan for trading.
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
    Get current system operational status.
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
