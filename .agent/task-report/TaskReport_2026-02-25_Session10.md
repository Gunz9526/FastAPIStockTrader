# Task Report: Session 10 — Model Diagnostics & Sentiment Optimization

**Date**: 2026-02-25  
**Phase**: J.3.2  
**Status**: PASS

---

## Implementation Summary

### 1. Sentiment Schedule Optimization
- **Before**: `crontab(minute="0", hour="9-15", day_of_week="1-5")` → 7×/day
- **After**: `crontab(minute="0", hour="8,12", day_of_week="1-5")` → 2×/day
- **Rationale**: Sentiment is confidence adjuster only (15% weight), not ML feature. Finnhub aggregates 24h news. 2×/day (pre-market + mid-session) is sufficient.

### 2. Training Report Overhaul
- Removed vestigial Sharpe column (always showed 'N/A')
- Added: F1 Score, NEUTRAL Recall columns to summary table
- Added: sklearn `classification_report` (per-class precision/recall/f1) to detailed section
- Added: Top-10 Feature Importance (averaged across 3 ensemble models) to detailed section

### 3. Regime-specific CLASS_WEIGHTS
- **bull_trending**: {0:1.3, 1:1.2, 2:1.0} — UP bias reduction
- **bear_trending**: {0:1.0, 1:1.5, 2:1.3} — NEUTRAL recovery (was 7/64=11%)
- **sideways_calm**: {0:1.2, 1:1.3, 2:1.0} — UP over-prediction (2775 vs 1739) correction
- **sideways_volatile**: {0:1.2, 1:1.3, 2:1.0} — same as sideways_calm
- Injected into EnsembleClassifierWrapper via `model_params` per-model keys

### 4. Classification Report Logging
- `sklearn.metrics.classification_report()` output logged after holdout validation
- NEUTRAL recall calculated separately and stored in holdout_results

### 5. Feature Importance Logging
- Top-10 features extracted from `production_ensemble.model.named_estimators_`
- Averaged across CatBoost/LightGBM/XGBoost
- Logged + written to training report

---

## Files Modified

| File | Changes | Errors |
|------|---------|--------|
| `app/worker.py` | Sentiment crontab 9-15 → 8,12 | 0 |
| `app/tasks/training.py` | +75 lines: REGIME_CLASS_WEIGHTS, classification_report, neutral_recall, feature importance, report overhaul | 0 |

**Total Errors: 0**

---

## QA Results

| Layer | Check | Result |
|-------|-------|--------|
| L1: Boundary | All changes within app/tasks/ and app/worker.py | PASS |
| L1: Dependency | `classification_report` already in sklearn.metrics | PASS |
| L1: Logic | REGIME_CLASS_WEIGHTS covers all 4 MarketRegime values | PASS |
| L2: Type Hints | `REGIME_CLASS_WEIGHTS: dict[str, dict[int, float]]` | PASS |
| L2: Error Handling | Feature importance wrapped in try-except | PASS |
| L3: Dead Code | Sharpe column removed, no unused imports | PASS |

---

## Next Steps
1. Run `train_models` Celery task to validate all changes with real data
2. Evaluate new training report (check NEUTRAL recall improvement, feature importance)
3. If accuracy ≥ 45% and NEUTRAL recall ≥ 15% → Phase J.3 complete → Phase K
4. If not → Consider Optuna tuning (Phase M.3), feature selection (Phase M.2)
