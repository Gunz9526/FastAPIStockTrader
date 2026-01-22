# Ensemble Improvement & Scaler Fix Plan

## Goal
1.  **Fix Missing Scaler**: Ensure `feature_scaler.pkl` is saved correctly by using absolute paths.
2.  **Improve Ensemble**: Explain current Voting (Averaging) strategy and propose/implement "Weighted Averaging" based on recent performance (validation scores).

## Problem Analysis
1.  **Missing Scaler**: `app/ml/features.py` uses `scaler_path = "model_artifacts/feature_scaler.pkl"` (relative). This likely lands in `/app/model_artifacts`, but consistency with `predictor.py` (which we fixed to `/app/model_artifacts/...`) is key. Also, `extract_feature_vector` must be called with `fit_scaler=True` during training for the file to be created.
2.  **Ensemble Strategy**: Current is `VotingRegressor` (simple average). This assumes all base models (CatBoost, XGB, LGBM) are equally good.
    - **Better Approach**: **Weighted Ensemble**. Assign weights based on validation performance (e.g., Sharpe Ratio or MSE).
    - **Even Better**: **Stacking**. Use a meta-learner (e.g., LinearRegression) to combine predictions. (Might be overkill for now, weighted is a good middle ground).

## Solutions

### 1. Fix Scaler Path
- **File**: `app/ml/features.py`
- **Change**: Default `scaler_path` to `/app/model_artifacts/feature_scaler.pkl`. Ensure directory creation permission (same as model fix).

### 2. Implement Weighted Ensemble
- **File**: `app/ml/models.py`
- **Change**: Modify `EnsembleWrapper` to accept weights.
- **File**: `app/tasks/training.py`
- **Change**: Calculate weights based on validation set performance (e.g., inverse MSE or direct Sharpe). Pass these weights to `EnsembleWrapper`.

## Execution Steps
1.  Modify `app/ml/features.py` (Path fix).
2.  Modify `app/ml/models.py` (Add `weights` support to `EnsembleWrapper`).
3.  Modify `app/tasks/training.py` (Calculate and pass weights).

## Verification
- Run training.
- Check `feature_scaler.pkl` existence.
- Check logs for "Ensemble weights: [...]".
