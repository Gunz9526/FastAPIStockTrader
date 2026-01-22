# CatBoost-ScikitLearn Wrapper Plan

## Goal
Implement a compatibility wrapper for `CatBoostRegressor` that supports the new `__sklearn_tags__` API introduced in scikit-learn 1.6+, allowing the use of the latest `scikit-learn` version without downgrading.

## Problem
`scikit-learn` 1.6+ requires estimators to implement `__sklearn_tags__` to define their capabilities (e.g., regressor vs classifier). The current `catboost` version (1.2.8) does not implement this, causing `AttributeError` when used in meta-estimators like `VotingRegressor`.

## Solution
Create a custom class `CompatibleCatBoostRegressor` that inherits from `CatBoostRegressor` and `sklearn.base.BaseEstimator` (or implements the required method), exposing the necessary tags.

## Changes
### `app/ml/models.py`
- [NEW] Import `BaseEstimator`, `RegressorMixin` from `sklearn.base`.
- [NEW] Define `CompatibleCatBoostRegressor(CatBoostRegressor, BaseEstimator, RegressorMixin)` class.
    - Implement `__sklearn_tags__` method returning appropriate tags.
- [MODIFY] Update `EnsembleWrapper.train` to use `CompatibleCatBoostRegressor` instead of `CatBoostRegressor`.
- [MODIFY] Update `CatBoostWrapper` (if it uses `VotingRegressor` internally, though it seems standalone currently). *Note: The error came from `EnsembleWrapper` usage.*

## Verification
- Revert `requirements.txt` change (keep `scikit-learn` unpinned or latest).
- Rebuild Docker images.
- Run `curl -X POST http://localhost:8000/api/v1/operations/train-models`.
- Verify successful training in worker logs.
