# Celery Task Registration Refactor

## Goal
Replace `@shared_task` with `@celery_app.task` in all task modules to resolve task discovery issues and ensure explicit registration with the Celery app instance.

## Context
The user reported that tasks using `@shared_task` are not being recognized by the worker. Switching to `@celery_app.task` binds the tasks directly to the instantiated application, avoiding discovery ambiguity.

## Changes

### 1. `app/tasks/training.py`
- [MODIFY] Import `celery_app` from `app.worker`
- [MODIFY] Change `@shared_task` to `@celery_app.task`

### 2. `app/tasks/trading.py`
- [MODIFY] Import `celery_app` from `app.worker`
- [MODIFY] Change `@shared_task` to `@celery_app.task`

### 3. `app/tasks/market_analysis.py`
- [MODIFY] Import `celery_app` from `app.worker`
- [MODIFY] Change `@shared_task` to `@celery_app.task`

### 4. `app/tasks/data_tasks.py`
- [MODIFY] Import `celery_app` from `app.worker`
- [MODIFY] Change `@shared_task` to `@celery_app.task`

## Verification
- Restart Celery worker
- Verify that `celery -A app.worker inspect registered` lists all tasks.
