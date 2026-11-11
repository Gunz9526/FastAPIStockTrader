import copy
import json
from datetime import UTC, date, datetime

import pandas as pd
import pytest
from pydantic import SecretStr

from stock_trader.clock import NEW_YORK
from stock_trader.etf_shadow import (
    CONTRACT,
    START,
    DailyReader,
    Shadow,
    eligible_through,
    fingerprint,
    read_sealed,
    seal,
    validate_bars,
)
from stock_trader.etf_study import SYMBOLS
from tests.test_observer import MemoryStore, configured


class Reader:
    def __init__(self):
        self.calls = []
        self.missing = False

    async def calendar(self, start, end):
        self.calls.append(("calendar", start, end))
        return [
            {
                "date": d.strftime("%Y-%m-%d"),
                "open": "09:30",
                "close": "13:00" if d.strftime("%Y-%m-%d") == "2026-11-27" else "16:00",
            }
            for d in pd.bdate_range(start, end)
            if d.strftime("%Y-%m-%d") not in ("2026-11-26",)
        ]

    async def daily(self, day):
        self.calls.append(("daily", day.isoformat()))
        assert day >= START
        t = datetime.combine(day, datetime.min.time(), NEW_YORK).astimezone(UTC).isoformat()
        return {
            "bars": {
                s: [{"t": t, "o": 100, "h": 101, "l": 99, "c": 100, "v": 1000}]
                for s in SYMBOLS
                if not (self.missing and s == "BIL")
            }
        }


def dt(value):
    return datetime.fromisoformat(value)


@pytest.mark.parametrize(
    "day,expected_start,expected_end",
    [
        (date(2026, 10, 5), "2026-10-05T04:00:00+00:00", "2026-10-06T03:59:59.999999+00:00"),
        (date(2026, 11, 1), "2026-11-01T04:00:00+00:00", "2026-11-02T04:59:59.999999+00:00"),
    ],
)
async def test_real_reader_excludes_inclusive_next_midnight(day, expected_start, expected_end):
    class Client:
        async def request(self, method, path, *, data_api, params):
            assert method == "GET" and path == "/v2/stocks/bars" and data_api
            assert params["start"] == expected_start and params["end"] == expected_end
            assert params["feed"] == "sip" and params["adjustment"] == "raw"
            # An inclusive endpoint returns two daily bars when end is next midnight.
            start = dt(params["start"]).astimezone(NEW_YORK)
            end = dt(params["end"]).astimezone(NEW_YORK)
            assert start.date() == end.date() == day
            return await Reader().daily(day)

    reader = DailyReader.__new__(DailyReader)
    reader._client = Client()
    validate_bars(await reader.daily(day), {"date": day.isoformat()})


@pytest.mark.parametrize(
    "now,expected",
    [
        ("2026-10-08T09:59:59+00:00", "2026-10-06"),
        ("2026-10-08T10:00:00+00:00", "2026-10-07"),
        ("2026-11-02T10:59:59+00:00", "2026-10-31"),
        ("2026-11-02T11:00:00+00:00", "2026-11-01"),
    ],
)
def test_new_york_6am_and_dst_boundary(now, expected):
    assert eligible_through(dt(now)).isoformat() == expected


async def test_restart_uses_immutable_cache_zero_requests_and_initial_is_forward(settings):
    settings = configured(settings)
    store = MemoryStore()
    reader = Reader()
    now = dt("2026-10-08T16:00:00+00:00")
    worker = Shadow(settings, store, reader)
    status = await worker.tick(now)
    assert status["sessions"] == 4
    assert status["latest_completed_session"] == "2026-10-07"
    assert status["prospective_from"] == "2026-10-09" and status["prospective_sessions"] == 0
    assert status["initial_target"]["execution_session"] == "2026-10-09"
    assert status["initial_target"]["timely"] and status["orders_submitted"] == 0
    assert len(reader.calls) == 6  # two calendar months, four completed sessions
    fresh = Reader()
    again = await Shadow(settings, store, fresh).tick(now)
    assert not fresh.calls and again["added_sessions"] == 0
    assert again["initial_target"] == status["initial_target"]
    assert again["api_requests_this_tick"] == 0


async def test_increment_fetches_only_new_complete_session(settings):
    settings = configured(settings)
    store = MemoryStore()
    await Shadow(settings, store, Reader()).tick(dt("2026-10-08T16:00:00+00:00"))
    reader = Reader()
    status = await Shadow(settings, store, reader).tick(dt("2026-10-09T16:00:00+00:00"))
    assert reader.calls == [("daily", "2026-10-08")]
    assert status["sessions"] == 5 and status["prospective_sessions"] == 0
    status = await Shadow(settings, store, Reader()).tick(dt("2026-10-10T16:00:00+00:00"))
    assert status["prospective_sessions"] == 1


async def test_missing_symbol_never_commits_partial_file_and_retries(settings):
    settings = configured(settings)
    reader = Reader()
    reader.missing = True
    worker = Shadow(settings, MemoryStore(), reader)
    with pytest.raises(ValueError, match="symbols"):
        await worker.tick(dt("2026-10-08T16:00:00+00:00"))
    assert not list((worker.directory / "daily").glob("*.json"))
    reader.missing = False
    status = await worker.tick(dt("2026-10-08T16:00:00+00:00"))
    assert status["sessions"] == 4


async def test_changed_cache_rejected_without_refetch_or_overwrite(settings):
    worker = Shadow(configured(settings), MemoryStore(), Reader())
    now = dt("2026-10-08T16:00:00+00:00")
    await worker.tick(now)
    path = worker.directory / "daily" / "2026-10-02.json"
    value = json.loads(path.read_text())
    value["payload"]["bars"]["SPY"][0]["c"] = 100.1
    path.write_text(json.dumps(value))
    corrupted = path.read_bytes()
    worker.reader.calls.clear()
    with pytest.raises(ValueError, match="checksum"):
        await worker.tick(now)
    assert not worker.reader.calls and path.read_bytes() == corrupted


async def test_calendar_missing_duplicate_and_future_daily_rejected(settings):
    worker = Shadow(configured(settings), MemoryStore(), Reader())
    for day in ("2026-10-01", "2026-10-08"):
        with pytest.raises(ValueError, match="unfinished"):
            await worker.ensure_day(
                {"date": day, "open": "09:30", "close": "16:00"}, dt("2026-10-08T16:00:00+00:00")
            )
    assert not worker.reader.calls
    valid = await Reader().daily(date(2026, 10, 2))
    for field, value in (("h", 98), ("o", float("nan")), ("v", 0), ("t", "2026-10-03T04:00:00Z")):
        changed = copy.deepcopy(valid)
        changed["bars"]["SPY"][0][field] = value
        with pytest.raises(ValueError):
            validate_bars(changed, {"date": "2026-10-02"})


async def test_monthly_decision_uses_completed_month_only_and_late_not_forward(settings):
    settings = configured(settings)
    store = MemoryStore()
    worker = Shadow(settings, store, Reader())
    await worker.tick(dt("2026-10-08T16:00:00+00:00"))
    # Tick limit catches up without unbounded calls.
    now = dt("2026-10-31T12:00:00+00:00")
    first = await worker.tick(now)
    assert first["state"] == "catching_up" and first["added_sessions"] == 10
    status = await worker.tick(now)
    assert status["monthly_decisions"] == 1
    path = worker.directory / "decisions" / "2026-11-02-monthly.json"
    decision = read_sealed(path)
    assert decision["signal_session"] == "2026-10-30" and decision["timely"]
    assert decision["intended_open"] == "2026-11-02T14:30:00+00:00"
    before = path.read_bytes()
    await worker.tick(dt("2026-11-03T16:00:00+00:00"))
    assert path.read_bytes() == before
    other = Shadow(
        settings.model_copy(update={"data_dir": settings.data_dir / "late"}), MemoryStore(), Reader()
    )
    for _ in range(3):
        await other.tick(dt("2026-11-03T16:00:00+00:00"))
    late = read_sealed(other.directory / "decisions" / "2026-11-02-monthly.json")
    assert not late["timely"] and not late["prospective"]


async def test_fetch_crossing_open_is_not_timely(settings, monkeypatch):
    worker = Shadow(configured(settings), MemoryStore(), Reader())
    worker.register(dt("2026-10-08T16:00:00+00:00"))
    for session in await worker.calendar_month(date(2026, 10, 1)):
        if session["date"] >= START.isoformat():
            await worker.ensure_day(session, dt("2026-10-31T12:00:00+00:00"))
    before = dt("2026-11-02T14:29:59+00:00")
    after = dt("2026-11-02T14:30:01+00:00")
    clock = iter((before, after))
    monkeypatch.setattr("stock_trader.etf_shadow.utcnow", lambda: next(clock))
    status = await worker.tick()
    decision = read_sealed(worker.directory / "decisions" / "2026-11-02-monthly.json")
    assert not decision["timely"] and not decision["prospective"]
    assert decision["recorded_at"] == after.isoformat()
    assert status["last_success_at"] == after.isoformat()


async def test_missing_keys_never_registers_or_queries(settings):
    settings = settings.model_copy(update={"alpaca_key": SecretStr(""), "alpaca_secret": SecretStr("")})
    worker = Shadow(settings, MemoryStore(), object())
    await worker.tick(dt("2026-10-08T16:00:00+00:00"))
    assert not worker.directory.exists()


def test_seal_idempotency_partial_files_and_tamper(tmp_path):
    path = tmp_path / "data.json"
    seal(path, CONTRACT)
    before = path.read_bytes()
    seal(path, CONTRACT)
    assert path.read_bytes() == before
    with pytest.raises(ValueError, match="immutable"):
        seal(path, {})
    assert read_sealed(path) == CONTRACT
    assert not list(tmp_path.glob("*.tmp"))
    assert json.loads(path.read_text())["sha256"] == fingerprint(CONTRACT)


async def test_pg_leader_excludes_duplicate_worker(store):
    async with store.leader("etf-shadow-0053"):
        with pytest.raises(RuntimeError, match="another engine"):
            async with store.leader("etf-shadow-0053"):
                raise AssertionError("duplicate shadow acquired lock")
    async with store.leader("etf-shadow-0053") as leader:
        await leader.execute("SELECT 1")
