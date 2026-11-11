"""ADR-0062/0063: one durable SPY Paper pilot with the original account lock."""

import argparse
import asyncio
import hashlib
import signal
from datetime import timedelta

from psycopg.types.json import Jsonb

from stock_trader.allocation import freeze_plan
from stock_trader.broker import PAPER_URL, Alpaca
from stock_trader.clock import parse_time, utcnow
from stock_trader.config import Settings
from stock_trader.etf_account import AccountMonitor, market_snapshot, reviewed_bootstrap
from stock_trader.etf_execution import AllocationRunner, validate_snapshot
from stock_trader.etf_ledger import number
from stock_trader.etf_service import allocation
from stock_trader.execution import TERMINAL, Executor
from stock_trader.logging import configure_logging, event
from stock_trader.notifications import Notifier
from stock_trader.storage import Store

KEY = "etf-pilot-job-0062"
CYCLE = "paper-pilot-0062"
FINISHED = {"complete_filled", "complete_partial", "complete_no_fill", "rejected", "expired_unsubmitted"}


def binding(account):
    return hashlib.sha256(account["id"].encode()).hexdigest()[:24]


def checked_clock(clock, now):
    if abs((now - parse_time(clock["timestamp"])).total_seconds()) > 30:
        raise ValueError("clock skew")
    return (parse_time(clock["next_close"]) - now).total_seconds() if clock["is_open"] else 0


async def handoff_check(broker, store, approval):
    snapshot = await AccountMonitor(broker, store, approval).sync()
    checks = {
        "paper": PAPER_URL == "https://paper-api.alpaca.markets",
        "reconciled": snapshot["reconciled"],
        "empty_positions": not snapshot["positions"],
        "no_open_orders": not snapshot["open_orders"],
        "no_unknown_intents": not await store.unresolved(),
        "no_previous_intents": not await store.one("SELECT client_id FROM intents LIMIT 1"),
        "not_paused": not await store.get("manual_pause", False),
        "no_pending_controls": not await store.one("SELECT id FROM controls WHERE state='pending' LIMIT 1"),
        "guard_allows": snapshot["guard"]["additions_allowed"],
        "active_account": snapshot["account"].get("status") == "ACTIVE"
        and snapshot["account"].get("trading_blocked") is False
        and snapshot["account"].get("account_blocked") is False,
    }
    return checks, snapshot


class Pilot:
    def __init__(self, executor, monitor, *, account_binding, arm_once=False):
        self.executor, self.monitor, self.arm_once = executor, monitor, arm_once
        self.store, self.broker = executor.store, executor.broker
        self.account_binding = account_binding

    async def controls(self):
        for row in await self.store.all("SELECT * FROM controls WHERE state='pending' ORDER BY id LIMIT 10"):
            command = row["command"]
            if command in ("pause", "resume"):
                await self.store.put("manual_pause", command == "pause")
                result = {"applied": command, "risk_reset": False}
            else:
                result = {"error": "unsupported_in_paper_pilot", "command": command}
            await self.store.execute(
                "UPDATE controls SET state='completed',completed_at=now(),result=%s WHERE id=%s",
                (Jsonb(result), row["id"]),
            )
            await self.store.audit("pilot_control", control_id=row["id"], **result)

    async def save(self, job, state, **updates):
        job.update(state=state, **updates)
        await self.store.put(KEY, job)
        return {"state": state, "paper": True}

    async def arm(self, now):
        checks, snapshot = await handoff_check(self.broker, self.store, self.monitor.approval)
        checks["account_binding_matches"] = binding(snapshot["account"]) == self.account_binding
        if not all(checks.values()):
            return None, {"state": "preflight_blocked", "checks": checks, "paper": True}
        job = {
            "state": "armed",
            "cycle": CYCLE,
            "account_binding": binding(snapshot["account"]),
            "max_buy_cash": "100",
            "paper": True,
            "armed_at": now.isoformat(),
            "performance_approved": False,
            "monthly_orders_enabled": False,
        }
        await self.store.put(KEY, job)
        return job, None

    async def fresh(self, job):
        snapshot = await market_snapshot(self.broker, await self.monitor.sync())
        if binding(snapshot["account"]) != job["account_binding"]:
            raise ValueError("account binding changed")
        if await self.store.get("manual_pause", False) or await self.store.one(
            "SELECT id FROM controls WHERE state='pending' AND command='pause' LIMIT 1"
        ):
            raise ValueError("manual pause requested")
        if checked_clock(snapshot["clock"], utcnow()) < 300:
            raise ValueError("too close to session end")
        return snapshot

    async def finish(self, job, order, now):
        if "terminal_seen_at" not in job:
            await self.save(job, "awaiting_ledger", terminal_seen_at=now.isoformat())
        try:
            snapshot = await self.monitor.sync()
            qty = number(order["filled_qty"])
            expected = {"SPY": str(qty)} if qty else {}
            actual = {s: number(q) for s, q in snapshot["positions"].items()}
            if (
                binding(snapshot["account"]) != job["account_binding"]
                or not snapshot["reconciled"]
                or actual != {s: number(q) for s, q in expected.items()}
                or snapshot["open_orders"]
            ):
                raise ValueError("terminal holdings mismatch")
        except ValueError:
            if (now - parse_time(job["terminal_seen_at"])).total_seconds() >= 300:
                await self.save(
                    job, "awaiting_ledger", review_required=True, error="ledger_delay_or_mismatch"
                )
            return {"state": "awaiting_ledger", "paper": True}
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
            order_status=order["status"],
            filled_qty=str(qty),
            filled_avg_price=order.get("filled_avg_price"),
            reconciled_at=now.isoformat(),
            cash=snapshot["account"]["cash"],
            positions=snapshot["positions"],
        )
        await self.store.alert(
            "paper-pilot-0062-complete", f"SPY Paper 시험 완료: {state}. 추가 자동 주문은 중단 상태입니다."
        )
        return result

    async def watch(self, job, clock, now):
        payload = job["plan"]["orders"][0]
        identity = payload["client_order_id"]
        await self.executor.recover()
        order = await self.broker.order_by_client(identity)
        if order is None:
            current = await self.store.one("SELECT state FROM intents WHERE client_id=%s", (identity,))
            if current["state"] in ("abandoned", "rejected"):
                return await self.save(
                    job, "rejected" if current["state"] == "rejected" else "expired_unsubmitted"
                )
            overdue = (now - parse_time(job["plan"]["snapshot_at"])).total_seconds() >= 300
            if overdue:
                await self.store.alert(
                    "paper-pilot-0062-unknown",
                    "SPY Paper 시험 접수 결과 확인이 지연되고 있습니다. 재주문은 차단했습니다.",
                )
            return await self.save(
                job, "await_unknown", review_required=overdue or job.get("review_required", False)
            )
        if (
            any(order.get(k) != payload[k] for k in ("symbol", "side", "client_order_id"))
            or number(order["qty"]) != number(payload["qty"])
            or order.get("type") != "limit"
            or order.get("time_in_force") != "day"
            or number(order["limit_price"]) != number(payload["limit_price"])
            or not 0 <= number(order["filled_qty"]) <= number(payload["qty"])
            or (
                number(order["filled_qty"])
                and not 0 < number(order["filled_avg_price"]) <= number(payload["limit_price"])
            )
        ):
            raise ValueError("pilot broker order mismatch")
        await self.store.save_order(order)
        # A stale REST response cannot undo an already confirmed fill or terminal state.
        order = (await self.store.one("SELECT payload FROM broker_orders WHERE client_id=%s", (identity,)))[
            "payload"
        ]
        if order["status"] in TERMINAL:
            if order["status"] == "replaced" or (
                order["status"] == "filled" and number(order["filled_qty"]) != number(payload["qty"])
            ):
                raise ValueError("unexpected terminal pilot state")
            return await self.finish(job, order, now)
        age = (now - parse_time(order.get("submitted_at") or order["created_at"])).total_seconds()
        if age < -30:
            raise ValueError("future pilot order timestamp")
        paused = await self.store.get("manual_pause", False)
        cancel = paused or age >= 90 or (clock["is_open"] and checked_clock(clock, now) <= 60)
        attempts = job.get("cancel_attempts", 0)
        last = job.get("cancel_requested_at")
        due = last is None or (now - parse_time(last)).total_seconds() >= 30
        if cancel and clock["is_open"] and due and attempts < 3:
            checked_clock(clock, utcnow())
            await self.save(
                job, "await_cancel", cancel_attempts=attempts + 1, cancel_requested_at=now.isoformat()
            )
            await self.executor.cancel(order["id"])
            return {"state": "await_cancel", "paper": True}
        if cancel and attempts >= 3:
            await self.save(job, "await_cancel", review_required=True, error="cancel_attempt_limit")
            await self.store.alert(
                "paper-pilot-0062-cancel-review",
                "SPY Paper 시험의 취소 상태를 확인해야 합니다. 재주문은 차단했습니다.",
            )
            return {"state": "await_cancel", "paper": True}
        return await self.save(job, "await_cancel" if last else "await_fill")

    async def tick(self, now=None):
        now = now or utcnow()
        await self.executor.fence()
        await self.controls()
        job = await self.store.get(KEY)
        if job is None and self.arm_once:
            job, blocked = await self.arm(now)
            if blocked:
                return blocked
        if job is None:
            return {"state": "disarmed", "paper": True}
        if (
            job["cycle"] != CYCLE
            or job["paper"] is not True
            or number(job["max_buy_cash"]) != 100
            or job["account_binding"] != self.account_binding
        ):
            raise ValueError("pilot contract changed")
        if job["state"] in FINISHED:
            return {"state": job["state"], "paper": True}
        clock = await self.broker.clock()
        checked_clock(clock, now)
        # Restore a plan frozen just before a crash, without changing its price or identity.
        plan = job.get("plan") or await self.store.get(f"etf-plan:{CYCLE}:cash_funded_buys")
        if plan:
            if (
                len(plan["orders"]) != 1
                or plan["orders"][0]["symbol"] != "SPY"
                or plan["orders"][0]["side"] != "buy"
                or number(plan["reserved_buy_cash"]) + number(plan["cash_buffer"]) > 100
            ):
                raise ValueError("pilot envelope changed")
            if "plan" not in job:
                await self.save(job, "executing", plan=plan)
            intent = await self.store.one(
                "SELECT * FROM intents WHERE client_id=%s", (plan["orders"][0]["client_order_id"],)
            )
            if intent:
                return await self.watch(job, clock, now)
            if job.get("review_required"):
                return {"state": "await_review", "paper": True}
            if (now - parse_time(plan["snapshot_at"])).total_seconds() > 30:
                return await self.save(job, "expired_unsubmitted")
        if await self.store.get("manual_pause", False):
            return {"state": "paused", "paper": True}
        if not clock["is_open"]:
            return {"state": "waiting_market", "paper": True, "next_open": clock["next_open"]}
        if checked_clock(clock, now) < 300:
            return {"state": "waiting_session_window", "paper": True}
        if job.get("review_required") or await self.store.unresolved():
            return {"state": "await_review", "paper": True}
        if not plan:
            snapshot = await self.fresh(job)
            if snapshot["positions"] or snapshot["open_orders"]:
                raise ValueError("pilot requires empty account")
            if "SPY" not in snapshot.get("quotes", {}):
                return {"state": "waiting_quote", "paper": True}
            try:
                plan = allocation(snapshot, CYCLE, utcnow(), pilot=True)
            except ValueError as exc:
                if str(exc) == "stale or future quote":
                    return {"state": "waiting_quote", "paper": True}
                raise
            if not plan.get("orders"):
                return {"state": plan["state"], "paper": True}
            validate_snapshot(plan, plan["orders"][0], snapshot, utcnow())
            await freeze_plan(self.store, plan)
            await self.save(job, "executing", plan=plan)
        result = await AllocationRunner(self.executor, lambda: self.fresh(job), enabled=True).step(plan)
        return {**result, "paper": True}


async def main_async(args):
    if PAPER_URL != "https://paper-api.alpaca.markets":
        raise ValueError("Paper endpoint invariant violated")
    settings, stop = Settings(), asyncio.Event()
    store, broker = Store(settings), Alpaca(settings)
    await store.open()
    notifier = Notifier(settings, store)
    try:
        approval = reviewed_bootstrap(settings)
        if args.check_handoff:
            checks, snapshot = await handoff_check(broker, store, approval)
            available = False
            try:
                async with store.leader("alpaca-" + snapshot["account"]["id"]):
                    available = True
            except RuntimeError:
                pass
            event("paper_handoff_check", checks=checks, account_writer_available=available, broker_writes=0)
            return
        account = await broker.account()
        async with store.leader("alpaca-" + account["id"]) as leader:
            pilot = Pilot(
                Executor(broker, store, leader),
                AccountMonitor(broker, store, approval),
                account_binding=binding(account),
                arm_once=args.arm_once,
            )
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, stop.set)
            next_tick, result, active = utcnow(), {"state": "starting", "paper": True}, False
            while not stop.is_set():
                await pilot.executor.fence()
                await pilot.controls()
                now = utcnow()
                if now >= next_tick:
                    try:
                        result = await pilot.tick()
                    except Exception as exc:
                        result = {"state": "await_review", "paper": True, "error": type(exc).__name__}
                        job = await store.get(KEY)
                        if job:
                            await pilot.save(
                                job, job["state"], review_required=True, error=type(exc).__name__
                            )
                        await store.alert(
                            "paper-pilot-0062-error-review",
                            "SPY Paper 시험에 검토가 필요한 오류가 있습니다. 추가 주문을 차단했습니다.",
                        )
                        event("paper_pilot_error", error=type(exc).__name__)
                    job = await store.get(KEY, {})
                    active = bool(job.get("plan")) and job.get("state") not in FINISHED
                    next_tick = utcnow() + timedelta(seconds=15 if active else 900)
                status = {
                    **result,
                    "owner": "etf_pilot",
                    "heartbeat": utcnow().isoformat(),
                    "monthly_orders_enabled": False,
                    "performance_approved": False,
                    "next_tick_at": next_tick.isoformat(),
                    "job": await store.get(KEY),
                }
                await store.put("etf_pilot", status)
                await store.put(
                    "engine_status",
                    {**status, "state": "paper_pilot_" + result["state"], "order_stream_connected": False},
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
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--arm-once", action="store_true")
    group.add_argument("--check-handoff", action="store_true")
    args = parser.parse_args()
    configure_logging()
    try:
        asyncio.run(main_async(args))
    except Exception as exc:
        event("paper_pilot_failed", error=type(exc).__name__)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
