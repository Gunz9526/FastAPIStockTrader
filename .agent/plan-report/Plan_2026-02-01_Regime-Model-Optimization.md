# Plan: Regime Model Optimization & Sector-Based Training

**Date:** 2026-02-01  
**Phase:** H.5 - Advanced Regime Model Optimization  
**Status:** Draft

---

## 1. Objectives

### 1.1 Bull Model Activation Strategy
- **Problem:** Bull model has 48.78% accuracy, -0.42 Sharpe (below random)
- **Root Cause:** Feature-regime mismatch (mean-reversion features vs trend-following needs)
- **Solution:** Add bull-specific trend-following features (ADX-based, breakout, momentum)

### 1.2 Regime Feature Sufficiency Analysis
- **Question:** Are current features sufficient for each regime?
- **Analysis Required:** Per-regime feature importance examination

### 1.3 Implementation Tasks
1. **Bull-Specific Features** - ADX direction, consecutive highs, MA alignment
2. **Regime-Specific TimeSeriesSplit** - Ensure regime representation in each fold
3. **Bear Model OOS Verification** - Validate 10.04 Sharpe is not overfit
4. **Sector-Based Model Separation** - Train sector-specific models

---

## 2. Technical Approach

### 2.1 Bull-Specific Features (New)

| Feature | Formula | Rationale |
|---------|---------|-----------|
| `adx_direction` | ADX 5-bar change | Bull markets show increasing ADX |
| `plus_di_minus_di` | +DI - -DI | Positive spread = bullish trend |
| `consec_higher_highs` | Count of consecutive HH | Strong uptrend confirmation |
| `ma_alignment` | EMA12 > EMA26 > SMA50 | Bull market alignment |
| `above_sma200` | close > SMA200 | Long-term trend filter |

### 2.2 Regime Feature Analysis

| Regime | Current Feature Effectiveness | Improvement |
|--------|------------------------------|-------------|
| bull_trending | ❌ Poor (features fight trend) | Add trend-following |
| bear_trending | ⚠️ Overfit risk (10.04 Sharpe) | OOS validation |
| sideways_calm | ✅ Good (mean-reversion works) | None needed |
| sideways_volatile | ⚠️ Disabled (70 samples) | Data accumulation |

### 2.3 Sector-Based Model Architecture

```
Models to train:
├── sector_tech_model.pkl (Technology sector: AAPL, MSFT, NVDA, AMD)
├── sector_consumer_model.pkl (Consumer: AMZN, CRM)
├── sector_financial_model.pkl (Financial: if data available)
├── sector_healthcare_model.pkl (Healthcare: if data available)
└── sector_general_model.pkl (Fallback for unknown sectors)
```

### 2.4 OOS Validation Enhancement

```python
# Enhanced validation with statistical significance
def validate_regime_model(regime, model, X, y):
    1. TimeSeriesSplit with 5 folds
    2. Calculate IS vs OOS Sharpe ratio
    3. Bootstrap confidence intervals
    4. T-test for Sharpe > 0
    5. Flag if: OOS/IS < 0.3 OR (IS > 5 AND OOS < 1)
```

---

## 3. File Changes

| File | Action | Description |
|------|--------|-------------|
| `app/ml/features.py` | MODIFY | Add 5 bull-specific features |
| `app/tasks/training.py` | MODIFY | Add sector-based training, regime validation |
| `app/core/config.py` | MODIFY | Add sector model config |
| `app/ml/predictor.py` | MODIFY | Support sector-based prediction |
| `app/services/trading_strategy_sync.py` | MODIFY | Use sector models |

---

## 4. Test Scenarios

### 4.1 Bull Features Test
- **Happy Path:** Bull features calculated correctly for trending data
- **Edge Case:** Insufficient data for 200-bar SMA

### 4.2 Sector Training Test
- **Happy Path:** Tech sector model trains with AAPL, MSFT, AMD, NVDA
- **Edge Case:** Sector with < 1000 samples falls back to general model

### 4.3 Bear OOS Validation Test
- **Happy Path:** Bear model passes OOS validation (ratio > 0.3)
- **Edge Case:** Overfit detected, model disabled or flagged

---

## 5. Risks

| Risk | Mitigation |
|------|------------|
| Bull features still don't improve performance | Keep fallback to sideways_calm |
| Sector models have insufficient data | Use hierarchical fallback |
| Bear model confirmed overfit | Reduce model complexity, add regularization |

---

## 6. Success Criteria

1. Bull model accuracy > 51% (better than random)
2. Bull model Sharpe > 0 (positive expected return)
3. Bear model OOS validation ratio > 0.3
4. Sector models trained for >= 3 sectors
5. All changes pass unused code check

---

**Approval Required:** Y/N/Revision
