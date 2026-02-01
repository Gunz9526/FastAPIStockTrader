# Task Report: Issue Investigation (6 Items)
**Date:** 2026-02-01

## Objective
Investigate and resolve 6 issues reported by the user regarding VIX cache, Discord notifications, sentiment cache cleanup, feature count matching, and sentiment/fundamental usage in trading.

---

## 1. VIX Cache Issue

### Problem
```
worker-training-1  | VIX 캐시 없음 (ATR 기반 레짐 감지 사용)
```
Despite VIX data being successfully cached:
```
worker-data-1  | Redis VIX 캐시: 17.44 (source: yfinance)
```

### Root Cause
**Data format mismatch between writer and reader:**

- **Writer** ([vix_data.py#L102-L106](app/tasks/vix_data.py#L102-L106)): Stores plain string
  ```python
  redis_client.setex('vix:latest', 86400, str(latest_vix_value))
  ```

- **Reader** ([training.py#L173-L177](app/tasks/training.py#L173-L177)): Uses `cache.get()` which expects JSON
  ```python
  from app.core.cache import cache
  vix_cached = cache.get("vix:latest")
  ```

- **Cache Service** ([cache.py#L44-L47](app/core/cache.py#L44-L47)): Parses as JSON
  ```python
  value = self.redis_client.get(key)
  if value:
      return json.loads(value)  # Fails on plain "17.44"
  ```

### Solution
Modify `vix_data.py` to store VIX value as JSON format, OR modify `training.py` to use direct Redis access like `get_latest_vix()` function.

**Recommended Fix:** Use `get_latest_vix()` function in training.py (already exists).

---

## 2. Discord Webhook Notification Issue

### Problem
Error notifications not being sent to Discord.

### Analysis
- Decorator order is correct: `@celery_app.task` then `@notify_on_failure`
- `notify_on_failure` decorator implementation is correct
- Re-raises exception after sending notification

### Root Cause (Probable)
1. **DISCORD_WEBHOOK_URL not set**: The notifier logs warning if URL is missing
2. **No actual errors occurring**: If tasks succeed, no notification is sent

### Solution
1. Verify `DISCORD_WEBHOOK_URL` is set in `.env` or Docker environment
2. Check logs for "Discord webhook not configured" message
3. Test manually: Deliberately cause an error to verify notification works

---

## 3. Sentiment Cache Deletion Count = 0

### Problem
```
worker-data-1  | 감성 캐시 항목이 없습니다
worker-data-1  | deleted_count: 0
```

### Root Cause
The `clear_stale_sentiment_cache` task only deletes keys with **TTL = -1** (no expiration):
```python
for key in keys:
    ttl = analyzer.redis_client.ttl(key)
    if ttl == -1:  # Only delete if no expiration set
        analyzer.redis_client.delete(key)
```

Since `cache_sentiment()` properly sets TTL when caching, there are no keys with TTL=-1.

### Conclusion
**This is expected behavior**, not a bug. Redis automatically expires keys based on TTL. The task is redundant but harmless.

---

## 4. Feature Count Matching (Training vs Inference)

### Analysis

| Component | Feature Set | Count |
|-----------|-------------|-------|
| Training ([training.py](app/tasks/training.py)) | Default (legacy) | 25 |
| Inference ([trading_strategy_sync.py#L210](app/services/trading_strategy_sync.py#L210)) | `feature_set="legacy"` | 25 |

**Features in legacy set:**
- Core technical (17): rsi, macd, macd_signal, macd_hist, bb_width, bb_position, sma_20, sma_50, ema_12, ema_26, atr_pct, adx, stoch_k, stoch_d, volume_ratio, roc, mom
- Cross-sectional (2): sector_id, relative_volume
- VWAP & liquidity (2): vwap_distance, trade_intensity
- Phase F (4): sentiment_score, pe_ratio, pb_ratio, roe

### Conclusion
**Feature counts match.** Both training and inference use 25 features.

---

## 5. Sentiment & Fundamental Usage in Trading

### User Request
Previous request was to NOT include sentiment/fundamentals in ML features.

### Current State
**ISSUE:** `legacy` feature set includes Phase F features:
```python
# features.py legacy_feature_columns
'sentiment_score',
'pe_ratio', 'pb_ratio', 'roe',
```

### Recommendation
If user wants to exclude sentiment/fundamentals from ML training:
1. Use `feature_set="core"` (21 features) instead of `legacy`
2. Update both training.py and trading_strategy_sync.py

The `core` feature set excludes Phase F:
```python
# 21 core technical features only
# No sentiment_score, pe_ratio, pb_ratio, roe, beta
```

---

## 6. Rules Directory Structure

### Current Rules
| File | Role | Description |
|------|------|-------------|
| [role-backend.md](.agent/rules/role-backend.md) | Backend Developer | FastAPI, DB, Docker, Security |
| [role-quant.md](.agent/rules/role-quant.md) | Quant Analyst | Strategy, Backtesting, ML models |
| [role-trading.md](.agent/rules/role-trading.md) | Trading Logic | Learning & execution logic |
| [role-pm.md](.agent/rules/role-pm.md) | PM (English) | Project management workflow |
| [role-pm-kr.md](.agent/rules/role-pm-kr.md) | PM (Korean) | Korean version |

### Assessment
Rules are minimal but functional. Consider adding:
- QA Engineer role for testing focus
- More detailed constraints and verification checklists

---

## Summary of Required Fixes

| Issue | Priority | Fix Required |
|-------|----------|--------------|
| VIX Cache | HIGH | Use `get_latest_vix()` in training.py |
| Discord | MEDIUM | Verify DISCORD_WEBHOOK_URL environment variable |
| Sentiment Cache | LOW | No fix needed (expected behavior) |
| Feature Count | N/A | Already matching |
| Phase F in Features | MEDIUM | Switch to `core` feature set if exclusion desired |

---

## Execution Time
Analysis completed in single session.
