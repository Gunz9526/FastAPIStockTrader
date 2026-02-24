# ADR-001: Redis Usage Pattern and Celery Queue Architecture

**Status:** Proposed
**Date:** 2026-01-14
**Author:** PM Agent
**Context:** Phase E.1 Operational Reliability

---

## Context

The FastAPI Stock Trader system currently faces several production reliability issues:

1. **Race Conditions:** Multiple Celery workers can simultaneously enter positions for the same symbol
2. **Resource Contention:** Long-running training tasks (30+ minutes) share the same queue as time-sensitive trading tasks (< 1 second tolerance)
3. **Query Performance:** Active position lookups scan entire `position_tracking` table

The system uses:
- Celery with Redis broker (single worker, single queue)
- PostgreSQL + TimescaleDB for data storage
- Redis for caching (sentiment, regime, VIX values)

---

## Decision

### 1. Redis Usage: Synchronous Client Only

**We will NOT adopt async Redis (`aioredis`) for this system.**

Rationale:
- Celery workers run synchronously (`--pool=solo`)
- Trading logic (`trading_strategy_sync.py`) is synchronous
- Mixing async/sync patterns adds complexity without benefit
- `redis-py` sync operations complete in < 1ms (sufficient for trading)

### 2. Redis Pattern: Simple Key-Value with TTL, NOT Pub/Sub

**We will NOT use Redis Pub/Sub.**

Rationale:
- **Use Case:** Distributed locking and caching
- **Pub/Sub Purpose:** Broadcasting messages to multiple subscribers
- **Mismatch:** Trading requires mutual exclusion, not message distribution

Pattern chosen:
```
SET trading:{symbol}:lock NX EX 30  # Lock with 30s auto-expire
GET sentiment:{symbol}              # Cache read
SETEX market:regime {value} 300     # Cache write with TTL
```

### 3. Celery Queue Separation: 3 Priority Queues

**We will split the single queue into 3 dedicated queues.**

| Queue | Priority | Tasks | Worker Resources |
|-------|----------|-------|------------------|
| `trading` | HIGH | Market scan, trailing stops, rebalancing | Dedicated, low latency |
| `data` | MEDIUM | 15m collection, sentiment, VIX | Shared with trading if needed |
| `training` | LOW | Model training, tuning | Isolated, can use all CPU |

Implementation:
- `task_routes` configuration in `worker.py`
- Separate Docker services: `worker-trading`, `worker-data`, `worker-training`
- Flower dashboard for queue monitoring

---

## Consequences

### Positive

1. **No Race Conditions:** Redis locks prevent duplicate position entries
2. **Predictable Latency:** Trading tasks no longer blocked by training
3. **Scalability:** Can scale trading workers independently
4. **Simplicity:** Sync Redis is easier to debug than async
5. **No New Dependencies:** Uses existing `redis-py`

### Negative

1. **Operational Complexity:** 3 worker services vs 1
2. **Resource Usage:** Slightly higher memory footprint
3. **Monitoring Overhead:** Must watch 3 queues in Flower

### Neutral

1. **No Pub/Sub:** If real-time push notifications are needed later, will require separate decision
2. **No Async Redis:** FastAPI endpoints may benefit from async cache access in future (separate ADR if needed)

---

## Alternatives Considered

### Alternative 1: Async Redis with `aioredis`
- **Rejected:** Adds complexity, workers are sync, no clear benefit

### Alternative 2: Redis Pub/Sub for order events
- **Rejected:** No subscribers need real-time order updates currently
- **Future:** May reconsider if WebSocket gateway added

### Alternative 3: Database-level advisory locks
- **Rejected:** More complex than Redis `SET NX`, requires SQL knowledge
- **Future:** Could be backup if Redis unavailable

### Alternative 4: Single queue with task priorities
- **Rejected:** Celery priority routing is complex and unreliable
- **Chosen:** Separate queues with dedicated workers is simpler

---

## Implementation Notes

See: `.agent/plan-report/Plan_2026-01-14_Stability_Enhancement.md`

Key files:
- `app/core/distributed_lock.py` - Redis lock wrapper
- `app/worker.py` - Queue routing configuration
- `docker-compose.yml` - Multi-worker setup
