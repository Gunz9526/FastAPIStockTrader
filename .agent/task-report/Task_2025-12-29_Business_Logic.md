# Task Report: Business Logic & ML Implementation

**Date**: 2025-12-29
**Task**: Implement ML Engine & Scheduling

## Summary
Implemented a CPU-optimized Machine Learning engine using Tree-based models (CatBoost, LightGBM, XGBoost) and an Ensemble wrapper. Set up Celery for scheduling automated trading during market hours and model retraining after hours. Removed KisDataProvider to focus solely on Alpaca.

## Changes Created
### Machine Learning (`app/ml/`)
- `models.py`: Wrappers for CatBoost, LGBM, XGBoost, VotingRegressor.
- `predictor.py`: Singleton service for loading models and low-latency inference.
- `trainer.py`: (Integrated into celery task for MVP) - see `tasks/training.py`.

### Scheduling (`app/worker.py`, `app/tasks/`)
- `worker.py`: Celery app with Beat schedule (Market Scan: 9-16 EST, Retraining: 18 EST).
- `tasks/trading.py`: Async market scan task linked to Strategy Engine.
- `tasks/training.py`: Model retraining task with Optuna hyperparameter optimization.

### Refactoring
- `app/services/data_provider.py`: Rewritten for Alpaca-only logic.
- `pyproject.toml` / `requirements.txt`: Added ML libraries.

## Status
- **ML**: Ready (Tree-based, CPU optimized).
- **Scheduling**: Ready (Celery Beat).
- **Data**: Alpaca-only.
