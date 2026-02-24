# Plan: Session 10 — Model Performance Improvement & Sentiment Schedule

**Date**: 2026-02-25  
**Phase**: J.3 (Model Training Evaluation & Improvement)  
**Session**: 10

---

## Objective

Improve model diagnostics, fix class imbalance per regime, reduce sentiment API overhead, and prepare for retrain cycle.

---

## Technical Approach

### Task 1: Sentiment Schedule Change (worker.py)
- Change `crontab(minute="0", hour="9-15", day_of_week="1-5")` → `crontab(minute="0", hour="8,12", day_of_week="1-5")`
- Redis TTL stays at 1h (natural expiration, no premature staleness)
- Impact: 7 calls/day → 2 calls/day for Finnhub + Gemini APIs

### Task 2: Training Report Enhancement (training.py)
- Remove vestigial Sharpe column from `_save_training_report()`
- Add: NEUTRAL Recall, Per-class Precision, Per-class Recall, Per-class F1
- Add: Confusion Matrix (text format) to DETAILED METRICS section
- Source: `holdout_results` dict already contains `pred_distribution` and `actual_distribution`
- Need: Add `classification_report_str` and `neutral_recall` to holdout_results in `_train_regime_specific_models()`

### Task 3: Regime-specific CLASS_WEIGHTS (training.py + models.py)
- Current: All regimes use `DEFAULT_CLASS_WEIGHTS = {0:1.3, 1:1.0, 2:1.3}`
- Proposed regime-specific weights:
  - `bull_trending`: {0:1.3, 1:1.2, 2:1.0} — slightly favor DOWN detection, reduce UP bias
  - `bear_trending`: {0:1.0, 1:1.5, 2:1.3} — strongly boost NEUTRAL (was 7/64)
  - `sideways_calm`: {0:1.2, 1:1.3, 2:1.0} — boost NEUTRAL, reduce UP over-prediction
  - `sideways_volatile`: same as sideways_calm (fallback)
- Implementation: Add `REGIME_CLASS_WEIGHTS` dict in training.py, pass to EnsembleClassifierWrapper
- EnsembleClassifierWrapper already accepts class weights via `model_params` → need to inject into regime_params

### Task 4: Classification Report Logging (training.py)
- After holdout predictions, add `sklearn.metrics.classification_report()` output to logger
- Include in holdout_results as `classification_report_str` key
- Written to training report DETAILED METRICS section

### Task 5: Feature Importance Logging (training.py)
- After production ensemble training, extract top-10 feature importances
- CatBoost: `model.feature_importances_`; LightGBM: `model.feature_importances_`; XGBoost: `model.feature_importances_`
- Log and include in training report

---

## File Changes

| File | Change | Risk |
|------|--------|------|
| `app/worker.py` | Sentiment crontab 9-15 → 8,12 | Low |
| `app/tasks/training.py` | Report enhancement, regime weights, classification_report, feature importance | Medium |

---

## Test Scenarios

1. **Sentiment schedule**: Verify crontab syntax `crontab(minute="0", hour="8,12", ...)` is valid
2. **Training report**: Run training → verify new columns appear, no Sharpe column
3. **Regime weights**: Each regime gets correct CLASS_WEIGHTS
4. **Classification report**: Full precision/recall/f1 per class logged
5. **Feature importance**: Top-10 features logged after training

---

## Risks

- Regime-specific weights are empirical; may need 1-2 iterations
- Feature importance access differs per model wrapper (need to check API)
- Training report changes affect downstream parsing (if any)
