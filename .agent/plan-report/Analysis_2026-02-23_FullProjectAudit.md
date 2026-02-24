# Full Project Audit & Improvement Analysis

**Date**: 2026-02-23  
**Scope**: Complete codebase analysis — architecture, ML pipeline, trading logic, infrastructure  
**Status**: Analysis Complete

---

## 1. System Overview

### 1.1 Tech Stack
| Layer | Technology | Version |
|-------|-----------|---------|
| Language | Python | 3.14 |
| Web Framework | FastAPI | ≥0.128.0 |
| Database | PostgreSQL + TimescaleDB | - |
| ORM | SQLAlchemy (async + sync) | ≥2.0.46 |
| Migration | Alembic | ≥1.18.1 |
| Task Queue | Celery + Redis | ≥5.6.2 |
| ML Models | CatBoost, LightGBM, XGBoost (Ensemble) | - |
| Hyperparameter Tuning | Optuna | ≥4.7.0 |
| Feature Engineering | TA-Lib (15+ indicators) | ≥0.6.8 |
| Backtesting | Backtrader | 1.9.78.123 |
| Broker API | Alpaca (Paper/Live) | alpaca-py ≥0.43.2 |
| Sentiment AI | Google Gemini (genai) | ≥1.60.0 |
| News API | Finnhub | ≥2.4.26 |
| Fundamentals | yfinance | ≥1.0 |
| Monitoring | Prometheus + Grafana | - |
| Notification | Discord Webhook | httpx-based |
| Containerization | Docker + Docker Compose | Multi-stage build |

### 1.2 Architecture Pattern
- **Layered Architecture** (not strict Clean Architecture)
  - `api/` → FastAPI endpoints
  - `domain/models/` → SQLAlchemy ORM models
  - `domain/schemas/` → Pydantic schemas
  - `repositories/` → Data access layer
  - `services/` → Business logic
  - `ml/` → ML pipeline (features, models, predictor)
  - `tasks/` → Celery async tasks
  - `core/` → Configuration, DB, cache, security

### 1.3 Trading System Design
- **15-minute intraday** trading strategy
- **4 Market Regimes**: Bull Trending, Bear Trending, Sideways Volatile, Sideways Calm
- **Ensemble ML Models**: Regime-specific CatBoost + LightGBM + XGBoost
- **Multi-position portfolio** (max 5 simultaneous positions)
- **Risk Management**: Circuit breaker, cooldowns, min hold time, stop loss

---

## 2. CRITICAL Issues (Must Fix)

### 2.1 [CRITICAL] Model Performance — Fundamentally Broken
**Current Performance:**
| Regime | Accuracy | Sharpe | Verdict |
|--------|----------|--------|---------|
| bull_trending | 48.78% | -0.42 | ❌ Worse than random (uses fallback) |
| bear_trending | 52.49% | 10.04 | ❌ Severe overfitting |
| sideways_calm | 53.08% | 5.99 | ⚠️ Suspicious Sharpe |
| sideways_volatile | N/A | N/A | ❌ Disabled (only 70 samples) |

**Root Causes:**
1. **Target Variable Design Flaw**: The target is `next bar return` (close pct_change shifted -1). This is an **extremely noisy signal** for 15m bars — the signal-to-noise ratio is inherently low. The models are essentially trying to predict random walk noise.
2. **Directional Accuracy ≈ Coin Flip**: 48-53% accuracy on direction prediction means the model provides almost zero edge.
3. **Sharpe Ratio Anomalies**: bear_trending Sharpe of 10.04 is **impossible in practice** — this is textbook overfitting on in-sample data.
4. **Training/Inference Feature Mismatch**: Training uses `base_feature_columns` (27 features) but inference uses `legacy` (25 features with Phase F additions). This fundamental mismatch means the model never sees the features it was trained on during live prediction.

### 2.2 [CRITICAL] Feature Mismatch Between Training and Inference
**File**: `app/services/trading_strategy_sync.py` line ~210

```python
# Training uses base_feature_columns (27 features with momentum)
all_X.append(features_df[feature_engineer.base_feature_columns])

# But inference uses "legacy" (25 features with sentiment/PE instead of momentum)
scaled_features = self.feature_engineer.extract_feature_vector(
    current_features, fit_scaler=False, feature_set="legacy"
)
```

The model trains on momentum features (momentum_5, momentum_10, rsi_momentum, trend_strength, price_position, breakout_flag) but at inference time receives sentiment_score, pe_ratio, pb_ratio, roe instead. **The model is making predictions on completely different features than what it learned.**

### 2.3 [CRITICAL] Position Sizing is Essentially Fixed at 1 Share
**File**: `app/services/trading_strategy_sync.py` line ~432

```python
base_qty = 1
qty = max(1, int(base_qty * position_scale))
```

For a `position_scale` of 0.5 (bull_trending), this yields `max(1, int(0.5)) = 1 share`. For 1.0, `max(1, 1) = 1 share`. The system **always buys exactly 1 share** regardless of available capital, Kelly criterion, or portfolio optimization. This completely negates the sophisticated portfolio optimization framework.

### 2.4 [CRITICAL] Sentiment & Fundamentals Have Near-Zero Impact
**File**: `app/services/trading_strategy_sync.py`

```python
sentiment_adjustment = sentiment_score * 0.005  # Max ±0.005
fundamentals_adjustment = -0.003 or +0.002  # Fixed tiny values

adjusted = (
    ml_prediction * 0.75 +           # Dominates
    sentiment_adjustment * 0.15 +     # Max: ±0.00075
    fundamentals_adjustment * 0.10    # Max: ±0.0003
)
```

The sentiment and fundamentals adjustments are **double-multiplied** (once by scale factor, once by weight), resulting in virtually zero impact. Max sentiment contribution: `0.005 × 0.15 = 0.00075`. Against typical ML predictions of 0.001-0.01, these adjustments are noise.

### 2.5 [CRITICAL] Walk-Forward Validation Uses Future Data (Look-Ahead Bias)
**File**: `app/tasks/training.py` line ~720

Training feature scaling is fitted on the **entire regime dataset** before walk-forward splits:

```python
X_regime_scaled = feature_engineer.extract_feature_vector(
    X_regime, fit_scaler=True, market_avg_volume=market_avg_volume
)
```

Then TimeSeriesSplit is applied on already-scaled data. The scaler has already seen future data distributions, causing look-ahead bias. This inflates in-sample and out-of-sample metrics.

---

## 3. HIGH Priority Issues

### 3.1 Regime Classification is Computationally Catastrophic
**File**: `app/tasks/training.py` line ~180

```python
for idx, timestamp in enumerate(X.index):
    spy_window = spy_features[spy_features.index <= timestamp].tail(200)
    regime = regime_detector.detect_regime(spy_window, vix_value=vix_value)
```

This iterates over **every single sample** in the training set (could be 100K+), doing a pandas filter + sort + feature extraction for each one. With 100K samples, this takes hours unnecessarily. The VIX value used is also a **single cached value**, not the historical VIX at that timestamp — another look-ahead bias.

### 3.2 PredictorService Singleton Anti-Pattern
**File**: `app/ml/predictor.py`

`PredictorService` uses `__new__` singleton pattern with class-level `_models = {}`. This means:
- Models are shared across all threads/requests (no thread safety)
- `reload_models()` is not atomic — concurrent predictions during reload get inconsistent models
- The `_model_path` attribute referenced in `retrain_weighted()` doesn't exist (will crash)
- The singleton persists stale models until explicit reload

### 3.3 RiskManager is In-Memory Only — State Lost on Restart
**File**: `app/services/risk_manager.py`

```python
self.position_entry_times: dict[str, datetime] = {}  # In-memory cache
self.symbol_cooldowns: dict[str, datetime] = {}      # Redis-backed (future) ← NOT IMPLEMENTED
```

All cooldown and position tracking state is in-memory. When Celery worker restarts, all cooldowns are lost, allowing immediate re-entry to positions that should be in cooldown.

### 3.4 `REGIME_STRATEGY_WEIGHTS` in regime.py is Dead Code
**File**: `app/services/regime.py`

The elaborate `REGIME_STRATEGY_WEIGHTS` dict maps each regime to strategy weights (Momentum, MeanReversion, Breakout, MLEnsemble). However, **this is never used anywhere**. The actual trading uses only the ML ensemble prediction. The traditional strategies in `strategies.py` (MomentumStrategy, MeanReversionStrategy, BreakoutStrategy) are entirely unused in the trading pipeline.

### 3.5 Trailing Stop System is Non-Functional
**File**: `app/tasks/trading.py` line ~79

```python
logger.warning("트레일링 스톱 업데이트는 일시 비활성화되어 있습니다 - 동기 리팩토링 필요")
```

Despite being scheduled every 15 minutes, the trailing stop task does nothing. The `Position` table's stop_loss_price, take_profit_price, trailing_stop_price fields are never populated.

### 3.6 Backtest Engine Doesn't Use Current Models
**File**: `app/backtest/engine.py`

The backtest engine uses `MLStrategy` (from `app/backtest/ml_strategy.py`) which likely doesn't incorporate recent regime detection, feature mismatches, or ensemble predictions. Backtesting results don't reflect actual live trading behavior.

---

## 4. MEDIUM Priority Issues

### 4.1 Double HTTP Middleware Overhead
**File**: `app/main.py`

Two separate `@app.middleware("http")` handlers both log requests:
- `metrics_middleware`: Logs `[{method}] {endpoint} - IP: {client_ip}`
- `log_requests`: Logs `Incoming: {method} {url}` and `Response: {status_code}`

Every request is logged twice with redundant information, causing unnecessary I/O.

### 4.2 No Transaction Isolation on Position Updates
**File**: `app/services/trading_strategy_sync.py`

The `_place_order` method acquires a distributed lock but doesn't wrap the Alpaca API call + DB update in a proper transaction. If the API call succeeds but DB commit fails, the system loses track of the position. The `finally` block records circuit breaker results regardless of commit status.

### 4.3 Kelly Criterion Backtest Fallback is Mock Data
**File**: `app/services/portfolio_optimizer.py` line ~290

```python
def _get_backtest_trades(self, repo, symbol: str) -> list[dict]:
    """Simulate trades from backtest data (mock for initial period)."""
```

When live trade data is insufficient (< 10 trades), the system falls back to generating random mock trades. This means initial Kelly sizing is based on fake data.

### 4.4 Correlation Matrix Uses Incompatible Data Lengths
**File**: `app/services/portfolio_optimizer.py` line ~86

```python
min_length = min(len(v) for v in returns_data.values())
aligned_returns = {k: v[:min_length] for k, v in returns_data.items()}
```

Aligning by truncating to shortest length loses valuable data and doesn't account for actual date alignment. Two symbols with 100 data points each might have completely different date ranges.

### 4.5 Cache Serialization Inconsistency
**File**: `app/core/cache.py`

The `CacheService.get()` uses `json.loads()` but `SentimentAnalyzer` directly calls `self.redis_client.get()` without JSON parsing. Some code paths use the `CacheService` singleton, others create raw Redis connections. This dual-path cache architecture causes confusion.

### 4.6 Optuna Tuning Objective Only Optimizes Sharpe
**File**: `app/tasks/training.py`

The tuning objective function optimizes only for Sharpe ratio via directional prediction. There's no regularization penalty for model complexity, no constraint on drawdown, and no check for minimum accuracy. This explains why bear_trending achieves Sharpe=10 while being essentially overfit.

---

## 5. LOW Priority Issues

### 5.1 Unused Imports and Dead Code
- `strategies.py`: `BreakoutStrategy` is fully implemented but never instantiated
- `regime.py`: `REGIME_STRATEGY_WEIGHTS` and `REGIME_RISK_PARAMS` never referenced
- `DMatrix` imported in `models.py` but only used inside predict() — could be lazy imported
- `worker.py`: `print()` statements for debug (`print("REDIS_URL =", ...)`)

### 5.2 Hardcoded Paths
- `/app/model_artifacts/` hardcoded in `PredictorService` — breaks local development
- `/app/model_artifacts/feature_scaler.pkl` hardcoded in `FeatureEngineer`
- Should use `settings` or environment variable

### 5.3 Missing Error Recovery in Celery Tasks
- `train_models` doesn't checkpoint progress — if it fails after 2 hours of training, all work is lost
- No retry with exponential backoff for Alpaca API calls
- Task timeout not configured (long training could block the queue)

### 5.4 Test Coverage Gaps
- 44 tests with ~45% coverage
- No tests for: `portfolio_optimizer.py`, `circuit_breaker.py`, `trading_strategy_sync.py`
- Integration tests use mocks so heavily they don't validate real behavior

---

## 6. Investment Strategy Issues

### 6.1 Fundamental Strategy Problem
The system tries to predict **exact 15-minute returns** — one of the hardest problems in quantitative finance. Even sophisticated hedge funds with billions in R&D budget achieve only small edges on this timeframe. The current approach has several fundamental problems:

1. **Signal-to-Noise Ratio**: 15m returns are dominated by microstructure noise, making ML prediction nearly impossible without tick-level features
2. **Regime Detection Granularity**: Using SPY ADX/ATR for 15m regime detection is too coarse — market microstructure changes within minutes
3. **No Risk-Reward Asymmetry**: All positions are sized at 1 share with symmetric thresholds — no exploitation of favorable risk/reward setups
4. **No Mean-Reversion vs Momentum Switch**: Despite having separate strategies defined, the system always uses the ML regression approach regardless of regime

### 6.2 Recommendation: Strategy Overhaul
Instead of predicting exact returns, the model should:
1. **Predict classification** (UP/DOWN/NEUTRAL) with confidence calibration
2. **Use regime-strategy mapping** (the already-defined but unused REGIME_STRATEGY_WEIGHTS)
3. **Implement asymmetric position sizing** — bigger positions on high-confidence signals
4. **Add minimum expected move filter** — don't trade if predicted move < transaction cost + slippage
5. **Move to daily timeframe** for ML predictions, use 15m only for execution timing

### 6.3 Model Architecture Recommendations
1. **Replace regression with classification**: Binary (UP ≥ 0.2%/DOWN) or ternary with neutral zone
2. **Add feature selection**: Use SHAP values to prune noisy features (27 features is too many for the signal quality)
3. **Implement purged cross-validation**: Prevent data leakage in time series with embargo periods
4. **Add ensemble diversity metrics**: Ensure CatBoost/LightGBM/XGBoost aren't making identical predictions
5. **Train on daily bars first**: Establish a baseline on cleaner signal before attempting 15m

---

## 7. Summary of Recommended Action Plan

### Immediate (Week 1)
| Priority | Issue | Impact |
|----------|-------|--------|
| P0 | Fix feature mismatch between training/inference | Predictions currently meaningless |
| P0 | Fix position sizing (use Kelly/portfolio value) | Currently always 1 share |
| P0 | Fix scaler look-ahead bias in training | Inflated backtest metrics |
| P1 | Persist RiskManager cooldowns to Redis | Safety mechanism broken |

### Short-term (Week 2-3)
| Priority | Issue | Impact |
|----------|-------|--------|
| P1 | Redesign target variable (classification) | Core model improvement |
| P1 | Implement proper purged CV | Reliable performance metrics |
| P1 | Fix sentiment/fundamentals double-scaling | Phase F features useless |
| P1 | Optimize regime classification (vectorized) | Training takes hours → minutes |

### Medium-term (Month 2)
| Priority | Issue | Impact |
|----------|-------|--------|
| P2 | Implement trailing stops properly | Risk management gap |
| P2 | Wire up strategy framework (regime→strategy) | Diversified alpha sources |
| P2 | Add proper backtesting with current models | Can't evaluate without this |
| P2 | Thread-safe PredictorService | Production stability |

### Long-term (Month 3+)
| Priority | Issue | Impact |
|----------|-------|--------|
| P3 | Consider daily timeframe for ML | Cleaner signal |
| P3 | Feature selection with SHAP | Remove noise features |
| P3 | MLflow model registry | Model versioning |
| P3 | Comprehensive test suite (70%+ coverage) | Regression prevention |

---

## 8. Architecture Diagram (Current)

```
┌─────────────┐     ┌──────────────┐     ┌───────────────┐
│  FastAPI     │────▶│  PostgreSQL  │◀────│  Celery Beat  │
│  (API Layer) │     │  +TimescaleDB│     │  (Scheduler)  │
└──────┬──────┘     └──────────────┘     └──────┬────────┘
       │                                         │
       │     ┌──────────────┐              ┌─────▼────────┐
       └────▶│    Redis     │◀─────────────│ Celery Worker│
             │  (Cache/Lock)│              │ (--pool=solo)│
             └──────────────┘              └──────┬───────┘
                                                  │
                    ┌─────────────────────────────┤
                    │            │                 │
             ┌──────▼──┐  ┌─────▼─────┐  ┌───────▼──────┐
             │ Training │  │  Trading  │  │ Data Collect │
             │ Pipeline │  │ Strategy  │  │ (OHLCV/VIX)  │
             └──────┬───┘  └─────┬─────┘  └──────────────┘
                    │            │
             ┌──────▼──┐  ┌─────▼──────┐
             │ ML Model│  │ Alpaca API │
             │Ensemble │  │(Paper/Live)│
             └─────────┘  └────────────┘
```

---

*Report generated by Lead Technical PM — 2026-02-23*
