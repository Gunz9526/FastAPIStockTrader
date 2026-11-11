"""ADR-0059/0060: ongoing reconciliation and unsubmitted initial/monthly plans."""

import argparse
import asyncio
import signal

from stock_trader.allocation import freeze_plan, plan_allocation
from stock_trader.broker import PAPER_URL, Alpaca
from stock_trader.clock import NEW_YORK, utcnow
from stock_trader.config import Settings
from stock_trader.etf_account import AccountMonitor, market_snapshot, reviewed_bootstrap
from stock_trader.etf_execution import AllocationRunner, validate_snapshot
from stock_trader.etf_ledger import number
from stock_trader.etf_shadow import read_sealed
from stock_trader.execution import Executor
from stock_trader.logging import configure_logging, event
from stock_trader.storage import Store


def allocation(snapshot, cycle, now, *, pilot=False):
    if not snapshot["reconciled"]:
        return {"state": "ownership_mismatch", "orders": []}
    plan = plan_allocation(
        cycle=cycle,
        weights={"SPY": "1"},
        positions=snapshot["positions"],
        owned=snapshot["owned"],
        cash=min(number(snapshot["account"]["cash"]), number(100)) if pilot else snapshot["account"]["cash"],
        quotes=snapshot.get("quotes", {}),
        assets=snapshot.get("assets", {}),
        now=now,
        market_open=snapshot["market_open"],
        open_orders=snapshot["open_orders"],
    )
    if not snapshot["guard"]["additions_allowed"] and plan["state"] == "cash_funded_buys":
        return {"state": "additions_paused", "orders": []}
    return plan


class Service:
    def __init__(self, settings, store, broker, approval):
        self.settings, self.store, self.broker = settings, store, broker
        self.monitor = AccountMonitor(broker, store, approval)

    async def tick(self):
        snapshot = await market_snapshot(self.broker, await self.monitor.sync())
        now = utcnow()
        day = now.astimezone(NEW_YORK).date()
        path = self.settings.data_dir / "etf-shadow" / "calendar" / f"{day:%Y-%m}.json"
        sessions = read_sealed(path)["sessions"] if path.exists() else []
        first = bool(sessions) and day.isoformat() == sessions[0]["date"]
        initial = not await self.store.get("etf-initial-plan-recorded", False)
        cycle = "etf-initial-0059" if initial else f"etf-monthly-0059-{day:%Y-%m}"
        proposed = {"state": "not_decision_day", "orders": []}
        if initial or first:
            key = f"etf-plan:{cycle}:cash_funded_buys"
            existing = await self.store.get(key) or await self.store.get(f"etf-plan:{cycle}:sell_first")
            if existing:
                proposed = {"state": "plan_already_frozen", "orders": existing["orders"]}
                if initial:
                    await self.store.put("etf-initial-plan-recorded", True)
            else:
                proposed = allocation(snapshot, cycle, now)
                if proposed.get("orders"):
                    await freeze_plan(self.store, proposed)
                    if initial:
                        await self.store.put("etf-initial-plan-recorded", True)
        status = {
            "state": "monitoring",
            "paper": True,
            "updated_at": now.isoformat(),
            "last_success_at": snapshot["last_success_at"],
            "mode": "read_only",
            "activity_count": snapshot["activity_count"],
            "full_audit": snapshot["full_audit"],
            "read_requests": snapshot["read_requests"] + snapshot["market_read_requests"],
            "cash": snapshot["account"]["cash"],
            "equity": snapshot["account"]["equity"],
            "positions": snapshot["positions"],
            "owned": snapshot["owned"],
            "reconciled": snapshot["reconciled"],
            "guard": snapshot["guard"],
            "funding": snapshot["funding"],
            "market_open": snapshot["market_open"],
            "next_open": snapshot["clock"]["next_open"],
            "plan_state": proposed["state"],
            "planned_orders": len(proposed.get("orders", [])),
            "orders_submitted": 0,
            "performance_approved": False,
        }
        await self.store.put("etf_account", status)
        return status

    async def run(self, stop):
        async with self.store.leader("etf-account-0059") as leader:
            while not stop.is_set():
                await leader.execute("SELECT 1")
                try:
                    await self.tick()
                except Exception as exc:
                    previous = await self.store.get("etf_account", {})
                    await self.store.put(
                        "etf_account",
                        {
                            **previous,
                            "state": "failed",
                            "updated_at": utcnow().isoformat(),
                            "error": type(exc).__name__,
                            "orders_submitted": 0,
                        },
                    )
                    event("etf_account_error", error=type(exc).__name__)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=900)
                except TimeoutError:
                    pass


async def probe(broker, store, approval, *, submit=False):
    if PAPER_URL != "https://paper-api.alpaca.markets":
        raise ValueError("Paper endpoint invariant violated")
    if await store.get("manual_pause", False):
        return {"state": "manually_paused", "paper": True, "orders_submitted": 0}
    if await store.unresolved():
        return {"state": "await_reconciliation", "paper": True, "orders_submitted": 0}
    clock = await broker.clock()
    if not clock["is_open"]:
        return {
            "state": "market_closed",
            "paper": True,
            "next_open": clock["next_open"],
            "orders_submitted": 0,
        }
    monitor = AccountMonitor(broker, store, approval)
    snapshot = await market_snapshot(broker, await monitor.sync())
    if snapshot["positions"] or snapshot["open_orders"]:
        return {"state": "probe_requires_empty_account", "orders_submitted": 0, "paper": True}
    now = utcnow()
    plan = allocation(snapshot, "paper-probe-0060", now, pilot=True)
    if not plan.get("orders"):
        return {"state": plan["state"], "paper": True, "orders_submitted": 0}
    for payload in plan["orders"]:
        validate_snapshot(plan, payload, snapshot, now)
    if not submit:
        return {"state": "validated_not_submitted", "paper": True, "plan": plan, "orders_submitted": 0}
    manager = store.leader("alpaca-" + snapshot["account"]["id"])
    try:
        leader = await manager.__aenter__()
    except RuntimeError:
        return {"state": "account_writer_active", "paper": True, "orders_submitted": 0}
    try:

        async def refresh():
            return await market_snapshot(broker, await monitor.sync())

        runner = AllocationRunner(Executor(broker, store, leader), refresh, enabled=True)
        result = await runner.step(plan)
        return {**result, "paper": True}
    finally:
        await manager.__aexit__(None, None, None)


async def main_async(args):
    settings, stop = Settings(), asyncio.Event()
    store, broker = Store(settings), Alpaca(settings)
    await store.open()
    try:
        approval = reviewed_bootstrap(settings)
        if args.probe:
            result = await probe(broker, store, approval, submit=args.submit)
            await store.put("etf_paper_probe", {**result, "checked_at": utcnow().isoformat()})
            event("etf_paper_probe", **result)
            return
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        await Service(settings, store, broker, approval).run(stop)
    finally:
        await broker.close()
        await store.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--submit", action="store_true")
    args = parser.parse_args()
    if args.submit and not args.probe:
        parser.error("--submit requires --probe")
    configure_logging()
    try:
        asyncio.run(main_async(args))
    except Exception as exc:
        event("etf_account_failed", error=type(exc).__name__)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
