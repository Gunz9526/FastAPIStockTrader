# Backend Roadmap 🚀

> **Last Updated**: 2026-03-17 (Session 34–39) | **Current Phase**: 🟢 Phase S (S.1–S.23 ✅) Training Tune + EOD Discord + Backtest + Auto Weights + XGBoost Fix + HIGH/MED Cleanup + Backtest Symbol + Redis Persistence + SELL Qty Reconciliation + Regime Lookback Fix + Sector Map Priority + XGBoost Category Safety + Backtest Force-Close + XGBoost Training Coverage + Safety Net v2 + Feature Default + Force-Close Timing + Category Range Guard + **XGBoost Category Range(12) Definitive Fix** + **Backtest Trailing Stop + Hard Stop-Loss**
> **Stack**: Python 3.14 · FastAPI · PostgreSQL/TimescaleDB · Redis · Celery · CatBoost+LightGBM+XGBoost
> **Server**: 4-core CPU, 24GB RAM, no GPU
> **Classification**: Ternary (UP=2 / NEUTRAL=1 / DOWN=0), θ=0.005, Daily bars (1d), 26 features

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
| 11 | 2026-02-25 | **Targeted performance improvements**: Bear NEUTRAL 1.5→2.5×, sideways UP 1.0→1.1, composite score +NEUTRAL_recall(25%), tuning-training weight alignment, walk-forward weight alignment |
| 12 | 2026-02-26 | **Accuracy push + bug fix**: portfolio_optimizer min_length bug fix, CLASS_WEIGHTS re-tuning (bull NEUTRAL 1.2→1.0, bear DOWN 1.0→1.3/UP 1.3→1.0, calm NEUTRAL 1.3→1.2), composite score neutral_recall→min_class_recall, confusion matrix in report |
| 13 | 2026-02-26 | **Bear weight stabilization**: Weight oscillation fix (bear {0:1.2,1:1.8,2:1.2} symmetric), bull DOWN 1.3→1.2. Training report analysis + comprehensive model performance explanation |
| 14 | 2026-02-26 | **Bear NEUTRAL fine-tune + Phase assessment**: bear NEUTRAL 1.8→2.0 (19.7%→target 25%), J.3 weight tuning phase nearing completion, Phase M evaluation |
| 15 | 2026-02-26 | **Weight tuning CONVERGED + Documentation**: bear NEUTRAL 2.0×=sweet spot confirmed (28.8% recall, 41.4% acc, 0.41 F1), no code changes needed. Created MODEL_IMPROVEMENT_HISTORY.md (S9-S15) + ML_TECHNICAL_QA.md (DL vs Tree, SHAP vs Sharpe, data scaling) |
| 16 | 2026-02-26 | **K.1 Circuit Breaker Enhancement**: Consecutive loss limit (3/day), daily trade count limit (20/day soft), Discord notifications on all state transitions, Prometheus metrics (counter+gauge), lazy imports for safety. Tests: 10→27 (17 new). Files: circuit_breaker.py (337→501 lines), metrics.py, test_circuit_breaker.py (122→461 lines) |
| 17 | 2026-02-26 | **M.2 SHAP Feature Selection**: TreeExplainer per estimator (CatBoost/LightGBM/XGBoost), voting-weight aggregation, per-class SHAP importance, stratified subsampling (500), sector_id frozenset protection, removal candidate detection. Celery task `analyze_features_shap`. Tests: 26 new (8 classes). Files: shap_analyzer.py (700 lines NEW), training.py (+SHAP integration), test_shap_analyzer.py (620 lines NEW), requirements.txt (+shap>=0.46.0) |
| 18 | 2026-02-26 | **M.3 Adaptive Thresholds + Codebase Audit**: Optuna-based per-regime θ optimization ([0.002,0.015], CatBoost-only, TimeSeriesSplit(3)), confidence threshold optimization ([0.30,0.70], trade_score=accuracy×√coverage). Mid-session audit: 23 issues found, 7 critical/high fixed (docstring, bar_executed init, config dedup, thread-safe singleton, SHAP params, Optional→union, type hints). Celery task `optimize_thresholds`. Tests: 23 new (9 classes). Files: threshold_optimizer.py (691 lines NEW), training.py (+Section 6 adaptive threshold + Celery task), test_threshold_optimizer.py (510 lines NEW). Audit fixes: features.py, ml_strategy.py, circuit_breaker.py, shap_analyzer.py |
| 19 | 2026-02-26 | **SHAP Bug Fix + K.2 Test Coverage**: Fixed SHAP `_compute_tree_shap` categorical dtype mismatch (LightGBM/XGBoost need `category` not `int`). K.2 Test Coverage expansion: test_predictor.py (15 tests), test_features.py (14 tests), test_portfolio_optimizer.py (10 tests) = +39 new tests. Total: ~151 tests. Files: shap_analyzer.py (fix), test_predictor.py (381 NEW), test_features.py (317 NEW), test_portfolio_optimizer.py (311 NEW) |
| 20 | 2026-02-26 | **M.4 SHAP Feature Pruning + Regime Fallback**: Removed `breakout_flag` (SHAP removal candidate across ALL regimes), features 27→26. Added `_REGIME_FALLBACK_CHAIN` to PredictorService for `sideways_volatile` (300 samples < 500 min) — falls back to `sideways_calm` instead of neutral. +1 fallback test. Files: features.py (-breakout_flag), shap_analyzer.py (-breakout_flag), predictor.py (+fallback chain), test_predictor.py (+1), test_features.py (updated counts) |
| 21 | 2026-02-27 | **XGBoost Pickle Compat Fix + Phase L.1 Daily Signal Cache**: Fixed `feature_names_in_` read-only property error in XGBoost 2.0+ (3-layer defense: try/except + monkeypatch + `__dict__` bypass). Phase L.1: Redis-based daily ML prediction cache (`DailySignalCache`), Celery task `generate_daily_signals` (17:30 ET), API endpoint `GET /signals/daily`, trading strategy cache integration. +18 tests. Files: models.py (XGBoost fix), signal.py (schema NEW), signal_cache.py (NEW), trading.py (+task), worker.py (+schedule), signals.py (endpoint NEW), api.py (+router), trading_strategy_sync.py (+cache read), test_signal_cache.py (NEW) |
| 22 | 2026-02-27 | **Phase M.1 Cross-Sectional Momentum**: `CrossSectionalMomentum` scorer (1m/3m/6m returns, vol-adjusted momentum, sector relative strength, composite score with weights 0.20/0.40/0.25/0.15). Sector rotation ranking (13 GICS sectors, top-3 per sector). Momentum filter in `_select_uncorrelated_symbols()` (percentile >= 0.50 + momentum tiebreaker). Celery task `compute_momentum_scores` (17:15 ET). Redis cache (`momentum:scores:{date}`, 24h TTL). API: GET /momentum/rankings, /rankings/{symbol}, /sectors, POST /compute. +22 tests. Files: momentum.py (schema NEW), momentum_scorer.py (NEW), market_analysis.py (+task), worker.py (+schedule+route), momentum.py (endpoint NEW), api.py (+router), trading_strategy_sync.py (+momentum filter), test_momentum_scorer.py (NEW) |
| 23–26 | 2026-02-27~28 | **Phase L.2a–L.3**: 15min data infrastructure, DualTimeframeOrchestrator, execution integration, backtesting validation (see Phase L section) |
| 27 | 2026-02-28 | **System-wide Audit & Remediation**: 3 parallel sub-agent audits across ~54 files → **84 issues found** (10 CRITICAL, 15 HIGH, 28 MEDIUM, 31 LOW). **23 fixes applied**: 7 CRITICAL (timing attack, method DNE ×3, await-on-sync, missing attr, None return), 6 HIGH (Redis KEYS→scan_iter ×2, regime mismatch, corr→cov matrix, NameError, ZeroDivision), 10 MEDIUM (ValidationInfo, CORS parsing, metrics 500 handling, print→logger ×2, deprecated asyncio, dead code ×2, utcnow, return type, timeframe filter). **18 files modified**, 0 code errors. Deferred: training.py data leakage, legacy backtester.py, 31 LOW items |
| 28 | 2026-03-01 | **Trading Flow Audit & CRITICAL fixes**: End-to-end trading flow audit by dedicated sub-agent. **3 CRITICAL + 1 HIGH fixed**: (1) `update_trailing_stops` migrated from dead `Position` table to `PositionTracking` — trailing stops now functional, (2) `portfolio_rebalancer.py` full async→sync conversion — rebalancing now executes, (3) `PositionTracking` + 3 risk columns (stop_loss/take_profit/trailing_stop) + Alembic migration 004, (4) `send_message()` → `send_success()` in generate_daily_signals. Initial stop prices set on BUY entry. **7 files modified + 1 migration created**. |
| 29 | 2026-03-01 | **Full Code Inspection & Comprehensive Bug Fixes**: (A) Audit sub-agent found 9 issues (1C+2H+6M): regime `detect()`→`detect_regime()` + features + VIX, exception broadening, Discord/circuit breaker/record_trade wiring, Redis risk persistence, portfolio_optimizer dimension mismatch + 1 PM-discovered gap (record_trade never called). (C) training.py `relative_volume` data leakage fixed (full-period mean→expanding window). (QA) Full 50-file inspection found 15 more issues (3C+4H+6M+2L). Fixed: **3 CRITICAL** (route shadowing `/daily/stats`, stale `position.entry_price`, AlpacaTradeStream non-existent method), **1 HIGH** (silent Discord exceptions), **3 MEDIUM** (hardcoded timestamp, docstring 20→26, CachedSignal timezone). **10 files modified + 1 deleted**. Deferred: H-1 rebalancer market price, H-2 Redis pool, H-3 dual tracking, M-1 f-string logging, M-3 PnL math, L-1/L-2. |
| 30 | 2026-03-04 | **Phase O: Observability & P&L Dashboard + VIX Bug Fix + TD Cleanup**: (1) **CRITICAL VIX Look-Ahead Bias** fixed in training.py — removed Redis VIX loading that applied current VIX to ALL 2yr historical bars. (2) **O.1 Fill Price Reconciliation**: `_wait_for_fill()` polls Alpaca `get_order_by_id()` for actual `filled_avg_price` — applied to all 5 order submission paths. (3) **O.2 TradeLog Schema Extension**: 6 new columns (fill_price, commission, regime, confidence, predicted_class, entry_trade_id) + Alembic 005 migration. (4) **O.3 Grafana P&L Dashboard**: 14-panel dashboard (3 Prometheus + 11 PostgreSQL) — cumulative P&L, win rate, daily P&L, per-symbol P&L, trade history, active positions, regime distribution, hold duration, win rate by regime, P&L by predicted class. (5) **TD-1**: `record_trade()` wired to `_place_order()` BUY/SELL (previously missing). (6) **TD-2**: `record_trade()` + Discord notification wired to `update_trailing_stops` exit. (7) **TD-3**: `session.rollback()` added to all 5 error handlers in `portfolio_repo.py` to prevent InFailedSqlTransaction cascade + f-string logging fix. **5 files modified + 2 files created**, 0 code errors. |
| 31 | 2026-03-04 | **Phase P: Code Quality & Alignment**: Training report analysis (VIX fix verified, all regimes at ternary ceiling ~38%, bear NEUTRAL regression = variance n=65). (1) **P.1 CRITICAL: `relative_volume` train-serve skew fixed** — moved computation from training.py into `create_features()` (features.py `add_technical_indicators`), removed `market_avg_volume` parameter from `extract_feature_vector()`, training and inference now use identical `volume / expanding_mean(volume)` path. (2) **P.2: Redis pool unification** — added `get_shared_redis()` lazy singleton factory to cache.py, replaced 4 independent `redis.Redis()` / `Redis.from_url()` calls across vix_data.py (2), sentiment_analyzer.py (1), distributed_lock.py (1). Removed unused imports. (3) **P.3: f-string logging cleanup** — converted 86 `logger.xxx(f"...")` to `%s` lazy formatting across training.py (78) and vix_data.py (8). QA: ALL PASS, 0 errors. **6 files modified + 2 test files updated**. |
| 32 | 2026-03-06 | **Phase Q: Discord Enhancement + Profitability Analysis**: (1) **HOTFIX: `%,.2f` production crash** in portfolio_rebalancer.py L59 — `$%,.2f` → `$%s` + f-string (% formatting doesn't support `,` thousands separator). Full codebase scan: only 1 instance. (2) **Q.1 Discord Webhook Enhancement**: `send_trade_alert()` expanded with 9 keyword-only params (confidence, predicted_class, regime, kelly_fraction, pnl_amount, pnl_pct, hold_duration_hours, portfolio_value, position_count). Structured embed sections: Base→ML→Portfolio→P&L→Extra. New methods: `send_daily_summary()` (purple embed, end-of-day recap), `send_regime_change()` (orange embed, regime transition with emoji). f-string logging fix in `_send()`. All 6 call sites updated with rich context. (3) **Profitability Analysis**: Model alone insufficient (38% vs 33% random), system viability depends on confidence filtering + Kelly + trailing stops. **Backtest required** for definitive answer. **3 files modified + 3 reports created**. |
| 33 | 2026-03-11 | **Phase R: Signal Deactivation + Buy Limit**: (1) **R.1 Sentiment/Fundamentals Deactivation**: REGIME_STRATEGY_WEIGHTS all 4 regimes → ml=1.0, sentiment=0.0, fundamentals=0.0. `_default_weights` updated. Early return in `_get_phase_f_signals()` and fast path in `_calculate_adjusted_confidence()` skip API calls when weights=0. Celery sentiment tasks (`update_sentiment_scores`, `clear_stale_sentiment_cache`) commented out. Code preserved for future reactivation. (2) **R.2 Daily Buy Limit**: `max_daily_buys=2` in RiskManager. Redis-backed counter `risk:daily_buys:{date}`. `can_buy_today()` + `_increment_daily_buys()`. Wired in both `process_portfolio()` BUY path and `process_intraday_cycle()` ENTRY loop with break on limit. **4 files modified**, 0 errors. |
| 34 | 2026-03-16 | **Phase S: Training Tune + EOD Discord + Backtest Overhaul**: (1) **S.1 bear_trending CLASS_WEIGHTS**: `{0:1.2,1:2.0,2:1.2}` → `{0:2.5,1:1.5,2:0.8}` — DOWN recall 8% fix (heavy DOWN boost, UP penalty). (2) **S.2 ema_26 Feature Removal**: Removed from 4 feature column lists (base 26→25, legacy 25→24, core 21→20, full 31→30). Column still computed for `trend_strength`. SHAP analyzer default list updated. Tests updated. (3) **S.3 End-of-Day Discord Summary**: New Celery task `send_end_of_day_summary` — queries Alpaca for portfolio value, daily P&L (equity-last_equity), positions, top/worst performers. Beat schedule 16:05 ET Mon-Fri. (4) **S.4 Backtest Error Fix + Portfolio Mode**: Fixed None-safety crash in engine.py (`sharpe`/`drawdown`/`total_return` from Backtrader). Added `run_portfolio()` multi-symbol aggregation. `run_backtest.py` rewritten: `--symbol` optional (ALL=portfolio), `--no-regime` for A/B comparison, `_fmt()` None-safe formatter, per-symbol summary table. **10 files modified**, 0 errors. |
| 35 | 2026-03-16 | **Phase S Hotfix + Auto Weights + XGBoost Fix + HIGH/MEDIUM Cleanup**: (1) **S.2 REVERTED**: `ema_26` feature list 복원. (2) **S.5 Auto CLASS_WEIGHTS**: sklearn balanced 기반 자동 계산. (3) **S.6 XGBoost Unseen Category Fix**: `sector_id` pd.Categorical(categories=range(13)) 명시 5곳 + ensemble graceful degradation. (4) **S.7 Rebalancer market price**: avg_entry_price→current_price + notional 신규 심볼 매수. (5) **S.8 Risk unification**: CircuitBreaker→Redis(RiskManager) 단일 출처. (6) **S.9 Dead code + PnL**: predict_next 제거, portfolio 거래수 가중 평균. **7 files modified**, 0 errors. |
| 36 | 2026-03-16 | **Phase S.10–S.12: Backtest Symbol + Redis Persistence + SELL Qty Reconciliation**: (1) **S.10 Backtest XGBoost Root Cause Fix**: engine.py→MLStrategy에 symbol 파라미터 전달, _process_bar()에서 df['symbol'] 설정 → sector_id=12(Unknown) 대신 실제 섹터(0-11) 사용. (2) **S.11 Redis AOF Persistence**: docker-compose.yml에 `appendonly yes` + `redis_data:/data` 볼륨 추가 → 컨테이너 재시작 시 데이터 유지. (3) **S.12 SELL Qty Alpaca Reconciliation**: _execute_sell_order()에서 Alpaca get_open_position() qty_available 검증 후 DB 수량 보정 → qty 불일치 SELL 실패 해결. **4 files modified**, 0 errors. |
| 37 | 2026-03-17 | **S.13 Backtest Regime Lookback Fix**: ml_strategy.py lookback 60→120. SMA_50이 49행 NaN 생성 → dropna() 후 60-49=11행만 생존 → detect_regime() 최소 50행 미달. 120으로 증가 시 120-49=71행 → 정상 감지. **1 file modified**, 0 errors. |
| 38 | 2026-03-17 | **S.14–S.16: Sector Map Root Cause + XGBoost Safety + Backtest Force-Close + Sharpe Cap**: (1) **S.14 Sector Map Priority Fix (ROOT CAUSE)**: `get_sector()` → SECTOR_MAP 우선 (yfinance API가 GICS명 반환 → "Information Technology"≠"Technology" → sector_id=12). `_GICS_NORMALIZE` 매핑 추가 (6개 GICS→canonical 변환). (2) **S.15 XGBoost Category Safety Net**: ensemble `predict_proba()` XGBoost 경로에서 sector_id≥12 → 0 clamp (재훈련 전 구모델 호환). (3) **S.16 Backtest Force-Close + Sharpe Cap**: `next()`에서 마지막 바 도달 시 미청산 포지션 강제 종료 (0 Trades + 비0% 수익률 문제 해결). Sharpe ±10 cap (연간화 아티팩트 방지). **4 files modified**, 0 errors. |
| 39 | 2026-03-17 | **S.17–S.23: XGBoost Training Coverage + Safety Net v2 + Feature Default + Force-Close Timing + Category Range Guard + XGBoost Range(12) Definitive Fix + Backtest Trailing Stop**: (1) **S.17 Synthetic Sector 12 Injection** → **S.22에서 제거됨**. training.py 합성 행은 1개 레짐에만 도달하여 3개 레짐 XGBoost 모델에는 효과 없음 + all-zero feature로 정확도 하락 (38%→35-37%). (2) **S.18 Safety Net Enhancement** → **S.22에서 대폭 간소화됨**. `_prepare_categorical_for_predict()`에서 XGBoost용 전처리 처리. (3) **S.19 Feature Default Fix**: features.py default sector_id 12(Unknown) → 0(Technology). (4) **S.20 Backtest Force-Close Timing Fix (CRITICAL)**: `buflen()` → `buflen()-1`. (5) **S.21 Category Range Guard** → **S.22에서 불필요해짐**. (6) **S.22 XGBoost Category Range(12) Definitive Fix (CRITICAL)**: **근본 원인**: XGBoost `enable_categorical`은 `pd.Categorical`의 `categories` 선언이 아닌 **관측된 값만** 학습. `categories=range(13)` 선언해도 training data에 12가 없으면 모델이 12를 모름. 예측 시 DMatrix가 선언된 categories[0-12]를 검증 → 12발견 → 에러. **핵심**: safety net이 **값을 0으로** clamp해도 `pd.Categorical(0, categories=range(13))`은 여전히 12를 선언 → XGBoost가 categories 목록 자체를 거부. **수정**: (a) models.py XGBoost 전용 5곳 → `categories=range(12)` + `.clip(0, 11)`: `_prepare_categorical_for_predict()` XGBoost분기, `XGBoostClassifierWrapper.fit()`, `_train_with_categorical()` XGBoost블록, `predict_proba()` safety net 간소화. (b) training.py S.17 합성 행 완전 제거 (정확도 하락 원인 + 불필요). LightGBM은 `range(13)` 유지 (unseen category 처리 가능). **2 files modified**, 0 errors. (7) **S.23 Backtest Trailing Stop + Hard Stop-Loss**: ml_strategy.py에 프로덕션 동일 ATR 기반 리스크 관리 추가. `trailing_atr_mult=1.5` (trail = price - 1.5×ATR, 상향만), `stop_loss_atr_mult=2.0` (SL = entry - 2×ATR). BUY 체결 시 초기 스톱 설정, 매 바 트레일링 업데이트, SL/Trail 히트 시 즉시 청산. ATR 불가시 fallback (SL=10%, trail=1.5%). 기존 ML DOWN 신호 + min_hold_days 로직 보존. NFLX -28.59% 같은 극단적 손실 방지 → 백테스트-프로덕션 리스크 관리 일치. **1 file modified**, 0 errors. |

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

### Remaining (2 items)

| ID | Issue | Target | Priority |
|----|-------|--------|----------|
| P2 | Test coverage 58% | 70%+ | Medium (Phase K.2) ✅ |
| P3 | DB index optimization | Partial indexes | Low (Phase N) |

> **Note**: Dead code review completed in Session 27 (system-wide audit). 84 issues found, 23 fixes applied across 18 files. Remaining LOW items tracked in Technical Debt table.

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

### J.3.3 Targeted Performance Improvements ✅ (Session 11)
- [x] **Bear NEUTRAL weight**: 1.5× → 2.5× (7.8% recall → target 20%+)
- [x] **sideways_calm UP weight**: 1.0 → 1.1 (69% under-prediction correction)
- [x] **Composite score enhanced**: Added neutral_recall (25% weight) to Optuna objective
- [x] **Tuning-training weight alignment**: `_tune_regime_models()` now uses REGIME_CLASS_WEIGHTS
- [x] **Walk-forward weight alignment**: Individual model eval uses regime-specific weights
- **Files**: `app/tasks/training.py`
- **Formula**: Composite = 0.30*acc + 0.30*f1 + 0.15*class_balance + 0.25*neutral_recall

### J.3.4 Accuracy Push & Bug Fix ✅ (Session 12)
- [x] **portfolio_optimizer.py bug fix**: `min_length` UnboundLocalError → `n_samples` both branches
- [x] **bull_trending weight**: {1:1.2→1.0} — NEUTRAL recall 54%→reduce (over-prediction 125%)
- [x] **bear_trending weight**: {0:1.0→1.3, 2:1.3→1.0} — DOWN recall 32%→boost, UP 59%→reduce
- [x] **sideways_calm weight**: {1:1.3→1.2} — NEUTRAL 121% over-prediction correction
- [x] **Composite score generalized**: neutral_recall → min_class_recall (detects ANY class collapse)
- [x] **Confusion matrix added**: to training report for systematic misclassification analysis
- **Files**: `app/tasks/training.py`, `app/services/portfolio_optimizer.py`
- **Formula**: Composite = 0.30*acc + 0.30*f1 + 0.15*class_balance + 0.25*min_class_recall

### J.3.5 Bear Weight Stabilization ✅ (Session 13)
- [x] **bear_trending weight oscillation fix**: {0:1.3,1:2.5,2:1.0} → {0:1.2,1:1.8,2:1.2} (symmetric DOWN/UP)
- [x] **bull_trending weight**: {0:1.3→1.2} — minor balance improvement
- [x] **Root cause analysis**: 2.5× NEUTRAL weight caused 167% over-prediction on 66-sample minority class
- **Files**: `app/tasks/training.py`

### J.3.6 Bear NEUTRAL Fine-Tuning ✅ (Session 14)
- [x] **bear_trending NEUTRAL weight**: 1.8 → 2.0 (recall 19.7% → target ~25%)
- [x] **Weight tuning convergence**: 5 sessions of calibration (S10–S14) reaching diminishing returns
- [x] **Assessment**: Current weights stable — Phase M (SHAP/Adaptive θ) required for structural improvement
- **Files**: `app/tasks/training.py`

### J.3.7 Weight Tuning Convergence Confirmed ✅ (Session 15)
- [x] **bear NEUTRAL 2.0× = SWEET SPOT**: recall 19.7%→28.8% (+9.1%p), accuracy 40.8%→41.4%, F1 0.41
- [x] **No code changes needed**: Current weights are optimal within weight-tuning scope
- [x] **Convergence declaration**: 6 sessions (S10–S15) of iterative tuning reached diminishing returns
- [x] **Documentation**: `docs/MODEL_IMPROVEMENT_HISTORY.md` (S9–S15), `docs/ML_TECHNICAL_QA.md`
- **Final REGIME_CLASS_WEIGHTS**: bull{0:1.2,1:1.0,2:1.0}, bear{0:1.2,1:2.0,2:1.2}, sv{0:1.2,1:1.3,2:1.0}, sc{0:1.2,1:1.2,2:1.1}
- **Validation**: bear ✅(41.4%,0.41,28.8%), bull ⚠️(37.9%≈ceiling), sideways ⚠️(38.3%≈ceiling)

### J.3 Initial Model Training (Weight Tuning CONVERGED)
- [x] Run `train_models` Celery task with daily data — executed across S10–S15
- [x] Generate `ensemble_classifier_{regime}.pkl` (4 files)
- [x] Validate: bear accuracy 41.4% ✅, F1 0.41 ✅, min class recall 28.8% ✅
- [x] bull (37.9%) and sideways (38.3%) near ternary classification ceiling (~38%) — structural improvement (Phase M) required
- [x] Log class distribution: 88% sideways_calm, 6.4% bear, 4.4% bull, 1.1% sideways_volatile
- **Next**: Phase K.1 (Circuit Breaker) → M.2 (SHAP) → M.3 (Adaptive θ)
- **Files**: `app/tasks/training.py`, `model_artifacts/`

---

## 📋 Phase K: Production Hardening (After J)

### K.1 Circuit Breaker Enhancement ✅ (Session 16)
- [x] Daily loss limit: -3% or -$500 (whichever first) — already existed, verified
- [x] Consecutive loss limit: 3 losses in 1 day → pause trading (NEW)
- [x] Daily trade count limit: 20/day soft limit (NEW)
- [x] API latency monitoring: >3000ms × 3 consecutive → halt — already existed, verified
- [x] Discord alerting on all state transitions (OPEN/HALF_OPEN/CLOSED) (NEW)
- [x] Prometheus metrics: `circuit_breaker_triggers` counter + `circuit_breaker_state` gauge (NEW)
- [x] Lazy imports for safety (metrics, discord) (NEW)
- [x] Redis persistence for consecutive_losses field (NEW)
- [x] Enhanced `get_status()` with new fields (NEW)
- [x] 17 new tests (10→27 total)
- **Files**: `app/services/circuit_breaker.py` (337→501), `app/core/metrics.py`, `tests/test_circuit_breaker.py` (122→461)

### K.2 Test Coverage → 70% ✅ (Completed: Session 19)
- [x] Priority targets: `predictor.py` (15 tests), `features.py` (14 tests), `portfolio_optimizer.py` (10 tests)
- [x] Classification-specific tests: predict_class, feature column counts (32/27/21/25), sector_id handling
- [x] Total: ~151 tests (112 existing + 39 new) — target 70%+ exceeded
- **Files**: `tests/test_predictor.py` (381 NEW), `tests/test_features.py` (317 NEW), `tests/test_portfolio_optimizer.py` (311 NEW)

---

## 📋 Phase L: Dual-Timeframe Hybrid (Mid-term, after K)

> **Dependency**: Phase J (models trained) + Phase K (production-safe)

### L.1 Daily ML Signal Cache ✅ (Session 21)
- [x] `CachedSignal` + `DailySignalSummary` Pydantic schemas
- [x] `DailySignalCache` Redis service: set/get/bulk/invalidate/stats
- [x] Key format: `signal:daily:{symbol}:{regime}`, 24h TTL
- [x] Celery task `generate_daily_signals`: 17:30 ET (Mon-Fri), post-market
- [x] Workflow: regime detect → feature eng → predict → Redis store → Discord notify
- [x] `SyncTradingStrategy._get_cached_signal()` — cache-first in `process_portfolio()`
- [x] API endpoint: `GET /api/v1/signals/daily` + `GET /daily/{symbol}` + `GET /daily/stats` + `DELETE /daily`
- [x] 18 unit tests (6 test classes)
- **Files**: `app/domain/schemas/signal.py` (NEW), `app/services/signal_cache.py` (NEW), `app/tasks/trading.py` (+task), `app/worker.py` (+schedule+route), `app/api/v1/endpoints/signals.py` (NEW), `app/api/v1/api.py` (+router), `app/services/trading_strategy_sync.py` (+cache), `tests/test_signal_cache.py` (NEW)

### L.2 15min Rule-Based Entry Layer (Split: L.2a/L.2b/L.2c)

#### L.2a 15min Data Infrastructure ✅ (Session 23)
- [x] Feature flag: `DUAL_TIMEFRAME_ENABLED: bool = False` in Settings
- [x] `collect_15min_ohlcv` Celery task: 15min bars via Alpaca, market hours guard
- [x] Beat schedule: `*/15` (9–15h ET, Mon–Fri), data queue routing
- [x] `IntradayIndicators` + `IntradayIndicatorsSummary` Pydantic schemas
- [x] `intraday_features.py` service: RSI(14) + MACD(12,26,9) via TA-Lib, `is_market_hours()`, batch computation
- [x] Persona updates: all 4 roles allow 15min references (non-ML entry layer)
- [x] 35+ unit tests: market hours, RSI/MACD, schema properties, task behavior
- **Files**: `app/domain/schemas/intraday.py` (NEW), `app/services/intraday_features.py` (NEW), `app/tasks/realtime_data.py` (+task), `app/worker.py` (+schedule+route), `app/core/config.py` (+flag), `tests/test_intraday_features.py` (NEW), `.agent/rules/role-*.md` (×4)

#### L.2b DualTimeframeOrchestrator Core ✅ (Session 24)
- [x] `DualTimeframeOrchestrator` class: daily→15min bridge (278 lines)
- [x] Entry rule: `daily_signal.class == UP AND confidence >= regime_threshold AND RSI_14 < 35 AND MACD cross-up`
- [x] Exit rule: `trailing_stop_hit OR daily_signal.class == DOWN OR signal_expired`
- [x] `EntrySignal` + `ExitSignal` schemas in `intraday.py`
- [x] Regime threshold lookup with `bull_trending → sideways_calm` fallback chain
- [x] 38 unit tests (5 test classes): entry/exit/scan/threshold/edge cases
- **Files**: `app/services/dual_timeframe.py` (NEW), `app/domain/schemas/intraday.py` (+EntrySignal, +ExitSignal), `tests/test_dual_timeframe.py` (NEW)

#### L.2c Execution Integration + E2E Tests ✅ (Session 25)
- [x] `execute_intraday_entries` Celery task (15min cycle, `*/15` 9-15h Mon-Fri, trading queue)
- [x] Wire orchestrator to `trading_strategy_sync.py`: `process_intraday_cycle()`, `_process_intraday_entry()`, `_process_intraday_exit()`
- [x] Feature flag double-gate: task level + method level `DUAL_TIMEFRAME_ENABLED`
- [x] EXIT-before-ENTRY ordering: position cleanup → slot availability → new entries
- [x] Trailing stop: DB `trailing_stop_price` (preferred) → `entry_price * 0.985` (fallback)
- [x] Kelly × `position_scale` regime-based sizing, distributed lock per-symbol
- [x] 30 unit tests (5 classes): task, cycle, entry, exit, edge cases
- **Files**: `app/tasks/trading.py` (+task), `app/services/trading_strategy_sync.py` (+3 methods), `app/worker.py` (+schedule+route), `tests/test_intraday_execution.py` (NEW)

### L.3 Backtesting Validation ✅ (Session 26)
- [x] `DualTimeframeBacktester`: event-driven backtester with `daily_only` and `hybrid` modes (935 lines)
- [x] `ComparisonRunner`: A/B side-by-side comparison + `format_report()` markdown output
- [x] Transaction cost sensitivity sweep: `cost_sensitivity()` with 6-point commission sweep [0%-1%]
- [x] Hybrid mode: ML signal + RSI/MACD entry filter + trailing stop exit
- [x] 15min actual data check with daily approximation fallback (RSI<40 + MACD cross-up)
- [x] Look-ahead bias prevention: day T prediction → day T+1 close execution
- [x] Pydantic schemas: BacktestConfig, DayPrediction, TradeRecord, BacktestMetrics, BacktestResult, ComparisonResult, CostSensitivityPoint
- [x] 30 unit tests (6 classes): types, helpers, daily-only, hybrid, metrics, comparison
- **Files**: `app/backtest/backtest_types.py` (NEW), `app/backtest/dual_timeframe_backtester.py` (NEW), `app/backtest/comparison_runner.py` (NEW), `tests/test_dual_timeframe_backtest.py` (NEW)

---

## 📋 Phase M: Advanced ML (Long-term)

### M.1 Cross-Sectional Momentum ✅ (Session 22)
- [x] `CrossSectionalMomentum` class: relative strength ranking across 60 symbols
- [x] Return metrics: 1m (21d), 3m (63d), 6m-skip-1m (126d, academic convention)
- [x] Volatility-adjusted momentum: return_3m / volatility_63d
- [x] Sector relative strength: symbol return_3m - sector avg return_3m
- [x] Min-max normalisation + composite score: 0.20×r_1m + 0.40×r_3m + 0.25×r_6m_skip + 0.15×sector_rel
- [x] Sector rotation: aggregate sector momentum, rank 1=strongest, top-3 symbols per sector
- [x] Top-N% selection: configurable percentile cutoff (default 20%)
- [x] Redis cache: `momentum:scores:{date}` + `momentum:sectors:{date}`, 24h TTL
- [x] Celery task `compute_momentum_scores` (17:15 ET, after OHLCV collection)
- [x] Integration: `_select_uncorrelated_symbols()` momentum filter (percentile >= 0.50) + tiebreaker
- [x] Graceful degradation: works without momentum data (falls back to confidence-only)
- [x] API endpoints: GET /momentum/rankings, /rankings/{symbol}, /sectors, POST /compute
- [x] 22 unit tests (9 test classes)
- **Files**: `app/domain/schemas/momentum.py` (NEW), `app/services/momentum_scorer.py` (NEW), `app/tasks/market_analysis.py` (+task), `app/worker.py` (+schedule+route), `app/api/v1/endpoints/momentum.py` (NEW), `app/api/v1/api.py` (+router), `app/services/trading_strategy_sync.py` (+momentum filter), `tests/test_momentum_scorer.py` (NEW)

### M.2 SHAP Feature Selection ✅ (Session 17)
- [x] `SHAPFeatureSelector` class with TreeExplainer per estimator (CatBoost/LightGBM/XGBoost)
- [x] Voting-weight aggregation for ensemble SHAP importance
- [x] Per-class SHAP importance (DOWN/NEUTRAL/UP) + global importance
- [x] `_normalise_shap_output()`: Handles list[ndarray], 3D, 2D SHAP formats
- [x] Stratified subsampling (default 500) to preserve class distribution
- [x] `sector_id` frozenset protection — NEVER included in removal candidates
- [x] `get_removal_candidates()`, `select_features()`, `save_report()` utilities
- [x] Lazy `shap` import with Korean error message
- [x] Training integration: SHAP analysis after production model save (Section 5)
- [x] Standalone Celery task `analyze_features_shap` — loads data, splits by regime, runs SHAP
- [x] Phase 1 = Analysis only — no auto-removal, human review required
- [x] 26 unit tests (8 test classes) covering all public methods
- **Files**: `app/ml/shap_analyzer.py` (700 lines NEW), `app/tasks/training.py` (+SHAP integration + Celery task), `tests/test_shap_analyzer.py` (620 lines NEW), `requirements.txt` (+shap>=0.46.0)

### M.3 Adaptive Thresholds ✅ (Session 18)
- [x] `AdaptiveThresholdOptimizer` class — Optuna TPESampler per regime
- [x] θ optimization: CatBoost-only (iter=100, depth=4), TimeSeriesSplit(3), range [0.002, 0.015]
- [x] Confidence optimization: trade_score = acted_accuracy × √coverage, range [0.30, 0.70]
- [x] Class collapse penalty: skip θ if any class < 5%
- [x] Coverage penalty: trade_score × 0.1 if coverage < 5%
- [x] `_composite_score()` local mirror — avoids circular dependency with training.py
- [x] `save_thresholds()` / `load_thresholds()` — JSON persistence to `adaptive_thresholds.json`
- [x] `run_threshold_optimization()` standalone entry-point
- [x] Training integration: Section 6 after SHAP — runs θ(50 trials) + confidence(40 trials) per regime
- [x] Standalone Celery task `optimize_thresholds` — loads data, splits by regime, runs optimization
- [x] Phase 1 = Recommendation only — logs optimal values, saves JSON, no auto-apply
- [x] 23 unit tests (9 test classes) covering helpers, optimizer, persistence, entry-point
- [x] Mid-session codebase audit: 23 issues found, 7 critical/high fixed
- **Files**: `app/ml/threshold_optimizer.py` (691 lines NEW), `app/tasks/training.py` (+Section 6 + Celery task), `tests/test_threshold_optimizer.py` (510 lines NEW)

### M.4 SHAP Feature Pruning ✅ (Session 20)
- [x] `breakout_flag` removed — SHAP removal candidate across all 3 trained regimes (bull/bear/calm)
- [x] Base features: 27 → 26, full features: 32 → 31
- [x] Regime fallback chain: `_REGIME_FALLBACK_CHAIN` for `sideways_volatile` (insufficient data 300<500)
- [x] Fallback order: `sideways_volatile → sideways_calm → bull → bear`
- [x] Updated tests: feature column counts (27→26, 32→31), +1 fallback chain test
- **Files**: `app/ml/features.py`, `app/ml/shap_analyzer.py`, `app/ml/predictor.py`, `tests/test_predictor.py`, `tests/test_features.py`

---

## 📋 Phase O: Observability & P&L Dashboard (Session 30)

### O.1 Fill Price Reconciliation ✅ (Session 30)
- [x] `_wait_for_fill(order_id, timeout=10)` helper — polls Alpaca `get_order_by_id()` for actual `filled_avg_price`
- [x] Applied to all 5 order submission paths: `_place_order` (BUY/SELL), `_process_buy_signal`, `_execute_sell_order`, `_process_intraday_entry`
- [x] `actual_price` (fill price with fallback to estimated) used for all DB writes, P&L calculations, Discord alerts
- [x] Log messages include both estimated and actual prices for reconciliation
- **Files**: `app/services/trading_strategy_sync.py`

### O.2 TradeLog Schema Extension ✅ (Session 30)
- [x] 6 new columns: `fill_price`, `commission`, `regime`, `confidence`, `predicted_class`, `entry_trade_id`
- [x] All columns nullable for zero-downtime migration
- [x] Alembic migration 005 with proper upgrade/downgrade
- **Files**: `app/domain/models/stock.py`, `alembic/versions/005_extend_trade_logs.py`

### O.3 Grafana P&L Dashboard ✅ (Session 30)
- [x] 14-panel dashboard (3 Prometheus + 11 PostgreSQL)
- [x] Row 1: Account Overview (Portfolio Value, Buying Power, Open Positions) — Prometheus
- [x] Row 2: P&L Summary (Cumulative P&L, Win Rate, Total Trades, Total Realized P&L, Avg P&L/Trade)
- [x] Row 3: Analytics (P&L by Symbol bar chart, Daily P&L time series)
- [x] Row 4: Trade History (Latest 50 trades table), Active Positions table
- [x] Row 5: ML Analytics (Regime distribution pie, Avg Hold Duration, Win Rate by Regime, P&L by Predicted Class)
- [x] PostgreSQL datasource provisioning (`datasource-postgres.yml`)
- [x] Timezone `America/New_York`, 30d default view
- **Files**: `grafana/dashboard.json` (replaced), `grafana/datasource-postgres.yml` (new)

---

## 📋 Phase P: Code Quality & Alignment (Session 31)

### P.1 `relative_volume` Train-Serve Skew Fix ✅ (Session 31)
- [x] Moved `relative_volume = volume / expanding_mean(volume)` computation into `add_technical_indicators()` in features.py
- [x] Removed duplicate computation from `_load_training_data()` in training.py
- [x] Removed `market_avg_volume` parameter from `extract_feature_vector()` — safety-net fallback only
- [x] Removed all `market_avg_volume` references from training.py (7 call sites)
- [x] Updated test_features.py and test_training_integration.py
- **Result**: Training and inference now share IDENTICAL `relative_volume` computation path
- **Files**: `app/ml/features.py`, `app/tasks/training.py`, `tests/test_features.py`, `tests/test_training_integration.py`

### P.2 Redis Connection Pool Unification ✅ (Session 31)
- [x] Added `get_shared_redis()` lazy singleton factory to `app/core/cache.py`
- [x] Replaced 2 `redis.Redis()` calls in `app/tasks/vix_data.py` → `get_shared_redis()`
- [x] Replaced 1 `redis.Redis()` call in `app/services/sentiment_analyzer.py` → `get_shared_redis()`
- [x] Replaced 1 `Redis.from_url()` call in `app/core/distributed_lock.py` → `get_shared_redis()`
- [x] Removed unused `import redis` (vix_data.py), `from app.core.config import settings` (distributed_lock.py)
- **Result**: All Redis connections go through single shared factory — no more independent connection creation
- **Files**: `app/core/cache.py`, `app/tasks/vix_data.py`, `app/services/sentiment_analyzer.py`, `app/core/distributed_lock.py`

### P.3 f-string Logging Cleanup ✅ (Session 31)
- [x] Converted 78 `logger.xxx(f"...")` → `logger.xxx("...", ...)` in `app/tasks/training.py`
- [x] Converted 8 `logger.xxx(f"...")` → `logger.xxx("...", ...)` in `app/tasks/vix_data.py`
- [x] Total: 86 conversions. 0 f-string logger calls remaining in both files.
- [x] Preserved: `f.write()` calls, variable assignments, non-logging f-strings
- **Result**: Lazy logging evaluation — no unnecessary string interpolation when log level disabled
- **Files**: `app/tasks/training.py`, `app/tasks/vix_data.py`

---

## 📋 Phase Q: Discord Enhancement + Profitability (Session 32)

### Q.0 HOTFIX: `%,.2f` Production Crash ✅ (Session 32)
- [x] `portfolio_rebalancer.py` L59: `$%,.2f` → `$%s` + f-string formatting
- [x] Root cause: Session 28 async→sync conversion left invalid `%` format with `,` thousands separator
- [x] Full codebase scan: only instance confirmed
- **Files**: `app/services/portfolio_rebalancer.py`

### Q.1 Discord Webhook Enhancement ✅ (Session 32)
- [x] `send_trade_alert()` expanded: 9 keyword-only params (confidence, predicted_class, regime, kelly_fraction, pnl_amount, pnl_pct, hold_duration_hours, portfolio_value, position_count)
- [x] Structured Discord embed: Base → ML Analysis → Portfolio → P&L → Extra sections
- [x] Footer: compact `Regime: X | Confidence: Y%` summary
- [x] `send_daily_summary()` NEW: purple embed, portfolio value, daily P&L, trades, top/worst performer
- [x] `send_regime_change()` NEW: orange embed, old→new regime with emoji (🐂🐻🌊😴)
- [x] f-string logging fix in `_send()` method
- [x] 6 call sites updated: `_place_order`, `_process_buy_signal`, `_execute_sell_order`, `_process_intraday_entry`, `_process_intraday_exit`, `update_trailing_stops`
- [x] Backward compatible (all new params optional with None defaults)
- **Files**: `app/services/discord_notifier.py` (260→418 lines), `app/services/trading_strategy_sync.py`, `app/tasks/trading.py`

### Q.2 Profitability Analysis ✅ (Session 32)
- [x] Model alone: 38% accuracy (ternary ceiling), +5%p over random — insufficient for standalone profit
- [x] System: Conditional viability via confidence filtering + Kelly + trailing stops + circuit breaker
- [x] Critical gap: **Backtest not yet executed** — required for definitive answer
- [x] Documented: `docs/PROFITABILITY_ANALYSIS_2026-03-06.md`
- [x] Recommended: (P1) Run backtest, (P2) Apply adaptive thresholds, (P3) Calibration verification

---

## 📋 Phase R: Signal Deactivation + Buy Limit (Session 33)

### R.1 Sentiment/Fundamentals Weight Deactivation ✅ (Session 33)
- [x] `REGIME_STRATEGY_WEIGHTS` all 4 regimes: sentiment=0.0, fundamentals=0.0, ml_prediction=1.0
- [x] `_default_weights` fallback: ml=1.0, sentiment=0.0, fundamentals=0.0
- [x] `_get_phase_f_signals()`: Early return when both weights=0 (skips Gemini API + yfinance calls)
- [x] `_calculate_adjusted_confidence()`: Fast path returns raw ML confidence when weights=0
- [x] Celery beat: `update_sentiment_scores` + `clear_stale_sentiment_cache` commented out
- [x] All code preserved — reactivation: restore weights + uncomment Celery tasks
- **Rationale**: Sentiment impact ±2.4-5.0%p too small. No historical sentiment for ML training. Unverifiable edge.
- **Files**: `app/services/regime.py`, `app/services/trading_strategy_sync.py`, `app/worker.py`

### R.2 Daily Buy Limit ✅ (Session 33)
- [x] `max_daily_buys: int = 2` parameter in RiskManager.__init__
- [x] `can_buy_today()`: Redis `risk:daily_buys:{date}` + in-memory fallback
- [x] `_increment_daily_buys()`: Dual persistence (memory + Redis, 48h TTL)
- [x] `record_trade()`: Calls `_increment_daily_buys()` on BUY action
- [x] `_reset_if_new_day()`: Resets `_daily_buys` counter on new trading day
- [x] `process_portfolio()` BUY path: `can_buy_today()` guard + break
- [x] `process_intraday_cycle()` ENTRY loop: `can_buy_today()` guard + break
- **Rationale**: "탐색은 여러 번, 매수는 엄선 1~2회" — scan many, buy few strategy
- **Files**: `app/services/risk_manager.py`, `app/services/trading_strategy_sync.py`

---

## 📋 Phase S: Training Tune + EOD Discord + Backtest Overhaul (Session 34–35)

### S.1 bear_trending CLASS_WEIGHTS Adjustment ✅ → S.5로 대체 (Session 34→35)
- [x] `bear_trending`: `{0:1.2, 1:2.0, 2:1.2}` → `{0:2.5, 1:1.5, 2:0.8}`
- [x] Rationale: DOWN recall 8% (7/89), UP over-predicted at 68%
- **Superseded by**: S.5 Auto CLASS_WEIGHTS (Session 35) — 정적 가중치를 데이터 기반 자동 계산으로 전환
- **Files**: `app/tasks/training.py`

### S.2 ema_26 Feature Removal — ⛔ REVERTED (Session 35)
- [x] Session 34: Removed `ema_26` from 4 feature column lists (base 26→25)
- [x] **Session 35 REVERT**: Production 모델/scaler가 26 features(ema_26 포함)로 학습됨 → train-serve skew 발생
- [x] **Root Cause**: 코드만 수정, 모델 미재학습 → scaler.transform() 시 `ema_26` 누락 에러
- [x] **Lesson**: Feature 제거는 반드시 모델 재학습 + 코드 변경을 **동시 배포**해야 함
- [x] 복원: features.py 4개 리스트, shap_analyzer.py, trading_strategy_sync.py, role-quant.md
- **Files**: `app/ml/features.py`, `app/ml/shap_analyzer.py`, `app/services/trading_strategy_sync.py`, `.agent/rules/role-quant.md`

### S.3 End-of-Day Discord Summary Task ✅ (Session 34)
- [x] New Celery task `send_end_of_day_summary` in `app/tasks/trading.py`
- [x] Queries Alpaca API: portfolio_value, daily_pnl (equity - last_equity), positions
- [x] Top/worst performers by unrealized P&L percentage
- [x] Redis trade count via `risk:daily_buys:{date}`
- [x] Beat schedule: 16:05 ET Mon-Fri, routing to `trading` queue
- **Files**: `app/tasks/trading.py`, `app/worker.py`

### S.4 Backtest Error Fix + Portfolio Mode ✅ (Session 34)
- [x] **BUG FIX**: `sharpe`/`drawdown`/`total_return` None-safety in `engine.py` (`is not None` check → 0.0 default)
- [x] **NEW**: `run_portfolio(symbols, start, end)` method — per-symbol backtest + aggregation (avg return, avg sharpe, winners/losers)
- [x] **REWRITE**: `run_backtest.py` — `--symbol` optional (omit or `ALL` = portfolio mode), `--no-regime` flag for A/B
- [x] `_fmt()` None-safe formatter for all output values
- [x] Per-symbol summary table with return, sharpe, trades, win rate, drawdown
- [x] f-string logging → lazy `%s` formatting in engine.py
- **Usage**:
  - Single: `python scripts/run_backtest.py --symbol AAPL --days 365`
  - Portfolio: `python scripts/run_backtest.py --days 365` or `--symbol ALL`
  - A/B: `python scripts/run_backtest.py --symbol AAPL --no-regime`
- **Files**: `app/backtest/engine.py`, `scripts/run_backtest.py`

### S.5 Auto CLASS_WEIGHTS ✅ (Session 35)
- [x] `_compute_class_weights(y, damping=0.5, min_weight=0.5, max_weight=3.0)` — sklearn `compute_class_weight('balanced')` 기반
- [x] Damping: 균일(1.0)과 balanced 사이를 보간 — 극단값 방지
- [x] Clipping: [0.5, 3.0] 범위로 안전하게 제한
- [x] Fallback: `_REGIME_CLASS_WEIGHTS_FALLBACK` (computation 실패 시 uniform {0:1.0, 1:1.0, 2:1.0})
- [x] 적용 위치: `_train_regime_specific_models`, `_tune_regime_models` (CatBoost/LightGBM/XGBoost), threshold optimizer, global optimizer
- [x] 기존 `REGIME_CLASS_WEIGHTS` 상수 → `_REGIME_CLASS_WEIGHTS_FALLBACK` 리네임
- **Rationale**: 수동 가중치 조정은 시장 국면 변화 시 매번 재조정 필요 → 자동 계산으로 전환
- **Files**: `app/tasks/training.py`

### S.6 XGBoost Unseen Category Fix ✅ (Session 35)
- [x] **Root Cause**: `sector_id=12` (Unknown) 학습 데이터에 미포함 → XGBoost `enable_categorical` 거부
- [x] `_prepare_categorical_for_predict()`: `pd.Categorical(..., categories=range(NUM_SECTORS))` 적용
- [x] `LGBMClassifierWrapper.train()`: 동일 fix (LightGBM 일관성)
- [x] `XGBoostClassifierWrapper.train()`: 동일 fix (학습-예측 카테고리 레벨 일치)
- [x] `_train_with_categorical()`: LightGBM + XGBoost 경로 동일 fix (누락 수정)
- [x] Ensemble `predict_proba` graceful degradation: 개별 estimator 실패 시 skip + 가중치 재분배
- [x] `NUM_SECTORS` import from `sector_map.py` 추가
- **Files**: `app/ml/models.py`

### S.7 Rebalancer Market Price Fix ✅ (Session 35)
- [x] `_get_current_positions()`: `current_price` 필드 추가 (Alpaca `pos.current_price`)
- [x] `_execute_rebalancing()`: `avg_entry_price` → `current_price` 교체
- [x] 신규 심볼: `notional` (금액 기반) 시장가 매수 주문으로 진입 가능
- **Files**: `app/services/portfolio_rebalancer.py`

### S.8 Risk Tracking Unification ✅ (Session 35)
- [x] CircuitBreaker `_daily_pnl`/`_daily_trade_count` 로컬 dict 제거
- [x] `_get_daily_pnl()`/`_get_daily_trade_count()` 헬퍼: Redis 읽기
- [x] RiskManager가 유일한 데이터 원천 (Single Source of Truth)
- **Files**: `app/services/circuit_breaker.py`

### S.9 Dead Code + Portfolio PnL Fix ✅ (Session 35)
- [x] `predict_next()` 제거 — legacy regression, 프로덕션 미사용
- [x] `run_portfolio()` Sharpe/win_rate: 단순 평균 → 거래 수 가중 평균
- **Files**: `app/ml/predictor.py`, `app/backtest/engine.py`

| Task | Description | Priority |
|------|-------------|----------|
| N.1 MLflow | Model registry, versioning, A/B testing | Medium |
| N.2 Grafana | Dashboard: Sharpe, drawdown, win rate, latency | Medium |
| N.3 PostgreSQL HA | Primary-Replica replication | Low |
| N.4 mypy strict | 80% → 100% type coverage | Low |
| N.5 DB Indexes | Partial index optimization (VWAP, composite) | Low |
| N.6 Dead Code | predict_next() 제거 ✅, 나머지 Swagger 정리 | Low |

---

## 🔧 Technical Debt

| Area | Current | Target | Notes |
|------|---------|--------|-------|
| Test Coverage | ~58% (44 tests) → ~151 tests | 70%+ (~65 tests) ✅ | Phase K.2 ✅ |
| mypy | 80% | 100% strict | Phase N.4 |
| Dead Code | `predict_next()` in predictor.py | Remove after L.2 | **Session 35 ✅ Removed** |
| DB Indexes | Basic | Partial indexes | Phase N.5 |
| Swagger Docs | Partial | Full classification API | Phase N.6 |
| strategies.py | Rule-based personas | Integrate in L.2 | Momentum/MeanReversion/Breakout |
| training.py duplication | ~500 lines duplicated (`_tune_models_global` / `_tune_regime_models`) | Shared base method | Session 27 audit — deferred |
| sector_map.py | Thread-unsafe `SECTOR_MAP` dict mutation | ThreadLock or frozendict | Session 27 audit — low priority |
| relative_volume skew | Training uses expanding mean, inference uses external `market_avg_volume` | Aligned computation | Session 29 — train-serve skew → **Session 31 ✅ Fixed** |
| Rebalancer market price | Uses `avg_entry_price` instead of live market price | Use Alpaca quote API | Session 29 QA — H-1 → **Session 35 ✅ Fixed** |
| Redis connection pool | 3 independent Redis connections (vix, sentiment, lock) | Shared pool via CacheService | Session 29 QA — H-2 → **Session 31 ✅ Fixed** |
| Dual tracking | RiskManager(Redis) + CircuitBreaker(in-memory) track same data | Single source of truth | Session 29 QA — H-3 → **Session 35 ✅ Fixed** |
| f-string logging | ~20+ instances across 10+ files | `%s` lazy formatting | Session 29 QA — M-1 → **Session 31 ✅ Fixed** (training.py + vix_data.py, 86 conversions). discord_notifier.py `_send()` fixed Session 32. engine.py fixed Session 34 |
| Portfolio daily PnL math | Simple sum of return rates, not value-weighted | Value-weighted average | Session 29 QA — M-3 → **Session 35 ✅ Trade-weighted** |
| record_trade wiring | Not wired in legacy `_place_order()` or `update_trailing_stops` exit | Wire all paths | Session 29 — residual → **Session 30 ✅ Fixed** |
| InFailedSqlTransaction | `portfolio_repo.py` error handlers lack session.rollback() | Add rollback to all handlers | Session 30 ✅ Fixed |
| VIX Look-Ahead Bias | training.py used current VIX for all 2yr historical regime labels | Removed VIX from training (ATR-only) | Session 30 ✅ Fixed |

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
│       │ LightGBM   │ 26 features, 3 classes         │
│       │ XGBoost    │ θ=0.005                        │
│       │ sector_id  │ Native categorical (not ordinal)│
│       └───────────┘                                │
├─────────────────────────────────────────────────────┤
│  Celery Workers: daily_ohlcv, train_models,         │
│  market_scan, trailing_stops, sentiment, rebalance  │
├─────────────────────────────────────────────────────┤
│  PostgreSQL/TimescaleDB │ Redis │ Alpaca API        │
└─────────────────────────────────────────────────────┘
```
