from fastapi import APIRouter, HTTPException
from typing import Dict, Any
import logging
import os
import json

from app.ml.predictor import PredictorService

router = APIRouter()
logger = logging.getLogger(__name__)

_MODEL_PATH = os.getenv("MODEL_SAVE_PATH", "model_artifacts")

@router.get("/metrics", response_model=Dict[str, Any])
async def get_model_metrics():
    """
    현재 모델 성능 메트릭 및 가중치를 조회합니다.
    
    Returns:
        - ensemble_weights: 모델 가중치 목록 (CatBoost, LGBM, XGBoost)
        - training_date: 마지막 학습 일시 (ISO 8601 포맷)
        - model_names: 앙상블 모델 명칭
        - regime_info: 레짐별 학습 데이터 분포
    
    Raises:
        - 404: 모델 메타데이터가 없는 경우 (학습 먼저 실행 필요)
        - 500: 서버 오류
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
        metadata_path = os.path.join(_MODEL_PATH, "ensemble_model_metadata.json")
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
    모델 기본 정보를 조회합니다 (경량 엔드포인트).
    
    확인 항목:
        - model_exists: 모델 파일 존재 여부
        - scaler_exists: Feature Scaler 파일 존재 여부
        - metadata_exists: 메타데이터 파일 존재 여부
        - artifacts_path: 모델 아티팩트 저장 경로
    
    Returns:
        - 모델 파일 상태 정보
    """
    try:
        metadata_path = os.path.join(_MODEL_PATH, "ensemble_model_metadata.json")
        model_path = os.path.join(_MODEL_PATH, "ensemble_model.pkl")
        scaler_path = os.path.join(_MODEL_PATH, "feature_scaler.pkl")
        
        return {
            "model_exists": os.path.exists(model_path),
            "scaler_exists": os.path.exists(scaler_path),
            "metadata_exists": os.path.exists(metadata_path),
            "artifacts_path": _MODEL_PATH
        }
        
    except Exception as e:
        logger.error(f"Error fetching model info: {e}")
        raise HTTPException(status_code=500, detail=str(e))
