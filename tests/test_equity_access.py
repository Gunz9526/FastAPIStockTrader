import copy

import pytest

from stock_trader.broker import BrokerError
from stock_trader.equity_access import CONTRACT, REQUESTS, ROOT, assessment, collect
from stock_trader.etf_shadow import read_sealed, seal


class Reader:
    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail

    async def request(self, method, path, **kwargs):
        self.calls.append((method, path, copy.deepcopy(kwargs)))
        assert method == "GET" and "payload" not in kwargs
        if len(self.calls) == self.fail:
            raise BrokerError(403, "only-for-test")
        return [] if path == "/v2/assets" else {"bars": {}, "next_page_token": "do-not-follow"}


async def fence():
    pass


async def test_access_collection_is_bounded_and_restart_uses_all_cached_responses(settings):
    reader = Reader()
    first = await collect(settings, reader, fence)
    assert first["logical_gets"] == 6 and len(reader.calls) == len(REQUESTS) == 6
    assert all(call[2]["params"] == spec["params"] for call, spec in zip(reader.calls, REQUESTS, strict=True))
    second = await collect(settings, reader, fence)
    assert second["logical_gets"] == 0 and second["cache_hits"] == 6 and len(reader.calls) == 6
    assert CONTRACT["max_transport_attempts"] == 24


async def test_access_http_error_is_recorded_without_repeated_entitlement_requests(settings):
    reader = Reader(fail=2)
    await collect(settings, reader, fence)
    cached = read_sealed(settings.data_dir / ROOT / "rename_after.json")
    assert cached["state"] == "error" and cached["http_status"] == 403
    await collect(settings, reader, fence)
    assert len(reader.calls) == 6 and "only-for-test" not in str(cached)


async def test_changed_access_cache_stops_instead_of_silently_repairing(settings):
    reader = Reader()
    await collect(settings, reader, fence)
    path = settings.data_dir / ROOT / "registration.json"
    path.write_text(path.read_text().replace('"ADR-0080"', '"modified"'))
    with pytest.raises(ValueError, match="checksum"):
        await collect(settings, reader, fence)
    assert len(reader.calls) == 6


async def test_lost_leader_cannot_publish_new_response(settings):
    reader = Reader()
    calls = 0

    async def lost():
        nonlocal calls
        calls += 1
        if calls == 3:
            raise RuntimeError("writer lost")

    with pytest.raises(RuntimeError, match="writer lost"):
        await collect(settings, reader, lost)
    assert len(reader.calls) == 1 and not (settings.data_dir / ROOT / "assets.json").exists()


def assessment_cache(root, *, invalid=False):
    bar = {"t": "2022-06-06T04:00:00Z", "o": 100, "h": 101, "l": 99, "c": 100, "v": 1000}
    if invalid:
        bar["c"] = 1000
    for spec in REQUESTS:
        if spec["name"] == "assets":
            response = [{"id": "one", "symbol": "META", "status": "active"}]
        elif spec["name"] == "actions":
            response = {"corporate_actions": {"cash_dividends": [{"process_date": "2016-01-01"}]}}
        else:
            symbols = spec["params"]["symbols"].split(",")
            rows = [bar] if spec["name"] != "inactive_bars" else [{**bar, "t": "2022-10-24T04:00:00Z"}]
            response = {"bars": {s: rows for s in symbols}, "next_page_token": None}
        seal(root / (spec["name"] + ".json"), {"state": "received", "request": spec, "response": response})


def test_assessment_reports_alias_overlap_and_missing_pay_dates_without_returns(tmp_path):
    assessment_cache(tmp_path)
    result = assessment(tmp_path)
    assert result["rename"]["mapped_series_equal"] and result["rename"]["literal_alias_overlap"] == 1
    assert result["actions"]["missing_dividend_payable_date"] == 1
    assert result["strategy_evaluations"] == result["new_broker_requests"] == 0
    assert not result["full_market_point_in_time_ready"]


def test_assessment_rejects_invalid_bar_instead_of_certifying_access_quality(tmp_path):
    assessment_cache(tmp_path, invalid=True)
    result = assessment(tmp_path)
    assert result["state"] == "sample_quality_failed" and result["invalid_bars"]
    assert not result["full_market_point_in_time_ready"]


async def test_assessment_keeps_entitlement_error_as_incomplete(settings):
    await collect(settings, Reader(fail=2), fence)
    value = assessment(settings.data_dir / ROOT)
    assert value["state"] == "access_incomplete" and value["http_errors"] == {"rename_after": 403}
