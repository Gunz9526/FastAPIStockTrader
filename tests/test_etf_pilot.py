import copy
from datetime import UTC, datetime, timedelta

import pytest

from stock_trader.broker import AmbiguousSubmission
from stock_trader.etf_account import AccountMonitor
from stock_trader.etf_ledger import number
from stock_trader.etf_pilot import CYCLE, KEY, Pilot, binding, handoff_check
from stock_trader.execution import Executor
from tests.test_broker_execution import Fence
from tests.test_etf_account import APPROVAL, Broker

NOW = datetime(2026, 10, 12, 14, tzinfo=UTC)


class PilotBroker(Broker):
    def __init__(self):
        super().__init__()
        self.now = NOW
        self.market_open = True
        self.orders, self.submissions, self.cancels = {}, [], []
        self.ambiguous, self.hide_order, self.cancel_fills = False, False, False
        self.quote_age = 0
        self.next_close = NOW + timedelta(hours=6)
        self.activity_delay = False
        self.pending_fill = None

    async def clock(self):
        return {
            "is_open": self.market_open,
            "timestamp": self.now.isoformat(),
            "next_open": (NOW + timedelta(days=1)).isoformat(),
            "next_close": self.next_close.isoformat(),
        }

    async def quotes(self, symbols):
        return {
            s: {
                "bp": "99.99",
                "ap": "100.01",
                "t": (self.now - timedelta(seconds=self.quote_age)).isoformat(),
            }
            for s in symbols
        }

    async def open_orders(self):
        return [
            copy.deepcopy(o)
            for o in self.orders.values()
            if o["status"] in ("new", "partially_filled", "pending_cancel")
        ]

    async def submit(self, payload):
        self.submissions.append(copy.deepcopy(payload))
        self.orders[payload["client_order_id"]] = {
            **payload,
            "id": "pilot-order",
            "status": "new",
            "filled_qty": "0",
            "filled_avg_price": None,
            "created_at": self.now.isoformat(),
            "submitted_at": self.now.isoformat(),
        }
        if self.ambiguous:
            raise AmbiguousSubmission()
        return copy.deepcopy(self.orders[payload["client_order_id"]])

    async def order_by_client(self, identity):
        return None if self.hide_order else copy.deepcopy(self.orders.get(identity))

    async def cancel(self, identity):
        self.cancels.append(identity)
        if self.cancel_fills:
            self.fill()
        else:
            next(iter(self.orders.values()))["status"] = "pending_cancel"

    def fill(self, qty=None, status="filled"):
        order = next(iter(self.orders.values()))
        qty = qty or order["qty"]
        order.update(filled_qty=qty, filled_avg_price="100", status=status)
        self.balance = str(number(100000) - number(qty) * 100)
        self.holdings = {"SPY": qty}
        row = {
            "id": "pilot-fill",
            "activity_type": "FILL",
            "symbol": "SPY",
            "side": "buy",
            "qty": qty,
            "price": "100",
            "order_id": order["id"],
            "type": "fill",
            "transaction_time": self.now.isoformat(),
        }
        if self.activity_delay:
            self.pending_fill = row
        else:
            self.rows.append(row)


@pytest.fixture
def pilot_clock(monkeypatch):
    broker = PilotBroker()
    for module in ["etf_pilot", "etf_account", "etf_execution"]:
        monkeypatch.setattr(f"stock_trader.{module}.utcnow", lambda: broker.now)
    return broker


def create(store, broker, *, arm=True):
    return Pilot(
        Executor(broker, store, Fence()),
        AccountMonitor(broker, store, APPROVAL),
        account_binding=binding({"id": "test-account"}),
        arm_once=arm,
    )


async def test_handoff_cannot_take_existing_account_lock(store, pilot_clock):
    broker = pilot_clock
    checks, state = await handoff_check(broker, store, APPROVAL)
    assert all(checks.values()) and state["reconciled"]
    async with store.leader("alpaca-test-account"):
        with pytest.raises(RuntimeError):
            async with store.leader("alpaca-test-account"):
                pass
    async with store.leader("alpaca-test-account") as leader:
        await leader.execute("SELECT 1")
    assert not broker.submissions and not broker.cancels


async def test_disarmed_and_closed_market_arm_restart_no_orders(store, pilot_clock):
    broker = pilot_clock
    assert (await create(store, broker, arm=False).tick())["state"] == "disarmed"
    broker.market_open = False
    first = await create(store, broker).tick()
    original = await store.get(KEY)
    assert first["state"] == "waiting_market" and original["state"] == "armed"
    assert (await create(store, broker).tick())["state"] == "waiting_market"
    assert await store.get(KEY) == original and not broker.submissions


@pytest.mark.parametrize("failure", ["pause", "unknown", "pending_control", "blocked"])
async def test_handoff_preflight_blocks_arming(store, pilot_clock, failure):
    broker = pilot_clock
    if failure == "pause":
        await store.put("manual_pause", True)
    if failure == "unknown":
        await store.prepare("st-foreign", "SPY", "etf_buy", {})
        await store.intent_state("st-foreign", "unknown")
    if failure == "pending_control":
        await store.execute("INSERT INTO controls(command) VALUES('pause')")
    if failure == "blocked":
        original = broker.account

        async def account():
            return {**await original(), "trading_blocked": True}

        broker.account = account
    checks, _ = await handoff_check(broker, store, APPROVAL)
    assert not all(checks.values()) and not await store.get(KEY)
    assert not broker.submissions


async def test_full_fill_activity_delay_restart_completion_never_repeats(store, pilot_clock):
    broker = pilot_clock
    assert (await create(store, broker).tick())["submitted"]
    payload = broker.submissions[0]
    job = await store.get(KEY)
    assert number(job["plan"]["reserved_buy_cash"]) + number(job["plan"]["cash_buffer"]) <= 100
    assert number(payload["qty"]) * number(payload["limit_price"]) <= 100
    broker.activity_delay = True
    broker.fill()
    assert (await create(store, broker).tick())["state"] == "awaiting_ledger"
    broker.rows.append(broker.pending_fill)
    assert (await create(store, broker).tick())["state"] == "complete_filled"
    assert (await create(store, broker).tick())["state"] == "complete_filled"
    completed = await store.get(KEY)
    assert completed["positions"] == broker.holdings and completed["reconciled_at"]
    assert not completed["monthly_orders_enabled"] and not completed["performance_approved"]
    assert len(broker.submissions) == 1 and not broker.cancels


async def test_accepted_timeout_404_recovery_never_resubmits(store, pilot_clock):
    broker = pilot_clock
    broker.ambiguous = True
    await create(store, broker).tick()
    broker.hide_order = True
    assert (await create(store, broker).tick())["state"] == "await_unknown"
    assert len(broker.submissions) == 1
    broker.now += timedelta(seconds=301)
    assert (await create(store, broker).tick())["state"] == "await_unknown"
    assert (await store.get(KEY))["review_required"]
    broker.hide_order = False
    broker.fill()
    assert (await create(store, broker).tick())["state"] == "complete_filled"
    assert len(broker.submissions) == 1


async def test_partial_fill_cancel_persisted_throttled_then_ledger_complete(store, pilot_clock):
    broker = pilot_clock
    await create(store, broker).tick()
    broker.fill(".1", "partially_filled")
    broker.now += timedelta(seconds=91)
    assert (await create(store, broker).tick())["state"] == "await_cancel"
    job = await store.get(KEY)
    assert job["cancel_attempts"] == 1 and job["cancel_requested_at"]
    await create(store, broker).tick()
    assert len(broker.cancels) == 1
    next(iter(broker.orders.values()))["status"] = "canceled"
    assert (await create(store, broker).tick())["state"] == "complete_partial"
    assert len(broker.submissions) == 1 and broker.holdings == {"SPY": ".1"}


async def test_cancel_fill_race_finishes_from_actual_fill(store, pilot_clock):
    broker = pilot_clock
    await create(store, broker).tick()
    broker.now += timedelta(seconds=91)
    broker.cancel_fills = True
    assert (await create(store, broker).tick())["state"] == "await_cancel"
    assert (await create(store, broker).tick())["state"] == "complete_filled"
    assert len(broker.submissions) == len(broker.cancels) == 1


async def test_pause_cancels_resume_does_not_reset_finished_job(store, pilot_clock):
    broker = pilot_clock
    await create(store, broker).tick()
    await store.execute("INSERT INTO controls(command) VALUES('pause')")
    assert (await create(store, broker).tick())["state"] == "await_cancel"
    next(iter(broker.orders.values()))["status"] = "canceled"
    assert (await create(store, broker).tick())["state"] == "complete_no_fill"
    await store.execute("INSERT INTO controls(command) VALUES('resume'),('reset-risk'),('flatten')")
    assert (await create(store, broker).tick())["state"] == "complete_no_fill"
    results = await store.all("SELECT command,result FROM controls ORDER BY id")
    assert results[-1]["result"]["error"] == results[-2]["result"]["error"] == "unsupported_in_paper_pilot"
    assert len(broker.submissions) == 1 and not await store.get("manual_pause")


async def test_crash_after_frozen_plan_before_intent_expires_without_replan(store, pilot_clock):
    broker = pilot_clock
    real_submit = broker.submit
    runner = create(store, broker)

    async def crash(*args, **kwargs):
        raise RuntimeError("simulated before intent creation")

    runner.executor.submit = crash
    with pytest.raises(RuntimeError):
        await runner.tick()
    plan = (await store.get(KEY))["plan"]
    assert plan["cycle"] == CYCLE and not await store.unresolved()
    broker.now += timedelta(seconds=31)
    broker.submit = real_submit
    assert (await create(store, broker).tick())["state"] == "expired_unsubmitted"
    assert not broker.submissions


async def test_cancel_limit_no_resubmit_and_future_clock_rejected(store, pilot_clock):
    broker = pilot_clock
    await create(store, broker).tick()
    broker.now += timedelta(seconds=91)
    for _ in range(4):
        await create(store, broker).tick()
        broker.now += timedelta(seconds=31)
    assert len(broker.cancels) == 3 and (await store.get(KEY))["review_required"]
    assert len(broker.submissions) == 1
    with pytest.raises(ValueError, match="clock skew"):
        await create(store, broker).tick(broker.now + timedelta(seconds=31))


@pytest.mark.parametrize("failure", ["stale_quote", "session_end", "guard", "binding"])
async def test_armed_job_still_obeys_fresh_pre_send_checks(store, pilot_clock, failure):
    broker = pilot_clock
    broker.market_open = False
    await create(store, broker).tick()
    broker.market_open = True
    if failure == "session_end":
        broker.next_close = broker.now + timedelta(seconds=299)
        assert (await create(store, broker).tick())["state"] == "waiting_session_window"
    elif failure == "guard":
        rows = await store.all("SELECT key,value FROM kv WHERE key LIKE %s", ("etf-account-checkpoint:%",))
        checkpoint = rows[0]["value"]
        checkpoint["guard"]["drawdown_latch"] = True
        await store.put(rows[0]["key"], checkpoint)
        assert (await create(store, broker).tick())["state"] == "additions_paused"
    elif failure == "stale_quote":
        broker.quote_age = 31
        assert (await create(store, broker).tick())["state"] == "waiting_quote"
    else:
        job = await store.get(KEY)
        job["account_binding"] = "other-account"
        await store.put(KEY, job)
        with pytest.raises(ValueError):
            await create(store, broker).tick()
    assert not broker.submissions and not await store.all("SELECT * FROM intents")


@pytest.mark.parametrize("failure", ["missing", "stale"])
async def test_quote_delay_without_plan_recovers_to_exactly_one_submission(store, pilot_clock, failure):
    broker = pilot_clock
    real_quotes = broker.quotes
    if failure == "missing":

        async def missing(symbols):
            return {}

        broker.quotes = missing
    else:
        broker.quote_age = 31
    assert (await create(store, broker).tick())["state"] == "waiting_quote"
    job = await store.get(KEY)
    assert "plan" not in job and not job.get("review_required")
    assert not broker.submissions and not await store.unresolved()
    broker.quotes, broker.quote_age = real_quotes, 0
    assert (await create(store, broker).tick())["submitted"]
    assert (await create(store, broker).tick())["state"] == "await_fill"
    assert len(broker.submissions) == 1
