from datetime import UTC, datetime, timedelta

import pytest

from stock_trader.etf_funding import REGISTRY
from stock_trader.etf_ledger import fingerprint
from stock_trader.etf_shadow import seal, session_times
from stock_trader.health import SHA
from stock_trader.operational_study import CONTRACT, PREFIX, REGISTRATION, OperationalStudy, quality

BEFORE = datetime(2026, 10, 17, 4, tzinfo=UTC)
SESSION = {"date": "2026-10-19", "open": "09:30", "close": "16:00"}


def health(now, state="healthy"):
    return {
        "contract_sha256": SHA,
        "observed_at": now.isoformat(),
        "state": state,
        "issues": [] if state == "healthy" else ["test_issue"],
    }


def complete(session=SESSION):
    opening, closing = session_times(session)
    return {
        "session_sha256": fingerprint(session),
        "samples": {
            str(n): {
                "observed_at": (opening + timedelta(minutes=n, seconds=5)).isoformat(),
                "state": "healthy",
                "issues": [],
            }
            for n in range(int((closing - opening).total_seconds() / 60))
        },
    }


async def setup(store, settings, sessions=None):
    await store.put(REGISTRY, {"paper": True, "capital_limit": "10000", "version": 72})
    months = {"2026-10": []}
    for session in sessions or [SESSION]:
        months.setdefault(session["date"][:7], []).append(session)
    for month, rows in months.items():
        seal(settings.data_dir / f"etf-shadow/calendar/{month}.json", {"sessions": rows})
    study = OperationalStudy(settings, store)
    assert (await study.tick(health(BEFORE), BEFORE))["state"] == "not_started"
    return study


def test_quality_uses_actual_session_length_dst_and_missing_minutes():
    record = complete()
    assert quality(record, SESSION)["observed_minutes"] == 390
    for n in range(100, 106):
        del record["samples"][str(n)]
    result = quality(record, SESSION)
    assert (
        result["coverage"] > 0.9
        and result["longest_gap_minutes"] == 6
        and not result["initial_quality_passed"]
    )
    early = {"date": "2026-11-27", "open": "09:30", "close": "13:00"}
    assert session_times(early)[0].hour == 14
    assert quality(complete(early), early)["expected_minutes"] == 210


@pytest.mark.parametrize("defect", ["critical", "timestamp", "calendar", "coverage"])
def test_quality_never_counts_critical_missing_or_relabelled_samples_as_clean(defect):
    record = complete()
    if defect == "critical":
        record["samples"]["1"]["state"] = "blocked"
        assert not quality(record, SESSION)["initial_quality_passed"]
    elif defect == "coverage":
        record["samples"] = {k: v for k, v in record["samples"].items() if int(k) % 10 == 0}
        assert not quality(record, SESSION)["initial_quality_passed"]
    else:
        if defect == "timestamp":
            record["samples"]["1"]["observed_at"] = record["samples"]["0"]["observed_at"]
        else:
            record["session_sha256"] = "changed"
        with pytest.raises(ValueError):
            quality(record, SESSION)


async def test_registration_is_future_only_and_does_not_rewrite_policy(store, settings):
    study = await setup(store, settings)
    registration = await store.get(REGISTRATION)
    await store.put("risk_sentinel", {"drawdown_latch": True})
    assert (await study.tick(health(BEFORE), BEFORE))["observed_sessions"] == 0
    assert await store.get(REGISTRATION) == registration
    assert await store.get("risk_sentinel") == {"drawdown_latch": True}
    assert await store.all("SELECT * FROM kv WHERE key LIKE %s", (PREFIX + "%",)) == []


async def test_real_minute_only_first_sample_survives_restart_and_absence_remains_absent(store, settings):
    study = await setup(store, settings)
    opening, closing = session_times(SESSION)
    first = opening + timedelta(seconds=5)
    await study.tick(health(first), first)
    second = opening + timedelta(seconds=35)
    await OperationalStudy(settings, store).tick(health(second, "blocked"), second)
    saved = await store.get(PREFIX + SESSION["date"])
    assert len(saved["samples"]) == 1 and saved["samples"]["0"]["state"] == "healthy"
    outside = closing + timedelta(seconds=1)
    result = await study.tick(health(outside), outside)
    assert result["window"][0]["observed_minutes"] == 1 and result["window"][0]["longest_gap_minutes"] == 389
    assert result["monthly_decisions_completed"] == 0 and result["state"] == "collecting"


async def test_late_registration_and_changed_registration_require_review(store, settings):
    await store.put(REGISTRY, {"paper": True, "capital_limit": "10000", "version": 72})
    now = session_times(SESSION)[0]
    assert (await OperationalStudy(settings, store).tick(health(now), now))["reason"] == "late_registration"
    study = await setup(store, settings)
    registration = await store.get(REGISTRATION)
    registration["contract"] = {"changed": True}
    await store.put(REGISTRATION, registration)
    assert (await study.tick(health(now), now))["state"] == "review_required"


async def test_twenty_closed_sessions_and_real_monthly_decision_are_both_required(store, settings):
    sessions = []
    day = datetime(2026, 10, 19)
    while len(sessions) < 20:
        if day.weekday() < 5:
            sessions.append({"date": day.date().isoformat(), "open": "09:30", "close": "16:00"})
        day += timedelta(days=1)
    study = await setup(store, settings, sessions)
    for session in sessions:
        await store.put(PREFIX + session["date"], complete(session))
    now = session_times(sessions[-1])[1] + timedelta(minutes=1)
    assert (await study.tick(health(now), now))["state"] == "collecting"
    await store.put(
        "etf-auto-cycle:paper-auto-monthly-0068-2026-11",
        {"cycle": "paper-auto-monthly-0068-2026-11", "state": "budget_exhausted"},
    )
    result = await study.tick(health(now), now)
    assert result["state"] == "review_ready" and result["monthly_decisions_completed"] == 1
    assert not result["performance_approved"] and not CONTRACT["automatic_promotion"]
    record = await store.get(PREFIX + sessions[-1]["date"])
    record["samples"]["20"]["state"] = "blocked"
    await store.put(PREFIX + sessions[-1]["date"], record)
    assert (await study.tick(health(now), now))["state"] == "collecting"


async def test_missing_calendar_or_changed_health_never_produces_review_ready(store, settings):
    study = await setup(store, settings)
    now = datetime(2026, 11, 2, 15, tzinfo=UTC)
    assert (await study.tick(health(now), now))["state"] == "waiting_calendar"
    now = session_times(SESSION)[0]
    wrong = health(now)
    wrong["contract_sha256"] = "changed"
    assert (await study.tick(wrong, now))["state"] == "review_required"
