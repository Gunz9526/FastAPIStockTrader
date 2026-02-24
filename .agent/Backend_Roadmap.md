# Backend Roadmap 🚀

> **Last Updated**: 2026-02-25 (Session 10) | **Current Phase**: 🟡 Phase J (J.1/J.2 ✅, J.2.1 ✅, J.3.1 ✅, J.3.2 Model Diagnostics ✅, J.3 pending re-execution)
> **Stack**: Python 3.14 · FastAPI · PostgreSQL/TimescaleDB · Redis · Celery · CatBoost+LightGBM+XGBoost
> **Server**: 4-core CPU, 24GB RAM, no GPU
> **Classification**: Ternary (UP=2 / NEUTRAL=1 / DOWN=0), θ=0.005, Daily bars (1d), 27 features

---

## ✅ Completed Phases (A–I Summary)

| Phase | Name | Key Deliverables | Completed |
|-------|------|-----------------|-----------|
| A | Core Trading System | FastAPI + PostgreSQL/TimescaleDB + Alpaca API + TA-Lib | 2025 |
| B | Reliability & Automation | Celery/Redis task queue, Beat scheduler, Docker security | 2025 |
| C | Performance Optimization | TimescaleDB hypertables, Redis caching, Prometheus metrics | 2025 |
| D | ML Core | Ensemble (CatBoost+LGBM+XGB), Optuna tuning, Backtrader engine | 2025 |
| CI/CD | Build Pipeline | Conda env, GitHub Actions 3min build, Docker multi-stage, Python 3.14 | 2026-01-22 |
| E | Production Hardening | Basic circuit breaker, Redis persistence, test infra (44 tests, 58%) | Partial |
| F | Advanced AI | Sentiment (Gemini+Finnhub), Fundamentals (yfinance), VIX regime, Monte Carlo | 2026-01-05 |
| G | Daily Bar + Classification | 14+ file daily conversion, VotingClassifier(soft), confidence_threshold per regime | 2026-02-24 |
| H | Market Regime Awareness | 4-regime detection (SPY), regime-specific classifiers, bull fallback, 27 features | 2026-02-24 |
| I | Risk & Position Defense | Min hold 2d, cooldown 1d, Kelly sizing, MPT optimization, 5-position portfolio | 2026-01-05 |

---

## 📊 Session History

| Session | Date | Summary |
|---------|------|---------|
| 1 | 2026-01-05 | Core infrastructure: Sentiment(Gemini+Finnhub), Fundamentals(yfinance), VIX, Feature Engineering, Monte Carlo |
| 2 | 2026-01-06 | Gemini SDK migration, Feature pipeline fix, SQL logging, Sector lookup |
| 3 | 2026-01-19 | 8 audit fixes: Redis persistence, Optuna multi-objective, Trailing stops, Backtest regime, Kelly+Correlation fixes |
| 4 | 2026-02-23 | 7 audit fixes: Signal normalization, Thread safety, Transaction isolation, Hardcoded paths, Celery retry, Strategy analysis |
| 5 | 2026-02-24 | Daily bar conversion (14+ files), Ternary Classification (models.py/training.py/predictor.py/config.py/trading_strategy_sync.py) |
| 6 | 2026-02-24 | Classification cleanup: Backtest ml_strategy.py, RAG endpoint, predictor.retrain(), strategies.py, Rule personas, Roadmap overhaul |
| 7 | 2026-02-24 | Phase J prep: 60-symbol GICS expansion, backfill_ohlcv daily support, sector_map 17→62 symbols (Unknown 99→12), Native categorical encoding (CatBoost/LightGBM/XGBoost + Ensemble), symbol_limit 10→None |
| 8 | 2026-02-24 | **8 critical bug fixes**: LightGBM categorical predict mismatch, feature_set legacy→base, CatBoost empty data cascade, regime params JSON structure, single scaler overwrite→regime-specific, tuning wrong feature_set, regime param files never loaded, relative_volume always 1.0. + Inference path scaler-regime alignment (6 files, 0 errors) |
| 9 | 2026-02-24 | **Training result analysis + 4 fixes**: Data Leakage removal (holdout validation), NEUTRAL class recovery (CLASS_WEIGHTS 0.5→1.0), θ adjustment (0.003→0.005), min_samples 300→500. Dead code cleanup (6 unused imports removed) |
| 10 | 2026-02-25 | **Model diagnostics + sentiment optimization**: Regime-specific CLASS_WEIGHTS (bear NEUTRAL 1.5×), training report overhaul (Sharpe→NEUTRAL_R/F1/classification_report), feature importance top-10 logging, sentiment schedule 7×→2×/day |

---

## 🔍 Audit Status

### Resolved: 19/19 P0 + 16/19 Total Original Items ✅

| ID | Issue | Resolution | Session |
|----|-------|-----------|---------|
| P0-2.1 | Model performance | Daily + Ternary Classification | 5 |
| P0-2.2 | Feature mismatch | base_feature_columns 27=27 (training=inference) | 5 |
| P0-2.3 | Position sizing | Dynamic Kelly / confidence-based | 3+5 |
| P0-2.4 | Signal normalization | Classification replaces signal weighting | 4+5 |
| P0-2.5 | Scaler look-ahead bias | Per-fold scaling in WF validation | 5 |
| P1-3.1 | Regime O(N) | Vectorized | 3 |
| P1-3.2 | Thread safety | RLock + atomic reload | 4 |
| P1-3.3 | RiskManager persistence | Redis | 3 |
| P1-3.4 | Dead code REGIME_STRATEGY_WEIGHTS | Kept for Dual-Timeframe future use | 4 |
| P1-3.5 | Trailing stops | Full ATR-based implementation | 3 |
| P1-3.6 | Backtest alignment | predict_class() + confidence | 6 |
| P2-4.1 | Double logging | Removed | 3 |
| P2-4.2 | Transaction isolation | Distributed locks on all order paths | 4 |
| P2-4.3 | Kelly mock data | SMA crossover strategy | 3 |
| P2-4.4 | Correlation alignment | Date-indexed | 3 |
| P2-4.5 | Optuna multi-objective | Composite score (Sharpe+Accuracy+MaxDD) | 3 |
| P3-5.2 | Hardcoded paths | env var (`MODEL_SAVE_PATH`) | 4 |
| P3-5.3 | Celery retry | autoretry on 5 tasks | 4 |

### Remaining (3 items)

| ID | Issue | Target | Priority |
|----|-------|--------|----------|
| P2 | Test coverage 58% | 70%+ | Medium (Phase K.2) |
| P3 | DB index optimization | Partial indexes | Low (Phase N) |
| P3 | Dead code review | Remove truly unused code | Low (Phase N) |

---

## 🔜 Phase J: Data Backfill & Model Training (NEXT IMMEDIATE)

> **Prerequisite**: No models exist yet for daily classification. Must complete before anything else.

### J.1 Daily OHLCV Backfill ✅ (Session 7)
- [x] Update `scripts/backfill_ohlcv.py` to support `timeframe='1d'` (default)
- [x] Added `--timeframe` CLI arg: `1d` | `15m` | `1h`
- [x] Expand symbol universe: 10 → 60 symbols (11 GICS sectors + 2 Market Index ETFs)
- [x] Target: 2+ years daily data per symbol (≈500 bars each)
- [x] Sector diversification: all 11 GICS sectors represented
- [x] `verify_backfill()` efficient SQL COUNT/MIN/MAX query
- **Files**: `scripts/backfill_ohlcv.py`, `scripts/add_symbols.py`
- **Run**: `python scripts/add_symbols.py` → `python scripts/backfill_ohlcv.py --years 2 --timeframe 1d`

### J.2 Symbol Universe Expansion ✅ (Session 7)
- [x] 60 symbols: Tech(12), CommSvc(4), ConsCycl(6), ConsDef(5), Fin(6), Health(6), Energy(4), Ind(6), BasMat(3), RE(3), Util(3), MktIdx(2)
- [x] `sector_map.py`: 17→62 entries, SECTOR_TO_ID contiguous 0–12, `NUM_SECTORS=13`
- [x] GOOGL/META → Communication Services, AMZN → Consumer Cyclical (GICS-correct)
- [x] **Native categorical encoding**: CatBoost (Ordered Target Stats), LightGBM (native categorical), XGBoost (enable_categorical)
- [x] Ensemble `_train_with_categorical()`: per-estimator training with correct dtype/params
- [x] `training.py` symbol_limit: 10 → None (use all active symbols)
- **Files**: `scripts/add_symbols.py`, `app/ml/sector_map.py`, `app/ml/features.py`, `app/ml/models.py`, `app/tasks/training.py`

### J.2.1 Training Pipeline Bug Fixes ✅ (Session 8)
- [x] **Bug 1 (CRITICAL)**: LightGBM categorical_feature mismatch — added `_prepare_categorical_for_predict()` to all predict/predict_proba methods
- [x] **Bug 2 (CRITICAL)**: feature_set "legacy" (25 features) → "base" (27 features) in validation and tuning paths
- [x] **Bug 3 (CRITICAL)**: CatBoost empty data cascade — fixed by resolving Bug 2
- [x] **Bug 4 (CRITICAL)**: Regime-tuned params JSON structure mismatch — `_load_regime_params()` with 5-level fallback
- [x] **Bug 5 (HIGH)**: Single `feature_scaler.pkl` overwritten by all regimes — `scaler_suffix` parameter for regime-specific scalers
- [x] **Bug 6 (HIGH)**: Tuning used wrong feature_set — added `feature_set="base"` to `tune_models()` and `_tune_models_global()`
- [x] **Bug 7 (HIGH)**: Regime-specific param files never loaded — merged into Bug 4 fix
- [x] **Bug 8 (MEDIUM)**: `relative_volume` always 1.0 during training — conditional overwrite only when not pre-computed
- [x] **Inference path fix**: `scaler_suffix` added to ALL 4 inference callers (trading_strategy_sync ×2, backtest, RAG endpoint)
- [x] **Scaler-model alignment**: Fallback regime resolved BEFORE scaling so scaler matches model's regime
- **Files**: `app/ml/models.py`, `app/ml/features.py`, `app/tasks/training.py`, `app/services/trading_strategy_sync.py`, `app/backtest/ml_strategy.py`, `app/api/v1/endpoints/rag.py`

### J.3.1 Validation Integrity & NEUTRAL Recovery ✅ (Session 9)
- [x] **Data Leakage removed**: Holdout 20% split BEFORE training, true OOS validation metrics
- [x] **Production model**: Still trained on 100% data after validation, best of both worlds
- [x] **NEUTRAL class recovered**: CLASS_WEIGHTS {0:1.5, 1:0.5, 2:1.5} → {0:1.3, 1:1.0, 2:1.3}
- [x] **θ widened**: CLASSIFICATION_THRESHOLD 0.003 → 0.005 (±0.5% daily, reduces noise trades)
- [x] **min_samples raised**: 300 → 500 (sideways_volatile falls back to sideways_calm)
- [x] **Dead code cleanup**: Removed 6 unused imports (log_loss, PredictorService, discord_notifier, CatBoostWrapper, LGBMWrapper, XGBoostWrapper)
- **Files**: `app/tasks/training.py`, `app/ml/models.py`
- **Expected next training results**: accuracy ~45-55% (honest OOS), NEUTRAL predictions ~15-25%

### J.3.2 Model Diagnostics & Regime Weight Tuning ✅ (Session 10)
- [x] **Sentiment schedule optimized**: 7×/day → 2×/day (8AM, 12PM EST), saves Finnhub+Gemini API quota
- [x] **Training report overhaul**: Removed vestigial Sharpe column → Added F1, NEUTRAL_Recall, classification_report, feature importance top-10
- [x] **Regime-specific CLASS_WEIGHTS**: bull{0:1.3,1:1.2,2:1.0}, bear{0:1.0,1:1.5,2:1.3}, sideways{0:1.2,1:1.3,2:1.0}
- [x] **Per-class metrics**: sklearn classification_report logged per regime after holdout validation
- [x] **Feature importance**: Production ensemble top-10 features logged + saved in report
- **Files**: `app/tasks/training.py`, `app/worker.py`

### J.3 Initial Model Training (Est: 1 day)
- [ ] Run `train_models` Celery task with daily data (after J.3.2 fixes)
- [ ] Generate `ensemble_classifier_{regime}.pkl` (4 files)
- [ ] Validate: accuracy ≥ 45% OOS, F1 weighted ≥ 0.40 per regime, NEUTRAL recall ≥ 15%
- [ ] Log class distribution (check for imbalance: DOWN/NEUTRAL/UP ratio)
- [ ] CPU estimate: ~2–4 hours per training run on 4-core server
- **Files**: `app/tasks/training.py`, `model_artifacts/`

---

## 📋 Phase K: Production Hardening (After J)

### K.1 Circuit Breaker Enhancement (Est: 2–3 days)
- [ ] Daily loss limit: -3% or -$500 (whichever first)
- [ ] Consecutive loss limit: 3 in 1 day → pause trading
- [ ] API latency monitoring: >3000ms → halt
- [ ] Discord/Slack alerting on breaker triggers
- **File**: `app/services/circuit_breaker.py`

### K.2 Test Coverage → 70% (Est: 3–4 days)
- [ ] Priority targets: `predictor.py` (predict_class), `features.py`, `portfolio_optimizer.py`
- [ ] Add classification-specific tests: class weights, confidence scores, feature count 27
- [ ] Update `test_training_integration.py` for classifier pipeline
- [ ] Current: 44 tests (~58%) → Target: ~65 tests (70%+)

---

## 📋 Phase L: Dual-Timeframe Hybrid (Mid-term, after K)

> **Dependency**: Phase J (models trained) + Phase K (production-safe)

### L.1 Daily ML Signal Cache (Est: 1–2 days)
- [ ] Redis cache: daily prediction (class + confidence + probs), 24h TTL
- [ ] Celery task: `generate_daily_signals` at 17:30 ET (after data collection)

### L.2 15min Rule-Based Entry Layer (Est: 5–7 days)
- [ ] Entry rules: RSI < 35 + MACD cross-up (when daily signal = UP)
- [ ] Exit rules: Trailing stop or immediate (when daily signal = DOWN)
- [ ] New class: `DualTimeframeOrchestrator`
- [ ] Requires: 15min data collection re-enabled (market hours only)

### L.3 Backtesting Validation (Est: 3–4 days)
- [ ] Dual-timeframe backtest engine
- [ ] Compare: Daily-only vs Hybrid performance
- [ ] Transaction cost sensitivity analysis

---

## 📋 Phase M: Advanced ML (Long-term)

### M.1 Cross-Sectional Momentum (Est: 5–7 days)
- [ ] Relative strength ranking across 50–100 symbols
- [ ] Sector rotation signals
- [ ] Top-N% stock selection

### M.2 SHAP Feature Selection (Est: 2–3 days)
- [ ] Remove noisy features based on SHAP values
- [ ] Regime-specific feature importance

### M.3 Adaptive Thresholds (Est: 3–4 days)
- [ ] Optuna auto-tune CLASSIFICATION_THRESHOLD (θ) per regime
- [ ] Dynamic confidence_threshold adjustment

---

## 📋 Phase N: Infrastructure & DevOps (Ongoing)

| Task | Description | Priority |
|------|-------------|----------|
| N.1 MLflow | Model registry, versioning, A/B testing | Medium |
| N.2 Grafana | Dashboard: Sharpe, drawdown, win rate, latency | Medium |
| N.3 PostgreSQL HA | Primary-Replica replication | Low |
| N.4 mypy strict | 80% → 100% type coverage | Low |
| N.5 DB Indexes | Partial index optimization (VWAP, composite) | Low |
| N.6 Dead Code | Remove unused legacy code, update Swagger | Low |

---

## 🔧 Technical Debt

| Area | Current | Target | Notes |
|------|---------|--------|-------|
| Test Coverage | ~58% (44 tests) | 70%+ (~65 tests) | Phase K.2 |
| mypy | 80% | 100% strict | Phase N.4 |
| Dead Code | `predict_next()` in predictor.py | Remove after L.2 | Legacy, preserved for backward compat |
| DB Indexes | Basic | Partial indexes | Phase N.5 |
| Swagger Docs | Partial | Full classification API | Phase N.6 |
| strategies.py | Rule-based personas | Integrate in L.2 | Momentum/MeanReversion/Breakout |

---

## 📐 System Architecture (Current)

```
┌─────────────────────────────────────────────────────┐
│                    FastAPI (main.py)                 │
│  ┌─────────┐  ┌──────────┐  ┌────────────────────┐ │
│  │ RAG API │  │ Trade API │  │ Portfolio API       │ │
│  └────┬────┘  └────┬─────┘  └────────┬───────────┘ │
│       │            │                  │             │
│  ┌────▼────────────▼──────────────────▼───────────┐ │
│  │         trading_strategy_sync.py                │ │
│  │  predict_class() → (class, confidence, probs)   │ │
│  └──────────┬──────────────────┬──────────────────┘ │
│       ┌─────▼─────┐    ┌──────▼──────┐             │
│       │ Predictor  │    │ RiskManager │             │
│       │ (4 regime  │    │ (Kelly/MPT  │             │
│       │ classifiers│    │  VaR/ATR)   │             │
│       └─────┬─────┘    └─────────────┘             │
│       ┌─────▼─────┐                                │
│       │ ML Models  │ ensemble_classifier_{regime}.pkl│
│       │ CatBoost   │ VotingClassifier(soft)         │
│       │ LightGBM   │ 27 features, 3 classes         │
│       │ XGBoost    │ θ=0.003                        │
│       │ sector_id  │ Native categorical (not ordinal)│
│       └───────────┘                                │
├─────────────────────────────────────────────────────┤
│  Celery Workers: daily_ohlcv, train_models,         │
│  market_scan, trailing_stops, sentiment, rebalance  │
├─────────────────────────────────────────────────────┤
│  PostgreSQL/TimescaleDB │ Redis │ Alpaca API        │
└─────────────────────────────────────────────────────┘
```
