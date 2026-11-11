import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from stock_trader.etf_shadow import seal
from stock_trader.notifications import Notifier
from stock_trader.operations import (
    KEY,
    POLICY,
    BackupCheck,
    Monitor,
    daily_check,
    disk_check,
    entry_allowed,
)

NOW = datetime(2026, 10, 16, 7, tzinfo=UTC)
GIB = 1024**3


def usage(free=30, total=200):
    return SimpleNamespace(total=total * GIB, free=free * GIB)


def backup(folder, now=NOW):
    folder.mkdir(mode=0o700, exist_ok=True)
    name = f"paper-db-{now:%Y%m%dT%H%M%SZ}-test1234.dump"
    dump = folder / name
    dump.write_bytes(b"simulated custom-format backup for checks")
    dump.chmod(0o600)
    metadata = {
        "state": "complete",
        "file": name,
        "sha256": hashlib.sha256(dump.read_bytes()).hexdigest(),
        "bytes": dump.stat().st_size,
        "created_at": now.strftime("%Y%m%dT%H%M%SZ"),
    }
    manifest = folder / (name + ".json")
    manifest.write_text(json.dumps(metadata))
    manifest.chmod(0o600)
    return dump, manifest


def daily(root, day="2026-10-14"):
    seal(
        root / "etf-shadow/calendar/2026-10.json",
        {
            "sessions": [{"date": f"2026-10-{n}"} for n in [14, 15, 16, 19]],
        },
    )
    seal(root / "etf-shadow/daily" / (day + ".json"), {"date": day})


@pytest.mark.parametrize(
    "free,total,expected",
    [
        (30, 200, "ok"),
        (12, 100, "warning"),
        (6, 100, "warning"),
        (4, 50, "critical"),
        (40, 1000, "critical"),
    ],
)
def test_disk_accounts_for_absolute_and_fractional_free_space(free, total, expected):
    assert disk_check(usage(free, total))["severity"] == expected


def test_backup_staleness_checksum_and_permissions_are_enforced(tmp_path):
    folder = tmp_path / "backups"
    reader = BackupCheck(folder)
    assert reader.check(NOW)["state"] == "missing"
    dump, manifest = backup(folder)
    assert reader.check(NOW)["state"] == "verified"
    assert reader.check(NOW + timedelta(hours=30))["severity"] == "ok"
    assert reader.check(NOW + timedelta(hours=30, seconds=1))["state"] == "stale"
    manifest.chmod(0o644)
    assert reader.check(NOW)["state"] == "invalid"
    manifest.chmod(0o600)
    dump.write_bytes(b"x" * dump.stat().st_size)
    assert reader.check(NOW)["state"] == "invalid"


def test_new_invalid_backup_is_not_silently_replaced_by_an_old_one(tmp_path):
    folder = tmp_path / "backups"
    backup(folder)
    _, latest = backup(folder, NOW + timedelta(seconds=1))
    latest.write_text("{incomplete metadata")
    assert BackupCheck(folder).check(NOW + timedelta(seconds=2))["state"] == "invalid"


def test_daily_next_day_6am_grace_and_weekends(tmp_path):
    daily(tmp_path)
    shadow = {"state": "observing", "latest_completed_session": "2026-10-14"}
    assert daily_check(tmp_path, shadow, NOW)["state"] == "current"
    assert daily_check(tmp_path, shadow, NOW.replace(hour=10, minute=5))["state"] == "grace"
    assert daily_check(tmp_path, shadow, NOW.replace(hour=10, minute=21))["severity"] == "warning"
    seal(tmp_path / "etf-shadow/daily/2026-10-15.json", {"date": "2026-10-15"})
    shadow["latest_completed_session"] = "2026-10-15"
    assert daily_check(tmp_path, shadow, NOW.replace(hour=10, minute=21))["state"] == "current"
    seal(tmp_path / "etf-shadow/daily/2026-10-16.json", {"date": "2026-10-16"})
    shadow["latest_completed_session"] = "2026-10-16"
    monday = NOW.replace(day=19, hour=9)
    assert daily_check(tmp_path, shadow, monday)["expected_session"] == "2026-10-16"
    path = tmp_path / "etf-shadow/daily/2026-10-16.json"
    path.write_text(path.read_text().replace('"2026-10-16"', '"2026-10-15"'))
    assert daily_check(tmp_path, shadow, monday)["severity"] == "critical"


def test_daily_calendar_month_boundary_and_dst(tmp_path):
    seal(tmp_path / "etf-shadow/calendar/2026-10.json", {"sessions": [{"date": "2026-10-30"}]})
    seal(tmp_path / "etf-shadow/calendar/2026-11.json", {"sessions": [{"date": "2026-11-02"}]})
    seal(tmp_path / "etf-shadow/daily/2026-10-30.json", {"date": "2026-10-30"})
    shadow = {"state": "observing", "latest_completed_session": "2026-10-30"}
    # After DST ends: Nov 2 06:01 New York is 11:01 UTC.
    result = daily_check(tmp_path, shadow, datetime(2026, 11, 2, 11, 1, tzinfo=UTC))
    assert result["state"] == "current" and result["expected_session"] == "2026-10-30"


def test_entry_gate_requires_matching_policy_recent_heartbeat_and_permission():
    healthy = {
        "heartbeat": NOW.isoformat(),
        "policy_sha256": POLICY,
        "state": "healthy",
        "additions_allowed": True,
    }
    assert entry_allowed(healthy, NOW) and entry_allowed(healthy, NOW + timedelta(seconds=180))
    assert not entry_allowed(healthy, NOW + timedelta(seconds=181))
    assert not entry_allowed(healthy, NOW - timedelta(seconds=1))
    assert not entry_allowed({}, NOW)
    assert not entry_allowed({**healthy, "policy_sha256": "foreign"}, NOW)
    assert not entry_allowed({**healthy, "state": "blocked"}, NOW)


async def test_incident_and_recovery_are_atomic_deduplicated_and_do_not_reset_risk(store, settings, tmp_path):
    folder = tmp_path / "backups"
    backup(folder)
    daily(settings.data_dir)
    await store.put("engine_status", {"state": "paper_auto_waiting_market", "heartbeat": NOW.isoformat()})
    await store.put("etf_account", {"state": "monitoring", "last_success_at": NOW.isoformat()})
    await store.put("etf_shadow", {"state": "observing", "latest_completed_session": "2026-10-14"})
    await store.put("manual_pause", True)
    await store.put("risk_sentinel", {"drawdown_latch": True})
    monitor = Monitor(settings, store, folder)
    assert (await monitor.tick(NOW, usage()))["state"] == "healthy"
    assert await store.all("SELECT * FROM alerts") == []
    blocked = await monitor.tick(NOW, usage(4, 50))
    assert blocked["state"] == "blocked" and not blocked["additions_allowed"]
    await Monitor(settings, store, folder).tick(NOW, usage(4, 50))
    assert len(await store.all("SELECT * FROM alerts")) == 1
    recovered = await monitor.tick(NOW, usage())
    assert recovered["state"] == "healthy" and recovered["broker_requests"] == 0
    assert len(await store.all("SELECT * FROM alerts")) == 2
    assert await store.get("manual_pause") is True
    assert (await store.get("risk_sentinel"))["drawdown_latch"]
    assert (await store.get(KEY))["transition"] == 3
    stale = await monitor.tick(NOW + timedelta(minutes=21), usage())
    assert stale["checks"]["account"]["severity"] == "critical"
    assert stale["checks"]["engine"]["severity"] == "critical"
    await store.put("engine_status", {"state": "paper_auto_waiting_market", "heartbeat": NOW.isoformat()})
    await store.put(
        "etf_account", {"state": "monitoring", "last_success_at": NOW.isoformat(), "reconciled": False}
    )
    unowned = await monitor.tick(NOW, usage())
    assert unowned["checks"]["account"] == {"severity": "critical", "state": "ownership_unverified"}
    assert not unowned["additions_allowed"]


async def test_two_notification_senders_do_not_post_the_same_pending_alert(store, settings):
    configured = settings.model_copy(
        update={"discord_webhook": SecretStr("https://discord.com/api/webhooks/test/test")}
    )
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def handle(request):
        calls.append(request)
        entered.set()
        await release.wait()
        return httpx.Response(200, json={"id": "test"})

    a = Notifier(configured, store, transport=httpx.MockTransport(handle))
    b = Notifier(configured, store, transport=httpx.MockTransport(handle))
    await store.alert("test-concurrent", "모의 운영 장애")
    task = asyncio.create_task(a.flush())
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        await asyncio.wait_for(b.flush(), timeout=5)
        assert len(calls) == 1
        release.set()
        await task
        await b.flush()
        assert len(calls) == 1
        assert (await store.one("SELECT state FROM alerts WHERE key='test-concurrent'"))["state"] == "sent"
    finally:
        release.set()
        await task
        await a.close()
        await b.close()
