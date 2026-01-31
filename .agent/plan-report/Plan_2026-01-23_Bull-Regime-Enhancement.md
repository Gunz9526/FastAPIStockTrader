# Plan: Bull Regime Model Enhancement & Walk-Forward Validation

**Date:** 2026-01-23  
**Phase:** H.4 (New) - Regime Model Optimization  
**Status:** In Progress

---

## 1. Objective

Address the poor performance of the `bull_trending` regime model (49% accuracy, -0.22 Sharpe) through:
1. Feature importance analysis to identify weak predictors
2. Bull-market-specific feature engineering (momentum indicators)
3. Regime-aware trading strategy adjustments
4. Walk-forward out-of-sample validation

---

## 2. Problem Analysis

### Current Performance Metrics
| Regime | Accuracy | Sharpe | Assessment |
|--------|----------|--------|------------|
| bull_trending | 49.02% | -0.2246 | ❌ CRITICAL |
| bear_trending | 52.81% | 10.4766 | ✅ Good (but suspicious) |
| sideways_volatile | - | - | ⚠️ Skipped (70 samples) |
| sideways_calm | 53.21% | 6.5283 | ✅ Good |

### Root Cause Hypotheses
1. **Insufficient bull-market features**: Current features favor mean-reversion (RSI, BB) over momentum
2. **Trend-following vs Mean-reversion conflict**: Bull markets require trend-following logic
3. **Overfitting on bear/sideways**: Model may have overfit to recent market conditions
4. **Sharpe > 10 is unrealistic**: Bear model may be overfit (needs OOS validation)

---

## 3. Technical Approach

### 3.1 Feature Importance Analysis (Task 1)
- Extract feature importance from each regime model
- Generate JSON + visualization for analysis
- Identify low-importance features for potential removal
- **File:** `app/tasks/training.py::analyze_feature_importance()`

### 3.2 Bull-Specific Feature Engineering (Task 2)
Add momentum-focused features optimized for trending markets:

| Feature | Formula | Rationale |
|---------|---------|-----------|
| `momentum_5d` | Close / Close(-5 bars) - 1 | Short-term momentum |
| `momentum_10d` | Close / Close(-10 bars) - 1 | Medium-term momentum |
| `rsi_momentum` | RSI - RSI(-5 bars) | RSI trend direction |
| `trend_strength` | (EMA12 - EMA26) / ATR | Normalized trend measure |
| `price_position` | (Close - Low20) / (High20 - Low20) | Price in 20-bar range |
| `breakout_flag` | Close > High(20) ? 1 : 0 | Breakout detection |

**File:** `app/ml/features.py::add_technical_indicators()`

### 3.3 Regime-Aware Trading Adjustment (Task 3)
Modify `SyncTradingStrategy` to apply regime-specific thresholds:

```python
REGIME_THRESHOLDS = {
    'bull_trending': {'buy': 0.004, 'sell': -0.001, 'confidence': 0.3},  # Conservative
    'bear_trending': {'buy': 0.002, 'sell': -0.002, 'confidence': 0.7},
    'sideways_volatile': {'buy': 0.003, 'sell': -0.003, 'confidence': 0.5},
    'sideways_calm': {'buy': 0.002, 'sell': -0.002, 'confidence': 0.7},
}
```

**Logic:**
- Bull regime: Higher buy threshold (0.4% vs 0.2%), lower sell threshold
- Add `confidence` factor to reduce position size in unreliable regimes
- Option to skip trading entirely in bull_trending until model improves

**File:** `app/services/trading_strategy_sync.py`

### 3.4 Walk-Forward Out-of-Sample Validation (Task 4)
Implement proper walk-forward validation to detect overfitting:

```
Training Window: 18 months rolling
Validation Window: 3 months (out-of-sample)
Periods:
  - Train: 2024-01 to 2025-06 → Validate: 2025-07 to 2025-09
  - Train: 2024-04 to 2025-09 → Validate: 2025-10 to 2025-12
  - Train: 2024-07 to 2025-12 → Validate: 2026-01 (most recent)
```

**Output:**
- Per-regime OOS Sharpe ratio
- Overfitting detection: Train Sharpe >> OOS Sharpe → overfit
- Regime-specific model confidence scores

**File:** `app/tasks/training.py::_walk_forward_validation_enhanced()`

---

## 4. File Changes

### Modified Files
| File | Changes |
|------|---------|
| `app/ml/features.py` | Add 6 momentum features |
| `app/services/trading_strategy_sync.py` | Regime-specific thresholds |
| `app/tasks/training.py` | Enhanced walk-forward validation |

### New Files
| File | Purpose |
|------|---------|
| `app/ml/feature_analyzer.py` | Feature importance analysis utility |
| `docs/BULL_REGIME_ANALYSIS.md` | Analysis report |

---

## 5. Test Scenarios

### Happy Path
1. Train bull_trending model with new momentum features
2. Verify accuracy improves from 49% → 52%+
3. Verify Sharpe improves from -0.22 → 0.0+ (at least neutral)

### Edge Cases
1. **Insufficient bull data**: If < 500 bull samples, fallback to generic model
2. **Feature NaN handling**: Momentum features require 10+ bars lookback
3. **Walk-forward data gap**: Handle missing data periods gracefully

### Validation Criteria
- [ ] OOS Sharpe within 50% of in-sample Sharpe (no severe overfit)
- [ ] Bull model accuracy > 50% (better than random)
- [ ] No regression in bear/sideways models

---

## 6. Risks & Mitigations

| Risk | Probability | Impact | Mitigation |
|------|-------------|--------|------------|
| New features cause overfitting | Medium | High | OOS validation mandatory |
| Bull data still insufficient | High | Medium | Consider shorter lookback |
| Bear model overfit confirmed | Medium | High | Re-train with proper CV |

---

## 7. Estimated Timeline

| Task | Duration | Dependency |
|------|----------|------------|
| Feature importance analysis | 30 min | None |
| Momentum feature implementation | 1 hour | None |
| Trading strategy adjustment | 30 min | None |
| Walk-forward validation | 1 hour | Features ready |
| Documentation | 30 min | All complete |

**Total:** ~3.5 hours

---

## 8. Success Criteria

1. ✅ Bull regime accuracy > 50%
2. ✅ Bull regime Sharpe > 0.0
3. ✅ OOS validation shows no severe overfitting
4. ✅ Bear/sideways models maintain performance
5. ✅ Documentation complete
