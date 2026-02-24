# Cross-Reference Audit: Original Audit vs Current Codebase State

**Date:** 2026-02-24  
**Scope:** Post-Session 5 (Daily Bar + Ternary Classification Conversion)  
**Method:** Direct code inspection of all relevant files

---

## Summary

| Category | Count |
|---|---|
| **COMPLETED** | 16 |
| **PARTIALLY RESOLVED** | 5 |
| **STILL VALID** | 2 |
| **NO LONGER APPLICABLE** | 0 |
| **NEW ISSUES** | 3 |

---

## Section 2: Critical Issues (P0)

| ID | Original Issue | Status | Evidence | Priority | Notes |
|---|---|---|---|---|---|
| 2.1 | Model Performance — fundamentally flawed (bull_trending 48.78%, bear_trending Sharpe 10) | **COMPLETED** | [training.py](app/tasks/training.py#L746-L780) `_calculate_composite_score`: composite = 0.40*acc + 0.40*f1 + 0.20*class_balance. Ternary classification replaces regression. | — | Converted to classification with accuracy+F1+balance optimization. Old Sharpe metric no longer primary. |
| 2.2 | Feature mismatch (training: base 27 vs inference: legacy 25) | **COMPLETED** | Training: [training.py](app/tasks/training.py#L556) `feature_set="base"`. Inference: [trading_strategy_sync.py](app/services/trading_strategy_sync.py#L229) `feature_set="base"`. Both 27 features. | — | Unified on `base_feature_columns` (27 features). Legacy 25-feature set preserved for backward compat but unused in active pipeline. |
| 2.3 | Position sizing stuck at 1 share (`base_qty = 1`) | **COMPLETED** | [trading_strategy_sync.py](app/services/trading_strategy_sync.py#L557) `risk_manager.calculate_position_size()` with ATR-based sizing. Kelly Criterion at [L777](app/services/trading_strategy_sync.py#L777). | — | Dynamic sizing via ATR + portfolio risk + Kelly. `base_qty=1` eliminated. |
| 2.4 | Sentiment/Fundamentals impact near zero (double-scaling) | **COMPLETED** | [trading_strategy_sync.py](app/services/trading_strategy_sync.py#L387-L487) `_calculate_adjusted_confidence`: sentiment/fundamentals applied as confidence modifiers, not model features. Training uses `feature_set="base"` (no Phase F). | — | No double-scaling. Sentiment = ±10% max confidence modifier. Fundamentals = PE-ratio-based modifier. |
| 2.5 | Walk-Forward scaler look-ahead bias | **COMPLETED** | [training.py](app/tasks/training.py#L554-L560) `fit_scaler=True` on training fold only, `fit_scaler=False` on validation fold. Per-fold scaler fitting. | — | Fixed in Session 4. Scaler fit per-fold during TimeSeriesSplit. |

---

## Section 3: High Priority (P1)

| ID | Original Issue | Status | Evidence | Priority | Notes |
|---|---|---|---|---|---|
| 3.1 | Regime classification O(N) inefficiency | **COMPLETED** | [training.py](app/tasks/training.py#L211-L226) Pre-computes regime per SPY bar O(M), then maps via `pd.merge_asof` (vectorized). | — | Changed from O(N*M) per-sample detection to O(M) + vectorized merge. |
| 3.2 | PredictorService singleton anti-pattern (thread safety, atomic reload) | **COMPLETED** | [predictor.py](app/ml/predictor.py#L30) `threading.RLock`. [L162-L168](app/ml/predictor.py#L162-L168) `reload_models()` loads into new dict, atomically swaps under lock. | — | Full thread safety with RLock. Atomic model reload pattern. |
| 3.3 | RiskManager state in-memory only | **COMPLETED** | [risk_manager.py](app/services/risk_manager.py#L370-L395) Redis-first + in-memory fallback for cooldowns. [L464-L482](app/services/risk_manager.py#L464-L482) Redis persistence for entry times. | — | Redis + in-memory dual-write pattern with TTL. Survives restarts. |
| 3.4 | REGIME_STRATEGY_WEIGHTS dead code | **COMPLETED** | Defined: [regime.py](app/services/regime.py#L109-L130). Used: [trading_strategy_sync.py](app/services/trading_strategy_sync.py#L426) `REGIME_STRATEGY_WEIGHTS.get(regime_key, ...)`. | — | Actively used in `_calculate_adjusted_confidence` for regime-specific weight resolution. |
| 3.5 | Trailing stop not working | **COMPLETED** | [tasks/trading.py](app/tasks/trading.py#L75-L214) `update_trailing_stops` task: ATR-based trailing calculation, exit condition checks, position status updates. | — | Full implementation with ATR or fixed-percentage fallback, talib integration. |
| 3.6 | Backtest engine doesn't use current model | **PARTIALLY** | [backtest/ml_strategy.py](app/backtest/ml_strategy.py#L139) uses `predict_next()` (regression float) not `predict_class()`. Thresholds are float-based, not classification-based. | **P1** | Backtest still uses legacy regression interface. Needs conversion to ternary classification. |

---

## Section 4: Medium Priority (P2)

| ID | Original Issue | Status | Evidence | Priority | Notes |
|---|---|---|---|---|---|
| 4.1 | HTTP middleware double logging | **COMPLETED** | [main.py](app/main.py#L39-L68) Single `metrics_middleware` only. No duplicate logging middleware. Only CORS + rate limit added. | — | No double logging. Single middleware with Prometheus metrics. |
| 4.2 | Transaction isolation missing | **PARTIALLY** | [database.py](app/core/database.py#L41-L48) `SessionLocal` uses default isolation (PostgreSQL READ_COMMITTED). No explicit isolation level. [trading_strategy_sync.py](app/services/trading_strategy_sync.py#L525) uses Redis distributed lock. | **P2** | Distributed lock mitigates concurrent access but explicit DB isolation level not set. |
| 4.3 | Kelly Criterion mock data fallback | **PARTIALLY** | [portfolio_optimizer.py](app/services/portfolio_optimizer.py#L300-L375) Live→backtest fallback logic exists. [L375-L410](app/services/portfolio_optimizer.py#L375-L410) `_get_backtest_trades` uses SMA crossover simulation on real OHLCV data. | **P2** | Uses real OHLCV with simple strategy (not random), but still synthetic trades. Acceptable for bootstrapping. |
| 4.4 | Correlation matrix date alignment | **COMPLETED** | [portfolio_optimizer.py](app/services/portfolio_optimizer.py#L85-L95) Date-indexed Series: `pd.concat(..., axis=1).dropna()`. [L175](app/services/portfolio_optimizer.py#L175) Returns `pd.Series` with datetime index. | — | Proper date-based alignment using concat + dropna. |
| 4.5 | Optuna tunes Sharpe only | **COMPLETED** | [training.py](app/tasks/training.py#L746-L780) `_calculate_composite_score`: `0.40*accuracy + 0.40*f1_weighted + 0.20*class_balance`. All Optuna objectives use this composite. | — | Multi-metric composite score. No longer Sharpe-only. |

---

## Section 5: Low Priority (P3)

| ID | Original Issue | Status | Evidence | Priority | Notes |
|---|---|---|---|---|---|
| 5.1 | Dead code (strategies.py, regime.py weights) | **PARTIALLY** | REGIME_STRATEGY_WEIGHTS: now ACTIVE (trading_strategy_sync.py L426). But [strategies.py](app/services/strategies.py) (301 lines: Momentum, MeanReversion, Breakout, MLStrategy classes) has zero imports across the codebase. | **P3** | strategies.py is entirely dead code. Not imported anywhere. Should be archived or removed. |
| 5.2 | Hardcoded paths (/app/model_artifacts/) | **PARTIALLY** | [features.py L14](app/ml/features.py#L14): `os.getenv("MODEL_SAVE_PATH", ...)` ✓. [predictor.py L14](app/ml/predictor.py#L14): `os.getenv(...)` ✓. [training.py L36](app/tasks/training.py#L36): `MODEL_SAVE_PATH = "model_artifacts"` hardcoded ✗. | **P3** | 2/3 files use env var. training.py still hardcoded. |
| 5.3 | Celery error recovery | **COMPLETED** | [tasks/trading.py](app/tasks/trading.py#L9-L16) `max_retries=3`, `autoretry_for=(Exception,)`, `retry_backoff=60`, `retry_backoff_max=600`. `@notify_on_failure` decorator. | — | Retry + exponential backoff + Discord notification on failure. |
| 5.4 | Test coverage (~45%) | **STILL VALID** | 7 test files total. No tests for ternary classification, `predict_class`, regime-specific training, `_calculate_adjusted_confidence`, Kelly sizing, trailing stops. | **P2** | Critical gap. New classification system has zero test coverage. Should be elevated to P2. |

---

## Section 6: Investment Strategy Issues

| ID | Original Issue | Status | Evidence | Priority | Notes |
|---|---|---|---|---|---|
| 6.1 | Fundamental strategy problem (15m regression) | **COMPLETED** | Entire pipeline converted to daily bars: [trading_strategy_sync.py L186](app/services/trading_strategy_sync.py#L186) `timeframe='1d'`, training uses daily OHLCV. | — | 15-min bars eliminated. Daily timeframe throughout. |
| 6.2 | Strategy overhaul recommended (classification + daily) | **COMPLETED** | Ternary classification: [training.py L100-L107](app/tasks/training.py#L100-L107). `CLASSIFICATION_THRESHOLD = 0.003`. [predictor.py L170](app/ml/predictor.py#L170) `predict_class()`. Daily bars in all pipelines. | — | Exact recommendation implemented in Session 5. |
| 6.3 | Model architecture (regression→classification, SHAP, purged CV) | **PARTIALLY** | Regression→Classification: ✓ `EnsembleClassifierWrapper`. SHAP: ✗ not implemented. Purged CV: ✗ uses standard `TimeSeriesSplit` without purging gap. | **P2** | SHAP and purged CV remain unimplemented. |

---

## NEW ISSUES (Discovered from Daily Bar Conversion)

| ID | Issue | Evidence | Priority | Notes |
|---|---|---|---|---|
| N1 | Backtest engine uses `predict_next()` (regression) instead of `predict_class()` | [backtest/ml_strategy.py L139](app/backtest/ml_strategy.py#L139): `self.predictor.predict_next(scaled_features, regime=...)`. Uses float thresholds, not ternary classification. | **P1** | Backtest results will not match production behavior. Needs `predict_class` + classification logic. |
| N2 | `predictor.retrain()` still creates `EnsembleWrapper` (regression) | [predictor.py L298-L313](app/ml/predictor.py#L298-L313): `model = EnsembleWrapper(...)` not `EnsembleClassifierWrapper`. | **P2** | Calling `retrain()` would create a regression model, inconsistent with the classification pipeline. Should use `EnsembleClassifierWrapper`. |
| N3 | `portfolio_optimizer._get_backtest_trades` docstring references "15-min bars" | [portfolio_optimizer.py L362](app/services/portfolio_optimizer.py#L362): docstring says "SMA(5) vs SMA(20) crossover on 15-min bars". Code actually uses `timeframe='1d'` at L375. | **P3** | Stale docstring. Functional code is correct (uses daily bars). |

---

## Recommended Next Actions (Priority Order)

### P1 — Immediate
1. **Backtest engine alignment (3.6 + N1):** Convert `backtest/ml_strategy.py` from `predict_next()` to `predict_class()`. Implement ternary classification trading logic matching production.

### P2 — Short-term
2. **Test coverage (5.4):** Write tests for ternary classification, `predict_class`, confidence adjustment, Kelly sizing.
3. **Fix `predictor.retrain()` (N2):** Change `EnsembleWrapper` → `EnsembleClassifierWrapper` in retrain method.
4. **SHAP integration (6.3):** Add SHAP feature importance analysis post-training.
5. **Purged cross-validation (6.3):** Replace `TimeSeriesSplit` with purged CV (gap between train/val).

### P3 — Maintenance
6. **Remove dead code (5.1):** Archive or delete `strategies.py` (301 lines, zero imports).
7. **Fix hardcoded path (5.2):** Use `os.getenv("MODEL_SAVE_PATH", ...)` in `training.py`.
8. **Fix stale docstring (N3):** Update `_get_backtest_trades` docstring.
9. **Transaction isolation (4.2):** Consider explicit isolation level for trading-critical queries.
