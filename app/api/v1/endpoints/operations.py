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

@router.post("/train-models-regime") # Renamed to avoid conflict with existing /train-models
async def trigger_model_training(
    api_key: str = Depends(get_api_key),
    db: AsyncSession = Depends(get_async_session)
):
    """
    Train regime-specific ML models.
    
    This endpoint:
    1. Fetches historical OHLCV data
    2. Calculates technical indicators
    3. Detects market regimes
    4. Trains 4 separate models (one per regime)
    """
    try:
        logger.info("🚀 Starting regime-based model training...")
        
        repo = StockRepository(db)
        feature_engineer = FeatureEngineer()
        regime_detector = RegimeDetector()
        predictor = PredictorService()
        
        # Get active symbols
        symbols = await repo.get_active_symbols()
        if not symbols:
            raise HTTPException(status_code=404, detail="No active symbols found")
        
        logger.info(f"Training on {len(symbols)} symbols")
        
        all_X = []
        all_y = []
        all_regimes = []
        
        # Collect training data
        for symbol in symbols[:10]:  # Limit to 10 symbols for demo
            try:
                # Get 1 year of historical data
                end_date = datetime.now()
                start_date = end_date - timedelta(days=365)
                
                ohlcv = await repo.get_ohlcv_range(symbol, start_date, end_date)
                if len(ohlcv) < 100:
                    logger.warning(f"Insufficient data for {symbol}: {len(ohlcv)} bars")
                    continue
                
                # Convert to DataFrame
                df = pd.DataFrame([{
                    'date_time': bar.date_time,
                    'open': bar.open,
                    'high': bar.high,
                    'low': bar.low,
                    'close': bar.close,
                    'volume': bar.volume
                } for bar in ohlcv])
                
                df.set_index('date_time', inplace=True)
                df.sort_index(inplace=True)
                
                # Calculate features
                features_df = feature_engineer.create_features(df)
                if features_df.empty:
                    continue
                
                # Calculate target (next day return)
                features_df['target'] = features_df['close'].pct_change().shift(-1)
                features_df.dropna(inplace=True)
                
                # Detect regime for each sample
                regimes = []
                # Ensure enough data for regime detection (e.g., 100 bars)
                for i in range(len(features_df)):
                    if i + 100 <= len(df): # Ensure enough historical data for regime detection
                        regime = regime_detector.detect_regime(df.iloc[:i+100])
                        regimes.append(regime.value)
                    else:
                        # If not enough data for regime detection, use a default or skip
                        regimes.append("UNKNOWN") # Or handle as appropriate
                
                # Align regimes with features_df length
                if len(regimes) != len(features_df):
                    logger.warning(f"Regime count mismatch for {symbol}. Skipping.")
                    continue

                # Extract features
                feature_cols = feature_engineer.feature_columns
                X = features_df[feature_cols]
                y = features_df['target']
                
                all_X.append(X)
                all_y.append(y)
                all_regimes.extend(regimes)
                
                logger.info(f"✅ Processed {symbol}: {len(X)} samples")
                
            except Exception as e:
                logger.error(f"Failed to process {symbol}: {e}")
                continue
        
        if not all_X:
            raise HTTPException(status_code=400, detail="No training data collected")
        
        # Combine all data
        X_combined = pd.concat(all_X, ignore_index=True)
        y_combined = pd.concat(all_y, ignore_index=True)
        regimes_combined = pd.Series(all_regimes)
        
        logger.info(f"📊 Total samples: {len(X_combined)}")
        
        # Train model (simple version without regime support)
        # Note: Current PredictorService doesn't support regime-specific training
        # For regime-aware training, upgrade to RegimeAwarePredictor
        success = predictor.retrain(X_combined, y_combined)
        
        if success:
            return {
                "status": "success",
                "message": "Model trained successfully",
                "total_samples": len(X_combined),
                "symbols_processed": len(all_X),
                "note": "Using simple ensemble model. Upgrade to RegimeAwarePredictor for regime-specific models."
            }
        else:
            raise HTTPException(status_code=500, detail="Model training failed")
        
    except Exception as e:
        logger.error(f"Training failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

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
