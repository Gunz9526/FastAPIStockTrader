"""ADR-0068: cumulative-funded SPY Paper initial/monthly execution."""

import asyncio
import signal
from datetime import timedelta

from psycopg.types.json import Jsonb

from stock_trader.allocation import freeze_plan, plan_allocation
from stock_trader.broker import PAPER_URL, Alpaca
from stock_trader.clock import NEW_YORK, parse_time, utcnow
from stock_trader.config import Settings
from stock_trader.etf_account import AccountMonitor, market_snapshot, reviewed_bootstrap
from stock_trader.etf_execution import AllocationRunner, validate_snapshot
from stock_trader.etf_funding import ACTIVE, INITIAL, LIMIT, REGISTRY, job_key, register_funding
from stock_trader.etf_ledger import ZERO, fingerprint, number
from stock_trader.etf_pilot import KEY as PILOT_KEY
from stock_trader.etf_pilot import binding, checked_clock
from stock_trader.etf_shadow import read_sealed
from stock_trader.execution import TERMINAL, Executor
from stock_trader.logging import configure_logging, event
from stock_trader.notifications import Notifier
from stock_trader.operations import KEY as OPERATIONS_KEY
from stock_trader.operations import entry_allowed
from stock_trader.storage import Store

FEE_RESERVE = number("0.02")
FINISHED = {
    "complete_filled",
    "complete_partial",
    "complete_no_fill",
    "rejected",
    "expired_unsubmitted",
    "budget_exhausted",
}


def remaining(snapshot):
    return max(ZERO, LIMIT - number(snapshot["funding"]["cash_used"]))


def buy_plan(snapshot, cycle, now):
    if not snapshot["reconciled"] or set(snapshot["positions"]) - {"SPY"}:
        raise ValueError("strategy ownership mismatch")
    if not snapshot["guard"]["additions_allowed"]:
        return {"state": "additions_paused", "orders": []}
    if "SPY" not in snapshot.get("quotes", {}):
        return {"state": "waiting_quote", "orders": []}
    budget = min(remaining(snapshot), number(snapshot["account"]["cash"]))
    plan = plan_allocation(
        cycle=cycle,
        weights={"SPY": "1"},
        positions={},
        owned={},
        cash=max(ZERO, budget - FEE_RESERVE),
        quotes=snapshot["quotes"],
        assets=snapshot["assets"],
        now=now,
        market_open=snapshot["market_open"],
        open_orders=snapshot["open_orders"],
    )
    if not plan.get("orders"):
        return {
            "state": "budget_exhausted" if plan["state"] == "cash_funded_buys" else plan["state"],
            "orders": [],
        }
    plan["starting_positions"] = dict(snapshot["positions"])
    plan["funding_limit"] = str(LIMIT)
    return plan


class Automated:
    def __init__(self, settings, executor, monitor, account_binding, validation):
        self.settings, self.executor, self.monitor = settings, executor, monitor
        self.store, self.broker = executor.store, executor.broker
        self.account_binding, self.validation = account_binding, validation

    async def register(self):
        pilot = await self.store.get(PILOT_KEY, {})
        if (
            pilot.get("state") != "complete_filled"
            or pilot.get("review_required")
            or pilot.get("account_binding") != self.account_binding
            or not self.validation.get("actual_fill_verified")
            or self.validation.get("pilot_job_sha256") != fingerprint(pilot)
        ):
            raise ValueError("verified pilot prerequisite missing")
        await register_funding(self.store, self.account_binding, fingerprint(self.validation), utcnow())

    async def controls(self):
        from stock_trader.etf_controls import Controls

        return await Controls(self).process()

    async def save(self, job, state, **changes):
        job.update(state=state, **changes)
        async with self.store.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO kv(key,value) VALUES(%s,%s) ON CONFLICT(key) "
                "DO UPDATE SET value=excluded.value,updated_at=now()",
                (job_key(job["cycle"]), Jsonb(job)),
            )
            if job.get("control_id") and state in FINISHED:
                result = {
                    k: job[k]
                    for k in ["cycle", "state", "filled_qty", "filled_avg_price", "order_status"]
                    if k in job
                }
                result["manual_pause"] = await self.store.get("manual_pause", False)
                await conn.execute(
                    "UPDATE controls SET state='completed',completed_at=now(),result=%s "
                    "WHERE id=%s AND state='executing'",
                    (Jsonb(result), job["control_id"]),
                )
                await conn.execute(
                    "INSERT INTO audit(kind,payload) VALUES('control_exit_terminal',%s)",
                    (Jsonb({"control_id": job["control_id"], **result}),),
                )
        return {"state": state, "cycle": job["cycle"], "paper": True}

    async def alert(self, job, kind, text):
        await self.store.alert(job_key(job["cycle"]) + ":" + kind, text)

    async def fresh(self, *, allow_paused=False):
        if not entry_allowed(await self.store.get(OPERATIONS_KEY, {}), utcnow()):
            raise ValueError("operations not ready")
        snapshot = await market_snapshot(self.broker, await self.monitor.sync())
        if binding(snapshot["account"]) != self.account_binding:
            raise ValueError("account changed")
        paused = await self.store.get("manual_pause", False)
        if paused != allow_paused or await self.store.one(
            "SELECT id FROM controls WHERE state='pending' AND command=%s LIMIT 1",
            ("resume" if allow_paused else "pause",),
        ):
            raise ValueError("manual pause requested")
        if checked_clock(snapshot["clock"], utcnow()) < 300:
            raise ValueError("session window closed")
        if not entry_allowed(await self.store.get(OPERATIONS_KEY, {}), utcnow()):
            raise ValueError("operations not ready")
        return snapshot

    async def finish(self, job, order, now):
        if "terminal_seen_at" not in job:
            await self.save(job, "awaiting_ledger", terminal_seen_at=now.isoformat())
        try:
            snapshot = await self.monitor.sync()
            expected = {s: number(q) for s, q in job["plan"]["starting_positions"].items()}
            delta = number(order["filled_qty"]) * (1 if order["side"] == "buy" else -1)
            expected["SPY"] = expected.get("SPY", ZERO) + delta
            expected = {s: q for s, q in expected.items() if q}
            actual = {s: number(q) for s, q in snapshot["positions"].items() if number(q)}
            if (
                binding(snapshot["account"]) != self.account_binding
                or not snapshot["reconciled"]
                or actual != expected
                or snapshot["open_orders"]
            ):
                raise ValueError("terminal holdings mismatch")
        except ValueError:
            if (now - parse_time(job["terminal_seen_at"])).total_seconds() >= 300:
                await self.save(job, "awaiting_ledger", review_required=True)
                await self.alert(
                    job,
                    "ledger-review",
                    "SPY Paper 주문의 활동·원장 반영을 확인해야 합니다. 신규 주문은 차단했습니다.",
                )
            return {"state": "awaiting_ledger", "paper": True}
        qty = number(order["filled_qty"])
        state = (
            "complete_filled"
            if order["status"] == "filled"
            else "complete_partial"
            if qty
            else "complete_no_fill"
        )
        result = await self.save(
            job,
            state,
            filled_qty=str(qty),
            filled_avg_price=order.get("filled_avg_price"),
            order_status=order["status"],
            reconciled_at=now.isoformat(),
            funding=snapshot["funding"],
            cash=snapshot["account"]["cash"],
            positions=snapshot["positions"],
        )
        await self.alert(
            job, "complete", f"SPY Paper 실행 완료: {state}. 누적 매수 한도는 ${LIMIT:,.0f}입니다."
        )
        return result

    async def watch(self, job, clock, now):
        payload = job["plan"]["orders"][0]
        identity = payload["client_order_id"]
        await self.executor.recover()
        order = await self.broker.order_by_client(identity)
        if order is None:
            intent = await self.store.one("SELECT state FROM intents WHERE client_id=%s", (identity,))
            if intent["state"] in ("abandoned", "rejected"):
                return await self.save(
                    job, "rejected" if intent["state"] == "rejected" else "expired_unsubmitted"
                )
            if (now - parse_time(job["plan"]["snapshot_at"])).total_seconds() >= 300:
                await self.save(job, "await_unknown", review_required=True)
                await self.alert(
                    job, "unknown", "SPY Paper 접수 결과 확인이 지연되고 있습니다. 중복 주문은 차단했습니다."
                )
            return await self.save(job, "await_unknown")
        filled = number(order["filled_qty"])
        average = number(order["filled_avg_price"]) if filled else ZERO
        limit = number(payload["limit_price"])
        bad_average = filled and (
            average <= ZERO
            or (payload["side"] == "buy" and average > limit)
            or (payload["side"] == "sell" and average < limit)
        )
        if (
            any(
                order.get(k) != payload[k]
                for k in ["symbol", "side", "client_order_id", "type", "time_in_force"]
            )
            or number(order["qty"]) != number(payload["qty"])
            or number(order["limit_price"]) != number(payload["limit_price"])
            or not ZERO <= number(order["filled_qty"]) <= number(payload["qty"])
            or bad_average
        ):
            raise ValueError("broker order contract changed")
        previous = await self.store.all("SELECT id FROM broker_orders WHERE client_id=%s", (identity,))
        if previous and {row["id"] for row in previous} != {order["id"]}:
            raise ValueError("broker order identity changed")
        await self.store.save_order(order)
        order = (await self.store.one("SELECT payload FROM broker_orders WHERE client_id=%s", (identity,)))[
            "payload"
        ]
        if order["status"] in TERMINAL:
            if order["status"] == "replaced" or (
                order["status"] == "filled" and number(order["filled_qty"]) != number(payload["qty"])
            ):
                raise ValueError("unexpected terminal order")
            return await self.finish(job, order, now)
        age = (now - parse_time(order.get("submitted_at") or order["created_at"])).total_seconds()
        if age < -30:
            raise ValueError("future order timestamp")
        paused = await self.store.get("manual_pause", False)
        cancel = (
            (not paused if job.get("control_id") else paused)
            or age >= 90
            or (clock["is_open"] and checked_clock(clock, now) <= 60)
        )
        last, attempts = job.get("cancel_requested_at"), job.get("cancel_attempts", 0)
        due = last is None or (now - parse_time(last)).total_seconds() >= 30
        if cancel and clock["is_open"] and due and attempts < 3:
            checked_clock(clock, utcnow())
            await self.save(
                job, "await_cancel", cancel_attempts=attempts + 1, cancel_requested_at=now.isoformat()
            )
            await self.executor.cancel(order["id"])
            return {"state": "await_cancel", "paper": True}
        if cancel and attempts >= 3:
            await self.save(job, "await_cancel", review_required=True)
            await self.alert(
                job, "cancel-review", "SPY Paper 취소 상태를 확인해야 합니다. 재주문은 차단했습니다."
            )
        return await self.save(job, "await_cancel" if last else "await_fill")

    async def submit(self, job):
        if job.get("review_required"):
            return {"state": "await_review", "paper": True}
        plan = job["plan"]
        if (utcnow() - parse_time(plan["snapshot_at"])).total_seconds() > 30:
            return await self.save(job, "expired_unsubmitted")

        async def read():
            snapshot = await self.fresh(allow_paused=True) if job.get("control_id") else await self.fresh()
            payload = plan["orders"][0]
            if (
                plan["cycle"] != job["cycle"]
                or len(plan["orders"]) != 1
                or payload["symbol"] != "SPY"
                or payload["side"] != ("sell" if job.get("control_id") else "buy")
            ):
                raise ValueError("invalid limited plan")
            if job.get("control_id"):
                from stock_trader.etf_controls import preview

                if (
                    plan.get("control_id") != job["control_id"]
                    or plan["starting_positions"] != snapshot["positions"]
                    or plan["approval_sha256"] != (await preview(self.store))["expected_snapshot_sha256"]
                ):
                    raise ValueError("control approval changed")
            elif number(plan["funding_limit"]) != LIMIT or number(payload["qty"]) * number(
                payload["limit_price"]
            ) * number("1.002") + FEE_RESERVE > remaining(snapshot):
                raise ValueError("cumulative funding limit exceeded")
            return snapshot

        return await AllocationRunner(self.executor, read, enabled=True).step(plan)

    async def tick(self):
        await self.executor.fence()
        await self.register()
        await self.controls()
        now, clock = utcnow(), await self.broker.clock()
        checked_clock(clock, now)
        active = await self.store.get(ACTIVE)
        if active:
            job = await self.store.get(job_key(active))
            if not job or job["cycle"] != active:
                raise ValueError("active cycle missing")
            if job["state"] in FINISHED:
                await self.store.put(ACTIVE, None)
                return {"state": job["state"], "cycle": active, "paper": True}
            identity = job["plan"]["orders"][0]["client_order_id"]
            intent = await self.store.one("SELECT state FROM intents WHERE client_id=%s", (identity,))
            result = await self.watch(job, clock, now) if intent else await self.submit(job)
            if job["state"] in FINISHED:
                await self.store.put(ACTIVE, None)
            return result
        if await self.store.get("manual_pause", False):
            return {"state": "paused", "paper": True}
        if await self.store.unresolved():
            return {"state": "await_reconciliation", "paper": True}
        if not clock["is_open"]:
            return {"state": "waiting_market", "next_open": clock["next_open"], "paper": True}
        if checked_clock(clock, now) < 300:
            return {"state": "waiting_session_window", "paper": True}
        if not entry_allowed(await self.store.get(OPERATIONS_KEY, {}), now):
            return {"state": "waiting_operations", "paper": True}
        cycle = INITIAL
        if await self.store.get(job_key(INITIAL)):
            day = now.astimezone(NEW_YORK).date()
            path = self.settings.data_dir / "etf-shadow/calendar" / f"{day:%Y-%m}.json"
            if not path.exists():
                return {"state": "waiting_calendar", "paper": True}
            sessions = read_sealed(path)["sessions"]
            if not sessions or sessions[0]["date"] != day.isoformat():
                return {"state": "not_decision_day", "paper": True}
            cycle = f"paper-auto-monthly-0068-{day:%Y-%m}"
        if await self.store.get(job_key(cycle)):
            return {"state": "decision_already_recorded", "paper": True}
        frozen = await self.store.get(f"etf-plan:{cycle}:cash_funded_buys")
        if not frozen:
            snapshot = await self.fresh()
            try:
                frozen = buy_plan(snapshot, cycle, utcnow())
            except ValueError as exc:
                if str(exc) == "stale or future quote":
                    return {"state": "waiting_quote", "paper": True}
                raise
            if not frozen.get("orders"):
                if frozen["state"] == "budget_exhausted":
                    await self.save({"cycle": cycle}, "budget_exhausted")
                return {"state": frozen["state"], "paper": True}
            validate_snapshot(frozen, frozen["orders"][0], snapshot, utcnow())
            await freeze_plan(self.store, frozen)
        job = {"cycle": cycle, "state": "planned", "plan": frozen}
        async with self.store.pool.connection() as conn:
            await conn.execute("INSERT INTO kv(key,value) VALUES(%s,%s)", (job_key(cycle), Jsonb(job)))
            await conn.execute(
                "INSERT INTO kv(key,value) VALUES(%s,%s) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (ACTIVE, Jsonb(cycle)),
            )
        return await self.submit(job)


async def run(*, stop=None):
    if PAPER_URL != "https://paper-api.alpaca.markets":
        raise ValueError("Paper endpoint invariant violated")
    settings = Settings()
    stop = stop or asyncio.Event()
    store, broker = Store(settings), Alpaca(settings)
    await store.open()
    notifier = Notifier(settings, store)
    try:
        validation = read_sealed(settings.report_dir / "paper-pilot-0067/validation.json")
        account = await broker.account()
        async with store.leader("alpaca-" + account["id"]) as leader:
            driver = Automated(
                settings,
                Executor(broker, store, leader),
                AccountMonitor(broker, store, reviewed_bootstrap(settings)),
                binding(account),
                validation,
            )
            await driver.monitor.sync()
            await driver.register()
            for sig in (signal.SIGTERM, signal.SIGINT):
                asyncio.get_running_loop().add_signal_handler(sig, stop.set)
            result, next_tick, active = {"state": "starting", "paper": True}, utcnow(), False
            while not stop.is_set():
                await driver.executor.fence()
                if await driver.controls():
                    next_tick = utcnow()
                if utcnow() >= next_tick:
                    try:
                        result = await driver.tick()
                    except Exception as exc:
                        result = {"state": "await_review", "paper": True, "error": type(exc).__name__}
                        cycle = await store.get(ACTIVE)
                        if cycle:
                            job = await store.get(job_key(cycle))
                            if job:
                                await driver.save(job, job["state"], review_required=True)
                        await store.alert(
                            "limited-paper-error-0068",
                            "제한 Paper 실행에 확인이 필요합니다. 추가 주문을 차단했습니다.",
                        )
                        event("limited_paper_error", error=type(exc).__name__)
                    active = bool(await store.get(ACTIVE))
                    next_tick = utcnow() + timedelta(seconds=15 if active else 900)
                status = {
                    **result,
                    "owner": "etf_auto",
                    "heartbeat": utcnow().isoformat(),
                    "capital_limit": str(LIMIT),
                    "monthly_orders_enabled": True,
                    "performance_approved": False,
                    "next_tick_at": next_tick.isoformat(),
                    "active_cycle": await store.get(ACTIVE),
                    "registration": await store.get(REGISTRY),
                }
                await store.put("etf_auto", status)
                await store.put(
                    "engine_status",
                    {**status, "state": "paper_auto_" + result["state"], "order_stream_connected": False},
                )
                await notifier.flush()
                try:
                    await asyncio.wait_for(stop.wait(), timeout=15 if active else 30)
                except TimeoutError:
                    pass
    finally:
        await notifier.close()
        await broker.close()
        await store.close()


def main():
    configure_logging()
    try:
        asyncio.run(run())
    except Exception as exc:
        event("limited_paper_failed", error=type(exc).__name__)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
