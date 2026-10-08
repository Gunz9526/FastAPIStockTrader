import json
from datetime import timedelta

import pandas as pd
import pytest
from pydantic import SecretStr

from stock_trader.observer import (
    Observer,
    append_observation,
    compare_bars,
    next_poll_delay,
    quote_observation,
)


class MemoryStore:
    def __init__(self):
        self.values = {}

    async def get(self, key, default=None):
        return self.values.get(key, default)

    async def put(self, key, value):
        self.values[key] = value


def configured(settings):
    return settings.model_copy(
        update={"alpaca_key": SecretStr("test-key"), "alpaca_secret": SecretStr("test-secret")}
    )


def bar(timestamp, volume=50):
    return {"t": timestamp, "o": 100, "h": 101, "l": 99, "c": 100.5, "v": volume}


def test_quote_quality_separates_stale_invalid_and_future():
    now = pd.Timestamp("2026-06-15T16:04:40Z").to_pydatetime()
    assert quote_observation("AAPL", {"bp": 100, "ap": 100.1, "t": now.isoformat()}, now)["state"] == "fresh"
    assert (
        quote_observation(
            "AAPL", {"bp": 100, "ap": 100.1, "t": (now - timedelta(seconds=31)).isoformat()}, now
        )["state"]
        == "stale"
    )
    assert (
        quote_observation("AAPL", {"bp": "nan", "ap": 100.1, "t": now.isoformat()}, now)["state"] == "invalid"
    )
    assert (
        quote_observation(
            "AAPL", {"bp": 100, "ap": 100.1, "t": (now + timedelta(seconds=1)).isoformat()}, now
        )["state"]
        == "future"
    )
    assert quote_observation("AAPL", None, now)["state"] == "missing"


def test_bar_observation_excludes_unconfirmed_and_early_close():
    now = pd.Timestamp("2026-06-15T16:04:40Z").to_pydatetime()
    opening, closing = (
        pd.Timestamp("2026-06-15T13:30Z").to_pydatetime(),
        pd.Timestamp("2026-06-15T17:00Z").to_pydatetime(),
    )
    minutes = [bar("2026-06-15T15:55Z"), bar("2026-06-15T15:56Z"), bar("2026-06-15T15:59Z")]
    native = [bar("2026-06-15T15:55Z", 150), bar("2026-06-15T16:00Z", 200), bar("2026-06-15T17:05Z")]
    seen = {}
    value = compare_bars("AAPL", minutes, native, now, opening, closing, seen, True)
    assert value["bar_start"] == "2026-06-15T15:55:00+00:00"
    assert value["observed_minutes"] == 3
    assert all(v == 0 for v in value["aggregate_minus_native"].values())
    later = compare_bars(
        "AAPL", minutes, native[:1], now + timedelta(minutes=1), opening, closing, seen, False
    )
    assert later["first_seen_delay_seconds"] == value["first_seen_delay_seconds"]
    assert later["cold_start"]


async def test_closed_session_collects_no_quotes_or_bars(settings):
    class Broker:
        async def clock(self):
            return {"is_open": False, "next_open": "2026-10-05T13:30:00Z"}

    store = MemoryStore()
    observer = Observer(configured(settings), store, Broker())
    await observer.tick(pd.Timestamp("2026-10-03T16:00Z").to_pydatetime())
    status = store.values["market_observation"]
    assert status["state"] == "market_closed"
    assert status["samples"] == status["orders_submitted"] == 0
    assert not (settings.data_dir / "observations").exists()


async def test_read_only_observation_persists_first_seen_across_restart(settings):
    now = pd.Timestamp("2026-06-15T16:04:40Z").to_pydatetime()

    class Broker:
        async def clock(self):
            return {"is_open": True}

        async def calendar(self, day, end):
            return [{"date": day, "open": "09:30", "close": "16:00"}]

        async def quotes(self, symbols):
            return {"AAPL": {"bp": 100, "ap": 100.1, "t": now.isoformat()}}

        async def bars(self, symbols, start, end, timeframe):
            if timeframe == "1Min":
                yield {"AAPL": [bar("2026-06-15T15:55Z"), bar("2026-06-15T15:56Z"), bar("2026-06-15T15:59Z")]}
            else:
                yield {"AAPL": [bar("2026-06-15T15:55Z", 150)]}

    store = MemoryStore()
    await Observer(configured(settings), store, Broker()).tick(now)
    first = store.values["market_observation"]["bars"][0]["first_seen_delay_seconds"]
    await Observer(configured(settings), store, Broker()).tick(now + timedelta(minutes=1))
    status = store.values["market_observation"]
    assert status["samples"] == 2
    assert status["incomplete_minute_buckets"] == 1
    assert status["bars"][0]["first_seen_delay_seconds"] == first
    assert status["orders_submitted"] == 0
    lines = (settings.data_dir / "observations" / "market-2026-06-15.jsonl").read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[1])["samples"] == 2


def test_retention_preserves_other_data_and_latest(tmp_path):
    old = tmp_path / "market-2026-01-01.jsonl"
    old.write_text("old")
    unrelated = tmp_path / "notes.jsonl"
    unrelated.write_text("keep")
    append_observation(tmp_path, "2026-06-15", {"samples": 1})
    assert not old.exists()
    assert unrelated.read_text() == "keep"
    assert (tmp_path / "market-2026-06-15.jsonl").exists()


async def test_missing_credentials_make_no_api_requests(settings):
    settings = settings.model_copy(update={"alpaca_key": SecretStr(""), "alpaca_secret": SecretStr("")})
    store = MemoryStore()
    await Observer(settings, store, object()).tick()
    assert store.values["market_observation"]["state"] == "waiting_alpaca_keys"


@pytest.mark.parametrize("future", [False, True])
async def test_quote_age_uses_response_receipt_and_bar_delay_uses_bar_receipt(settings, monkeypatch, future):
    started = pd.Timestamp("2026-06-15T16:04:40Z").to_pydatetime()
    times = iter(
        [
            started,
            started + timedelta(seconds=1),
            started + timedelta(seconds=8),
            started + timedelta(seconds=10),
        ]
    )
    monkeypatch.setattr("stock_trader.observer.utcnow", lambda: next(times))

    class Broker:
        async def clock(self):
            return {"is_open": True}

        async def calendar(self, day, end):
            return [{"date": day, "open": "09:30", "close": "16:00"}]

        async def quotes(self, symbols):
            # This update occurs during the request; comparing with request start is incorrect.
            quote_time = started + timedelta(seconds=2 if future else 0.8)
            return {"AAPL": {"bp": 100, "ap": 100.1, "t": quote_time.isoformat()}}

        async def bars(self, symbols, start, end, timeframe):
            yield {"AAPL": [bar("2026-06-15T15:55Z")]}

    store = MemoryStore()
    await Observer(configured(settings), store, Broker()).tick()
    snapshot = store.values["market_observation"]
    quote = next(q for q in snapshot["quotes"] if q["symbol"] == "AAPL")
    assert quote["state"] == ("future" if future else "fresh")
    assert quote["age_seconds"] == (-1 if future else pytest.approx(0.2))
    assert snapshot["bars"][0]["first_seen_delay_seconds"] == 288
    assert (
        snapshot["poll_started_at"]
        < snapshot["quotes_received_at"]
        < snapshot["bars_received_at"]
        < snapshot["updated_at"]
    )
    assert snapshot["schema_version"] == 2 and snapshot["orders_submitted"] == 0


@pytest.mark.parametrize("elapsed, expected", [(0, 60), (12, 48), (60, 60), (90, 60)])
def test_poll_cadence_counts_work_and_never_catches_up_in_bursts(elapsed, expected):
    assert next_poll_delay(elapsed) == expected
