# Task Report: Daily Bar + Ternary Classification Conversion
**Date**: 2026-02-24  
**Session**: 5  
**Status**: ✅ COMPLETED  
**Scope**: Cross-cutting refactor — 14+ files modified

---

## Summary

Full system conversion from 15-minute bars to daily bars, combined with a regression-to-classification transformation (VotingRegressor → VotingClassifier). The ML pipeline now outputs ternary predictions (UP / NEUTRAL / DOWN) with softmax confidence scores instead of continuous signal values. All data collection, training, prediction, and trading logic has been updated to operate on daily timeframes.

---

## Changes (15 Files)

### 1. `app/ml/models.py` (360 → 1001 lines)
- Added 5 classifier wrapper classes:
  - `CompatibleCatBoostClassifier` — pickle-safe CatBoost wrapper
  - `CatBoostClassifierWrapper` — training interface with class weights
  - `LGBMClassifierWrapper` — LightGBM classifier with class weights
  - `XGBoostClassifierWrapper` — XGBoost classifier with class weights
  - `EnsembleClassifierWrapper` — VotingClassifier(voting='soft') orchestrator
- Constants: `DEFAULT_CLASS_WEIGHTS = {0: 1.5, 1: 0.5, 2: 1.5}`, `CLASS_NAMES = ["DOWN", "NEUTRAL", "UP"]`
- All original regressor code preserved for backward compatibility

### 2. `app/tasks/training.py` (~1320 lines)
- Full classification conversion of training pipeline
- `CLASSIFICATION_THRESHOLD = 0.003` for ternary target generation
- Target: `np.where(returns > threshold, 2, np.where(returns < -threshold, 0, 1))`
- SPY timeframe changed to `'1d'`
- Minimum samples: 1000 → 300
- Accuracy-based ensemble weight calculation
- `EnsembleClassifierWrapper` training integration
- Classification metrics: `accuracy_score`, `f1_score` (macro)
- All composite scoring updated for classification

### 3. `app/ml/predictor.py` (~340 lines)
- New method: `predict_class()` → returns `(predicted_class, confidence, probabilities)`
- `_classifier_map` for new model filenames (`ensemble_classifier_{regime}.pkl`)
- `_load_models_from_disk` prefers classifiers, falls back to legacy regressors
- `_predict_regression` helper for backward compatibility
- `predict_next()` preserved unchanged

### 4. `app/services/regime.py` (130 lines)
- Thresholds updated for daily bars:
  - ADX: 18 → 25
  - ATR: 0.015 → 0.03
  - price_change: 0.005 → 0.02
- Comments updated to "일봉 기준" (daily bar basis)

### 5. `app/core/config.py` (134 lines)
- `REGIME_TRADING_CONFIG` redesigned:
  - Removed `buy_threshold` / `sell_threshold` (float signals)
  - Added `confidence_threshold` (0.40–0.60) per regime
  - Added `min_hold_days` (1–2) per regime

### 6. `app/services/risk_manager.py` (547 lines)
- `min_hold_bars`: 4 → 2
- `cooldown_bars`: 4 → 1
- `bars_per_cycle`: 15 → 1440
- `_entry_time_ttl`: 86400 → 604800 (1 week)

### 7. `app/repositories/stock_repo_sync.py` (181 lines)
- Default timeframe: `'15m'` → `'1d'` (both query methods)

### 8. `app/repositories/portfolio_repo.py` (276 lines)
- Default timeframe: `'15m'` → `'1d'`

### 9. `app/services/portfolio_optimizer.py` (448 lines)
- All 3 timeframe parameters: `'15m'` → `'1d'`

### 10. `app/worker.py` (157 lines)
- Celery Beat schedule overhauled:
  - Removed 15-minute collection task
  - Added `daily_ohlcv` (17:00 ET, post-market)
  - `market_scan` → once daily (10:00 ET)
  - `trailing_stops` → twice daily (10:00, 15:00 ET)
  - Task routing updated

### 11. `app/tasks/realtime_data.py` (158 lines)
- Complete rewrite: `collect_15m_realtime` → `collect_daily_ohlcv`
- `TimeFrame.Day` instead of `TimeFrame.Minute`
- Post-market single run instead of intraday polling
- All DB references: `'1d'`

### 12. `app/tasks/trading.py` (214 lines)
- Timeframe: `'15m'` → `'1d'`
- Time windows adjusted for daily granularity
- Comments updated

### 13. `app/tasks/market_analysis.py` (129 lines)
- Timeframe: `'15m'` → `'1d'`
- Minimum bars: 100 → 20
- Daily volume threshold: 100K → 1M
- Comments updated

### 14. `app/api/v1/endpoints/rag.py` (686 lines)
- SPY and symbol data timeframe: `'15m'` → `'1d'`
- Minimum bars: 500 → 50

### 15. `app/services/trading_strategy_sync.py` (1001 → 1055 lines)
- Major rewrite for classification:
  - SPY timeframe: `'1d'`
  - Cache TTL: 300 → 3600 seconds
  - Data window: 30 days → 365 days
  - Minimum bars: 500 → 50
  - Uses `predict_class()` instead of `predict_next()`
  - `_execute_trade_logic` redesigned for class/confidence/probabilities
  - `_calculate_adjusted_confidence` replaces `_calculate_adjusted_signal`
  - `process_portfolio` updated
  - `_select_uncorrelated_symbols` uses confidence
  - `_process_sell_signal` uses class-based signals

---

## QA Results: ✅ PASS

| Check | Result |
|-------|--------|
| Zero `timeframe='15m'` in `app/` | ✅ Verified |
| Zero `15분봉` references (except clock-time) | ✅ Verified |
| Classifier backward compatibility (regressor fallback) | ✅ Verified |
| Config redesigned for confidence thresholds | ✅ Verified |
| No orphaned imports or dead code | ✅ Verified |
| Type hints on all new public methods | ✅ Verified |

---

## Impact

### Architecture
- **Prediction Model**: Regression (continuous signal) → Classification (discrete class + confidence)
- **Data Granularity**: 15-minute intraday → Daily bars (post-market)
- **Trading Frequency**: ~26 decisions/day → 1 decision/day per symbol
- **Noise Reduction**: Daily bars eliminate intraday noise that degraded model accuracy

### Performance (Expected)
- **Signal Quality**: Classification with confidence thresholds produces cleaner, more actionable signals
- **Transaction Costs**: Significantly reduced (1 trade/day vs potential 26/day)
- **Model Training**: Faster convergence with cleaner daily targets (300 samples sufficient vs 1000)
- **Operational Simplicity**: Single daily collection task replaces complex 15-minute scheduling

### Risk Management
- **Holding Period**: 2 trading days minimum (was 60 minutes)
- **Cooldown**: 1 trading day (was 60 minutes)
- **Position Sizing**: Confidence-based (40–60% threshold) replaces signal-magnitude-based

---

## Dependencies for Next Steps
1. **Daily Bar Data Backfill** — Run `scripts/backfill_ohlcv.py` with `timeframe='1d'` for 50–100 symbols
2. **Model Retraining** — Train new classifier models using `EnsembleClassifierWrapper`
3. **Dual-Timeframe Orchestrator** — Daily ML direction + 15min entry timing (future phase)
