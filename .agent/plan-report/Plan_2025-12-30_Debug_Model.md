# Model Training & Data Count Investigation Plan

## Goal
1.  Verify why model files are not appearing on the host despite successful training logs.
2.  Investigate why data counts are low (449 samples for 2 years).
3.  Ensure model training is actually effective.

## Problem Analysis
1.  **Missing Model Files**:
    - `docker-compose.yml` mounts `.:/app`. Files written to `/app/model_artifacts` *should* appear on host.
    - **Hypothesis**: Permission issues (container runs as root, host user might not have access, or file ownership issues) or the path is incorrect.
    - **Action**: Check file existence inside container and permissions.

2.  **Low Data Count (449 samples)**:
    - 2 years of trading days $\approx 252 \times 2 = 504$ days.
    - `449` samples is remarkably close to 2 years, minus weekends/holidays/missing data.
    - **Hypothesis**: The data count might actually be *correct* for the available data in DB, but we need to confirm if normalization/dropping logic in `FeatureEngineer` (e.g., `dropna` after rolling windows) is reducing it further.
    - `FeatureEngineer` calculates `SMA(50)`, which drops initial 49 rows.
    - $504 - 50 \approx 454$. 449 is very close.
    - **Action**: The count seems technically explainable, but we should verify the "missing 2-3 years backfill" claim.

## Investigation Steps

### 1. Verify Model Existence in Container
- Execute command in `worker` container to list files in `model_artifacts`.
- Check file permissions.

### 2. Verify Data Count Logic
- Create a script `scripts/debug_data_count.py` to:
    - Connect to DB.
    - Count raw rows for 'AAPL' in `stock_ohlcv`.
    - Apply `FeatureEngineer` logic and count rows after dropping NaNs.
    - Print precise counts to isolate where data is lost.

### 3. Fix Permission/Path Issues (if confirmed)
- If files exist in container but not host, check volume mount status again (already confirmed `.:/app`).
- Ensure `MODEL_SAVE_PATH` is absolute or correctly relative.

## Proposed Changes
- **`app/ml/predictor.py`**:
    - Add more logging on absolute path of saved model.
- **New Script**: `scripts/debug_data_count.py` for verification.

## Verification
- Run `scripts/debug_data_count.py`.
- Check permissions of generated model files.
