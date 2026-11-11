import asyncio
import copy
from datetime import UTC, datetime, timedelta

import pytest

from stock_trader.etf_account import AccountMonitor, market_snapshot, replay_checkpoint
from stock_trader.etf_guard import observe_guard
from stock_trader.etf_ledger import fingerprint
from stock_trader.etf_service import Service, allocation, probe
from stock_trader.etf_shadow import seal

NOW = datetime(2026, 10, 11, 12, tzinfo=UTC)
INITIAL = {"id": "bootstrap", "activity_type": "JNLC", "date": "2026-10-02", "net_amount": "100000"}
APPROVAL = {"bootstrap": fingerprint(INITIAL)}


async def test_simultaneous_monitors_serialize_replay_and_keep_guard(store):
    broker = Broker()
    original = broker.request

    async def slow_request(*args, **kwargs):
        await asyncio.sleep(0.02)
        return await original(*args, **kwargs)

    broker.request = slow_request
    first, second = await asyncio.gather(
        AccountMonitor(broker, store, APPROVAL).sync(NOW),
        AccountMonitor(broker, store, APPROVAL).sync(NOW),
    )
    assert first["reconciled"] and second["reconciled"]
    assert first["guard"] == second["guard"]
    assert sorted([first["full_audit"], second["full_audit"]]) == [False, True]
    rows = await store.all("SELECT value FROM kv WHERE key LIKE %s", ("etf-account-checkpoint:%",))
    assert len(rows) == 1 and len(rows[0]["value"]["rows"]) == 1
    assert rows[0]["value"]["ledger"]["cash"] == "100000"


class Broker:
    def __init__(self):
        self.rows = [INITIAL]
        self.balance = "100000"
        self.holdings = {}
        self.requests = []
        self.market_open = False

    async def account(self):
        return {
            "id": "test-account",
            "cash": self.balance,
            "equity": "100000",
            "status": "ACTIVE",
            "trading_blocked": False,
            "account_blocked": False,
        }

    async def positions(self):
        return [{"symbol": s, "qty": q} for s, q in self.holdings.items()]

    async def open_orders(self):
        return []

    async def request(self, method, path, params):
        assert method == "GET" and path == "/v2/account/activities"
        self.requests.append(params)
        return copy.deepcopy(self.rows)

    async def clock(self):
        return {"is_open": self.market_open, "next_open": "2026-10-12T13:30:00Z"}

    async def asset(self, symbol):
        return {"status": "active", "tradable": True, "fractionable": True}

    async def quotes(self, symbols):
        return {s: {"bp": "99.99", "ap": "100.01", "t": NOW.isoformat()} for s in symbols}


def test_guard_latches_restart_day_reset_and_cash_flow():
    state = observe_guard(None, equity=100, day="2026-10-09")
    daily = observe_guard(state, equity="98.5", day="2026-10-09")
    assert daily["daily_latch"] and not daily["additions_allowed"] and not daily["forced_exit"]
    assert not observe_guard(daily, equity=101, day="2026-10-09")["additions_allowed"]
    next_day = observe_guard(daily, equity=99, day="2026-10-12")
    assert next_day["additions_allowed"]
    shock = observe_guard(next_day, equity=90, day="2026-10-12")
    recovered = observe_guard(shock, equity=110, day="2026-10-13")
    assert recovered["drawdown_latch"] and not recovered["additions_allowed"]
    funded = observe_guard(state, equity=200, day="2026-10-12", external_flows=100)
    assert funded["flow_latch"] and not funded["additions_allowed"]
    with pytest.raises(ValueError, match="regressed"):
        observe_guard(state, equity=100, day="2026-10-08")


def test_late_activity_replay_ownership_and_changed_or_deleted_rows():
    buy = {
        "id": "fill",
        "activity_type": "FILL",
        "symbol": "SPY",
        "side": "buy",
        "qty": "2",
        "price": "100",
        "order_id": "order-1",
        "type": "fill",
        "transaction_time": "2026-10-10T14:00:00Z",
    }
    params = {
        "full": True,
        "approval": APPROVAL,
        "account": {"cash": "99800", "equity": "100000"},
        "positions": {"SPY": "2"},
        "order_ids": set(),
        "now": NOW,
    }
    prior = replay_checkpoint({}, [INITIAL, buy], **params)
    assert not prior["reconciliation"]["strategy_ownership_verified"]
    fee = {"id": "late-fee", "activity_type": "FEE", "date": "2026-10-09", "net_amount": "-1"}
    params.update(full=False, account={"cash": "99799", "equity": "99999"}, order_ids={"order-1"})
    following = replay_checkpoint(prior, [fee], **params)
    assert following["reconciliation"]["strategy_ownership_verified"]
    assert following["ledger"]["cash"] == "99799"
    with pytest.raises(ValueError, match="corrected"):
        replay_checkpoint(following, [{**fee, "net_amount": "-2"}], **params)
    params["full"] = True
    with pytest.raises(ValueError, match="deleted"):
        replay_checkpoint(following, [INITIAL, buy], **params)


def test_unregistered_completed_roundtrip_is_not_owned_after_an_old_db_restore():
    buy = {
        "id": "unknown-buy",
        "activity_type": "FILL",
        "symbol": "SPY",
        "side": "buy",
        "qty": "2",
        "price": "100",
        "order_id": "missing-buy-order",
        "type": "fill",
        "transaction_time": "2026-10-09T14:00:00Z",
    }
    sell = {
        **buy,
        "id": "unknown-sell",
        "side": "sell",
        "price": "101",
        "order_id": "missing-sell-order",
        "transaction_time": "2026-10-09T15:00:00Z",
    }
    result = replay_checkpoint(
        {},
        [INITIAL, buy, sell],
        full=True,
        approval=APPROVAL,
        account={"cash": "100002", "equity": "100002"},
        positions={},
        order_ids=set(),
        now=NOW,
    )
    assert result["reconciliation"]["matched"]
    assert not result["reconciliation"]["strategy_ownership_verified"]
    verified = replay_checkpoint(
        {},
        [INITIAL, buy, sell],
        full=True,
        approval=APPROVAL,
        account={"cash": "100002", "equity": "100002"},
        positions={},
        order_ids={"missing-buy-order", "missing-sell-order"},
        now=NOW,
    )
    assert verified["reconciliation"]["strategy_ownership_verified"]
    assert verified["ledger"] == result["ledger"]


async def test_pg_checkpoint_overlap_full_audit_restart_and_failed_update_preserved(store):
    broker = Broker()
    monitor = AccountMonitor(broker, store, APPROVAL)
    first = await monitor.sync(NOW)
    assert first["reconciled"] and first["full_audit"]
    assert "after" not in broker.requests[-1]
    restarted = AccountMonitor(broker, store, APPROVAL)
    second = await restarted.sync(NOW + timedelta(minutes=15))
    assert second["reconciled"] and not second["full_audit"]
    assert broker.requests[-1]["after"] == (NOW + timedelta(minutes=15) - timedelta(days=7)).isoformat()
    before = await store.all("SELECT key,value FROM kv WHERE key LIKE %s", ("etf-account-checkpoint:%",))
    broker.balance = "99999"
    with pytest.raises(ValueError, match="mismatch"):
        await restarted.sync(NOW + timedelta(minutes=30))
    assert (
        await store.all("SELECT key,value FROM kv WHERE key LIKE %s", ("etf-account-checkpoint:%",)) == before
    )
    broker.balance = "100000"
    final = await restarted.sync(NOW + timedelta(days=1))
    assert final["full_audit"] and "after" not in broker.requests[-1]


async def test_snapshot_detects_cash_change_during_reads(store):
    class MovingBroker(Broker):
        async def positions(self):
            self.balance = "99999"
            return []

    with pytest.raises(ValueError, match="changed"):
        await AccountMonitor(MovingBroker(), store, APPROVAL).sync(NOW)
    assert not await store.all("SELECT * FROM kv WHERE key LIKE %s", ("etf-account-checkpoint:%",))


async def test_closed_service_and_probe_never_use_submit_or_quotes(settings, store):
    broker = Broker()  # Has no submit() method.
    status = await Service(settings, store, broker, APPROVAL).tick()
    assert status["paper"] and status["orders_submitted"] == 0
    assert status["reconciled"] and status["plan_state"] == "market_closed"
    result = await probe(broker, store, APPROVAL, submit=True)
    assert result["state"] == "market_closed" and result["orders_submitted"] == 0


async def test_probe_respects_pause_and_uncertain_intent(store):
    await store.put("manual_pause", True)
    assert (await probe(object(), store, {}, submit=True))["state"] == "manually_paused"
    await store.put("manual_pause", False)
    await store.prepare("st-uncertain", "SPY", "etf_buy", {})
    await store.intent_state("st-uncertain", "unknown")
    assert (await probe(object(), store, {}, submit=True))["state"] == "await_reconciliation"


async def test_pilot_cap_and_existing_account_writer_block(store, monkeypatch):
    monkeypatch.setattr("stock_trader.etf_service.utcnow", lambda: NOW)
    monkeypatch.setattr("stock_trader.etf_account.utcnow", lambda: NOW)
    broker = Broker()
    broker.market_open = True
    snapshot = await market_snapshot(broker, await AccountMonitor(broker, store, APPROVAL).sync(NOW))
    plan = allocation(snapshot, "test", NOW, pilot=True)
    assert float(plan["reserved_buy_cash"]) <= 100
    async with store.leader("alpaca-test-account"):
        result = await probe(broker, store, APPROVAL, submit=True)
    assert result["state"] == "account_writer_active"
    assert not await store.all("SELECT * FROM intents")


async def test_initial_then_monthly_plan_frozen_and_never_submitted(settings, store, monkeypatch):
    monkeypatch.setattr("stock_trader.etf_service.utcnow", lambda: NOW)
    monkeypatch.setattr("stock_trader.etf_account.utcnow", lambda: NOW)
    broker = Broker()
    broker.market_open = True
    service = Service(settings, store, broker, APPROVAL)
    first = await service.tick()
    assert first["planned_orders"] == 1 and first["orders_submitted"] == 0
    assert await store.get("etf-initial-plan-recorded")
    assert (await service.tick())["plan_state"] == "not_decision_day"
    # Inject a new month boundary, using a matching broker timestamp.
    next_month = datetime(2026, 11, 2, 15, tzinfo=UTC)
    monkeypatch.setattr("stock_trader.etf_service.utcnow", lambda: next_month)
    monkeypatch.setattr("stock_trader.etf_account.utcnow", lambda: next_month)

    async def quote(symbols):
        return {s: {"bp": "99.99", "ap": "100.01", "t": next_month.isoformat()} for s in symbols}

    broker.quotes = quote
    seal(settings.data_dir / "etf-shadow/calendar/2026-11.json", {"sessions": [{"date": "2026-11-02"}]})
    monthly = await service.tick()
    assert monthly["planned_orders"] == 1
    assert (await service.tick())["plan_state"] == "plan_already_frozen"
    assert not await store.all("SELECT * FROM intents")
