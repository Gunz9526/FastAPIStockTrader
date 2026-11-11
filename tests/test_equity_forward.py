import copy
import hashlib
from datetime import UTC, datetime

import httpx
import pytest
from pydantic import SecretStr

from stock_trader.api import create_app
from stock_trader.broker import BrokerError
from stock_trader.equity_contract import ROOT, SYMBOLS
from stock_trader.equity_forward import (
    CONTRACT,
    KEY,
    REGISTRY,
    ForwardReader,
    ForwardStudy,
    minute_prices,
)
from stock_trader.equity_forward_ledger import advance, compare, initial, mark
from stock_trader.equity_forward_review import coverage, normalized_actions, target_weights
from stock_trader.equity_shadow import sources as target_sources
from stock_trader.etf_ledger import number
from stock_trader.forward_notices import NOTICE_KEY, publish, transition
from stock_trader.research_seal import fingerprint, read, seal


def action(**changes):
    return {
        "id": "div",
        "symbol": "AAPL",
        "kind": "cash_dividend",
        "ex_date": "2026-11-03",
        "pay_date": "2026-11-06",
        "rate": "2",
        "currency": "USD",
        "special": False,
        **changes,
    }


def funded():
    ledger = initial("1000")
    ledger["positions"] = {"AAPL": "5"}
    ledger["cash"] = "500"
    ledger["day"] = "2026-11-02"
    return ledger


def test_ex_date_entitlement_survives_sale_then_payment_exactly_once():
    before = funded()
    sold = advance(before, "2026-11-03", [action()], {"AAPL": "98"}, {}, {}, "10")
    assert sold["positions"]["AAPL"] == "0"
    assert sold["receivables"]["div"]["amount"] == "10"
    assert number(sold["cash"]) == number("989.51")
    paid = advance(sold, "2026-11-06", [], {}, {}, None, "10")
    assert number(paid["cash"]) == number("999.51") and not paid["receivables"]
    assert advance(paid, "2026-11-06", [], {}, {}, None, "10") == paid
    later = advance(paid, "2026-11-09", [], {}, {}, None, "10")
    assert later["cash"] == paid["cash"] and before == funded()


def test_ex_date_buyer_no_entitlement_and_unpaid_cash_not_spent():
    fresh = advance(
        initial("1000"), "2026-11-03", [action()], {"AAPL": "100"}, {"AAPL": "100"}, {"AAPL": "1"}, "10"
    )
    assert fresh["receivables"]["div"]["amount"] == "0"
    old = funded()
    result = advance(
        old, "2026-11-03", [action(rate="1000")], {"AAPL": "100"}, {"AAPL": "100"}, {"AAPL": "1"}, "10"
    )
    assert number(result["positions"]["AAPL"]) < 10 and number(result["equity"]) > 5990


def test_split_preserves_raw_value_cash_merger_no_obsolete_price_needed():
    split = advance(
        funded(), "2026-11-03", [action(kind="split", ratio="2")], {"AAPL": "50"}, {"AAPL": "50"}, None, "10"
    )
    assert split["positions"]["AAPL"] == "10" and number(split["equity"]) == 1000
    merged = advance(funded(), "2026-11-03", [action(kind="cash_merger", rate="110")], {}, {}, None, "10")
    assert merged["positions"]["AAPL"] == "0" and number(merged["equity"]) == 1050
    assert number(merged["cash"]) == 500 and merged["retired"] == ["AAPL"]
    paid = advance(merged, "2026-11-06", [], {}, {}, None, "10")
    assert number(paid["cash"]) == 1050
    with pytest.raises(ValueError, match="retired"):
        advance(paid, "2026-11-09", [], {"AAPL": "100"}, {"AAPL": "100"}, {"AAPL": "1"}, "10")


@pytest.mark.parametrize(
    "change",
    [
        {"currency": "EUR"},
        {"special": True},
        {"pay_date": "2026-11-02"},
        {"rate": "NaN"},
        {"kind": "stock_merger"},
        {"ex_date": "2026-11-02"},
        {"kind": "split", "ratio": "0.3333333333333333"},
    ],
)
def test_invalid_actions_atomic(change):
    old = funded()
    with pytest.raises((ValueError, KeyError)):
        advance(old, "2026-11-03", [action(**change)], {"AAPL": "100"}, {"AAPL": "100"}, None, "10")
    assert old == funded()


def test_duplicate_actions_changed_day_bad_marks_and_short_sale_rejected():
    old = funded()
    with pytest.raises(ValueError):
        advance(old, "2026-11-03", [action(), action()], {"AAPL": "98"}, {"AAPL": "98"}, None, "10")
    valid = advance(old, "2026-11-03", [], {"AAPL": "100"}, {"AAPL": "100"}, None, "10")
    for weights, close in [({"AAPL": "-1"}, "100"), ({"AAPL": "1.1"}, "100"), (None, "0"), (None, "NaN")]:
        with pytest.raises(ValueError):
            advance(old, "2026-11-03", [], {"AAPL": "100"}, {"AAPL": close}, weights, "10")
    with pytest.raises(ValueError, match="out-of-order"):
        advance(valid, "2026-11-03", [], {"AAPL": "100"}, {"AAPL": "101"}, None, "10")
    bad = funded()
    bad["positions"]["AAPL"] = "-1"
    with pytest.raises(ValueError):
        mark(bad, {"AAPL": "100"})
    assert old == funded()


def test_sell_before_buy_cost_reserve_and_identical_control():
    prices = {"AAPL": "100", "SPY": "100"}
    books = compare(None, "2026-11-02", [], prices, prices, {"AAPL": ".20"})
    for bps in ("10", "25"):
        a, b = books[f"candidate_{bps}bps"], books[f"spy_{bps}bps"]
        assert a["equity"] == b["equity"] and a["cash"] == b["cash"]
        assert number(a["cash"]) > 7990
    assert number(books["candidate_25bps"]["equity"]) < number(books["candidate_10bps"]["equity"])
    old = funded()
    result = advance(old, "2026-11-03", [], prices, prices, {"SPY": "1"}, "25")
    assert [r["symbol"] for r in result["journal"] if r["type"] == "virtual_fill"] == ["AAPL", "SPY"]
    assert number(result["cash"]) >= 0


@pytest.mark.parametrize("bps", ["10", "25"])
def test_full_rotation_with_zero_cash_never_needs_borrowing(bps):
    old = initial("10000")
    old["cash"], old["positions"], old["day"] = "0", {"AAPL": "100"}, "2026-11-02"
    prices = {"AAPL": "100", "SPY": "100"}
    result = advance(old, "2026-11-03", [], prices, prices, {"SPY": "1"}, bps)
    assert number(result["cash"]) >= 0
    fills = [r for r in result["journal"] if r["type"] == "virtual_fill"]
    assert [r["symbol"] for r in fills] == ["AAPL", "SPY"]
    assert number(result["equity"]) + sum((number(r["cost"]) for r in fills), number(0)) == 10000


class Memory:
    def __init__(self):
        self.values = {}

    async def get(self, key, default=None):
        return copy.deepcopy(self.values.get(key, default))

    async def put(self, key, value):
        self.values[key] = copy.deepcopy(value)


async def fence():
    pass


class Reader:
    def __init__(self):
        self.calls = []
        self.error = None
        self.paged = False

    async def request(self, method, path, *, data_api=False, params=None):
        self.calls.append((method, path, data_api, copy.deepcopy(params)))
        if self.error:
            raise BrokerError(self.error, "redacted")
        if path == "/v1/corporate-actions":
            return {"corporate_actions": {}, "next_page_token": "more" if self.paged else None}
        if path == "/v2/calendar":
            month = params["start"][:7]
            return [{"date": f"{month}-01", "open": "09:30", "close": "16:00"}]
        stamp = params["start"]
        return {
            "bars": {s: [{"t": stamp, "o": 100, "h": 101, "l": 99, "c": 100, "v": 5}] for s in SYMBOLS},
            "next_page_token": None,
        }


def prepare(settings):
    settings = settings.model_copy(
        update={"alpaca_key": SecretStr("test"), "alpaca_secret": SecretStr("test")}
    )
    root = settings.data_dir / ROOT
    seal(root / "registration.json", {"source_sha256": target_sources(), "symbol_asof": "2026-10-18"})
    seal(
        root / "calendar/2026-11.json",
        {
            "month": "2026-11-01",
            "sessions": [
                {"date": "2026-11-02", "open": "09:30", "close": "16:00"},
                {"date": "2026-11-03", "open": "09:30", "close": "16:00"},
            ],
        },
    )
    return settings, Memory(), Reader()


def target(registration, **changes):
    return {
        "state": "recorded",
        "cycle": "2026-11",
        "execution_session": "2026-11-02",
        "registration_sha256": registration["target_registration_sha256"],
        "formation_start": "2026-10-31T10:00:00Z",
        "recorded_at": "2026-10-31T11:00:00Z",
        "intended_open": "2026-11-02T14:30:00Z",
        "orders_submitted": 0,
        "performance_calculated": False,
        "target_weights": {"AAPL": "0.20"},
        "selected": ["AAPL"],
        "cash_weight": "0.80",
        "input_file_sha256": {},
        **changes,
    }


def review(worker, day, manifest, *, actions=(), **changes):
    evidence = worker.root / "coverage-evidence/source.html"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text("Synthetic independent issuer/exchange evidence for tests only")
    value = {
        "session": day,
        "registration_sha256": manifest["registration_sha256"],
        "input_sha256": manifest["input_sha256"],
        "actions_sha256": fingerprint(list(actions)),
        "symbols": list(SYMBOLS),
        "no_gaps": True,
        "unsupported_actions": [],
        "reviewer": "synthetic test",
        "scope": "conditional_fixed_cohort_economic_review",
        "reviewed_at": manifest["observed_at"],
        "evidence": [
            {
                "file": "source.html",
                "sha256": hashlib.sha256(evidence.read_bytes()).hexdigest(),
                "source_url": "https://example.test/issuer",
                "kind": "issuer_notice",
            }
        ],
        **changes,
    }
    seal(worker.root / "coverage" / f"{day}.json", value)
    return value


async def test_pre_start_only_one_cached_actions_get_no_future_prices_or_fake_records(settings):
    settings, store, reader = prepare(settings)
    worker = ForwardStudy(settings, store, reader, fence)
    state = await worker.step("2026-10-18T07:00:00Z")
    assert state["state"] == "waiting_start" and state["api_requests_this_tick"] == 1
    assert not list(worker.root.glob("prices/**/*.json")) and not list(worker.root.glob("ledgers/*.json"))
    assert reader.calls[0][3]["data_quality"] == "all"
    assert reader.calls[0][3]["start"] == "2024-10-18" and reader.calls[0][3]["end"] == "2026-10-17"
    restarted = ForwardStudy(settings, store, reader, fence)
    again = await restarted.step("2026-10-18T07:01:00Z")
    assert again["api_requests_this_tick"] == 0 and len(reader.calls) == 1


async def test_future_collection_coverage_gate_fair_comparison_restart(settings):
    settings, store, reader = prepare(settings)
    worker = ForwardStudy(settings, store, reader, fence)
    await worker.step("2026-10-18T07:00:00Z")
    before = await worker.step("2026-11-03T10:59:59Z")
    assert before["collected_sessions"] == 0 and len(reader.calls) == 1
    state = await worker.step("2026-11-03T11:00:00Z")
    assert state["state"] == "waiting_coverage" and state["collected_sessions"] == 1
    assert state["api_requests_this_tick"] == 3 and state["observations"] == 0
    assert state["performance_calculated"] is False
    manifest = read(worker.root / "sessions/2026-11-02.json")
    registration = await store.get(REGISTRY)
    seal(worker.target_root / "targets/2026-11.json", target(registration))
    review(worker, "2026-11-02", manifest)
    result = await worker.step("2026-11-03T11:01:00Z")
    assert result["observations"] == result["monthly_decisions"] == 1 and not result["review_ready"]
    assert result["last_equities"]["candidate_10bps"] == result["last_equities"]["spy_10bps"]
    record = read(worker.root / "ledgers/2026-11-02.json")
    restored = ForwardStudy(settings, store, reader, fence)
    again = await restored.step("2026-11-03T11:02:00Z")
    assert again["api_requests_this_tick"] == 0 and read(worker.root / "ledgers/2026-11-02.json") == record
    assert len(reader.calls) == 4 and not list(worker.root.glob("coverage/2026-11-03.json"))


@pytest.mark.parametrize("status,paged", [(403, False), (429, False), (None, True)])
async def test_error_and_incomplete_pages_cached_without_repeated_requests(settings, status, paged):
    settings, store, reader = prepare(settings)
    reader.error, reader.paged = status, paged
    worker = ForwardStudy(settings, store, reader, fence)
    assert (await worker.step("2026-10-18T07:00:00Z"))["state"] == "review_required"
    reader.error, reader.paged = None, False
    assert (await worker.step("2026-10-18T07:01:00Z"))["state"] == "review_required"
    assert len(reader.calls) == 1


async def test_source_or_db_registration_change_blocks_without_api(settings):
    settings, store, reader = prepare(settings)
    worker = ForwardStudy(settings, store, reader, fence)
    await worker.step("2026-10-18T07:00:00Z")
    store.values[REGISTRY]["symbol_asof"] = "2026-10-19"
    assert (await worker.step("2026-10-18T07:01:00Z"))["state"] == "review_required"
    assert len(reader.calls) == 1


async def test_missing_keys_no_requests_and_missing_prior_registration_not_recreated(settings):
    settings, store, reader = prepare(settings)
    absent = settings.model_copy(update={"alpaca_key": SecretStr("")})
    worker = ForwardStudy(absent, store, reader, fence)
    assert (await worker.step("2026-10-18T07:00:00Z"))["state"] == "waiting_keys"
    assert not reader.calls
    worker = ForwardStudy(settings, store, reader, fence)
    await worker.step("2026-10-18T07:00:00Z")
    (worker.root / "registration.json").unlink()
    assert (await worker.step("2026-10-18T07:01:00Z"))["state"] == "review_required"


async def test_get_budget_bounded_and_late_corporate_revision_blocks_economics(settings):
    settings, store, reader = prepare(settings)
    worker = ForwardStudy(settings, store, reader, fence)
    await worker.step("2026-10-18T07:00:00Z")
    state = await worker.step("2026-11-04T11:00:00Z")
    assert state["state"] == "catching_up" and state["api_requests_this_tick"] == 4
    state = await worker.step("2026-11-04T11:01:00Z")
    assert state["state"] == "waiting_coverage" and state["api_requests_this_tick"] == 2
    reg = await store.get(REGISTRY)
    manifest = read(worker.root / "sessions/2026-11-02.json")
    review(worker, "2026-11-02", manifest)
    seal(worker.target_root / "targets/2026-11.json", target(reg))
    # Simulate a new vintage in an explicit direct accounting call, not overwrite any sealed source.
    first = read(worker.root / "actions/2026-11-02.json")["response"]
    later = {
        "corporate_actions": {
            "cash_dividends": [
                {
                    "id": "late",
                    "symbol": "AAPL",
                    "ex_date": "2026-11-02",
                    "rate": "1",
                    "currency": "USD",
                    "special": False,
                    "foreign": False,
                    "payable_date": "2026-11-06",
                }
            ]
        }
    }
    rows = read(worker.root / "calendar/2026-11.json")["sessions"]
    with pytest.raises(ValueError, match="later corporate"):
        await worker.accounting(
            [
                (rows[0], True, manifest, first),
                (rows[1], False, read(worker.root / "sessions/2026-11-03.json"), later),
            ],
            reg,
        )
    assert not list(worker.root.glob("ledgers/*.json"))


@pytest.mark.parametrize("field,value", [("v", 0), ("c", 0), ("o", "NaN"), ("t", "2026-11-02T14:31:00Z")])
async def test_bad_minute_bar_rejected(field, value):
    reader = Reader()
    response = await reader.request("GET", "/v2/stocks/bars", params={"start": "2026-11-02T14:30:00Z"})
    response["bars"]["AAPL"][0][field] = value
    with pytest.raises(ValueError):
        minute_prices(response, datetime(2026, 11, 2, 14, 30, tzinfo=UTC), "o")


def test_raw_actual_schema_missing_pay_date_or_currency_never_inferred():
    row = {
        "id": "a",
        "symbol": "AAPL",
        "ex_date": "2026-11-03",
        "rate": "1",
        "special": False,
        "foreign": False,
    }
    response = {"corporate_actions": {"cash_dividends": [row]}}
    with pytest.raises(ValueError, match="currency"):
        normalized_actions(response, "2026-11-02", "2026-11-03")
    row["currency"] = "USD"
    with pytest.raises(KeyError):
        normalized_actions(response, "2026-11-02", "2026-11-03")
    row["payable_date"] = "2026-11-06"
    assert normalized_actions(response, "2026-11-02", "2026-11-03")[0]["pay_date"] == "2026-11-06"


def test_complex_unknown_or_missing_date_events_block_even_empty_action_assumption():
    for row in [
        {"id": "a", "symbol": "AAPL", "ex_date": "2026-11-03"},
        {"id": "a", "symbol": "AAPL"},
    ]:
        with pytest.raises(ValueError):
            normalized_actions({"corporate_actions": {"spin_offs": [row]}}, "2026-11-02", "2026-11-03")


def test_explicit_missing_field_resolution_preserves_raw_and_blocks_conflicts():
    raw = {
        "corporate_actions": {
            "cash_dividends": [
                {
                    "id": "a",
                    "symbol": "AAPL",
                    "ex_date": "2026-11-03",
                    "rate": "1",
                    "foreign": False,
                    "special": False,
                }
            ]
        }
    }
    old = copy.deepcopy(raw)
    resolution = {
        "a": {"fields": {"currency": "USD", "payable_date": "2026-11-06"}, "evidence_files": ["issuer.html"]}
    }
    result = normalized_actions(raw, "2026-11-02", "2026-11-03", resolution)
    assert result[0]["currency"] == "USD" and raw == old
    for fields in [{"rate": "5"}, {"effective_date": "2026-11-04"}]:
        with pytest.raises(ValueError, match="conflicting"):
            normalized_actions(raw, "2026-11-02", "2026-11-03", {"a": {"fields": fields}})
    with pytest.raises(ValueError, match="vintage"):
        normalized_actions(raw, "2026-11-04", "2026-11-05", {"unknown": {"fields": {"currency": "USD"}}})


def test_acquirer_not_credited_for_cash_paid_to_other_company_and_process_date_not_ex_date():
    merger = {
        "corporate_actions": {
            "cash_mergers": [
                {
                    "id": "m",
                    "acquiree_symbol": "OTHER",
                    "acquirer_symbol": "XOM",
                    "effective_date": "2026-11-03",
                    "rate": "50",
                }
            ]
        }
    }
    assert normalized_actions(merger, "2026-11-02", "2026-11-03") == []
    rename = {
        "corporate_actions": {
            "name_changes": [
                {
                    "id": "n",
                    "old_symbol": "OLD",
                    "new_symbol": "AAPL",
                    "process_date": "2024-10-20",
                }
            ]
        }
    }
    with pytest.raises(ValueError, match="effective"):
        normalized_actions(rename, "2026-11-02", "2026-11-03")
    assert (
        normalized_actions(
            rename, "2026-11-02", "2026-11-03", {"n": {"fields": {"effective_date": "2024-10-21"}}}
        )
        == []
    )


async def test_early_close_dst_exact_last_regular_minute_and_failed_fence(settings):
    settings, store, reader = prepare(settings)
    worker = ForwardStudy(settings, store, reader, fence)
    await worker.step("2026-10-18T07:00:00Z")
    reg = await store.get(REGISTRY)
    worker.fixed_now = datetime(2026, 11, 28, 11, tzinfo=UTC)
    worker.requests = 0
    row = {"date": "2026-11-27", "open": "09:30", "close": "13:00"}
    await worker.session(row, reg)
    bars = [call[3] for call in reader.calls if call[1] == "/v2/stocks/bars"]
    assert bars[0]["start"] == "2026-11-27T14:30:00+00:00"
    assert bars[1]["start"] == "2026-11-27T17:59:00+00:00"
    assert bars[1]["end"] == "2026-11-27T17:59:59.999999+00:00"
    assert all(p["feed"] == "sip" and p["adjustment"] == "raw" for p in bars)

    async def lost():
        raise RuntimeError("leader lost")

    other = ForwardStudy(settings, store, reader, lost)
    with pytest.raises(RuntimeError, match="leader lost"):
        await other.step("2026-10-18T07:01:00Z")
    assert len(reader.calls) == 4


def test_weekend_entitlement_payment_and_split_then_sale_no_artificial_profit():
    split = advance(
        funded(), "2026-11-03", [action(kind="split", ratio="2")], {"AAPL": "50"}, {"AAPL": "50"}, {}, "0"
    )
    assert number(split["cash"]) == 1000 and number(split["equity"]) == 1000
    weekend = advance(
        funded(),
        "2026-11-09",
        [action(ex_date="2026-11-07", pay_date="2026-11-08")],
        {"AAPL": "98"},
        {"AAPL": "98"},
        None,
        "10",
    )
    assert number(weekend["cash"]) == 510 and number(weekend["equity"]) == 1000


async def test_coverage_evidence_hash_scope_and_target_timing_required(settings):
    settings, store, reader = prepare(settings)
    worker = ForwardStudy(settings, store, reader, fence)
    await worker.step("2026-10-18T07:00:00Z")
    await worker.step("2026-11-03T11:00:00Z")
    manifest = read(worker.root / "sessions/2026-11-02.json")
    reg = await store.get(REGISTRY)
    value = target(reg)
    with pytest.raises(ValueError, match="late"):
        target_weights(
            {**value, "recorded_at": value["intended_open"]}, reg["target_registration_sha256"], "2026-11-02"
        )
    review(worker, "2026-11-02", manifest)
    assert coverage(worker.root, "2026-11-02", manifest, fingerprint(reg), [], worker.now())
    (worker.root / "coverage-evidence/source.html").write_text("changed")
    with pytest.raises(ValueError, match="evidence"):
        coverage(worker.root, "2026-11-02", manifest, fingerprint(reg), [], worker.now())


@pytest.mark.parametrize(
    "method,path,data_api,payload",
    [
        ("POST", "/v2/orders", False, None),
        ("DELETE", "/v2/orders", False, None),
        ("GET", "/v2/account", False, None),
        ("GET", "/v2/assets", False, None),
        ("GET", "/v2/stocks/bars", False, None),
        ("GET", "/v1/corporate-actions", True, {}),
    ],
)
async def test_reader_rejects_unauthorized_transport(settings, method, path, data_api, payload):
    reader = ForwardReader(settings)
    try:
        with pytest.raises(ValueError, match="selected GET"):
            await reader.request(method, path, data_api=data_api, payload=payload)
    finally:
        await reader.close()


def sample_status(**changes):
    return {
        "heartbeat": "2026-10-18T07:00:00Z",
        "contract_sha256": fingerprint(CONTRACT),
        "mode": "research_only",
        "orders_submitted": 0,
        "automatic_promotion": False,
        "state": "waiting_start",
        **changes,
    }


def test_notices_quiet_on_normal_waits_only_incident_and_recovery():
    first, lines = transition(None, sample_status())
    assert not lines
    broken, lines = transition(
        first, sample_status(state="review_required", heartbeat="2026-10-18T07:01:00Z")
    )
    assert len(lines) == 1
    repeat, lines = transition(
        broken, sample_status(state="review_required", heartbeat="2026-10-18T07:02:00Z")
    )
    assert not lines
    recovered, lines = transition(
        repeat, sample_status(state="waiting_coverage", heartbeat="2026-10-18T07:03:00Z")
    )
    assert len(lines) == 1 and recovered["transition"] == 2
    assert transition(recovered, sample_status())[0] == recovered


async def test_atomic_notices_and_api_auth_stored_only(settings, store):
    await publish(store, sample_status(), fence)
    broken = sample_status(state="review_required", heartbeat="2026-10-18T07:01:00Z")
    await publish(store, broken, fence)
    await publish(store, broken, fence)
    assert (await store.one("SELECT count(*) AS n FROM alerts"))["n"] == 1
    previous = await store.get(NOTICE_KEY)

    async def failed():
        if (await store.get(NOTICE_KEY)) == previous:
            # Fail the second fence inside the transaction using call count below.
            failed.calls += 1
            if failed.calls == 2:
                raise RuntimeError("fenced")

    failed.calls = 0
    with pytest.raises(RuntimeError):
        await publish(store, sample_status(heartbeat="2026-10-18T07:02:00Z"), failed)
    assert await store.get(NOTICE_KEY) == previous
    assert (await store.one("SELECT count(*) AS n FROM alerts"))["n"] == 1
    await store.put(KEY, sample_status())
    settings = settings.model_copy(update={"api_token": SecretStr("test-auth-only")})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(settings, store)), base_url="http://test"
    ) as client:
        path = "/research/equity/accounting"
        assert (await client.get(path)).status_code == 401
        response = await client.get(path, headers={"Authorization": "Bearer test-auth-only"})
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        assert response.json() == sample_status()
