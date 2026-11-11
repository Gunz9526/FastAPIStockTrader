import copy
from datetime import timedelta

import pytest

from stock_trader.broker import AmbiguousSubmission
from stock_trader.etf_account import AccountMonitor, funding_summary
from stock_trader.etf_auto import ACTIVE, INITIAL, LIMIT, REGISTRY, Automated, buy_plan, job_key, remaining
from stock_trader.etf_funding import LEGACY_REGISTRY
from stock_trader.etf_ledger import fingerprint, number
from stock_trader.etf_pilot import KEY as PILOT_KEY
from stock_trader.etf_pilot import binding
from stock_trader.etf_shadow import seal
from stock_trader.execution import Executor, client_id
from stock_trader.operations import KEY as OPERATIONS_KEY
from stock_trader.operations import POLICY
from tests.test_broker_execution import Fence
from tests.test_etf_account import APPROVAL
from tests.test_etf_pilot import PilotBroker


class Broker(PilotBroker):
    async def submit(self, payload):
        self.submissions.append(copy.deepcopy(payload))
        order = {
            **payload,
            "id": f"auto-order-{len(self.submissions)}",
            "status": "new",
            "filled_qty": "0",
            "filled_avg_price": None,
            "created_at": self.now.isoformat(),
            "submitted_at": self.now.isoformat(),
        }
        self.orders[payload["client_order_id"]] = order
        if self.ambiguous:
            raise AmbiguousSubmission()
        return copy.deepcopy(order)

    async def cancel(self, identity):
        self.cancels.append(identity)
        order = next(o for o in self.orders.values() if o["id"] == identity)
        if self.cancel_fills:
            self.fill()
        else:
            order["status"] = "pending_cancel"

    def fill(self, qty=None, status="filled"):
        order = list(self.orders.values())[-1]
        qty = qty or order["qty"]
        delta = number(qty) - number(order["filled_qty"])
        direction = 1 if order["side"] == "buy" else -1
        price = number("100") if direction == 1 else number("99.99")
        order.update(filled_qty=qty, filled_avg_price=str(price), status=status)
        self.balance = str(number(self.balance) - direction * delta * price)
        self.holdings = {"SPY": str(number(self.holdings.get("SPY", 0)) + direction * delta)}
        self.holdings = {s: q for s, q in self.holdings.items() if number(q)}
        row = {
            "id": f"{order['id']}-fill-{qty}",
            "activity_type": "FILL",
            "symbol": "SPY",
            "side": order["side"],
            "qty": str(delta),
            "price": str(price),
            "order_id": order["id"],
            "type": "fill",
            "transaction_time": self.now.isoformat(),
        }
        if self.activity_delay:
            self.pending_fill = row
        else:
            self.rows.append(row)


@pytest.fixture
async def setup_auto(store, settings, monkeypatch):
    broker = Broker()
    for module in ["etf_auto", "etf_account", "etf_execution", "etf_controls"]:
        monkeypatch.setattr(f"stock_trader.{module}.utcnow", lambda: broker.now)
    identity = client_id("verified-pilot")
    payload = {
        "symbol": "SPY",
        "side": "buy",
        "qty": "1",
        "limit_price": "100",
        "type": "limit",
        "time_in_force": "day",
        "extended_hours": False,
        "client_order_id": identity,
    }
    order = {
        **payload,
        "id": "old-pilot-order",
        "status": "filled",
        "filled_qty": "1",
        "filled_avg_price": "100",
        "created_at": (broker.now - timedelta(days=3)).isoformat(),
        "submitted_at": (broker.now - timedelta(days=3)).isoformat(),
    }
    await store.prepare(identity, "SPY", "etf_buy", payload)
    await store.save_order(order)
    await store.intent_state(identity, "accepted")
    broker.orders[identity] = order
    broker.rows.append(
        {
            "id": "old-pilot-fill",
            "activity_type": "FILL",
            "symbol": "SPY",
            "side": "buy",
            "qty": "1",
            "price": "100",
            "order_id": order["id"],
            "type": "fill",
            "transaction_time": (broker.now - timedelta(days=3)).isoformat(),
        }
    )
    broker.balance, broker.holdings = "99900", {"SPY": "1"}
    pilot = {
        "state": "complete_filled",
        "account_binding": binding({"id": "test-account"}),
        "filled_qty": "1",
    }
    await store.put(PILOT_KEY, pilot)
    await store.put(
        OPERATIONS_KEY,
        {
            "heartbeat": broker.now.isoformat(),
            "policy_sha256": POLICY,
            "additions_allowed": True,
            "state": "healthy",
        },
    )
    seal(settings.data_dir / "etf-shadow/calendar/2026-10.json", {"sessions": [{"date": "2026-10-01"}]})
    validation = {"actual_fill_verified": True, "pilot_job_sha256": fingerprint(pilot)}
    await store.put(
        LEGACY_REGISTRY,
        {
            "paper": True,
            "capital_limit": "1000",
            "account_binding": pilot["account_binding"],
            "validation_sha256": fingerprint(validation),
            "strategy": "spy_hold",
            "version": 68,
            "registered_at": broker.now.isoformat(),
        },
    )
    await AccountMonitor(broker, store, APPROVAL).sync()

    def create():
        return Automated(
            settings,
            Executor(broker, store, Fence()),
            AccountMonitor(broker, store, APPROVAL),
            pilot["account_binding"],
            validation,
        )

    return broker, create


def test_funding_uses_posted_debits_fees_and_conservative_cent_rounding():
    checkpoint = {
        "rows": {
            "f": {
                "activity_type": "FILL",
                "order_id": "known",
                "side": "buy",
                "qty": ".127959082",
                "price": "779.046",
            },
            "fee": {"activity_type": "FEE", "net_amount": "-.01"},
        },
        "ledger": {"cash_by_type": {"JNLC": "100000"}},
    }
    result = funding_summary(checkpoint, {"cash": "99900.30"}, {"known"})
    assert number(result["cash_used"]) == number("99.70")
    assert number(result["buy_cash_debits"]) == number("99.686010995772")
    assert result["negative_fees"] == "0.01"


async def test_existing_holding_is_kept_and_incremental_fill_reconciles_restart(store, setup_auto):
    broker, create = setup_auto
    original = await store.get(PILOT_KEY)
    assert (await create().tick())["submitted"]
    job = await store.get(job_key(INITIAL))
    assert job["plan"]["starting_positions"] == {"SPY": "1"}
    assert number(job["plan"]["reserved_buy_cash"]) + number(".02") <= 9900
    assert broker.submissions[0]["side"] == "buy"
    broker.fill()
    assert (await create().tick())["state"] == "complete_filled"
    assert (await create().tick())["state"] == "not_decision_day"
    assert not await store.get(ACTIVE) and await store.get(PILOT_KEY) == original
    assert len(broker.submissions) == 1
    assert number(broker.holdings["SPY"]) > 1


async def test_monthly_only_remaining_budget_and_same_month_never_repeats(store, setup_auto):
    broker, create = setup_auto
    await create().tick()
    broker.fill()
    await create().tick()
    broker.now = broker.now.replace(month=11, day=2, hour=15)
    await store.put(OPERATIONS_KEY, {**await store.get(OPERATIONS_KEY), "heartbeat": broker.now.isoformat()})
    broker.next_close = broker.now + timedelta(hours=6)
    service = create()
    seal(
        service.settings.data_dir / "etf-shadow/calendar/2026-11.json", {"sessions": [{"date": "2026-11-02"}]}
    )
    assert (await service.tick())["submitted"]
    broker.fill()
    await create().tick()
    assert (await create().tick())["state"] == "decision_already_recorded"
    assert len(broker.submissions) == 2
    snapshot = await create().monitor.sync()
    assert number(snapshot["funding"]["cash_used"]) <= LIMIT
    broker.now = broker.now.replace(month=12, day=1)
    await store.put(OPERATIONS_KEY, {**await store.get(OPERATIONS_KEY), "heartbeat": broker.now.isoformat()})
    broker.next_close = broker.now + timedelta(hours=6)
    seal(
        service.settings.data_dir / "etf-shadow/calendar/2026-12.json", {"sessions": [{"date": "2026-12-01"}]}
    )
    assert (await create().tick())["state"] == "budget_exhausted"
    assert len(broker.submissions) == 2


async def test_price_marks_never_free_funding_or_create_sales(store, setup_auto):
    broker, create = setup_auto
    service = create()
    snapshot = await service.fresh()
    first = remaining(snapshot)
    snapshot["quotes"]["SPY"].update(bid="1000", ask="1000.01")
    plan = buy_plan(snapshot, INITIAL, broker.now)
    assert remaining(snapshot) == first and all(p["side"] == "buy" for p in plan["orders"])
    snapshot["quotes"]["SPY"].update(bid="10", ask="10.01")
    assert remaining(snapshot) == first


@pytest.mark.parametrize("failure", ["missing", "stale", "blocked", "policy"])
async def test_operations_gate_blocks_before_plan_and_recovers_without_risk_reset(store, setup_auto, failure):
    broker, create = setup_auto
    good = await store.get(OPERATIONS_KEY)
    bad = dict(good)
    if failure == "missing":
        bad = {}
    elif failure == "stale":
        bad["heartbeat"] = (broker.now - timedelta(seconds=181)).isoformat()
    elif failure == "blocked":
        bad.update(state="blocked", additions_allowed=False)
    else:
        bad["policy_sha256"] = "other"
    await store.put(OPERATIONS_KEY, bad)
    assert (await create().tick())["state"] == "waiting_operations"
    assert not broker.submissions and not await store.get(job_key(INITIAL))
    await store.put(OPERATIONS_KEY, good)
    assert (await create().tick())["submitted"]


async def test_operations_failure_is_rechecked_before_post_and_does_not_stop_order_watch(store, setup_auto):
    broker, create = setup_auto
    service = create()
    original = service.fresh

    async def change_after_first_check():
        snapshot = await original()
        await store.put(OPERATIONS_KEY, {})
        return snapshot

    service.fresh = change_after_first_check
    with pytest.raises(ValueError, match="operations not ready"):
        await service.tick()
    assert not broker.submissions
    # A separate scenario with an already accepted order must still cancel it.
    await store.put(
        OPERATIONS_KEY,
        {
            "heartbeat": broker.now.isoformat(),
            "policy_sha256": POLICY,
            "state": "healthy",
            "additions_allowed": True,
        },
    )
    await create().tick()
    assert len(broker.submissions) == 1
    await store.put(OPERATIONS_KEY, {})
    broker.now += timedelta(seconds=91)
    assert (await create().tick())["state"] == "await_cancel"
    assert len(broker.cancels) == 1 and len(broker.submissions) == 1


async def test_operations_change_during_account_reads_blocks_before_plan(store, setup_auto):
    broker, create = setup_auto
    service = create()
    original = service.monitor.sync

    async def read_then_block():
        snapshot = await original()
        await store.put(OPERATIONS_KEY, {})
        return snapshot

    service.monitor.sync = read_then_block
    with pytest.raises(ValueError, match="operations not ready"):
        await service.tick()
    assert not broker.submissions and not await store.get(job_key(INITIAL))


async def test_partial_cancel_reconciles_starting_position_without_same_day_retry(store, setup_auto):
    broker, create = setup_auto
    await create().tick()
    broker.fill(".1", "partially_filled")
    broker.now += timedelta(seconds=91)
    assert (await create().tick())["state"] == "await_cancel"
    list(broker.orders.values())[-1]["status"] = "canceled"
    assert (await create().tick())["state"] == "complete_partial"
    assert number(broker.holdings["SPY"]) == number("1.1")
    await create().tick()
    assert len(broker.submissions) == 1


async def test_unknown_404_and_late_acceptance_do_not_duplicate(store, setup_auto):
    broker, create = setup_auto
    broker.ambiguous = True
    await create().tick()
    broker.hide_order = True
    assert (await create().tick())["state"] == "await_unknown"
    broker.hide_order = False
    broker.fill()
    assert (await create().tick())["state"] == "complete_filled"
    assert len(broker.submissions) == 1


async def test_late_activity_preserves_checkpoint_then_completes(store, setup_auto):
    broker, create = setup_auto
    await create().tick()
    broker.activity_delay = True
    broker.fill()
    assert (await create().tick())["state"] == "awaiting_ledger"
    broker.rows.append(broker.pending_fill)
    assert (await create().tick())["state"] == "complete_filled"


@pytest.mark.parametrize("failure", ["missing", "stale", "pause", "closed", "guard"])
async def test_waiting_conditions_create_no_attempt_and_recover(store, setup_auto, failure):
    broker, create = setup_auto
    original = broker.quotes
    if failure == "missing":

        async def empty(symbols):
            return {}

        broker.quotes = empty
    elif failure == "stale":
        broker.quote_age = 31
    elif failure == "pause":
        await store.put("manual_pause", True)
    elif failure == "closed":
        broker.market_open = False
    else:
        await create().monitor.sync()
        rows = await store.all("SELECT key,value FROM kv WHERE key LIKE %s", ("etf-account-checkpoint:%",))
        row = rows[0]
        row["value"]["guard"]["drawdown_latch"] = True
        await store.put(row["key"], row["value"])
    await create().tick()
    assert not broker.submissions and not await store.get(job_key(INITIAL))
    if failure != "guard":
        broker.quotes, broker.quote_age, broker.market_open = original, 0, True
        await store.put("manual_pause", False)
        assert (await create().tick())["submitted"]


async def test_funding_is_rechecked_immediately_before_send(store, setup_auto):
    broker, create = setup_auto
    service = create()
    original = service.fresh
    calls = 0

    async def read():
        nonlocal calls
        calls += 1
        value = await original()
        if calls > 1:
            value["funding"]["cash_used"] = "9999.99"
        return value

    service.fresh = read
    with pytest.raises(ValueError, match="cumulative funding"):
        await service.tick()
    assert not broker.submissions


async def test_frozen_unsubmitted_plan_expires_without_changing_price(store, setup_auto):
    broker, create = setup_auto
    service = create()

    async def crash(*args, **kwargs):
        raise RuntimeError("before prepare")

    service.executor.submit = crash
    with pytest.raises(RuntimeError):
        await service.tick()
    plan = copy.deepcopy((await store.get(job_key(INITIAL)))["plan"])
    broker.now += timedelta(seconds=31)
    assert (await create().tick())["state"] == "expired_unsubmitted"
    assert (await store.get(job_key(INITIAL)))["plan"] == plan and not broker.submissions


async def test_registration_and_pilot_proof_cannot_be_changed(store, setup_auto):
    broker, create = setup_auto
    broker.market_open = False
    await create().tick()
    registry = await store.get(REGISTRY)
    registry["capital_limit"] = "100000"
    await store.put(REGISTRY, registry)
    with pytest.raises(ValueError, match="contract changed"):
        await create().tick()
    assert not broker.submissions


async def test_broker_id_change_is_blocked_without_choosing_an_ambiguous_row(store, setup_auto):
    broker, create = setup_auto
    await create().tick()
    list(broker.orders.values())[-1]["id"] = "unexpected-other-id"
    with pytest.raises(ValueError, match="broker order identity changed"):
        await create().tick()
    assert len(broker.submissions) == 1


@pytest.mark.parametrize("reason", ["pause", "timeout", "close"])
async def test_cancel_is_persisted_rate_limited_and_does_not_rebuy(store, setup_auto, reason):
    broker, create = setup_auto
    await create().tick()
    if reason == "pause":
        await store.execute("INSERT INTO controls(command) VALUES('pause')")
    elif reason == "timeout":
        broker.now += timedelta(seconds=91)
    else:
        broker.next_close = broker.now + timedelta(seconds=30)
    assert (await create().tick())["state"] == "await_cancel"
    await create().tick()
    assert len(broker.cancels) == 1 and len(broker.submissions) == 1
    list(broker.orders.values())[-1]["status"] = "canceled"
    assert (await create().tick())["state"] == "complete_no_fill"
    assert number(broker.holdings["SPY"]) == 1
