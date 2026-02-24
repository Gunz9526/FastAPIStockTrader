# Plan: Session 7 — Data Backfill, Symbol Expansion & Model Training Direction

**Date**: 2026-02-24  
**Phase**: J.1 + J.2 + Model Architecture Decision  
**Roadmap Reference**: Phase J (Data Backfill & Model Training)

---

## Objective

1. **Symbol Universe Expansion**: 17 → 60+ symbols covering all 11 GICS sectors
2. **Daily OHLCV Backfill Script**: Update `backfill_ohlcv.py` to support `timeframe='1d'`
3. **Model Training Direction**: Apply Option C (Sector as Categorical Feature) — fix `sector_id` from ordinal numeric to native categorical encoding
4. **SECTOR_MAP & sector_map.py**: Full GICS coverage, Unknown(99) → 12 remap

---

## Technical Approach

### Task 1: Symbol Universe Expansion (scripts/add_symbols.py)
- Add ~50 new symbols across 11 GICS sectors + 2 ETFs
- Target distribution: weighted by S&P 500 sector representation
- Include SPY, QQQ (existing), add VIX-tracking if available
- Criteria: Large-cap ($50B+), high daily volume (>1M shares), no OTC

### Task 2: Daily OHLCV Backfill (scripts/backfill_ohlcv.py)
- Add `--timeframe` argument (default: `1d`)
- Change hardcoded `TimeFrame(15, TimeFrameUnit.Minute)` → configurable
- Keep backward compat: existing 15m data untouched
- Target: 2+ years daily data per symbol
- Fix: remove 15m-specific logic (vwap check, adj_close comment)

### Task 3: Sector Categorical Feature (Model Direction)
- **Analysis Result**: Option C (Sector as Feature) → 92% confidence
- `sector_map.py`: Unknown=99 → 12, add all new symbols
- `models.py`: CatBoost `cat_features=[sector_id_index]`, LightGBM `categorical_feature`
- `features.py`: Ensure `sector_id` remains int, not scaled
- No model count change: still 4 regime-specific models

### Task 4: sector_map.py Complete GICS Coverage
- Add all ~60 new symbols to SECTOR_MAP
- Ensure SECTOR_TO_ID covers all GICS sectors
- Fix Unknown=99 → 12

---

## Files Changed

| File | Change Type | Description |
|------|------------|-------------|
| scripts/add_symbols.py | Major rewrite | 17 → 60+ symbols |
| scripts/backfill_ohlcv.py | Major rewrite | Daily timeframe support |
| app/ml/sector_map.py | Update | Full GICS coverage, Unknown remap |
| app/ml/models.py | Surgical edit | CatBoost/LightGBM categorical feature |
| app/ml/features.py | Minor verification | sector_id type check |

---

## Risk & Mitigation

| Risk | Mitigation |
|------|-----------|
| Alpaca API rate limiting during backfill | 0.5s delay per symbol, batch processing |
| Some symbols may not have 2yr daily data | Graceful skip with warning log |
| CatBoost cat_features index mismatch | Dynamic index lookup from column position |

---

## Success Criteria

- [ ] 60+ active symbols in stock_tickers
- [ ] backfill_ohlcv.py supports `--timeframe 1d` (default)
- [ ] CatBoost receives `cat_features=[sector_id_idx]`
- [ ] LightGBM receives `categorical_feature=[sector_id_idx]`
- [ ] sector_map covers all new symbols
- [ ] 0 lint errors in modified files
