import copy
from datetime import UTC, datetime

import pytest

from stock_trader.allocation import freeze_plan, plan_allocation
from stock_trader.etf_ledger import number
from stock_trader.execution import Executor
from tests.test_broker_execution import FakeBroker, Fence


def inputs():
    return {
        "cycle": "2026-11-initial",
        "weights": {"SPY": "1"},
        "positions": {},
        "owned": {},
        "cash": "100000",
        "quotes": {"SPY": {"bid": "99.99", "ask": "100.01", "timestamp": "2026-11-02T15:00:00+00:00"}},
        "assets": {"SPY": {"status": "active", "tradable": True, "fractionable": True}},
        "now": datetime(2026, 11, 2, 15, 0, 10, tzinfo=UTC),
        "market_open": True,
    }


def test_buy_plan_funds_price_fees_and_buffer_without_borrowing():
    result = plan_allocation(**inputs())
    order = result["orders"][0]
    assert order["side"] == "buy" and order["type"] == "limit" and not order["extended_hours"]
    spend = number(order["qty"]) * number(order["limit_price"]) * number("1.002")
    assert spend == number(result["reserved_buy_cash"])
    assert spend + number(result["cash_buffer"]) <= 100000
    assert plan_allocation(**inputs()) == result


def test_sell_phase_does_not_spend_unfilled_sales_and_partial_fill_blocks():
    params = inputs()
    params.update(positions={"SPY": "10"}, owned={"SPY": "10"}, cash=0, weights={"SPY": "0"})
    sell = plan_allocation(**params)
    assert sell["state"] == "sell_first" and sell["orders"][0]["qty"] == "10.000000000"
    params.update(
        positions={"SPY": "6"}, owned={"SPY": "6"}, cash="400", open_orders=[{"status": "partially_filled"}]
    )
    assert plan_allocation(**params)["state"] == "await_reconciliation"
    params.update(cycle="2026-11-reconciled-2", open_orders=[{"status": "canceled"}])
    remaining = plan_allocation(**params)
    assert number(remaining["orders"][0]["qty"]) == 6
    assert remaining["orders"][0]["client_order_id"] != sell["orders"][0]["client_order_id"]


@pytest.mark.parametrize(
    "status", ["new", "partially_filled", "pending_cancel", "pending_replace", "suspended", None]
)
def test_any_unfinished_order_blocks_new_plan(status):
    params = inputs()
    params["open_orders"] = [{"status": status}]
    assert plan_allocation(**params)["orders"] == []


def test_unknown_submission_and_closed_market_block():
    params = inputs()
    params["unresolved"] = [{"state": "unknown"}]
    assert plan_allocation(**params)["orders"] == []
    params = inputs()
    params["market_open"] = False
    assert plan_allocation(**params)["state"] == "market_closed"


@pytest.mark.parametrize(
    "stamp", ["2026-11-02T14:59:00+00:00", "2026-11-02T15:00:11+00:00", "2026-11-02T15:00:00"]
)
def test_stale_future_naive_quotes_rejected(stamp):
    params = inputs()
    params["quotes"]["SPY"]["timestamp"] = stamp
    with pytest.raises(ValueError, match="quote"):
        plan_allocation(**params)


def test_ownership_invalid_values_and_nonfractionable_rejected():
    params = inputs()
    params["positions"] = {"SPY": "1"}
    with pytest.raises(ValueError, match="ownership"):
        plan_allocation(**params)
    for value in ("NaN", "-1", "1.01"):
        params = inputs()
        params["weights"]["SPY"] = value
        with pytest.raises(ValueError):
            plan_allocation(**params)
    params = inputs()
    params["assets"]["SPY"]["fractionable"] = False
    with pytest.raises(ValueError, match="fractionable"):
        plan_allocation(**params)


def test_multi_buy_shares_available_cash_proportionately():
    params = inputs()
    params["weights"] = {"SPY": ".5", "BIL": ".5"}
    params["assets"]["BIL"] = params["assets"]["SPY"]
    params["quotes"]["BIL"] = params["quotes"]["SPY"]
    result = plan_allocation(**params)
    assert len(result["orders"]) == 2
    assert result["orders"][0]["qty"] == result["orders"][1]["qty"]
    assert number(result["reserved_buy_cash"]) + number(result["cash_buffer"]) <= 100000


async def test_frozen_plan_survives_restart_and_changed_cycle_payload_rejected(store):
    plan = plan_allocation(**inputs())
    assert await freeze_plan(store, plan) == plan
    assert await freeze_plan(store, plan) == plan
    changed = copy.deepcopy(plan)
    changed["orders"][0]["qty"] = "1"
    with pytest.raises(ValueError, match="already frozen"):
        await freeze_plan(store, changed)


async def test_fractional_plan_lost_reply_recovers_without_second_submission(store):
    class ETFBroker(FakeBroker):
        async def submit(self, payload):
            try:
                return await super().submit(payload)
            finally:
                # The shared legacy fake returns AAPL; this fixture must echo the ETF.
                self.orders[payload["client_order_id"]]["symbol"] = payload["symbol"]

    plan = await freeze_plan(store, plan_allocation(**inputs()))
    payload = plan["orders"][0]
    broker = ETFBroker()
    broker.ambiguous = True
    executor = Executor(broker, store, Fence())
    await executor.submit(payload, identity=payload["client_order_id"], action="etf_buy")
    assert plan_allocation(**{**inputs(), "unresolved": await store.unresolved()})["orders"] == []
    restarted = Executor(broker, store, Fence())
    assert not await restarted.recover()
    await restarted.submit(payload, identity=payload["client_order_id"], action="etf_buy")
    assert len(broker.submissions) == 1
    order = broker.orders[payload["client_order_id"]]
    order.update(
        symbol="SPY",
        qty=payload["qty"],
        filled_qty="0.123456789",
        filled_avg_price="100",
        status="partially_filled",
    )
    await store.save_order(order)
    await store.save_order(order)
    assert (await store.owned_quantities())["SPY"] == pytest.approx(0.123456789)
    assert await store.cash_flow() == pytest.approx(-12.3456789)
