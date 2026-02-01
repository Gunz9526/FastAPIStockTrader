# Plan: Feature Mismatch Fix + Trading Strategy Optimization

**Date:** 2026-01-31  
**Phase:** Phase H - ML Model Optimization (Critical Fix + Enhancement)  
**Related Roadmap:** Backend_Roadmap.md Phase H.1-H.4

---

## 1. Objective

**Primary Goal:** Fix the critical feature count mismatch between training (25 features) and prediction (32 features) that causes LightGBM to fail during live trading.

**Secondary Goals:**
1. Implement model performance reporting system
2. Fix trailing stop logic (always shows "no open positions")
3. Optimize trading thresholds to reduce premature exits
4. Enable Phase F features as post-prediction adjustments

**Error:**
```
LightGBMError: The number of features in data (32) is not the same as it was in training data (25).
```

**Root Cause:**
- Training uses `base_feature_columns` (27 technical indicators)
- Prediction uses `extract_feature_vector()` which automatically adds 5 Phase F features (sentiment + fundamentals)
- This inconsistency breaks the model at runtime

---

## 2. Technical Approach

### 2.1 Core Strategy
Modify `extract_feature_vector()` to conditionally include Phase F features based on a new parameter.

### 2.2 Design Decision
**Option A (Selected):** Add `include_phase_f: bool = False` parameter to `extract_feature_vector()`
- **Pros:** Minimal code change, backward compatible, explicit control
- **Cons:** Caller must know whether to include Phase F features

**Option B (Rejected):** Auto-detect based on model's `feature_names_in_`
- **Pros:** Automatic feature matching
- **Cons:** Requires model to be passed to feature engineer (tight coupling)

**Option C (Rejected):** Always train with 32 features (including Phase F)
- **Pros:** Consistent feature set
- **Cons:** Historical data lacks sentiment/fundamentals (would need backfill)

---

## 3. File Changes

### 3.1 `app/ml/features.py`
**Modification:** `extract_feature_vector()` method

**Change:**
```python
def extract_feature_vector(
    self,
    df: pd.DataFrame,
    fit_scaler: bool = False,
    market_avg_volume: float = None,
    sentiment_score: float | None = None,
    fundamental_data: dict[str, float] | None = None,
    include_phase_f: bool = False  # ← NEW PARAMETER
) -> pd.DataFrame:
```

**Logic:**
- If `include_phase_f=False`: Use only `base_feature_columns` (27 features)
- If `include_phase_f=True`: Use full `feature_columns` (32 features)

**Feature Selection:**
```python
if include_phase_f:
    feature_cols = self.feature_columns  # 32 features (base + Phase F)
else:
    feature_cols = self.base_feature_columns  # 27 features (base only)
```

### 3.2 `app/tasks/training.py`
**Modification:** `_load_and_prepare_data()` function

**Change:** Explicitly call `extract_feature_vector(fit_scaler=True, include_phase_f=False)`

**Reason:** Ensure training uses ONLY base features (historical data lacks Phase F features)

### 3.3 `app/services/trading_strategy_sync.py`
**Modification:** `process_symbol()` and `process_portfolio()` methods

**Change:** Call `extract_feature_vector(include_phase_f=False)` for now

**Future Work:** Once Phase F features are properly integrated, change to `include_phase_f=True` and retrain models

---

## 4. Test Scenarios (Mandatory)

### 4.1 Happy Path: Training → Prediction Consistency
**Test:**
1. Train a regime model using `base_feature_columns` (27 features)
2. Load model and predict on new data with `include_phase_f=False`
3. Verify: No feature count mismatch error

**Expected:** Prediction succeeds, no LightGBM error

### 4.2 Edge Case: Phase F Integration (Future)
**Test:**
1. Manually add sentiment/fundamental features to historical data
2. Train model with 32 features (`include_phase_f=True`)
3. Predict with 32 features (`include_phase_f=True`)

**Expected:** Model trained and predicts with full feature set

### 4.3 Regression Test: Scaler Behavior
**Test:**
1. Verify scaler only normalizes numeric features (excludes `sector_id`)
2. Check scaler saved/loaded correctly with 27 features

**Expected:** Scaler shape matches feature count

---

## 5. Risks & Mitigation

### Risk 1: Scaler Mismatch
**Issue:** Existing saved scaler (`feature_scaler.pkl`) may have 32 features  
**Mitigation:** Delete old scaler, retrain with `include_phase_f=False` to regenerate

### Risk 2: Model Retraining Required
**Issue:** All existing regime models trained with incorrect feature count  
**Mitigation:** Force retrain all regime models after fix deployed

### Risk 3: Phase F Rollout Delayed
**Issue:** Sentiment/fundamentals not yet integrated into training pipeline  
**Mitigation:** Document as Phase F.3 in Roadmap (deferred to next sprint)

---

## 6. Implementation Order

### Task 1: Feature Consistency (CRITICAL - Backend Agent)
1. **Modify `features.py`:** Add `include_phase_f` parameter
2. **Update `training.py`:** Ensure `include_phase_f=False` during training
3. **Update `trading_strategy_sync.py`:** Set `include_phase_f=False` for prediction
4. **Delete Old Artifacts:** Remove `feature_scaler.pkl` and all `*.pkl` models
5. **Retrain Models:** Run training tasks for all 4 regimes
6. **Verify Prediction:** Run trading task and confirm no errors

### Task 2: Model Performance Reporting (Backend Agent)
1. **Create Directory:** `.agent/model-reports/`
2. **Modify `training.py`:** Add `_save_performance_report()` function
3. **Output Format:** JSON + Markdown summary
4. **Include Metrics:** Sharpe, Accuracy, Overfit Ratio, Win Rate, Avg Win/Loss
5. **Auto-upload to Discord:** Optional notification with key metrics

### Task 3: Trailing Stop Fix (Trading Agent)
1. **Investigate:** Why `get_active_position()` returns empty
2. **Debug Logging:** Add position count verification
3. **Fix Query:** Check Position status filter logic
4. **Test:** Verify trailing stop updates on active positions

### Task 4: Threshold Optimization (Quant Agent)
1. **Analyze:** Current threshold (0.2%) vs trading costs
2. **Calculate:** Optimal threshold based on Sharpe/Win Rate
3. **Implement:** Regime-specific thresholds with minimum holding period
4. **Strategy:** Conservative bull, aggressive sideways_calm
5. **Min Holding:** Add 1-hour minimum position duration

---

## 7. Verification Checklist

- [ ] No unused imports added
- [ ] All new parameters have default values (backward compatible)
- [ ] Error logs removed after fix confirmed
- [ ] Roadmap updated to reflect Phase F.3 deferral
- [ ] Discord notification sent confirming fix

---

**Estimated Time:** 30 minutes  
**Complexity:** Low (parameter addition + flag propagation)
