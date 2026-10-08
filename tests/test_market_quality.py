import copy
import json

import pytest

from stock_trader.market_quality import audit_completed_sessions, summarize_observations
from stock_trader.observer import Observer
from tests.test_observer import MemoryStore, configured


def observation(day="2026-10-05", minute=0, complete=False):
    return {
        "day": day,
        "updated_at": f"{day}T14:{minute:02d}:00Z",
        "feed": "iex",
        "orders_submitted": 0,
        "quotes": [{"symbol": "AAPL", "state": "future", "age_seconds": -1, "spread_bps": 1}],
        "bars": [
            {
                "symbol": "AAPL",
                "bar_start": f"{day}T13:50:00Z",
                "observed_minutes": 5 if complete else 3,
                "native_close": 100,
                "aggregate_minus_native": {k: 0 for k in ("open", "high", "low", "close", "volume")},
                "first_seen_delay_seconds": 40,
                "cold_start": False,
            }
        ],
    }


def save(settings, rows, day="2026-10-05"):
    directory = settings.data_dir / "observations"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"market-{day}.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return path


def test_unique_bar_completeness_revision_and_repeats_are_distinct():
    rows = [observation(minute=0), observation(minute=1, complete=True), observation(minute=2, complete=True)]
    rows[-1]["bars"][0]["aggregate_minus_native"]["volume"] = 50
    summary = summarize_observations(rows, "2026-10-05")
    assert summary["bar_observations"] == 3 and summary["unique_symbol_bar_keys"] == 1
    assert summary["repeat_bar_observations"] == 2 and summary["unique_bars_ever_complete"] == 1
    assert summary["unique_comparison_signatures_changed"] == 1
    assert summary["unique_bars_price_mismatch"] == 0 and summary["unique_bars_volume_mismatch"] == 1
    assert summary["quote_future_lead_seconds"]["median"] == 1
    assert "legacy" in summary["quote_age_basis"] and summary["is_independent_trade_sample"] is False


def test_current_day_is_not_finalized_and_completed_audit_is_cached(settings):
    source = save(settings, [observation()])
    assert audit_completed_sessions(settings, "2026-10-05")["audited_sessions"] == 0
    first = audit_completed_sessions(settings, "2026-10-06")
    assert first["new_reports"] == 1 and first["state"] == "complete"
    directory = settings.report_dir / "market-quality-2026-10-05"
    original = (directory / "report.json").read_bytes()
    assert (directory / "observations.jsonl").read_bytes() == source.read_bytes()
    assert audit_completed_sessions(settings, "2026-10-06")["new_reports"] == 0
    source.write_text(source.read_text() + json.dumps(observation(minute=1)) + "\n")
    changed = audit_completed_sessions(settings, "2026-10-06")
    assert changed["state"] == "failed" and changed["errors"][0]["error"] == "ValueError"
    assert (directory / "report.json").read_bytes() == original


def test_duplicate_keys_and_schema2_receipt_order_are_rejected():
    row = observation()
    row["bars"].append(copy.deepcopy(row["bars"][0]))
    with pytest.raises(ValueError, match="duplicate bar"):
        summarize_observations([row], "2026-10-05")
    row = observation()
    row.update(
        schema_version=2,
        poll_started_at="2026-10-05T14:00:00Z",
        quotes_received_at="2026-10-05T14:00:02Z",
        bars_received_at="2026-10-05T14:00:01Z",
        updated_at="2026-10-05T14:00:03Z",
    )
    with pytest.raises(ValueError, match="receipt"):
        summarize_observations([row], "2026-10-05")


async def test_automatic_audit_failure_does_not_interrupt_closed_observation(settings, monkeypatch):
    class Broker:
        async def clock(self):
            return {"is_open": False, "next_open": "2026-10-07T13:30:00Z"}

    def failed(*args):
        raise OSError("simulated report storage failure")

    monkeypatch.setattr("stock_trader.observer.audit_completed_sessions", failed)
    store = MemoryStore()
    await Observer(configured(settings), store, Broker()).tick()
    assert store.values["market_observation"]["state"] == "market_closed"
    assert store.values["market_quality"]["state"] == "failed"
    assert "storage failure" not in json.dumps(store.values)


async def test_closed_observer_automatically_creates_quality_report(settings):
    import pandas as pd

    save(settings, [observation()])

    class Broker:
        async def clock(self):
            return {"is_open": False, "next_open": "2026-10-07T13:30:00Z"}

    store = MemoryStore()
    await Observer(configured(settings), store, Broker()).tick(
        pd.Timestamp("2026-10-07T12:00Z").to_pydatetime()
    )
    assert store.values["market_quality"]["audited_sessions"] == 1
    assert store.values["market_quality"]["new_fits"] == 0
