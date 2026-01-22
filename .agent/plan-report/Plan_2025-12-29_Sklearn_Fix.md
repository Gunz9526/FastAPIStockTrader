# CatBoost-ScikitLearn Compatibility Fix

## Goal
Resolve `AttributeError: 'CatBoostRegressor' object has no attribute '__sklearn_tags__'` by modifying package versions.

## Problem
The environment has `scikit-learn` version **1.7.1** and `catboost` **1.2.8**. `scikit-learn` 1.6+ introduced a new Tags API (`__sklearn_tags__`) that replaced old flags. CatBoost 1.2.8 does not yet implement this new API, causing failures when used within scikit-learn's `VotingRegressor` or pipelines.

## Solution
Downgrade `scikit-learn` to a version compatible with CatBoost 1.2.x (i.e., **<1.6**), usually **1.5.2** is a safe stable choice.

## Changes
### `requirements.txt`
- [MODIFY] Pin `scikit-learn<1.6` (e.g., `scikit-learn==1.5.2`)

### Docker Rebuild
- Rebuild the `app` and `worker` images to apply the version change.

## Verification
- Run `curl -X POST http://localhost:8000/api/v1/operations/train-models`.
- Verify successful training in worker logs.
