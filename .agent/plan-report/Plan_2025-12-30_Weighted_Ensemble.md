# Weighted Ensemble & Model Evaluation API Plan

## Goal
1.  **Fix feature_scaler path**: Use absolute path `/app/model_artifacts/feature_scaler.pkl`.
2.  **Implement Weighted Ensemble**: Calculate validation-based weights for CatBoost, XGBoost, LGBM.
3.  **Create Model Evaluation API**: Endpoint to view current model performance metrics.

## Changes

### 1. Feature Scaler Path Fix
**File**: `app/ml/features.py`
- [MODIFY] Line 17: Change default `scaler_path` to `/app/model_artifacts/feature_scaler.pkl`.
- [MODIFY] Line 144: Ensure directory creation uses `mode=0o777`.

### 2. Weighted Ensemble Implementation
**File**: `app/ml/models.py`
- [MODIFY] `EnsembleWrapper.__init__`: Add `weights` parameter (optional, defaults to None = equal weighting).
- [MODIFY] `EnsembleWrapper.train`: If weights provided, use `VotingRegressor(estimators, weights=weights)`.
- [MODIFY] `EnsembleWrapper.save`: Save weights along with model.

**File**: `app/tasks/training.py`
- [MODIFY] `train_models` function:
    - After feature extraction, train each model separately on validation set.
    - Calculate performance metric (Sharpe Ratio or inverse MSE) for each.
    - Normalize to weights that sum to 1.0.
    - Pass weights to `EnsembleWrapper`.
    - Log weights for monitoring.

### 3. Model Evaluation API
**New File**: `app/api/v1/endpoints/model.py`
- [NEW] `GET /api/v1/model/metrics`: Return current model performance.
    - Response: `{"ensemble_weights": [0.5, 0.3, 0.2], "last_training": "2025-12-30T00:00:00", "validation_sharpe": 1.8}`.
- [NEW] `GET /api/v1/model/predict`: Single prediction endpoint (for testing).
    - Request: `{"features": {...}}` or `{"symbol": "AAPL"}`.
    - Response: `{"prediction": 0.0023, "confidence": 0.85}`.

**File**: `app/api/v1/router.py`
- [MODIFY] Include `model` router.

**File**: `app/ml/predictor.py`
- [NEW] `get_model_info()`: Return metadata (weights, training date, metrics).

### 4. Metadata Storage
**File**: `app/ml/predictor.py` or new `model_artifacts/metadata.json`
- Save training metadata:
    - Weights: `[0.5, 0.3, 0.2]`.
    - Training date: ISO timestamp.
    - Validation metrics: Sharpe, Win Rate, MSE for each model.

## Verification Plan

### Automated Tests
- None yet (suggest adding in future).

### Manual Verification
1.  **Scaler Creation**: Run training -> Check `/app/model_artifacts/feature_scaler.pkl` exists.
2.  **Weighted Training**: Run training -> Check logs for `Ensemble weights: [...]`.
3.  **API Test**:
    ```bash
    curl http://localhost:8000/api/v1/model/metrics
    ```
    Expected: JSON with weights and metrics.
4.  **Prediction Test**:
    ```bash
    curl -X POST http://localhost:8000/api/v1/model/predict \
      -H "Content-Type: application/json" \
      -d '{"symbol": "AAPL"}'
    ```
    Expected: Prediction value.

## Implementation Order
1.  Fix `feature_scaler` path (quick fix).
2.  Implement weighted ensemble in `EnsembleWrapper`.
3.  Update `train_models` to calculate weights.
4.  Create `/api/v1/model/metrics` endpoint.
5.  Create `/api/v1/model/predict` endpoint.
