import copy
import json
import subprocess
import sys
from datetime import UTC, date, datetime, time, timedelta

import httpx
import pandas as pd
import pytest
from pydantic import SecretStr

from stock_trader.api import create_app
from stock_trader.broker import BrokerError
from stock_trader.clock import NEW_YORK
from stock_trader.equity_contract import (
    CONTRACT,
    EQUITIES,
    FIRST,
    KEY,
    LEADER,
    REGISTRY,
    SYMBOLS,
    rank_targets,
    shift,
)
from stock_trader.equity_shadow import (
    EquityShadow,
    ReadOnlyAlpaca,
    cohort_from_assets,
    eligibility,
)
from stock_trader.research_seal import fingerprint, read, seal


def dt(value):
    return datetime.fromisoformat(value)


async def fence():
    pass


class MemoryStore:
    def __init__(self):
        self.values = {}

    async def get(self, key, default=None):
        return copy.deepcopy(self.values.get(key, default))

    async def put(self, key, value):
        self.values[key] = copy.deepcopy(value)


def assets():
    return [
        {
            "id": "asset-" + symbol,
            "symbol": symbol,
            "status": "active",
            "tradable": True,
            "class": "us_equity",
            "exchange": "NASDAQ",
        }
        for symbol in EQUITIES
    ]


def month_rows(start, end):
    return [
        {
            "date": day.strftime("%Y-%m-%d"),
            "open": "09:30",
            "close": "13:00" if day.strftime("%Y-%m-%d") == "2026-11-27" else "16:00",
        }
        for day in pd.bdate_range(start, end)
        if day.strftime("%Y-%m-%d") not in {"2026-11-26", "2026-12-25", "2027-01-01"}
    ]


class Reader:
    def __init__(self):
        self.calls = []
        self.rows = assets()
        self.fail = None
        self.invalid = None
        self.on_request = None

    async def request(self, method, path, *, data_api, params):
        self.calls.append((method, path, copy.deepcopy(params)))
        assert method == "GET"
        if self.on_request:
            self.on_request()
        if path == self.fail:
            raise BrokerError(403, "DO-NOT-LOG-THIS")
        if path == "/v2/calendar":
            return month_rows(params["start"], params["end"])
        if path == "/v2/assets":
            return copy.deepcopy(self.rows)
        assert (
            path == "/v2/stocks/bars"
            and data_api
            and params["feed"] == "sip"
            and params["asof"] == "2026-10-18"
        )
        start, end = dt(params["start"]).astimezone(NEW_YORK), dt(params["end"]).astimezone(NEW_YORK)
        assert start.date() == end.date() and end.time() == time(23, 59, 59, 999999)
        close = 100 if start.year == 2025 else 120
        result = {
            "bars": {
                s: [
                    {
                        "t": start.astimezone(UTC).isoformat(),
                        "o": close,
                        "h": close + 1,
                        "l": close - 1,
                        "c": close,
                        "v": 1000,
                    }
                ]
                for s in SYMBOLS
            }
        }
        if self.invalid == "missing":
            result["bars"].pop("AAPL")
        elif self.invalid == "volume":
            result["bars"]["AAPL"][0]["v"] = 0
        elif self.invalid == "future":
            result["bars"]["AAPL"][0]["t"] = (start + timedelta(days=1)).isoformat()
        elif self.invalid == "page":
            result["next_page_token"] = "do-not-follow"
        elif self.invalid == "nonfinite":
            result["bars"]["AAPL"][0]["c"] = "NaN"
        return result


@pytest.fixture
def setup(settings):
    configured = settings.model_copy(
        update={"alpaca_key": SecretStr("test"), "alpaca_secret": SecretStr("test")}
    )
    source = configured.data_dir / "research/equity-access-0080/assets.json"
    seal(
        source,
        {
            "state": "received",
            "request": {"path": "/v2/assets", "params": {"asset_class": "us_equity"}},
            "observed_at": "2026-10-17T00:00:00+00:00",
            "response": assets(),
        },
    )
    for name in ("2026-10", "2026-11"):
        month = date.fromisoformat(name + "-01")
        last = shift(month, 1) - timedelta(days=1)
        seal(
            configured.data_dir / "etf-shadow/calendar" / (name + ".json"),
            {"month": month.isoformat(), "sessions": month_rows(month.isoformat(), last.isoformat())},
        )
    return configured, MemoryStore(), Reader()


async def warm(setup):
    settings, store, reader = setup
    worker = EquityShadow(settings, store, reader, fence)
    a = await worker.step(dt("2026-10-18T12:00:00+00:00"))
    assert a["state"] == "catching_up" and a["api_requests_this_tick"] == 4
    b = await worker.step(dt("2026-10-18T12:01:00+00:00"))
    assert b["state"] == "waiting_formation" and b["api_requests_this_tick"] == 2
    return worker


def test_fixed_price_signal_equal_slots_positive_cash_and_deterministic_ties():
    earlier, later = dict.fromkeys(SYMBOLS, "100"), dict.fromkeys(SYMBOLS, "110")
    result = rank_targets(earlier, later, dict.fromkeys(EQUITIES, True))
    assert result["selected"] == sorted(EQUITIES)[:5]
    assert all(value == "0.20" for value in result["target_weights"].values())
    later = dict.fromkeys(SYMBOLS, "90")
    later["NVDA"], later["AAPL"] = "120", "110"
    result = rank_targets(earlier, later, dict.fromkeys(EQUITIES, True))
    assert result["selected"] == ["NVDA", "AAPL"] and result["cash_weight"] == "0.60"
    assert shift(FIRST, -13) == date(2025, 10, 1) and shift(FIRST, -2) == date(2026, 9, 1)
    assert not CONTRACT["orders_enabled"] and CONTRACT["max_transport_attempts_per_tick"] == 16


async def test_bounded_preparation_reuses_calendar_and_restart_consumes_zero_requests(setup):
    worker = await warm(setup)
    before = len(worker.reader.calls)
    assert before == 6
    fresh = Reader()
    result = await EquityShadow(worker.settings, worker.store, fresh, fence).step(
        dt("2026-10-18T12:02:00+00:00")
    )
    assert result["state"] == "waiting_formation" and result["warmup_complete"]
    assert result["api_requests_this_tick"] == 0 and fresh.calls == []
    assert result["anchor_dates"] == ["2025-10-31", "2026-09-30"]
    assert not list((worker.root / "targets").glob("*.json"))
    assert len((await worker.store.get(REGISTRY))["cohort"]) == 28


async def test_real_formation_boundary_records_one_forward_target_and_is_immutable(setup):
    worker = await warm(setup)
    a = await worker.step(dt("2026-10-31T09:59:59+00:00"))
    assert a["state"] == "waiting_formation" and a["api_requests_this_tick"] == 0
    result = await worker.step(dt("2026-10-31T10:00:00+00:00"))
    assert result["state"] == "observing" and result["targets_recorded"] == 1
    target = read(worker.root / "targets/2026-11.json")
    assert target["selected"] == sorted(EQUITIES)[:5] and target["orders_submitted"] == 0
    assert target["intended_open"] == "2026-11-02T14:30:00+00:00"  # DST change
    assert len(worker.reader.calls) == 7
    fresh = Reader()
    again = await EquityShadow(worker.settings, worker.store, fresh, fence).step(
        dt("2026-10-31T10:01:00+00:00")
    )
    assert not fresh.calls and again["latest_target"] == target


@pytest.mark.parametrize("fault", ["missing", "volume", "future", "page", "nonfinite"])
async def test_bad_anchor_is_sealed_as_evidence_and_never_refetched_or_ranked(setup, fault):
    settings, store, reader = setup
    reader.invalid = fault
    worker = EquityShadow(settings, store, reader, fence)
    first = await worker.step(dt("2026-10-18T12:00:00+00:00"))
    assert first["state"] == "review_required"
    used = len(reader.calls)
    second = await worker.step(dt("2026-10-18T12:01:00+00:00"))
    assert second["state"] == "review_required" and len(reader.calls) == used
    assert not list((worker.root / "targets").glob("*.json"))


async def test_http_denial_does_not_repeat_and_never_leaks_broker_error_body(setup):
    settings, store, reader = setup
    reader.fail = "/v2/stocks/bars"
    worker = EquityShadow(settings, store, reader, fence)
    value = await worker.step(dt("2026-10-18T12:00:00+00:00"))
    used = len(reader.calls)
    again = await worker.step(dt("2026-10-18T12:01:00+00:00"))
    assert value["state"] == again["state"] == "review_required"
    assert len(reader.calls) == used and "DO-NOT-LOG-THIS" not in json.dumps(store.values)


@pytest.mark.parametrize("fault", ["missing_id", "changed_symbol", "duplicate_id"])
async def test_identity_change_blocks_all_targets_without_substitution(setup, fault):
    worker = await warm(setup)
    if fault == "missing_id":
        worker.reader.rows.pop()
    elif fault == "changed_symbol":
        worker.reader.rows[0]["symbol"] = "NEWNAME"
    else:
        worker.reader.rows.append(copy.deepcopy(worker.reader.rows[0]))
    result = await worker.step(dt("2026-10-31T10:00:00+00:00"))
    assert result["state"] == "review_required" and not list((worker.root / "targets").glob("*.json"))


def test_inactive_member_keeps_identity_and_cash_slot_instead_of_replacement():
    rows = assets()
    cohort = cohort_from_assets(rows)
    rows[0].update(status="inactive", tradable=False)
    allowed, details = eligibility(rows, cohort)
    assert not allowed["AAPL"] and details["AAPL"]["id"] == cohort[0]["id"]


def test_actual_rest_class_field_is_normalized_without_sdk_alias_assumption():
    rows = assets()
    assert all("class" in row and "asset_class" not in row for row in rows)
    cohort = cohort_from_assets(rows)
    assert len(cohort) == 28 and all(row["asset_class"] == "us_equity" for row in cohort)
    assert all(eligibility(rows, cohort)[0].values())


def test_missing_raw_class_or_conflicting_alias_is_rejected():
    rows = assets()
    rows[0]["asset_class"] = "crypto"
    with pytest.raises(ValueError, match="schema"):
        cohort_from_assets(rows)
    rows[0].pop("class")
    rows[0]["asset_class"] = "us_equity"
    with pytest.raises(ValueError, match="schema"):
        cohort_from_assets(rows)


async def test_missing_actual_window_is_recorded_without_backfilled_signals_or_prices(setup):
    worker = await warm(setup)
    used = len(worker.reader.calls)
    # The first month's opening has passed; later-month preparation can proceed.
    await worker.step(dt("2026-11-02T14:30:00+00:00"))
    missed = read(worker.root / "targets/2026-11.json")
    assert missed["state"] == "missed" and "selected" not in missed
    assert not any(path == "/v2/assets" for _, path, _ in worker.reader.calls[used:])


async def test_registration_source_changes_or_loss_cannot_silently_reenroll(setup):
    worker = await warm(setup)
    registration = await worker.store.get(REGISTRY)
    worker.store.values[REGISTRY]["symbol_asof"] = "2026-10-19"
    result = await worker.step(dt("2026-10-18T12:02:00+00:00"))
    assert result["state"] == "review_required"
    worker.store.values[REGISTRY] = registration
    (worker.root / "registration.json").unlink()
    result = await worker.step(dt("2026-10-18T12:02:00+00:00"))
    assert result["reason"] == "registration_file_lost"


async def test_lost_leader_after_http_cannot_commit_response(setup):
    settings, store, reader = setup
    lost = False

    def mark_lost():
        nonlocal lost
        lost = True

    async def checked():
        if lost:
            raise RuntimeError("leader connection lost")

    reader.on_request = mark_lost
    worker = EquityShadow(settings, store, reader, checked)
    with pytest.raises(RuntimeError, match="leader"):
        await worker.step(dt("2026-10-18T12:00:00+00:00"))
    assert not list((worker.root / "responses").rglob("*.json"))
    assert len(reader.calls) == 1


async def test_changed_registered_source_and_completed_anchor_are_rejected(setup, monkeypatch):
    worker = await warm(setup)
    import stock_trader.equity_shadow as module

    actual = module.sources()
    monkeypatch.setattr(module, "sources", lambda: {**actual, "equity_shadow.py": "changed"})
    result = await worker.step(dt("2026-10-18T12:02:00+00:00"))
    assert result["state"] == "review_required" and result["reason"] == "registration_inputs_changed"
    monkeypatch.setattr(module, "sources", lambda: actual)
    path = next((worker.root / "cycles/2026-11").glob("*-split.json"))
    raw = path.read_text()
    path.write_text(raw.replace('"c": 100', '"c": 101'))
    result = await worker.step(dt("2026-10-18T12:02:00+00:00"))
    assert result["state"] == "review_required"


async def test_missing_keys_never_initializes_collection_or_broker_call(setup):
    settings, store, reader = setup
    settings = settings.model_copy(update={"alpaca_key": SecretStr("")})
    result = await EquityShadow(settings, store, reader, fence).step(dt("2026-10-18T12:00:00+00:00"))
    assert result["state"] == "waiting_keys" and not reader.calls


@pytest.mark.parametrize(
    "method,path,data_api",
    [
        ("POST", "/v2/orders", False),
        ("DELETE", "/v2/orders/x", False),
        ("GET", "/v2/account", False),
        ("GET", "/v2/calendar", True),
        ("GET", "/v2/stocks/bars", False),
    ],
)
async def test_read_only_transport_rejects_writes_and_unapproved_gets(settings, method, path, data_api):
    calls = []

    async def handle(request):
        calls.append(request)
        return httpx.Response(200, json=[])

    client = ReadOnlyAlpaca(settings, transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(ValueError, match="selected GET"):
            await client.request(method, path, data_api=data_api)
        assert calls == []
    finally:
        await client.close()


async def test_duplicate_pg_worker_is_excluded(store):
    async with store.leader(LEADER):
        with pytest.raises(RuntimeError):
            async with store.leader(LEADER):
                pass


async def test_api_research_status_is_authenticated_and_reads_no_broker(store, settings):
    value = {"state": "waiting_formation", "orders_submitted": 0, "performance_calculated": False}
    await store.put(KEY, value)
    configured = settings.model_copy(update={"api_token": SecretStr("research-test-token")})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(configured, store)), base_url="http://test"
    ) as client:
        assert (await client.get("/research/equity")).status_code == 401
        response = await client.get(
            "/research/equity", headers={"Authorization": "Bearer research-test-token"}
        )
        assert response.status_code == 200 and response.json() == value
        assert response.headers["Cache-Control"] == "no-store"


def test_new_research_policy_import_does_not_load_scientific_stack():
    script = "import sys; import stock_trader.equity_contract; assert not {'numpy','pandas','stock_trader.etf_study','stock_trader.modeling'}.intersection(sys.modules)"
    subprocess.run([sys.executable, "-c", script], check=True, timeout=10)


async def test_next_calendar_month_prepares_new_inputs_but_preserves_prior_target(setup):
    worker = await warm(setup)
    await worker.step(dt("2026-10-31T10:00:00+00:00"))
    path = worker.root / "targets/2026-11.json"
    before = path.read_bytes()
    reader = Reader()
    result = await EquityShadow(worker.settings, worker.store, reader, fence).step(
        dt("2026-11-01T12:00:00+00:00")
    )
    assert result["api_requests_this_tick"] <= 4 and path.read_bytes() == before
    assert all(call[1] != "/v2/assets" for call in reader.calls)


async def test_tampered_target_time_is_not_accepted_as_a_timely_decision(setup):
    worker = await warm(setup)
    await worker.step(dt("2026-10-31T10:00:00+00:00"))
    path = worker.root / "targets/2026-11.json"
    value = read(path)
    value["recorded_at"] = value["intended_open"]
    path.write_text(json.dumps({"payload": value, "sha256": fingerprint(value)}))
    result = await worker.step(dt("2026-11-03T12:00:00+00:00"))
    assert result["state"] == "review_required" and result["reason"] == "late_recorded_target"


async def test_inherited_submit_is_blocked_before_transport(settings):
    calls = []

    async def handle(request):
        calls.append(request)
        return httpx.Response(200, json={})

    client = ReadOnlyAlpaca(settings, transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(ValueError):
            await client.submit({"symbol": "SPY", "qty": "1", "side": "buy"})
        with pytest.raises(ValueError):
            await client.request("GET", "/v2/calendar", payload={"anything": True})
        assert calls == []
    finally:
        await client.close()
