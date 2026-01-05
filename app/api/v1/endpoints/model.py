from fastapi import APIRouter, HTTPException
from typing import Dict, Any
import logging
import os
import json

from app.ml.predictor import PredictorService

router = APIRouter()
logger = logging.getLogger(__name__)

@router.get("/metrics", response_model=Dict[str, Any])
async def get_model_metrics():
    """
    Get current model performance metrics and weights.
    
    Returns:
        - ensemble_weights: List of model weights
        - training_date: ISO timestamp of last training
        - model_names: Names of ensemble members
    """
    try:
        predictor = PredictorService()
        metadata = predictor.get_model_info()
        
        if not metadata:
            raise HTTPException(
                status_code=404,
                detail="No model metadata found. Train model first."
            )
        
        # Load metadata file if available
        metadata_path = "/app/model_artifacts/ensemble_model_metadata.json"
        if os.path.exists(metadata_path):
            with open(metadata_path, 'r') as f:
                file_metadata = json.load(f)
                metadata.update(file_metadata)
        
        return {
            "status": "success",
            "metadata": metadata
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching model metrics: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/info")
async def get_model_info():
    """
    Get basic model information (lighter endpoint).
    """
    try:
        metadata_path = "/app/model_artifacts/ensemble_model_metadata.json"
        model_path = "/app/model_artifacts/ensemble_model.pkl"
        scaler_path = "/app/model_artifacts/feature_scaler.pkl"
        
        return {
            "model_exists": os.path.exists(model_path),
            "scaler_exists": os.path.exists(scaler_path),
            "metadata_exists": os.path.exists(metadata_path),
            "artifacts_path": "/app/model_artifacts"
        }
        
    except Exception as e:
        logger.error(f"Error fetching model info: {e}")
        raise HTTPException(status_code=500, detail=str(e))
