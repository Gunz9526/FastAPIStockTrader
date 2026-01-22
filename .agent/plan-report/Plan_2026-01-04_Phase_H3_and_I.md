# Plan: Phase H.3 & Phase I Implementation
**Date:** 2026-01-04  
**Plan Name:** Phase H.3 (Regime-Specific Training) + Phase I (Advanced Risk & Position Defense)  
**Status:** Planning Phase  
**Roadmap Alignment:** Backend_Roadmap.md Phase F.3 (Market Regime) + New Phase I

---

## 1. EXECUTIVE SUMMARY

**Objectives:**
1. **Phase H.3**: Implement regime-specific model training to create 4 dedicated ensemble models (bull_trending, bear_trending, sideways_volatile, sideways_calm).
2. **Phase I (Priority)**: Address critical trading defense mechanisms to prevent premature exits and rapid re-trading of the same symbol.
3. **Phase I (Secondary)**: Portfolio optimization and advanced risk management (MPT, Kelly Criterion, VaR).

**Critical Findings from Audit:**
- **NO position hold time tracking**: System can buy/sell the same symbol within minutes (15m cycles).
- **NO minimum profit threshold**: Positions exit on any prediction flip, even 0.1% profit.
- **NO cooldown period**: Symbol can be re-purchased immediately after sale.
- **Risk Manager exists** but does NOT enforce holding periods or profit minimums.

**Estimated Scope:**
- Phase H.3: ~3 file modifications (training.py, predictor.py refactor)
- Phase I Defense: ~5 file modifications (risk_manager.py, trading_strategy_sync.py, database schema)
- Phase I Advanced: Deferred (requires external APIs, new services)

---

## 2. PHASE H.3: REGIME-SPECIFIC MODEL TRAINING

### 2.1 Current State Analysis
**Completed:**
- ✅ RegimeDetector integrated into SyncTradingStrategy
- ✅ PredictorService supports 4 regime-specific models
- ✅ predict_next() accepts regime parameter

**Gap:**
- ❌ Only ONE generic model file exists: `ensemble_model.pkl`
- ❌ Training pipeline (training.py) does NOT classify data by regime
- ❌ No logic to split historical data into 4 regime buckets

### 2.2 Implementation Strategy

**File: `app/tasks/training.py`**

**Changes Required:**
1. Add regime classification step in `train_models()`:
   ```python
   # After feature generation, classify each row by regime
   regime_detector = RegimeDetector()
   regimes = []
   for idx, row in features_df.iterrows():
       regime = regime_detector.detect_regime(features_df.loc[:idx])
       regimes.append(regime.value)
   features_df['regime'] = regimes
   ```

2. Split data into 4 regime-specific datasets:
   ```python
   for regime in MarketRegime:
       regime_data = features_df[features_df['regime'] == regime.value]
       if len(regime_data) < 1000:  # Minimum data requirement
           logger.warning(f"Insufficient {regime.value} data: {len(regime_data)} rows")
           continue
       # Train ensemble for this regime
       ensemble = EnsembleWrapper(weights=best_weights)
       ensemble.fit(X_regime, y_regime)
       ensemble.save(f"ensemble_model_{regime.value}.pkl")
   ```

3. Add regime-aware walk-forward validation:
   - Each validation window classified by regime
   - Sharpe ratio calculated per regime
   - Ensemble weights optimized per regime

**File: `app/ml/predictor.py`**
- Already supports regime-aware loading ✅
- May need fallback logic refinement (use generic model if regime model fails)

**Complexity:** Medium  
**Estimated Lines Changed:** ~150 lines  
**Risk:** Medium (can fallback to generic model if training fails)

---

## 3. PHASE I.1: TRADING DEFENSE MECHANISMS (CRITICAL PRIORITY)

### 3.1 Problem Analysis

**Issue 1: Rapid Position Flip**
```
09:30 AM - BUY AAPL @ $180 (prediction: 0.52)
09:45 AM - SELL AAPL @ $180.10 (prediction: 0.48, profit: $0.10)
```
- **Root Cause:** No minimum holding period. 15m task cycles allow immediate exits.
- **Impact:** High transaction costs, missed trend continuations.

**Issue 2: Insufficient Profit Exit**
```
BUY @ $100 → SELL @ $100.05 (0.05% profit)
```
- **Root Cause:** No minimum profit threshold before allowing exits.
- **Impact:** Transaction fees exceed profits (Alpaca: ~$0.005/share).

**Issue 3: Immediate Re-Entry**
```
09:30 - BUY AAPL @ $180
09:45 - SELL AAPL @ $180.10
10:00 - BUY AAPL @ $180.05 (again!)
```
- **Root Cause:** No cooldown period after exit.
- **Impact:** Whipsaw trades, overtrading penalties.

### 3.2 Defense Mechanism Design

**Solution 1: Minimum Holding Period**
- **Rule:** Do not exit position if held < 4 bars (60 minutes for 15m trading)
- **Implementation:** Track `position_entry_time` in database or memory
- **Exception:** Override if stop-loss hit (safety first)

**Solution 2: Minimum Profit Threshold**
- **Rule:** Do not exit on prediction flip unless profit > 1.5% OR time > 8 bars
- **Implementation:** Calculate unrealized P&L, compare to threshold
- **Formula:** `(current_price - entry_price) / entry_price > 0.015`
- **Rationale:** 5x safety margin over transaction costs (0.3%), aligned with 15m ATR average

**Solution 3: Symbol Cooldown Period**
- **Rule:** After SELL, blacklist symbol for N bars (e.g., 4 bars = 60 min)
- **Implementation:** Track `last_exit_time` per symbol in RiskManager
- **Formula:** `current_time - last_exit_time > timedelta(minutes=60)`

### 3.3 Implementation Plan

**File 1: `app/core/database.py` (Schema Update)**
- Add table: `position_tracking`
  - Columns: symbol, entry_time, entry_price, quantity, exit_time (nullable), exit_price (nullable)
  - Purpose: Persistent tracking across container restarts (PostgreSQL)
  - Redis role: Cooldown temporary cache (60min TTL, volatile data)

**File 2: `app/services/risk_manager.py`**
```python
class RiskManager:
    def __init__(self):
        # Existing code...
        self.position_entry_times: Dict[str, datetime] = {}  # In-memory cache
        self.symbol_cooldowns: Dict[str, datetime] = {}     # Redis-backed
        self.min_hold_bars = 4                               # 60min (15m x 4 bars)
        self.min_profit_pct = 0.015                          # 1.5% (5x transaction cost)
        self.cooldown_bars = 4                               # 60min cooldown
        # TODO: Add ATR-based dynamic adjustment option
    
    def can_exit_position(
        self, 
        symbol: str, 
        entry_price: float, 
        current_price: float,
        entry_time: datetime,
        bars_per_cycle: int = 15  # minutes
    ) -> Tuple[bool, str]:
        """Check if position can be exited based on defense rules."""
        
        # Rule 1: Minimum holding period
        hold_duration = datetime.now() - entry_time
        min_hold_time = timedelta(minutes=self.min_hold_bars * bars_per_cycle)
        if hold_duration < min_hold_time:
            return False, f"MIN_HOLD: {hold_duration.seconds//60}m < {min_hold_time.seconds//60}m"
        
        # Rule 2: Minimum profit threshold (1.5%)
        profit_pct = (current_price - entry_price) / entry_price
        if profit_pct < self.min_profit_pct:
            # Allow exit after 8 bars (120min) even if unprofitable
            max_hold_time = timedelta(minutes=8 * bars_per_cycle)
            if hold_duration < max_hold_time:
                return False, f"MIN_PROFIT: {profit_pct:.2%} < 1.5%"
        
        return True, "OK"
    
    def can_enter_position(self, symbol: str) -> Tuple[bool, str]:
        """Check if symbol is in cooldown period."""
        if symbol in self.symbol_cooldowns:
            cooldown_end = self.symbol_cooldowns[symbol]
            if datetime.now() < cooldown_end:
                remaining = (cooldown_end - datetime.now()).seconds // 60
                return False, f"COOLDOWN: {remaining}m remaining"
        return True, "OK"
    
    def record_position_exit(self, symbol: str):
        """Record exit and start cooldown period."""
        cooldown_duration = timedelta(minutes=self.cooldown_bars * 15)
        self.symbol_cooldowns[symbol] = datetime.now() + cooldown_duration
        logger.info(f"{symbol} cooldown until {self.symbol_cooldowns[symbol]}")
```

**File 3: `app/services/trading_strategy_sync.py`**
- Modify `_execute_trade_logic()`:
  - Before BUY: Check `risk_manager.can_enter_position(symbol)`
  - Before SELL: Check `risk_manager.can_exit_position(symbol, ...)`
- Track `position_entry_time` when opening positions
- Pass entry_time to exit checks

**File 4: `app/repositories/stock_repo_sync.py`**
- Add methods:
  - `record_position_entry(symbol, price, qty, timestamp)`
  - `get_position_entry_time(symbol) -> Optional[datetime]`

**Complexity:** Medium-High  
**Estimated Lines Changed:** ~200 lines  
**Risk:** Low (defensive additions, no breaking changes)

---

## 4. PHASE I.2: PORTFOLIO OPTIMIZATION (DEFERRED)

**Reason for Deferral:**
- Modern Portfolio Theory (MPT) requires multiple concurrent positions
- Current system trades 1 symbol at a time (sequential scanning)
- Kelly Criterion needs win rate estimation (requires >50 backtests)
- VaR calculation needs historical portfolio snapshots (not yet tracked)

**Recommendation:**
- Complete Phase H.3 and Phase I.1 first
- Revisit after collecting 1 month of live trading data
- Requires architectural shift to batch position management

---

## 5. PHASE I.3: EXTERNAL DATA INTEGRATION (FUTURE PHASE)

**Out of Scope for Current Session:**
- Gemini API integration (needs API key setup, new service)
- News sentiment (News API requires subscription)
- Social media (Reddit/Twitter APIs rate-limited, complex parsing)
- FRED economic indicators (macro-level, not stock-specific)

**Recommendation:**
- Create separate "Phase J: External Intelligence" plan
- Requires MSA design for sentiment service
- Budget estimate: 3-5 days of implementation

---

## 6. EXECUTION STRATEGY (TOKEN-OPTIMIZED)

**Priority Order:**
1. **Phase I.1 (Defense)** - Critical for production safety
   - Prevents transaction fee bleeding
   - Reduces overtrading risk
   - Immediate business value
2. **Phase H.3 (Regime Training)** - AI performance enhancement
   - Unlocks regime-specific modeling
   - Requires significant compute time (not token-intensive)
   - Can run as background task

**Implementation Sequence:**
```
Step 1: Implement Position Defense (Risk Manager + Trading Strategy)
Step 2: Test defense logic with mock positions
Step 3: Implement Regime-Specific Training
Step 4: Trigger training task for all 4 regimes
Step 5: Validate 4 model files created
Step 6: Update documentation and roadmap
```

**Estimated Token Budget:**
- Phase I.1: ~15K tokens (code modifications + testing)
- Phase H.3: ~10K tokens (training.py refactor)
- Documentation: ~5K tokens (reports + roadmap)
- **Total:** ~30K tokens (well within budget)

---

## 7. SUCCESS CRITERIA

**Phase I.1 (Defense):**
- ✅ Positions held minimum 60 minutes (4 bars)
- ✅ No exits below 1.5% profit unless time > 120 minutes
- ✅ 60-minute cooldown enforced after exits (Redis TTL)
- ✅ Logs show "MIN_HOLD" and "COOLDOWN" messages
- ✅ Position records persisted in PostgreSQL

**Phase H.3 (Regime Training):**
- ✅ 4 model files exist: `ensemble_model_{regime}.pkl`
- ✅ Each model trained on regime-specific data (>1000 samples)
- ✅ PredictorService loads all 4 models without errors
- ✅ Trading logs show regime-specific model selection

---

## 8. RISK MITIGATION

**Risk 1: Insufficient Regime Data**
- **Mitigation:** Use generic model as fallback if <1000 samples
- **Detection:** Log warnings during training

**Risk 2: Defense Rules Too Strict**
- **Mitigation:** Make thresholds configurable (environment variables)
- **Testing:** Backtest with defense rules enabled

**Risk 3: Schema Migration Failures**
- **Mitigation:** Use Alembic migration (reversible)
- **Rollback:** Keep old RiskManager logic intact

---

## 9. NEXT STEPS AFTER APPROVAL

1. **User Review:** Present this plan (Korean summary in chat)
2. **Approval Gate:** Wait for "Y" confirmation
3. **Delegation:** Spawn sub-agents with rules:
   - Backend Agent: Risk Manager + Trading Strategy modifications
   - Quant Agent: Regime-specific training logic
4. **QA Loop:** Verify each change against `.agent/project_context.md`
5. **Final Report:** Generate task reports (EN + KR)
6. **Roadmap Update:** Mark Phase H.3 complete, add Phase I.1 entry

---

## 10. USER FEEDBACK INTEGRATION

**Q1: Redis vs DB, Threshold Appropriateness?**
- A: PostgreSQL primary storage (persistence), Redis for cooldown cache only
- A: Profit threshold 0.5% → **1.5% increased** (5x transaction cost margin)
- A: 60min hold appropriate, ATR dynamic adjustment as future option

**Q2: Multi-Position System?**
- A: Current: Sequential 1-symbol trading → Goal: Concurrent multi-symbol (AAPL+MSFT+GOOGL)
- A: MPT/Kelly requires portfolio-level risk calculation
- A: Timeline: After Phase I.1 + 2 weeks live data (mid-to-late Jan 2026)

**Open Questions:**
1. Regime-specific hold times? (VOLATILE 30min, TRENDING 90min)
2. ATR-based dynamic thresholds? (High volatility requires 2%)

**Recommendation:** Start with fixed parameters, optimize after 2 weeks live data.

---

**END OF PLAN**
