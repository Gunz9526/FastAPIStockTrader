import asyncio
import copy
from datetime import timedelta

import httpx
import pytest
from psycopg.types.json import Jsonb
from pydantic import SecretStr

from stock_trader.api import create_app
from stock_trader.etf_controls import Controls, preview
from stock_trader.etf_funding import ACTIVE, LEGACY_REGISTRY, LIMIT, REGISTRY, job_key
from stock_trader.etf_ledger import fingerprint, number
from tests import test_etf_auto
from tests.test_etf_account import APPROVAL

setup_auto = test_etf_auto.setup_auto


async def reviewed(store, create, command, *, qty=None, request_key="reviewed-request-1"):
    await create().register()
    await store.put("manual_pause", True)
    state = await preview(store)
    payload = {
        "command": command,
        "reason": "reviewed Paper operation",
        "expected_snapshot_sha256": state["expected_snapshot_sha256"],
    }
    if qty is not None:
        payload["qty"] = qty
    row = await store.one(
        "INSERT INTO controls(command,payload,request_key) VALUES(%s,%s,%s) RETURNING *",
        (command, Jsonb(payload), request_key),
    )
    return row


async def checkpoint(store):
    return (await store.all("SELECT key,value FROM kv WHERE key LIKE %s", ("etf-account-checkpoint:%",)))[0]


async def test_ten_percent_successor_preserves_old_registration_and_spending(store, setup_auto):
    broker, create = setup_auto
    old = await store.get(LEGACY_REGISTRY)
    await create().register()
    current = await store.get(REGISTRY)
    assert current["capital_limit"] == str(LIMIT) == "10000"
    assert current["predecessor_sha256"] == fingerprint(old)
    assert current["initial_approved_cash"] == "100000" and current["capital_fraction"] == "0.10"
    await create().register()
    assert await store.get(REGISTRY) == current and await store.get(LEGACY_REGISTRY) == old
    assert (await create().monitor.sync())["funding"]["cash_used"] == "100"
    assert not broker.submissions


async def test_budget_migration_requires_no_active_cycle_and_verified_predecessor(store, setup_auto):
    _, create = setup_auto
    await store.put(ACTIVE, "pending-before-migration")
    with pytest.raises(ValueError, match="idle"):
        await create().register()
    assert not await store.get(REGISTRY)
    await store.put(ACTIVE, None)
    legacy = await store.get(LEGACY_REGISTRY)
    legacy["capital_limit"] = "2000"
    await store.put(LEGACY_REGISTRY, legacy)
    with pytest.raises(ValueError, match="predecessor"):
        await create().register()


async def test_reviewed_risk_reset_preserves_ledger_and_pause_and_is_not_repeated(store, setup_auto):
    _, create = setup_auto
    cp = await checkpoint(store)
    cp["value"]["guard"].update(daily_latch=True, drawdown_latch=True, additions_allowed=False)
    await store.put(cp["key"], cp["value"])
    before = fingerprint(cp["value"]["ledger"])
    row = await reviewed(store, create, "reset-risk")
    assert (await create().tick())["state"] == "paused"
    result = await store.one("SELECT * FROM controls WHERE id=%s", (row["id"],))
    assert result["state"] == "completed" and result["result"]["risk_reset"]
    after = (await checkpoint(store))["value"]
    assert fingerprint(after["ledger"]) == before and not after["guard"]["daily_latch"]
    assert not after["guard"]["drawdown_latch"] and await store.get("manual_pause") is True
    await create().controls()
    assert len(await store.all("SELECT * FROM audit WHERE kind='reviewed_risk_reset'")) == 1


async def test_risk_reset_rechecks_guard_under_the_account_lock(store, setup_auto):
    _, create = setup_auto
    cp = await checkpoint(store)
    cp["value"]["guard"].update(drawdown_latch=True, additions_allowed=False)
    await store.put(cp["key"], cp["value"])
    row = await reviewed(store, create, "reset-risk")
    controller = Controls(create())
    checked = controller.checked

    async def racing(*args):
        value = await checked(*args)
        current = await checkpoint(store)
        current["value"]["guard"]["peak"] = "110000"
        await store.put(current["key"], current["value"])
        return value

    controller.checked = racing
    await controller.process()
    result = (await store.one("SELECT result FROM controls WHERE id=%s", (row["id"],)))["result"]
    assert result["error"] == "approval_snapshot_changed"
    assert (await checkpoint(store))["value"]["guard"]["drawdown_latch"]


@pytest.mark.parametrize("failure", ["approval", "pause", "flow", "no_latch"])
async def test_reset_requires_review_and_never_clears_flow_latch(store, setup_auto, failure):
    broker, create = setup_auto
    cp = await checkpoint(store)
    cp["value"]["guard"].update(daily_latch=True, additions_allowed=False)
    if failure == "flow":
        cp["value"]["guard"]["flow_latch"] = True
    if failure == "no_latch":
        cp["value"]["guard"].update(daily_latch=False, additions_allowed=True)
    await store.put(cp["key"], cp["value"])
    row = await reviewed(store, create, "reset-risk")
    if failure == "approval":
        row["payload"]["expected_snapshot_sha256"] = "0" * 64
        await store.execute("UPDATE controls SET payload=%s WHERE id=%s", (Jsonb(row["payload"]), row["id"]))
    if failure == "pause":
        await store.put("manual_pause", False)
    await create().controls()
    result = (await store.one("SELECT result FROM controls WHERE id=%s", (row["id"],)))["result"]
    assert result["applied"] is False and not broker.submissions
    if failure == "flow":
        assert (await checkpoint(store))["value"]["guard"]["flow_latch"]


@pytest.mark.parametrize("command,qty,left", [("reduce", ".25", ".75"), ("flatten", None, "0")])
async def test_reviewed_fractional_sale_reconciles_and_does_not_free_gross_budget(
    store, setup_auto, command, qty, left
):
    broker, create = setup_auto
    row = await reviewed(store, create, command, qty=qty)
    assert (await create().tick())["submitted"]
    assert broker.submissions[0]["side"] == "sell"
    await create().tick()
    assert not broker.cancels  # A required manual pause does not cancel an approved sale.
    broker.fill()
    assert (await create().tick())["state"] == "complete_filled"
    assert number(broker.holdings.get("SPY", 0)) == number(left)
    assert await store.get("manual_pause") is True
    result = await store.one("SELECT state,result FROM controls WHERE id=%s", (row["id"],))
    assert result["state"] == "completed" and result["result"]["manual_pause"]
    assert number((await create().monitor.sync())["funding"]["cash_used"]) >= 100
    await create().tick()
    assert len(broker.submissions) == 1


async def test_partial_sale_timeout_cancel_completes_without_repricing(store, setup_auto):
    broker, create = setup_auto
    row = await reviewed(store, create, "flatten")
    await create().tick()
    broker.fill(".1", "partially_filled")
    broker.now += timedelta(seconds=91)
    assert (await create().tick())["state"] == "await_cancel"
    list(broker.orders.values())[-1]["status"] = "canceled"
    assert (await create().tick())["state"] == "complete_partial"
    assert number(broker.holdings["SPY"]) == number(".9")
    await create().tick()
    assert len(broker.submissions) == 1
    assert (await store.one("SELECT state FROM controls WHERE id=%s", (row["id"],)))["state"] == "completed"


async def test_unknown_sale_is_recovered_after_restart_without_another_post(store, setup_auto):
    broker, create = setup_auto
    await reviewed(store, create, "reduce", qty=".2")
    broker.ambiguous = True
    await create().tick()
    broker.hide_order = True
    assert (await create().tick())["state"] == "await_unknown"
    broker.hide_order = False
    broker.fill()
    assert (await create().tick())["state"] == "complete_filled"
    assert len(broker.submissions) == 1


async def test_resume_cancels_active_sale_before_strategy_can_restart(store, setup_auto):
    broker, create = setup_auto
    await reviewed(store, create, "flatten")
    await create().tick()
    await store.execute("INSERT INTO controls(command) VALUES('resume')")
    assert (await create().tick())["state"] == "await_cancel"
    assert len(broker.cancels) == 1 and len(broker.submissions) == 1


async def test_sale_quantity_and_expired_plan_cannot_be_reissued(store, setup_auto):
    broker, create = setup_auto
    row = await reviewed(store, create, "reduce", qty="2")
    await create().controls()
    assert (await store.one("SELECT result FROM controls WHERE id=%s", (row["id"],)))["result"][
        "error"
    ] == "invalid_owned_quantity"
    row = await reviewed(store, create, "reduce", qty=".5", request_key="different-request-2")
    driver = create()

    async def crash(*args, **kwargs):
        raise RuntimeError("before send")

    driver.executor.submit = crash
    with pytest.raises(RuntimeError):
        await driver.tick()
    active = await store.get(ACTIVE)
    plan = copy.deepcopy((await store.get(job_key(active)))["plan"])
    broker.now += timedelta(seconds=31)
    assert (await create().tick())["state"] == "expired_unsubmitted"
    assert (await store.get(job_key(active)))["plan"] == plan and not broker.submissions
    assert (await store.one("SELECT state FROM controls WHERE id=%s", (row["id"],)))["state"] == "completed"


async def test_api_advanced_controls_are_authenticated_idempotent_and_conflict_checked(
    store, setup_auto, settings
):
    _, create = setup_auto
    await create().register()
    configured = settings.model_copy(update={"api_token": SecretStr("only-for-test")})
    app = create_app(configured, store)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/control-preview")).status_code == 401
        headers = {"Authorization": "Bearer only-for-test"}
        state = (await client.get("/control-preview", headers=headers)).json()
        body = {
            "command": "flatten",
            "request_id": "same-approved-request",
            "reason": "reviewed operation",
            "expected_snapshot_sha256": state["expected_snapshot_sha256"],
        }
        assert (
            await client.post("/controls", json={"command": "flatten"}, headers=headers)
        ).status_code == 422
        first, second = await asyncio.gather(
            *[client.post("/controls", json=body, headers=headers) for _ in range(2)]
        )
        assert first.status_code == second.status_code == 202 and first.json()["id"] == second.json()["id"]
        conflict = await client.post(
            "/controls", json={**body, "reason": "a different review"}, headers=headers
        )
        assert conflict.status_code == 409
        count = await store.one(
            "SELECT count(*) AS n FROM controls WHERE request_key=%s", (body["request_id"],)
        )
        assert count["n"] == 1


async def test_idle_runtime_processes_reviewed_sale_without_waiting_for_strategy_tick(
    store, setup_auto, settings, monkeypatch
):
    from stock_trader import etf_auto
    from stock_trader.etf_shadow import seal

    broker, create = setup_auto
    broker.market_open = False
    seal(settings.report_dir / "paper-pilot-0067/validation.json", create().validation)
    injected = False

    class RuntimeStore:
        def __getattr__(self, name):
            return getattr(store, name)

        async def open(self):
            pass

        async def close(self):
            pass

        async def put(self, key, value):
            nonlocal injected
            await store.put(key, value)
            if key == "engine_status" and value["state"] == "paper_auto_waiting_market" and not injected:
                injected = True
                broker.market_open = True
                await reviewed(store, create, "flatten")

    class Stop:
        done = False
        waits = 0

        def is_set(self):
            return self.done

        def set(self):
            self.done = True

        async def wait(self):
            self.waits += 1
            if broker.submissions or self.waits >= 4:
                self.done = True
                return True
            raise TimeoutError

    async def close():
        pass

    monkeypatch.setattr(etf_auto, "Settings", lambda: settings)
    monkeypatch.setattr(etf_auto, "Store", lambda _: RuntimeStore())
    monkeypatch.setattr(etf_auto, "Alpaca", lambda _: broker)
    monkeypatch.setattr(etf_auto, "reviewed_bootstrap", lambda _: APPROVAL)
    monkeypatch.setattr(broker, "close", close, raising=False)
    monkeypatch.setattr(asyncio.get_running_loop(), "add_signal_handler", lambda *args: None)
    await etf_auto.run(stop=Stop())
    assert injected and len(broker.submissions) == 1
    assert broker.submissions[0]["side"] == "sell" and await store.get(ACTIVE)
    assert (await store.one("SELECT state FROM controls WHERE command='flatten'"))["state"] == "executing"
