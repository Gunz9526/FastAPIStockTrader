import copy
from datetime import timedelta

import pytest

from stock_trader.allocation import plan_allocation
from stock_trader.etf_execution import AllocationRunner
from stock_trader.execution import Executor
from tests.test_allocation import inputs
from tests.test_broker_execution import FakeBroker, Fence


class Broker(FakeBroker):
    async def submit(self, payload):
        try:
            return await super().submit(payload)
        finally:
            self.orders[payload["client_order_id"]]["symbol"] = payload["symbol"]


def snapshot(params):
    return {
        "asof": params["now"].isoformat(),
        "guard": {"additions_allowed": True},
        "account": {
            "status": "ACTIVE",
            "cash": str(params["cash"]),
            "equity": "100000",
            "trading_blocked": False,
            "account_blocked": False,
        },
        "market_open": True,
        "reconciled": True,
        "open_orders": [],
        "positions": params["positions"],
        "owned": params["owned"],
        "quotes": params["quotes"],
        "assets": params["assets"],
    }


async def test_disabled_runner_has_no_broker_or_database_access():
    assert (await AllocationRunner(None, None).step({}))["state"] == "disabled"


async def test_unknown_partial_fill_restart_and_final_completion(store):
    params = inputs()
    plan = plan_allocation(**params)
    broker = Broker()
    broker.ambiguous = True

    async def read():
        return snapshot(params)

    runner = AllocationRunner(Executor(broker, store, Fence()), read, enabled=True)
    assert (await runner.step(plan, params["now"]))["submitted"]
    runner = AllocationRunner(Executor(broker, store, Fence()), read, enabled=True)
    assert (await runner.step(plan, params["now"]))["state"] == "await_fill"
    order = next(iter(broker.orders.values()))
    order.update(status="partially_filled", filled_qty=".1", filled_avg_price="100")
    assert (await runner.step(plan, params["now"]))["state"] == "await_fill"
    order.update(status="filled", filled_qty=plan["orders"][0]["qty"])
    assert (await runner.step(plan, params["now"] + timedelta(minutes=5)))["state"] == "phase_complete"
    assert len(broker.submissions) == 1


@pytest.mark.parametrize(
    "failure", ["cash", "ownership", "quote", "stale", "closed", "blocked", "unreconciled", "risk"]
)
async def test_pre_send_gates_prevent_any_submission(store, failure):
    params = inputs()
    state = snapshot(params)
    if failure == "cash":
        state["account"]["cash"] = "1"
    if failure == "ownership":
        state["positions"] = {"SPY": "1"}
    if failure == "quote":
        state["quotes"]["SPY"]["ask"] = "101"
    if failure == "stale":
        state["asof"] = (params["now"] - timedelta(minutes=1)).isoformat()
    if failure == "closed":
        state["market_open"] = False
    if failure == "blocked":
        state["account"]["trading_blocked"] = True
    if failure == "unreconciled":
        state["reconciled"] = False
    if failure == "risk":
        state["guard"]["additions_allowed"] = False
    broker = Broker()

    async def read():
        return state

    # Use an unmodified quote to create a valid frozen plan.
    plan = plan_allocation(**inputs())
    runner = AllocationRunner(Executor(broker, store, Fence()), read, enabled=True)
    with pytest.raises(ValueError):
        await runner.step(plan, params["now"])
    assert not broker.submissions and not await store.unresolved()


async def test_canceled_partial_order_requires_new_cycle_not_resubmit(store):
    params = inputs()
    broker = Broker()

    async def read():
        return snapshot(params)

    runner = AllocationRunner(Executor(broker, store, Fence()), read, enabled=True)
    plan = plan_allocation(**params)
    await runner.step(plan, params["now"])
    next(iter(broker.orders.values())).update(status="canceled", filled_qty=".1", filled_avg_price="100")
    assert (await runner.step(plan, params["now"]))["state"] == "needs_new_plan"
    assert len(broker.submissions) == 1


async def test_two_buys_sequential_recheck_cash_after_first_fill(store):
    params = inputs()
    params["weights"] = {"SPY": ".5", "BIL": ".5"}
    params["assets"]["BIL"] = copy.deepcopy(params["assets"]["SPY"])
    params["quotes"]["BIL"] = copy.deepcopy(params["quotes"]["SPY"])
    state = snapshot(params)
    broker = Broker()

    async def read():
        return state

    runner = AllocationRunner(Executor(broker, store, Fence()), read, enabled=True)
    plan = plan_allocation(**params)
    await runner.step(plan, params["now"])
    assert len(broker.submissions) == 1
    first = plan["orders"][0]
    broker.orders[first["client_order_id"]].update(
        status="filled", filled_qty=first["qty"], filled_avg_price="100"
    )
    state["account"]["cash"] = "1"
    with pytest.raises(ValueError):
        await runner.step(plan, params["now"])
    assert len(broker.submissions) == 1


async def test_unknown_404_and_prepared_crash_are_never_resent(store):
    params = inputs()
    plan = plan_allocation(**params)
    broker = Broker()

    async def read():
        return snapshot(params)

    runner = AllocationRunner(Executor(broker, store, Fence()), read, enabled=True)
    payload = plan["orders"][0]
    identity = payload["client_order_id"]
    await store.prepare(identity, "SPY", "etf_buy", payload)
    await store.intent_state(identity, "unknown")
    assert (await runner.step(plan, params["now"]))["state"] == "await_unknown"
    assert not broker.submissions
    await store.intent_state(identity, "prepared")
    assert (await runner.step(plan, params["now"]))["state"] == "needs_new_plan"
    assert not broker.submissions


async def test_sale_then_fresh_confirmed_cash_purchase(store):
    params = inputs()
    params.update(positions={"BIL": "10"}, owned={"BIL": "10"}, cash="0")
    params["assets"]["BIL"] = copy.deepcopy(params["assets"]["SPY"])
    params["quotes"]["BIL"] = copy.deepcopy(params["quotes"]["SPY"])
    state = snapshot(params)
    state["account"]["equity"] = "1000"
    broker = Broker()

    async def read():
        return state

    runner = AllocationRunner(Executor(broker, store, Fence()), read, enabled=True)
    sale = plan_allocation(**params)
    assert sale["state"] == "sell_first"
    await runner.step(sale, params["now"])
    assert (await runner.step(sale, params["now"]))["state"] == "await_fill"
    assert len(broker.submissions) == 1
    order = next(iter(broker.orders.values()))
    order.update(status="filled", filled_qty="10", filled_avg_price="99.99")
    assert (await runner.step(sale, params["now"]))["state"] == "phase_complete"
    params.update(cycle="new-cash-confirmed", cash="999.9", positions={}, owned={})
    state = snapshot(params)
    state["account"]["equity"] = "999.9"
    buy = plan_allocation(**params)
    assert (await runner.step(buy, params["now"]))["submitted"]
    assert [p["side"] for p in broker.submissions] == ["sell", "buy"]
