# Training Task Data Type Fix

## Goal
Resolve `Invalid comparison between dtype=datetime64[ns, UTC] and datetime` error in `app/tasks/training.py`.

## Problem
The `features_df.index` is a pandas Timestamp with UTC timezone (from `date_time` column in `stock_ohlcv`), but `val_start` is a naive Python `datetime` object (from `datetime.now()`). Pandas raises an error when comparing offset-naive and offset-aware datetimes.

## Solution
Ensure `val_start` is timezone-aware and compatible with the DataFrame index before comparison.

## Changes
### `app/tasks/training.py`
- [MODIFY] `train_models` function:
    - Make `end_date` timezone-aware (UTC).
    - Convert `val_start` to pandas Timestamp with UTC timezone to match `features_df.index`.

## Verification
- Run `curl -X POST http://localhost:8000/api/v1/operations/train-models`.
- Check worker logs for successful completion or at least progressing past the comparison error.
