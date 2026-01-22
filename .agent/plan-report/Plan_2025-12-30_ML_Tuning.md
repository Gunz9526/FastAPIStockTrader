# Implementation Plan - Phase F Model Tuning Fix & Bug Resolution

## Goal Description
Fix the `NameError: name 'strategy_returns' is not defined` critical bug preventing model evaluation. Resolve `LightGBM` warnings by refining hyperparameter space. Ensure CatBoost and XGBoost are also robustly configured and verified.

## Proposed Changes

### 1. Fix Critical Bugs (app/tasks/training.py)
#### [MODIFY] [app/tasks/training.py](file:///f:/Work/FastAPIStockTrader/app/tasks/training.py)
- **Bug Fix**: In `train_models` loop, calculate `strategy_returns` before usage.
    ```python
    pred_dir = (predictions > 0).astype(int) * 2 - 1
    strategy_returns = y_val.values * pred_dir
    ```
- **Robustness**: Ensure `y_val` alignment with `predictions`.

### 2. Robust Hyperparameter Tuning (All Models)
#### [MODIFY] [app/tasks/training.py](file:///f:/Work/FastAPIStockTrader/app/tasks/training.py)
- **LightGBM**:
    - `learning_rate`: Max `0.1`, `num_leaves`: Max `50`, `reg_lambda`: Max `2.0`.
    - Add `min_child_samples` (20-100).
- **CatBoost**:
    - Reduce `learning_rate` range (0.01 - 0.1).
    - Limit `depth` (4-8) to prevent memory issues.
- **XGBoost**:
    - Add `min_child_weight` to prevent overfitting on noise.
    - Limit `max_depth` to 6.

### 3. Model Wrapper Enhancements
#### [MODIFY] [app/ml/models.py](file:///f:/Work/FastAPIStockTrader/app/ml/models.py)
- **All Wrappers**: Add `min_child_samples` / `min_child_weight` support where applicable.
- **Logging**: Log configured parameters on initialization to debug what actsually gets passed.

## Verification Plan

### Automated Tests
1.  **Run Tuning Script**:
    - execute `python scripts/run_tuning.py` (New script to invoke `tune_models`).
    - **Success Criteria**:
        - No `NameError`.
        - No `[LightGBM] [Warning]`.
        - `best_params.json` contains valid params for all 3 models.

### Manual Verification
- Check logs for "Sharpe: X.XXXX" output for all 3 models (proving evaluation logic works).
