import json
from datetime import timedelta

import httpx
import pytest

from stock_trader.broker import PAPER_URL, Alpaca, AmbiguousSubmission
from stock_trader.clock import utcnow
from stock_trader.execution import Executor, client_id


async def test_post_timeout_has_exactly_one_attempt(settings):
    calls = []

    async def handle(request):
        calls.append(request)
        raise httpx.ReadTimeout("simulated accepted order with lost reply")

    broker = Alpaca(settings, transport=httpx.MockTransport(handle))
    with pytest.raises(AmbiguousSubmission):
        await broker.submit({"symbol": "AAPL", "client_order_id": "st-test"})
    assert len(calls) == 1
    assert str(calls[0].url).startswith(PAPER_URL)
    await broker.close()


async def test_explicit_iex_no_silent_feed_upgrade(settings):
    async def handle(request):
        assert request.url.params["feed"] == "iex"
        return httpx.Response(200, json={"quotes": {}})

    broker = Alpaca(settings, transport=httpx.MockTransport(handle))
    assert await broker.quotes(["AAPL"]) == {}
    await broker.close()


class Fence:
    async def execute(self, sql):
        pass


class FakeBroker:
    def __init__(self):
        self.submissions = []
        self.orders = {}
        self.canceled = []
        self.ambiguous = False

    async def submit(self, payload):
        self.submissions.append(payload)
        order = make_order(
            identity=f"order-{len(self.submissions)}",
            client=payload["client_order_id"],
            side=payload["side"],
            qty=payload["qty"],
        )
        self.orders[payload["client_order_id"]] = order
        if self.ambiguous:
            raise AmbiguousSubmission()
        return order

    async def order_by_client(self, identity):
        return self.orders.get(identity)

    async def order(self, identity):
        return next(order for order in self.orders.values() if order["id"] == identity)

    async def cancel(self, identity):
        self.canceled.append(identity)


def make_order(
    *,
    identity="order-1",
    client="st-entry",
    side="buy",
    qty="10",
    filled="0",
    price=None,
    status="new",
    legs=None,
):
    return {
        "id": identity,
        "client_order_id": client,
        "symbol": "AAPL",
        "side": side,
        "type": "limit",
        "qty": qty,
        "filled_qty": filled,
        "filled_avg_price": price,
        "status": status,
        "created_at": utcnow().isoformat(),
        "submitted_at": utcnow().isoformat(),
        "legs": legs or [],
    }


async def test_crash_after_accept_recovers_without_duplicate(store):
    broker = FakeBroker()
    broker.ambiguous = True
    executor = Executor(broker, store, Fence())
    payload = {"symbol": "AAPL", "side": "buy", "qty": "10"}
    identity = client_id("model", "AAPL", "2026-10-02T14:00:00Z")
    await executor.submit(payload, identity=identity, action="entry")
    assert len(await store.unresolved()) == 1
    restarted = Executor(broker, store, Fence())
    assert not await restarted.recover()
    await restarted.submit(payload, identity=identity, action="entry")
    assert len(broker.submissions) == 1


async def test_404_unknown_order_is_not_retried(store):
    identity = "st-unknown"
    await store.prepare(identity, "AAPL", "entry", {"symbol": "AAPL", "client_order_id": identity})
    await store.intent_state(identity, "submitting")
    broker = FakeBroker()
    executor = Executor(broker, store, Fence())
    for _ in range(3):
        assert len(await executor.recover()) == 1
    assert not broker.submissions


async def test_prepared_exit_crash_is_abandoned_and_can_be_recomputed(store):
    await store.prepare("st-before-send", "AAPL", "exit", {"symbol": "AAPL", "qty": "10"})
    executor = Executor(FakeBroker(), store, Fence())
    assert not await executor.recover()
    row = await store.one("SELECT state FROM intents WHERE client_id='st-before-send'")
    assert row["state"] == "abandoned"


async def test_lost_leader_blocks_order_before_network_call(store):
    class LostFence:
        async def execute(self, sql):
            raise ConnectionError("DB lock connection lost")

    broker = FakeBroker()
    with pytest.raises(ConnectionError):
        await Executor(broker, store, LostFence()).submit(
            {"symbol": "AAPL", "side": "buy", "qty": "1"}, identity="st-fenced", action="entry"
        )
    assert not broker.submissions


async def test_fill_duplicates_reverse_order_and_bracket_ledger(store):
    parent = make_order(
        filled="10",
        price="100",
        status="filled",
        legs=[
            make_order(
                identity="take",
                client="generated-leg",
                side="sell",
                filled="4",
                qty="10",
                price="102",
                status="partially_filled",
            )
        ],
    )
    await store.save_order(parent)
    await store.save_order(parent)
    await store.save_order(make_order(filled="1", price="100", status="partially_filled"))
    assert (await store.owned_quantities())["AAPL"] == 6
    assert await store.cash_flow() == -592


async def test_cancel_race_waits_then_uses_actual_remaining_qty(store):
    broker = FakeBroker()
    executor = Executor(broker, store, Fence())
    await store.prepare("st-entry", "AAPL", "entry", {})
    await store.intent_state("st-entry", "accepted")
    parent = make_order(filled="10", price="100", status="filled")
    protective = make_order(identity="stop", client="st-entry", side="sell", status="new")
    await store.save_order(parent)
    await store.save_order(protective)
    position = {"symbol": "AAPL", "qty": "10"}
    assert not await executor.close_symbol("AAPL", "holding_time", [protective], [position])
    assert broker.canceled == ["stop"]
    assert not broker.submissions
    # The protective order filled two shares while cancellation was pending.
    protective.update(status="canceled", filled_qty="2", filled_avg_price="99")
    await store.save_order(protective)
    position["qty"] = "8"
    assert await executor.close_symbol("AAPL", "holding_time", [], [position])
    assert broker.submissions[0]["qty"] == "8"


async def test_inflight_exit_is_not_canceled_or_duplicated(store):
    broker = FakeBroker()
    executor = Executor(broker, store, Fence())
    await store.save_order(make_order(filled="10", price="100", status="filled"))
    await store.prepare("st-exit", "AAPL", "exit", {})
    await store.intent_state("st-exit", "accepted")
    exit_order = make_order(identity="exit", client="st-exit", side="sell")
    await store.save_order(exit_order)
    assert not await executor.close_symbol(
        "AAPL", "holding_time", [exit_order], [{"symbol": "AAPL", "qty": "10"}]
    )
    assert not broker.canceled
    assert not broker.submissions


async def test_foreign_position_cannot_be_liquidated(store):
    broker = FakeBroker()
    executor = Executor(broker, store, Fence())
    assert not await executor.close_symbol("AAPL", "manual", [], [{"symbol": "AAPL", "qty": "10"}])
    assert not broker.submissions


async def test_expire_entry_not_protective_exit(store):
    broker = FakeBroker()
    executor = Executor(broker, store, Fence())
    order = make_order()
    order["submitted_at"] = (utcnow() - timedelta(seconds=91)).isoformat()
    sell = {**order, "id": "exit", "side": "sell"}
    await executor.expire_entries([order, sell])
    assert broker.canceled == ["order-1"]


async def test_second_engine_cannot_acquire_same_account_lock(store):
    async with store.leader("account-1"):
        with pytest.raises(RuntimeError):
            async with store.leader("account-1"):
                pass


@pytest.mark.parametrize("orders", [False, True])
async def test_stream_authentication_subscription_and_binary_order_messages(settings, monkeypatch, orders):
    sent = []
    if orders:
        messages = [
            json.dumps({"stream": "authorization", "data": {"status": "authorized"}}).encode(),
            json.dumps(
                {"stream": "trade_updates", "data": {"event": "fill", "order": {"id": "test"}}}
            ).encode(),
        ]
    else:
        messages = [
            json.dumps([{"T": "success", "msg": "connected"}]),
            json.dumps([{"T": "success", "msg": "authenticated"}]),
            json.dumps([{"T": "b", "S": "AAPL", "t": "2026-10-02T14:40:00Z"}]),
        ]

    class Socket:
        async def send(self, value):
            sent.append(json.loads(value))

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not messages:
                raise StopAsyncIteration
            return messages.pop(0)

    class Connection:
        async def __aenter__(self):
            return Socket()

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr("stock_trader.broker.websockets.connect", lambda *args, **kwargs: Connection())
    broker = Alpaca(settings)
    received = [message async for message in broker.stream(orders=orders)]
    assert received[0] == {"_status": "connected"}
    assert sent[0]["action"] == "auth"
    assert sent[1]["action"] == ("listen" if orders else "subscribe")
    assert len(received) == 2
    if not orders:
        assert len(sent[1]["bars"]) == 30
    await broker.close()
