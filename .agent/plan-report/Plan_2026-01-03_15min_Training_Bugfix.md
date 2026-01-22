# Plan: 15min Training Critical Bugfix & Validation

**Date**: 2026-01-03  
**Roadmap Alignment**: Phase D (ML Core) Debugging + Phase F.1 (15m Candle Integration Stability)  
**Project**: FastAPIStockTrader

---

## 1. PROBLEM STATEMENT

### 1.1 Critical Bug
**Error**: `NameError: name 'strategy_returns' is not defined`  
**Location**: `app/tasks/training.py` Line 149  
**Impact**: Model training completely fails after backfill

**Code Analysis**:
```python
# Line 149 (BROKEN)
sharpe = strategy_returns.mean() / (strategy_returns.std() + 1e-8) * (bars_per_year ** 0.5)

# BUT: Variable 'strategy_returns' is never defined!
# In tune_models (Line 282), it's named 'returns', not 'strategy_returns'
```

### 1.2 Secondary Issue
**LightGBM Warning**: `No further splits with positive gain`
- **NOT a critical bug** (model still runs)
- **Possible Causes**:
  - Small validation set after 15min conversion
  - Overlapping features (multicollinearity)
  - Early stopping too aggressive

---

## 2. ROOT CAUSE ANALYSIS

### 2.1 Variable Naming Inconsistency
| Function        | Line | Variable Name   | Status |
|-----------------|------|-----------------|--------|
| `tune_models`   | 282  | `returns`       | ✅ OK  |
| `tune_models`   | 326  | `returns`       | ✅ OK  |
| `tune_models`   | 370  | `ens_rets`      | ✅ OK  |
| `train_models`  | 149  | `strategy_returns` | ❌ UNDEFINED |

**Inconsistency Pattern**: Copy-paste error during refactoring.

### 2.2 Optuna Integration (CONFIRMED WORKING)
✅ **Tuning Process**:
1. `tune_models` → Finds best params via Optuna (30 trials/model)
2. Saves to `model_artifacts/best_params.json`
3. `train_models` → Loads params automatically (Line 124-133)
4. Uses loaded params for final training (Line 139-141)

**No changes needed in Optuna logic.**

---

## 3. SOLUTION DESIGN

### 3.1 Fix Strategy (Minimal Invasive)
**Principle**: Fix ONLY the bug. Do NOT touch working Optuna logic.

**Change Scope**:
- File: `app/tasks/training.py`
- Lines: 145-150 (6 lines)
- Operation: Add `strategy_returns` calculation before usage

**Before (Line 145-150)**:
```python
                predictions = model.predict(X_val_scaled)
                
                # Sharpe Ratio (Adjusted for 15m bars: 26 bars/day * 252 days)
                bars_per_year = 252 * 26
                sharpe = strategy_returns.mean() / (strategy_returns.std() + 1e-8) * (bars_per_year ** 0.5)
                sharpe_ratios.append(max(sharpe, 0.1))
```

**After**:
```python
                predictions = model.predict(X_val_scaled)
                
                # Calculate strategy returns
                pred_dir = (predictions > 0).astype(int) * 2 - 1  # -1 or +1
                strategy_returns = y_val.values * pred_dir
                
                # Sharpe Ratio (Adjusted for 15m bars: 26 bars/day * 252 days)
                bars_per_year = 252 * 26
                sharpe = strategy_returns.mean() / (strategy_returns.std() + 1e-8) * (bars_per_year ** 0.5)
                sharpe_ratios.append(max(sharpe, 0.1))
```

### 3.2 LightGBM Warning Mitigation (OPTIONAL)
**Approach**: Add logging + defensive checks, NO parameter changes.

**Rationale**:
- Optuna will find optimal params automatically
- Warning doesn't prevent training (models still train successfully)
- Better to monitor and let Optuna adapt

**Action**:
- Add data validation before training:
  ```python
  if len(X_train) < 500:
      logger.warning(f"Small training set: {len(X_train)} samples. Consider longer backfill.")
  ```

---

## 4. VALIDATION PLAN

### 4.1 Unit Test (Offline)
**Scenario**: Mock 1000 rows of 15min data
```python
# tests/test_training_15min.py
def test_train_models_with_mock_15min_data():
    # Generate 1000 rows (approx 1 week of 15min bars)
    # Run train_models task
    # Assert: No NameError
    # Assert: Models saved successfully
```

### 4.2 Integration Test (Server Execution)
**Steps**:
1. Run backfill: `celery -A app.worker call app.tasks.data_tasks.backfill_ohlcv --args='["AAPL"]'`
2. Run tuning: `celery -A app.worker call app.tasks.training.tune_models`
3. Run training: `celery -A app.worker call app.tasks.training.train_models`
4. **Success Criteria**:
   - No `strategy_returns` error
   - `best_params.json` created
   - Models saved in `model_artifacts/`
   - Logs show Sharpe + F1 scores

---

## 5. EXECUTION CHECKLIST

### Phase 1: Code Fix
- [ ] Backup current `app/tasks/training.py`
- [ ] Apply `strategy_returns` fix (Line 147-148)
- [ ] Add data size warning (Line 110)
- [ ] Commit: `git commit -m "fix: Add missing strategy_returns calculation"`

### Phase 2: Testing
- [ ] Create unit test file
- [ ] Run local pytest
- [ ] Deploy to server
- [ ] Run integration test (backfill → tune → train)

### Phase 3: Monitoring
- [ ] Check worker logs for warnings
- [ ] Verify `best_params.json` values are reasonable
- [ ] Compare Sharpe ratios (15min vs 1D baseline)

---

## 6. RISK ASSESSMENT

| Risk | Probability | Impact | Mitigation |
|------|-------------|--------|------------|
| Fix introduces new bug | Low | High | Code review + unit test |
| Optuna params suboptimal for 15min | Medium | Medium | Monitor for 1 week, retune if needed |
| Data insufficient for training | Low | Medium | Require min 1000 samples before training |

---

## 7. ROLLBACK PLAN

If training still fails after fix:
1. Revert commit: `git revert HEAD`
2. Restore backup: `cp training.py.bak app/tasks/training.py`
3. Investigate data quality: Run `scripts/check_data_quality.py`

---

## 8. POST-DEPLOYMENT VALIDATION

**Within 24 hours**:
- [ ] Monitor worker logs for errors
- [ ] Check model performance in backtest
- [ ] Verify ensemble weights are reasonable (not 100% to one model)

**Within 1 week**:
- [ ] Compare 15min strategy Sharpe vs 1D baseline
- [ ] Document any parameter patterns (e.g., LGBM prefers shallow trees on 15min data)

---

## APPENDIX: Code Reference

**Files to Modify**:
1. `app/tasks/training.py` (Lines 147-148, 110)

**Files to Create**:
1. `tests/test_training_15min.py`

**Files to Monitor**:
1. `model_artifacts/best_params.json`
2. `model_artifacts/*.cbm` (CatBoost)
3. `model_artifacts/*.pkl` (LGBM, XGBoost)
