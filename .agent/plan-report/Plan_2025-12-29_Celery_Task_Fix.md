# Celery Task Discovery Fix Plan

## Goal
Resolve `Received unregistered task` error by ensuring all task modules are imported when the Celery worker starts.

## Problem
The `autodiscover_tasks` mechanism may not be sufficient for the current directory structure (`app/tasks/*.py`), leading to tasks not being registered in the worker.

## Solution
Explicitly list all task modules in the `include` argument of the `Celery` application constructor. This forces the worker to import these modules on startup.

## Changes
### `app/worker.py`
- Update `Celery(...)` constructor to add `include` list:
    - `"app.tasks.training"`
    - `"app.tasks.trading"`
    - `"app.tasks.market_analysis"`
    - `"app.tasks.data_tasks"`
- Remove redundant `autodiscover_tasks` calls.

## Verification
- Restart worker container.
- Check logs for "Tasks" section or run `celery -A app.worker inspect registered`.
