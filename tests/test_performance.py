import copy
from datetime import UTC, datetime

import pytest

from stock_trader.account_ledger import AccountLedger
from stock_trader.etf_funding import REGISTRY
from stock_trader.etf_ledger import fingerprint, number
from stock_trader.etf_shadow import CONTRACT as DAILY_CONTRACT
from stock_trader.etf_shadow import seal
from stock_trader.etf_study import SYMBOLS
from stock_trader.performance import REGISTRATION, REVIEW, START, Study, measurement, metrics
from tests import test_etf_auto

setup_auto = test_etf_auto.setup_auto
EARLY = datetime(2026, 10, 16, 7, tzinfo=UTC)
LATER = datetime(2026, 10, 21, 10, 30, tzinfo=UTC)


def source(day, price="100"):
    return {
        "date": day,
        "session": {"date": day, "open": "09:30", "close": "16:00"},
        "bars": {
            s: [
                {
                    "t": day + "T04:00:00Z",
                    "o": price,
                    "c": price,
                    "h": str(number(price) + 1),
                    "l": str(number(price) - 1),
                    "v": 1000,
                }
            ]
            for s in SYMBOLS
        },
    }


async def prepare(store, setup_auto, settings, tmp_path):
    _, create = setup_auto
    await create().register()
    configured = settings.model_copy(update={"data_dir": tmp_path / "forward"})
    root = configured.data_dir / "etf-shadow"
    seal(root / "registration.json", {"contract": DAILY_CONTRACT})
    seal(
        root / "calendar/2026-10.json",
        {"sessions": [source(day)["session"] for day in ["2026-10-19", "2026-10-20"]]},
    )
    for day, price in [("2026-10-19", "100"), ("2026-10-20", "110")]:
        seal(root / "daily" / (day + ".json"), source(day, price))
    row = (await store.all("SELECT key,value FROM kv WHERE key LIKE %s", ("etf-account-checkpoint:%",)))[0]
    row["value"]["last_success_at"] = LATER.isoformat()
    await store.put(row["key"], row["value"])
    return Study(configured, store), row


async def test_registers_before_future_start_without_reading_future_prices(
    store, setup_auto, settings, tmp_path
):
    study, _ = await prepare(store, setup_auto, settings, tmp_path)
    result = await study.tick(EARLY)
    assert result["state"] == "not_started" and result["observations"] == 0
    registration = await store.get(REGISTRATION)
    assert registration["contract"]["start"] == START.isoformat()
    assert registration["funding_registration_sha256"] == fingerprint(await store.get(REGISTRY))
    assert await store.all("SELECT key FROM kv WHERE key LIKE %s", ("paper-study-observation-0075:%",)) == []


async def test_records_whole_and_sleeve_separately_and_restart_does_not_overwrite(
    store, setup_auto, settings, tmp_path
):
    study, _ = await prepare(store, setup_auto, settings, tmp_path)
    await study.tick(EARLY)
    result = await study.tick(LATER)
    assert result["state"] == "collecting" and result["return_intervals"] == 1
    assert number(result["whole_account_return"]) == number(".0001")
    assert number(result["sleeve_return"]) == number(".001")
    assert number(result["spy_price_return"]) == number(".1")
    assert result["economic_total_return"] == "not_calculated" and not result["performance_approved"]
    rows = await store.all(
        "SELECT key,value FROM kv WHERE key LIKE %s ORDER BY key", ("paper-study-observation-0075:%",)
    )
    await Study(study.settings, store).tick(LATER)
    assert rows == await store.all(
        "SELECT key,value FROM kv WHERE key LIKE %s ORDER BY key", ("paper-study-observation-0075:%",)
    )


async def test_late_fee_latches_review_without_changing_frozen_observations(
    store, setup_auto, settings, tmp_path
):
    study, cp = await prepare(store, setup_auto, settings, tmp_path)
    await study.tick(EARLY)
    await study.tick(LATER)
    frozen = await store.get("paper-study-observation-0075:2026-10-19")
    late = {"id": "late-test-fee", "activity_type": "FEE", "date": "2026-10-19", "net_amount": "-.5"}
    cp["value"]["rows"][late["id"]] = late
    ledger = AccountLedger.restore(cp["value"]["ledger"])
    ledger.apply([late])
    cp["value"]["ledger"] = ledger.snapshot()
    await store.put(cp["key"], cp["value"])
    assert (await study.tick(LATER))["state"] == "review_required"
    assert await store.get(REVIEW) and await store.get("paper-study-observation-0075:2026-10-19") == frozen
    cp["value"]["rows"].pop(late["id"])
    await store.put(cp["key"], cp["value"])
    assert (await study.tick(LATER))["state"] == "review_required"


async def test_gaps_and_late_registration_do_not_create_fake_evidence(store, setup_auto, settings, tmp_path):
    study, _ = await prepare(store, setup_auto, settings, tmp_path)
    assert (await study.tick(LATER))["reason"] == "late_registration"
    await study.tick(EARLY)
    missing = study.settings.data_dir / "etf-shadow/daily/2026-10-20.json"
    missing.unlink()
    result = await study.tick(LATER)
    assert result["state"] == "waiting_daily" and result["observations"] == 1
    assert result["missing_session"] == "2026-10-20"


async def test_measurement_uses_session_cutoff_and_rejects_unowned_or_external_activity(
    store, setup_auto, settings, tmp_path
):
    _, cp = await prepare(store, setup_auto, settings, tmp_path)
    value = cp["value"]
    after_close = {
        "id": "test-after-close",
        "activity_type": "FEE",
        "transaction_time": "2026-10-19T21:00:00Z",
        "net_amount": "-1",
    }
    changed = copy.deepcopy(value)
    changed["rows"][after_close["id"]] = after_close
    base = measurement(value, source("2026-10-19"), "source", {"old-pilot-order"})
    assert measurement(changed, source("2026-10-19"), "source", {"old-pilot-order"}) == base
    with pytest.raises(ValueError, match="unowned"):
        measurement(value, source("2026-10-19"), "source", set())
    flow = {"id": "test-transfer", "activity_type": "CSD", "date": "2026-10-19", "net_amount": "100"}
    changed["rows"][flow["id"]] = flow
    with pytest.raises(ValueError, match="cash flow"):
        measurement(changed, source("2026-10-19"), "source", {"old-pilot-order"})


def test_drawdown_is_not_diluted_by_whole_account_cash():
    rows = [
        {"whole_account_nav": whole, "sleeve_nav": sleeve, "spy_raw_close": price, "sleeve_exposure": ".99"}
        for whole, sleeve, price in [
            ("100000", "10000", "100"),
            ("101000", "11000", "110"),
            ("99000", "9000", "90"),
        ]
    ]
    result = metrics(rows)
    assert number(result["sleeve_max_drawdown"]) > number(result["whole_account_max_drawdown"])
    assert result["return_intervals"] == 2
