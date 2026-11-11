import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from stock_trader.equity_contract import CONTRACT
from stock_trader.equity_notices import NOTICE_KEY, publish
from stock_trader.research_seal import fingerprint

NOW = datetime(2026, 10, 18, 12, tzinfo=UTC)


def status(state="waiting_formation", seconds=0, **fields):
    return {
        "state": state,
        "heartbeat": (NOW + timedelta(seconds=seconds)).isoformat(),
        "contract_sha256": fingerprint(CONTRACT),
        "mode": "research_only",
        "orders_submitted": 0,
        "performance_calculated": False,
        **fields,
    }


async def fence():
    pass


async def count(store):
    return (await store.one("SELECT count(*) AS n FROM alerts"))["n"]


async def test_initial_normal_and_repeated_waiting_are_silent_then_failure_and_recovery_once(store):
    await publish(store, status(), fence)
    await publish(store, status(seconds=1), fence)
    assert await count(store) == 0
    await publish(store, status("review_required", 2, reason="cached_http_error"), fence)
    await publish(store, status("review_required", 3, reason="cached_http_error"), fence)
    assert await count(store) == 1
    await publish(store, status(seconds=4), fence)
    await publish(store, status(seconds=5), fence)
    assert await count(store) == 2
    await publish(store, status("review_required", 6, reason="cached_http_error"), fence)
    assert await count(store) == 3


async def test_target_and_missed_counts_never_regress_when_partial_status_has_no_counts(store):
    await publish(store, status(targets_recorded=1, missed_targets=0), fence)
    assert await count(store) == 1
    await publish(store, status("catching_up", 1), fence)
    await publish(store, status(seconds=2, targets_recorded=0), fence)
    assert await count(store) == 1
    await publish(store, status(seconds=3, missed_targets=1), fence)
    assert await count(store) == 2
    value = await store.get(NOTICE_KEY)
    assert value["targets"] == value["missed"] == 1


async def test_concurrent_duplicate_and_old_observation_cannot_add_duplicate_alerts(store):
    new = status("review_required", 10, reason="identity_changed")
    await asyncio.gather(publish(store, new, fence), publish(store, new, fence))
    assert await count(store) == 1
    await publish(store, status(seconds=9), fence)
    assert (await store.get(NOTICE_KEY))["issue"] and await count(store) == 1


async def test_lost_leader_rolls_back_both_queue_and_notice_state(store):
    calls = 0

    async def lost():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("lost leader")

    with pytest.raises(RuntimeError, match="lost leader"):
        await publish(store, status("review_required"), lost)
    assert await count(store) == 0 and await store.get(NOTICE_KEY) is None


async def test_invalid_notification_contract_or_counts_cannot_commit(store):
    for wrong in [status(orders_submitted=1), status(targets_recorded=-1), status(mode="trading")]:
        with pytest.raises(ValueError):
            await publish(store, wrong, fence)
    assert await count(store) == 0 and await store.get(NOTICE_KEY) is None
