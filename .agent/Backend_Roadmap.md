# Backend Roadmap 🚀

This roadmap outlines the systematic evolution of the FastAPI Stock Trader backend.
Current Status: **Phase E (Production Hardening) & F (Advanced AI) Preparation**

---

## ✅ Completed Phases (History)

### Phase A: Core Trading System
- [x] **FastAPI Setup** (Clean Architecture, Async/Sync separation)
- [x] **Database** (PostgreSQL + TimescaleDB, SQLAlchemy)
- [x] **Alpaca API** (Market Data & Trade Execution Integration)
- [x] **Features** (TA-Lib Technical Indicators)

### Phase B: Reliability & Automation
- [x] **Celery Task Queue** (Redis Broker, Synchronous Worker Mode)
- [x] **Scheduler** (Celery Beat Configuration)
- [x] **Docker Security** (Non-root user, Vulnerability scanning)

### Phase C: Performance Optimization (Latency & Throughput)
- [x] **DB Optimization** (TimescaleDB Hypertables, Continuous Aggregates)
- [x] **Caching Layer** (Redis for OHLCV, Positions, Account info with TTL)
- [x] **Connection Pooling** (SQLAlchemy QueuePool, PgBouncer readiness)
- [x] **Monitoring** (Prometheus Metrics, Health Checks)

### Phase D: ML Core (Training & Tuning)
- [x] **Model Architecture** (Ensemble: CatBoost + LGBM + XGBoost)
- [x] **Hyperparameter Tuning** (Optuna Framework, Dynamic Sharpe:F1 Ratio)
- [x] **Data Strategy** (24-month Rolling Window, TimeSeriesSplit)
- [x] **Feature Engineering** (TA-Lib 15+ indicators)
- [x] **Backtesting System** (Backtrader engine, CLI verification)

---

## CI/CD Infrastructure (Completed 2026-01-22) ✅

**Goal**: Fast and reliable GitHub Actions pipeline for Python 3.14 + CatBoost.

### Build Optimization
- [x] **Conda-based Dependency Management**
  - Migrated from pip to conda for Python 3.14 compatibility
  - Created `environment.yml` for reproducible environments
  - CatBoost installation: Source build (40min) → Pre-built wheel (2min)
- [x] **GitHub Actions Workflow**
  - Implemented `conda-incubator/setup-miniconda@v3`
  - Added `shell: bash -el {0}` for conda environment activation
  - Separated conda packages (catboost, numpy, pandas) from pip packages
- [x] **Dockerfile Multi-stage Build**
  - Optimized layer caching with environment.yml first
  - Added CatBoost installation verification step
  - Build time: 15-20min → 5-7min
- [x] **Version Pinning**
  - Python: Fixed to 3.14.x (`requires-python = "==3.14.*"`)
  - CatBoost: 1.2.8 from conda-forge (catboost-1.2.8-cpu_py314hf729cd6_6.conda)
  - Channel priority: conda-forge (strict)

**Impact:**
- GitHub Actions build time: 40min (timeout) → 3min (93% reduction)
- Docker build time: 15min → 6min (60% reduction)
- CI/CD reliability: 0% success → 100% success

---

## Code Quality & Cleanup (Ongoing)

**Goal**: Maintain high code quality through refactoring, optimization, and best practices.

### Completed Improvements (2026-01-05) ✅
- [x] **Unused Parameter Cleanup**
  - `app/tasks/training.py::_train_regime_specific_models`: Removed `repo: SyncStockRepository` and `end_date: pd.Timestamp` parameters
  - Inlined `_walk_forward_validation` logic (15 lines TimeSeriesSplit)
  - Result: Cleaner function signatures, no unused variables
- [x] **Sector Lookup Priority Reversal**
  - `app/ml/sector_map.py::get_sector()`: Changed to API-first strategy
  - Priority: yfinance API (real-time) → Manual SECTOR_MAP (fallback)
  - Rationale: Real-time data more accurate than static mapping
- [x] **Backfill Script Creation**
  - `scripts/backfill_sectors.py`: 70-line script for existing symbols
  - Usage: `docker compose exec app python scripts/backfill_sectors.py`
  - Purpose: Update sector data for symbols with NULL sector_id

### Critical Fixes (2026-01-06) ✅
- [x] **Gemini API Migration** (google-generativeai → google-genai)
  - Migrated from deprecated `google-generativeai` to official `google-genai>=1.33.0` SDK
  - Updated `app/services/sentiment_analyzer.py`: Client-based API pattern
  - Import change: `from google import genai` → `genai.Client(api_key=...)`
  - Model update: `gemini-pro` → `gemini-2.0-flash-exp`
  - API call: `client.models.generate_content(model=..., contents=...)`
- [x] **Feature Pipeline Fix** (Training KeyError Resolution)
  - Added `base_feature_columns` property to `FeatureEngineer` (19 technical indicators)
  - Separated training features (base) vs prediction features (full 24 with Phase F)
  - Updated `app/tasks/training.py` line 95: Use `base_feature_columns` for historical data
  - Updated `app/tasks/training.py` line 667: Feature importance uses `base_feature_columns`
  - Root cause: Phase F features (sentiment, fundamentals) not in historical OHLCV data
- [x] **SQL Logging Reduction**
  - Disabled SQLAlchemy echo in `app/core/database.py` (both async and sync engines)
  - Changed `echo=settings.ENV_STATE == "dev"` → `echo=False`
  - Logging now controlled by `app/core/logging.py` configuration only
  - Result: Clean logs, SQL statements only appear when needed

### CI/CD Pipeline Fixes (2026-01-22) ✅
- [x] **GitHub Actions Conda Environment Activation**
  - Removed deprecated `auto-activate-base: false` parameter from `setup-miniconda@v3`
  - Added `shell: bash -el {0}` to all lint and test steps
  - Fixed exit code 127 (command not found) errors in CI/CD
  - Impact: Ruff and mypy now execute in activated conda environment
- [x] **Training Pipeline Bug Fix**
  - Fixed `app/tasks/training.py` line 348: `predictor.load_model(regime)` → `predictor.get_model(regime)`
  - Root cause: PredictorService only has `get_model()` method, not `load_model()`
  - Impact: Model validation step now works correctly

### Testing Infrastructure (2026-01-22) ✅
- [x] **Python 3.14 Asyncio Modernization**
  - Removed deprecated `asyncio.get_event_loop_policy().new_event_loop()` from conftest.py
  - Migrated to pytest-asyncio automatic event loop management
  - No custom event_loop fixture needed (pytest-asyncio handles it)
  - Impact: Future-proof test infrastructure for Python 3.14+
- [x] **Integration Test Suite** (NEW - 11 tests added)
  - Created `tests/test_training_integration.py` (450+ lines)
  - Full workflow tests: train_models, tune_models, _load_and_prepare_data
  - Mock-based DB/API testing (no external dependencies)
  - Scenarios: Normal flow, edge cases (no symbols, insufficient data), Optuna tuning
  - Coverage: Training pipeline end-to-end ~65%
- [x] **Test Count Growth**
  - Before: 33 tests (19 original + 14 regime tests)
  - After: 44 tests (+11 integration tests)
  - Coverage increase: 45% → 58% (estimated)
  - Test files: 5 → 6 (new: test_training_integration.py)

### Pending Improvements (Low Priority)
- [ ] **DB Index Optimization**
  - Remove redundant index: `ix_stock_ohlcv_symbol` (covered by composite index)
  - Add composite index: `idx_ohlcv_timeframe_symbol_time` for multi-timeframe queries
  - Add partial index: `WHERE vwap IS NOT NULL` for VWAP-specific queries
- [ ] **Code Duplication Analysis**
  - Identify similar logic across services
  - Extract common patterns to utility functions
- [ ] **Type Hints Enhancement**
  - Add missing type hints to legacy functions
  - Enable strict mypy checking

---

## Phase E: Production Hardening (Current Focus)

**Goal**: Ensure the system runs autonomously and robustly in a live environment.

**Status**: 20% Complete (Infrastructure ready, operational features pending)

### E.1 Operational Reliability (Priority: HIGH)
- [ ] **Circuit Breakers Enhancement** (Next Task)
  - Current: Basic RiskManager checks (cooldown, min profit)
  - Planned: Portfolio-level circuit breakers
    - Daily loss limit: -3% or -$500 (whichever first)
    - API latency threshold: > 3000ms → halt trading
    - Consecutive loss limit: 3 losses in 1 hour → pause
  - Implementation: `app/services/circuit_breaker.py` expansion
  - Estimated: 2-3 days
- [ ] **Alerting System** (Discord/Slack Webhook)
  - Notify on: Trade Execution, Critical Errors, Risk Limits
  - Priority: MEDIUM (after circuit breakers)
- [ ] **Alpaca WebSocket** (Real-time Order Updates)
  - Replace polling with event-driven updates for filled orders
  - Priority: LOW (current polling works, optimization only)

### E.2 Infrastructure High Availability
- [ ] **PostgreSQL Replication** (Primary-Replica Setup Plan)
- [ ] **Redis Persistence** (AOF/RDB Policy Check)
- [ ] **Log Aggregation** (Centralized logging setup plan)

---

## 🔮 Phase F: Advanced AI Capabilities (95% Complete)

**Goal**: Transform from a "Technical Trader" to an "AI Hedge Fund" with Sentiment, Fundamentals, and Advanced Analytics.

### F.1 Sentiment Analysis Integration (100% Complete) ✅
*Real-time news sentiment with AI-powered analysis*
- [x] **SentimentAnalyzer Service** (Completed 2026-01-05)
  - Gemini API integration with JSON parsing
  - Redis caching (1-hour TTL): `sentiment:{symbol}`
  - Sentiment score: -1.0 (극도 부정) to +1.0 (극도 긍정)
  - Regime-weighted adjustment: Bull favors positive, Bear favors negative
- [x] **Celery Automation** (Completed 2026-01-05)
  - `update_sentiment_scores`: Hourly updates (crontab: `minute=0, hour=*`)
  - `clear_stale_sentiment_cache`: Daily cleanup (midnight)
- [x] **Feature Integration** (Completed 2026-01-05)
  - Added `sentiment_score` to ML feature vector (20th feature)
  - `add_sentiment_and_fundamentals()` convenience method
- [x] **Finnhub Integration** (Completed 2026-01-05)
  - Premium financial news API (Reuters, Bloomberg, WSJ, etc.)
  - Endpoint: GET /v1/company-news
  - Free tier: 60 calls/minute (sufficient for hourly updates)
  - Production: $59/month (Professional plan)
  - Top 10 articles per symbol (sorted by datetime)
  - Response: headline, summary, source, url, datetime
  - Error handling: RequestException, timeout (10s)
  - Superior news quality vs NewsAPI.org

### F.2 Fundamental Metrics Integration (100% Complete) ✅
*Enhanced feature engineering with financial health metrics*
- [x] **FundamentalDataProvider Service** (Completed 2026-01-05)
  - yfinance API integration with LRU cache (maxsize=500, 24h TTL)
  - Metrics: PE Ratio, PB Ratio, ROE, Dividend Yield, Market Cap, Beta
  - Stock categorization: VALUE, GROWTH, INCOME, BLEND, UNKNOWN
  - Risk-adjusted score: `(ROE / PE) * (1 + Div_Yield) / Beta`
- [x] **Feature Integration** (Completed 2026-01-05)
  - Added 4 fundamental features: `pe_ratio`, `pb_ratio`, `roe`, `beta`
  - Default values: PE=15.0, PB=3.0, ROE=0.10, Beta=1.0 (market averages)
  - Total features: 20 → 24 (including sentiment)
- [x] **Sector Auto-Fetch** (Completed 2026-01-05)
  - Priority: yfinance API → Manual SECTOR_MAP fallback
  - LRU cache (maxsize=1000) prevents excessive API calls
  - 11 sector categories + Unknown=99
  - Backfill script: `scripts/backfill_sectors.py`

### F.3 VIX Integration & Regime Enhancement (100% Complete) ✅
*Volatility Index for improved regime detection*
- [x] **VIX Data Collection** (Completed 2026-01-05)
  - Celery task: `collect_vix_data` (daily 6:30 AM EST)
  - Alpaca API: Daily VIX bars (symbol: 'VIX')
  - Storage: PostgreSQL (historical) + Redis (latest value, 24h TTL)
- [x] **Regime Detection Enhancement** (Completed 2026-01-05)
  - `RegimeDetector.detect_regime(vix_value=Optional[float])`
  - VIX thresholds: >30 (extreme fear), >20 (high fear)
  - VIX overrides ATR for volatility classification
  - Logging: VIX value included in regime detection logs
- [x] **VIX Interpretation**
  - VIX < 12: Low volatility (calm market)
  - VIX 12-20: Normal volatility
  - VIX 20-30: Elevated volatility (high fear)
  - VIX > 30: Extreme volatility (panic)

### F.4 Advanced Analytics (100% Complete) ✅
*Feature importance and portfolio stress testing*
- [x] **Feature Importance Analysis** (Completed 2026-01-05)
  - Celery task: `analyze_feature_importance`
  - Extraction from tree-based models (CatBoost, LGBM, XGBoost)
  - Weighted average using ensemble weights
  - Output: PNG plot (top 15 features) + JSON data
  - Files: `feature_importance_{regime}.png`, `feature_importance_{regime}.json`
- [x] **Monte Carlo Simulation** (Completed 2026-01-05)
  - `MonteCarloSimulator` class (10,000 simulations, 252 days)
  - Portfolio simulation: Cholesky decomposition for correlated returns
  - Single-asset simulation: Geometric Brownian Motion (GBM)
  - Risk metrics: VaR (95%), CVaR, probability of loss, percentiles
  - Use case: Portfolio stress testing and scenario analysis

**Phase F Status: 100% Complete** ✅

---

## 🎯 Phase G: Real-Time 15-Minute Trading (Completed 2026-01-04)

**Goal**: Enable intraday trading with 15-minute bars and real-time data collection.

### G.1 Real-Time Data Collection
- [x] **15-Minute OHLCV Collection** (Completed 2026-01-04)
  - Celery task: `collect_15m_realtime` (every 15 min during market hours 9:00-15:00 ET)
  - Alpaca integration: VWAP and trade_count fields added
  - Market hours validation (weekday check, 9:30 AM - 4:00 PM ET)

### G.2 VWAP Feature Engineering
- [x] **VWAP Distance Feature** (Completed 2026-01-04)
  - Formula: `(close - vwap) / vwap`
  - Interpretation: Institutional benchmark comparison
  - Total features: 19 → 20 (added vwap_distance)

### G.3 Trading Logic 15m Conversion
- [x] **SyncTradingStrategy 15m Mode** (Completed 2026-01-04)
  - Timeframe: '15m' (was '1d')
  - Minimum bars: 500 (≈5 trading days)
  - Thresholds adjusted: 0.5% → 0.2% (intraday sensitivity)
  - Logging: [15m] tag added

### G.4 Celery Beat Schedule
- [x] **15-Minute Collection Schedule** (Completed 2026-01-04)
  - Crontab: `minute=0,15,30,45 hour=9-15 day_of_week=1-5`
  - Worker: app.tasks.realtime_data included
  - Frequency: 4 times/hour, 7 hours/day, weekdays only

---

## 🧠 Phase H: Market Regime Awareness (Partial Complete 2026-01-04)

**Goal**: Adaptive AI that responds to market conditions (Bull, Bear, Volatile, Calm).

### H.1 Regime Detection Integration
- [x] **RegimeDetector Integration** (Completed 2026-01-04)
  - Method: `detect_market_regime()` in SyncTradingStrategy
  - Reference: SPY 15m data (90 days lookback)
  - Metrics: ADX > 25 (trend), ATR% > 3% (volatility), SMA50 (direction)
  - Output: 4 regimes (BULL_TRENDING, BEAR_TRENDING, SIDEWAYS_VOLATILE, SIDEWAYS_CALM)

### H.2 Regime-Aware Prediction
- [x] **PredictorService Multi-Model Support** (Completed 2026-01-04)
  - Model loading: 4 regime-specific pkl files or generic fallback
  - Method: `predict_next(features, regime=MarketRegime.SIDEWAYS_CALM)`
  - Fallback: Generic model if regime models missing

### H.3 Regime-Specific Model Training
- [x] **Training Pipeline Regime Classification** (Completed 2026-01-04)
  - Classify historical data by regime (SPY-based detection)
  - Split data into 4 regime datasets (minimum 1000 samples each)
  - Train 4 ensemble models: `ensemble_model_{regime}.pkl`
  - Walk-Forward validation per regime
  - Implementation: _train_regime_specific_models() in training.py

---

## 🛡️ Phase I: Advanced Risk & Position Defense (Partial Complete 2026-01-04)

**Goal**: Protect against overtrading, premature exits, and rapid re-trading.

### I.1 Trading Defense Mechanisms (Completed 2026-01-04)
**Critical vulnerabilities fixed:**
- ✅ **Minimum Holding Period**: 60 minutes (4 bars @ 15m)
  - Prevents rapid position flipping (buy → sell within 15 min)
  - Exception: Stop-loss signals override
- ✅ **Minimum Profit Threshold**: 1.5% (5x transaction cost margin)
  - Prevents premature exits on minimal profits
  - Force exit allowed after 120 minutes
- ✅ **Cooldown Period**: 60 minutes after exit
  - Prevents immediate re-trading (whipsaw protection)
  - Logged: "COOLDOWN: Xmin remaining"

**Implementation:**
- Database: `position_tracking` table (Alembic migration `002_position_tracking.py`)
- RiskManager: `can_enter_position()`, `can_exit_position()`, `record_position_exit()`
- Repository: `record_position_entry()`, `get_active_position()`, `update_position_exit()`
- TradingStrategy: Defense checks before BUY/SELL orders

**Impact:**
- Transaction fee ratio: 0.5% → 0.1% (estimated)
- Whipsaw trades: 20-30% → <5%
- Expected ROI improvement: +10-15% annualized

### I.2 Multi-Position System (Completed: 2026-01-05)
**Features:**
- ✅ **Concurrent Multi-Symbol Positions**
  - Hold AAPL + MSFT + GOOGL + NVDA + TSLA simultaneously (max 5)
  - Portfolio diversification based on correlation matrix
  - Symbol selection: Low correlation (<0.7 with active positions)
- ✅ **Modern Portfolio Theory (MPT)**
  - Sharpe ratio maximization via scipy.optimize
  - Constraint: Max 30% allocation per symbol, Sum of weights = 1.0
  - Auto-upgrade: Backtest data → Live data (when 50+ trades exist)
- ✅ **Kelly Criterion Position Sizing**
  - Formula: `f* = (bp - q) / b` with 25% safety fraction
  - Dynamic calculation per symbol based on win rate and P/L ratio
  - Live data integration: Auto-switches after 10+ trades per symbol
- ✅ **Portfolio-Level VaR**
  - Daily Value-at-Risk calculation (95% confidence, 14-day window)
  - Historical simulation method (percentile-based)
  - Conservative fallback: -3% daily risk if data insufficient
- ✅ **Daily Rebalancing**
  - Schedule: 3:45 PM ET (15 min before market close)
  - Trigger: Only if weight drift > 5%
  - Minimum trade value: $100 (avoid micro-trades)
- ✅ **Automated Parameter Updates**
  - Daily 00:00 ET: Correlation matrix, VaR, Kelly sizes
  - Rolling 14-day window (auto-refreshed)
  - Redis caching: 24-hour TTL

**Implementation:**
- PortfolioOptimizer: Correlation, VaR, Kelly, MPT optimization
- PortfolioRepository: P&L aggregation, trade history, position queries
- PortfolioRebalancer: Weight calculation, drift detection, order execution
- TradingStrategy.process_portfolio(): Multi-symbol batch processing
- Celery tasks: update_portfolio_parameters (00:00), rebalance_portfolio (15:45)

**Impact:**
- Diversification: 1 → 5 concurrent positions
- Risk reduction: Correlation-based selection (<0.7)
- Capital efficiency: Kelly-optimized position sizing
- Sharpe maximization: MPT weight optimization

### I.3 External Data Integration (Completed via Phase F)
**Already Implemented:**
- Gemini API for news sentiment (Phase F.1)
- Finnhub for financial news (Phase F.1)
- yfinance for fundamentals (Phase F.2)

**Out of Scope for Current Roadmap:**
- Reddit/Twitter social sentiment
- FRED economic indicators
- Alternative data providers (Quandl, Bloomberg Terminal)

---

## Next Steps (Priority Order)

### Immediate (This Week)
1. **Circuit Breaker Enhancement** (Phase E.1)
   - Portfolio-level loss limits
   - API latency monitoring
   - Consecutive loss detection
   - Estimated: 2-3 days

### Short-term (Next 2 Weeks)
2. **Alerting System** (Phase E.1)
   - Discord webhook integration
   - Alert templates (trade, error, risk)
   - Estimated: 1-2 days

3. **Test Coverage Improvement**
   - Target: 45% → 70%
   - Focus: `features.py`, `predictor.py`, `portfolio_optimizer.py`
   - Estimated: 3-4 days

### Mid-term (Next Month)
4. **PostgreSQL Replication** (Phase E.2)
   - Primary-Replica setup for high availability
   - Estimated: 5-7 days

5. **Production Monitoring Dashboard**
   - Grafana dashboard customization
   - Key metrics: Sharpe, drawdown, win rate, latency
   - Estimated: 2-3 days

### Long-term (Future Phases)
- Phase J: Microservices Architecture (if scaling needed)
- Phase K: Machine Learning Model Registry (MLflow integration)
- Phase L: Backtesting Platform (Web UI for strategy testing)

---

## Technical Debt & Cleanup

### Current Status
- Test Coverage: ~45% (needs improvement to 70%+)
- Documentation: Swagger partially updated (RAG endpoints documented)
- Code Quality: Ruff linting passing, mypy type checking 80%

### Pending Tasks
- [ ] **Remove Legacy Code**: Check for unused files (e.g., old `services/backtester.py`, mock strategies)
- [ ] **Unit Tests**: Coverage improvement for `features.py` and `predictor.py`
- [ ] **Documentation**: Update API docs (Swagger) with new Ops endpoints
- [ ] **Type Hints**: Add missing type hints to achieve 100% mypy coverage
- [ ] **English Logs**: Convert remaining Korean logs to English for international compatibility
