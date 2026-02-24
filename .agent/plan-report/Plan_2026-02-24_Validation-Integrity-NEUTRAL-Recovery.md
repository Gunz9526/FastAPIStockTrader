# Plan: Session 9 — Validation Integrity & NEUTRAL Recovery

**Date**: 2026-02-24  
**Phase**: J.3.1 (Training Pipeline Quality Improvement)  
**Triggered by**: Session 8 training result analysis

---

## Objective

Fix 3 critical issues in the training pipeline discovered through quantitative analysis of the first successful training run:

1. **Data Leakage**: Validation metrics are computed on data that was used for training (in-sample)
2. **NEUTRAL Class Collapse**: Model predicts NEUTRAL < 0.2% despite actual NEUTRAL being ~18.6%
3. **θ Too Narrow**: CLASSIFICATION_THRESHOLD = 0.003 (±0.3%) is too tight for daily return noise

---

## Technical Approach

### Fix 1: Validation Data Separation (Data Leakage Removal)

**Current flow** (training.py):
```
_train_regime_specific_models():
  ensemble.train(X_regime_scaled, y_regime)  # 100% of regime data
  
train_models() → Validation:
  X_val = X_regime.iloc[split_idx:]  # Last 20% — ALREADY IN TRAINING SET
```

**New flow**:
```
_train_regime_specific_models():
  # Split BEFORE training
  split_idx = int(len(X_regime) * 0.8)
  X_train = X_regime.iloc[:split_idx]
  y_train = y_regime.iloc[:split_idx]
  X_holdout = X_regime.iloc[split_idx:]
  y_holdout = y_regime.iloc[split_idx:]
  
  # WF + ensemble training on X_train ONLY
  ensemble.train(X_train_scaled, y_train)
  
  # Validation on X_holdout (true out-of-sample)
  accuracy = evaluate(ensemble, X_holdout_scaled, y_holdout)
  
  # THEN retrain on full data for production model
  ensemble_production.train(X_regime_scaled, y_regime)
  ensemble_production.save(...)
```

This gives us:
- **Honest metrics** from holdout validation
- **Full-data production model** (maximizes training data)
- Both reported in the training log

### Fix 2: CLASS_WEIGHTS Normalization

**Current**: `{0: 1.5, 1: 0.5, 2: 1.5}` → 3x penalty gap, NEUTRAL suppressed

**New**: `{0: 1.3, 1: 1.0, 2: 1.3}`
- UP/DOWN still emphasized (directional moves are important for trading)
- NEUTRAL gets fair representation (1.0 vs 0.5)
- Ratio changes from 3:1 to 1.3:1

### Fix 3: θ Adjustment

**Current**: θ = 0.003 (±0.3%)  
**New**: θ = 0.005 (±0.5%)

- S&P 500 daily return σ ≈ 1.5%
- ±0.5% ≈ ±0.33σ → captures ~26% of returns as NEUTRAL
- Reduces noise trades significantly
- Still captures meaningful directional moves (> 0.5% daily)

### Fix 4: min_samples Increase

**Current**: `min_samples = 300`  
**New**: `min_samples = 500`

- sideways_volatile (300 samples) is statistically insufficient for 3-model ensemble
- 500 samples allows for 400 train + 100 holdout minimum
- Insufficient-data regimes fall back to sideways_calm model

---

## File Changes

| File | Changes |
|------|---------|
| `app/tasks/training.py` | Validation separation, min_samples, θ |
| `app/ml/models.py` | DEFAULT_CLASS_WEIGHTS |

---

## Test Scenarios

1. After fix: training accuracy should DROP to ~45-55% range (honest metric)
2. NEUTRAL predictions should increase from ~0.2% to ~15-25%
3. F1-score may decrease numerically but becomes meaningful
4. sideways_volatile should be skipped (300 < 500)

---

## Risks

| Risk | Mitigation |
|------|-----------|
| Lower reported accuracy may alarm user | Document that old metrics were invalid |
| NEUTRAL increase may reduce trading frequency | This is desirable — fewer noise trades |
| sideways_volatile model lost | sideways_calm fallback already configured |
