# ADR-001: Regime-Specific Trading Thresholds and Conservative Bull Strategy

**Date:** 2026-01-23  
**Status:** Accepted  
**Decision Makers:** PM Agent, User

---

## Context

The current trading system uses uniform buy/sell thresholds (0.2%) across all market regimes. Analysis of model performance shows:

- **bull_trending**: 49% accuracy, -0.22 Sharpe (worse than random)
- **bear_trending**: 53% accuracy, 10.47 Sharpe (suspicious, likely overfit)
- **sideways_calm**: 53% accuracy, 6.53 Sharpe (acceptable)

The poor bull_trending performance means the system actively loses money in bull markets, which historically represent ~60% of market time.

## Decision

Implement **regime-specific trading thresholds** with a conservative approach for bull markets:

### 1. Threshold Configuration (New)
```python
REGIME_CONFIG = {
    'bull_trending': {
        'buy_threshold': 0.004,    # 0.4% (2x normal)
        'sell_threshold': -0.001,  # -0.1% (more conservative)
        'position_scale': 0.3,     # 30% of normal position
        'enabled': True,           # Can disable entirely
    },
    'bear_trending': {
        'buy_threshold': 0.002,
        'sell_threshold': -0.002,
        'position_scale': 0.7,
    },
    'sideways_volatile': {
        'buy_threshold': 0.003,
        'sell_threshold': -0.003,
        'position_scale': 0.5,
    },
    'sideways_calm': {
        'buy_threshold': 0.002,
        'sell_threshold': -0.002,
        'position_scale': 1.0,
    },
}
```

### 2. Model Confidence Integration
Add model confidence factor based on OOS validation results:
- Models with Sharpe > 0 and OOS/IS ratio > 0.5: High confidence
- Models with Sharpe < 0 or OOS/IS ratio < 0.3: Low confidence

### 3. Feature Enhancement for Bull Markets
Add 6 momentum-focused features specifically designed for trending markets:
- `momentum_5d`, `momentum_10d`: Price momentum
- `rsi_momentum`: RSI trend direction
- `trend_strength`: Normalized trend measure
- `price_position`: Position in range
- `breakout_flag`: Breakout detection

---

## Consequences

### Pros
1. **Reduced losses in bull markets**: Conservative thresholds prevent bad trades
2. **Improved risk-adjusted returns**: Position scaling based on model confidence
3. **Adaptability**: Easy to tune thresholds per regime
4. **Future-proof**: Features designed for trending market conditions

### Cons
1. **Reduced trading frequency in bulls**: May miss opportunities
2. **Configuration complexity**: More parameters to manage
3. **Feature maintenance**: 6 new features to maintain
4. **Training time**: Slightly longer due to additional features

### Risks
1. **Threshold tuning**: May need periodic re-optimization
2. **Feature correlation**: New features may be correlated with existing ones
3. **Regime detection lag**: Regime changes detected after the fact

---

## Alternatives Considered

### Alternative 1: Disable Bull Trading Entirely
- **Pros**: Simple, eliminates losses
- **Cons**: Misses all bull market opportunities (~60% of time)
- **Decision**: Rejected - too conservative

### Alternative 2: Use Generic Model Only
- **Pros**: Simpler architecture
- **Cons**: Doesn't adapt to market conditions
- **Decision**: Rejected - regime-awareness provides value

### Alternative 3: Ensemble Voting with Confidence Weights
- **Pros**: More sophisticated
- **Cons**: Complex implementation
- **Decision**: Deferred - may implement in Phase J

---

## Implementation Notes

1. Configuration should be externalized to `app/core/config.py`
2. Thresholds should be logged for debugging
3. A/B testing recommended before full production deployment
4. Monitor regime detection accuracy separately

---

## References

- [Plan_2026-01-23_Bull-Regime-Enhancement.md](../plan-report/Plan_2026-01-23_Bull-Regime-Enhancement.md)
- Backend_Roadmap.md Phase H.3
