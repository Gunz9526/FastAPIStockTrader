# Session 8 Task Report: Training Pipeline Bug Fixes

**Date**: 2026-02-24  
**Phase**: J.2.1 (Training Pipeline Bug Fixes)  
**Status**: ✅ COMPLETE  

---

## Objective

Fix 3 critical training errors from Docker worker logs + deep logic inspection for hidden bugs.

---

## Bugs Fixed (8 Total)

### Original 3 Errors (from worker logs)

| # | Error | Root Cause | Fix |
|---|-------|-----------|-----|
| 1 | LightGBM categorical_feature train/predict mismatch | `predict()` didn't cast `sector_id` to `category` dtype | `_prepare_categorical_for_predict()` helper in all 6 predict/predict_proba methods |
| 2 | Feature names mismatch (25 vs 27 features) | Validation used `feature_set="legacy"` instead of `"base"` | Added `feature_set="base"` to validation `extract_feature_vector` call |
| 3 | CatBoost "Input data must have at least one feature" | Cascade from Bug 2 — scaler.transform() failed | Fixed by resolving Bug 2 |

### 5 Hidden Bugs (from deep logic audit)

| # | Severity | Bug | Fix |
|---|----------|-----|-----|
| 4 | CRITICAL | Regime-tuned params JSON structure mismatch → Optuna results silently ignored | `_load_regime_params()` with 5-level fallback search |
| 5 | HIGH | Single `feature_scaler.pkl` overwritten by all 4 regimes | `scaler_suffix` parameter → `feature_scaler_{regime}.pkl` |
| 6 | HIGH | `tune_models()` and `_tune_models_global()` used wrong `feature_set` | Added `feature_set="base"` to all tuning paths |
| 7 | HIGH | `best_params_{regime}.json` files never loaded | Merged into Bug 4 fix via `_load_regime_params()` |
| 8 | MEDIUM | `relative_volume` always 1.0 during training | Conditional overwrite: only set 1.0 if not already in DataFrame |

### Additional Fixes (PM-discovered)

| # | Description |
|---|-------------|
| 9 | Inference path `scaler_suffix` — all 4 callers updated (trading_strategy_sync ×2, backtest, RAG) |
| 10 | Scaler-model regime alignment — fallback resolved BEFORE scaling so scaler matches model's regime |

---

## Files Modified

| File | Changes |
|------|---------|
| `app/ml/models.py` | `_prepare_categorical_for_predict()`, all predict/predict_proba categorical prep |
| `app/ml/features.py` | `scaler_suffix` parameter, `relative_volume` conditional overwrite |
| `app/tasks/training.py` | `_load_regime_params()`, `feature_set="base"` everywhere, `scaler_suffix=regime_value` |
| `app/services/trading_strategy_sync.py` | `scaler_suffix=effective_regime`, fallback-before-scaling logic |
| `app/backtest/ml_strategy.py` | `scaler_suffix=regime_suffix` |
| `app/api/v1/endpoints/rag.py` | `scaler_suffix=regime_suffix` |

---

## QA Results

| File | Errors | Status |
|------|--------|--------|
| `app/ml/models.py` | 0 | ✅ PASS |
| `app/ml/features.py` | 0 | ✅ PASS |
| `app/tasks/training.py` | 0 | ✅ PASS |
| `app/services/trading_strategy_sync.py` | 0 | ✅ PASS |
| `app/backtest/ml_strategy.py` | 0 | ✅ PASS |
| `app/api/v1/endpoints/rag.py` | 0 | ✅ PASS |

---

## Key Architectural Decisions

### Regime-Specific Scalers
- Each of 4 regimes now has its own scaler: `feature_scaler_{regime}.pkl`
- Prevents distribution mismatch: Bull RSI mean ≈ 65 vs Sideways RSI mean ≈ 50
- Inference path loads scaler matching the model's regime (after fallback resolution)

### 5-Level Param Loading (`_load_regime_params`)
1. `best_params_{regime}.json` (regime-specific file)
2. `best_params.json` → `regime_specific[regime]` (combined file)
3. `best_params.json` → `default` (combined file fallback)
4. `best_params.json` top-level keys (legacy format)
5. Empty dict (use model defaults)

### Categorical Predict Alignment
- `_prepare_categorical_for_predict(X, model_type)` ensures correct dtype per framework:
  - CatBoost: `sector_id` → `int`
  - LightGBM: `sector_id` → `category` dtype
  - XGBoost: `sector_id` → `category` dtype + DMatrix `enable_categorical=True`

---

## Model Direction Analysis

**Decision**: Keep **4 regime models with sector_id as native categorical feature** (current architecture).

**Rationale** (see Session 8 Korean report for detailed analysis):
- 60 symbols / 52 cells (4 regimes × 13 sectors) = ~1.25 symbols per cell → catastrophic data scarcity
- CatBoost/LightGBM/XGBoost natively handle categorical features via tree splits → learns sector-specific patterns within unified model
- Cross-sector information sharing (macro patterns) would be lost with splitting
- 52 models × 3 estimators = 156 total → impractical on 4-core CPU
- Future: Phase M.2 (SHAP) can identify sector-specific feature importance within regime models
