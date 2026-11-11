"""Read-only weekend evidence after ADR-0062~0064 writer handoff."""

import asyncio
import hashlib
import json
import platform
from pathlib import Path

import httpx

from stock_trader.broker import PAPER_URL, Alpaca
from stock_trader.clock import parse_time, utcnow
from stock_trader.config import Settings
from stock_trader.etf_ledger import fingerprint
from stock_trader.etf_pilot import CYCLE, KEY, binding
from stock_trader.storage import Store


async def main():
    settings = Settings()
    async with httpx.AsyncClient(base_url="http://api:8000", timeout=10) as client:
        assert (await client.get("/status")).status_code == 401
        ready = await client.get("/health/ready")
        ready.raise_for_status()
        public = await client.get(
            "/status", headers={"Authorization": "Bearer " + settings.api_token.get_secret_value()}
        )
        public.raise_for_status()
        public = public.json()
    engine, pilot = public["engine"], public["etf_pilot"]
    assert engine["owner"] == "etf_pilot" and pilot["paper"]
    assert 0 <= (utcnow() - parse_time(engine["heartbeat"])).total_seconds() < 90
    assert pilot["state"] == "waiting_market" and pilot["job"]["state"] == "armed"
    assert not pilot["performance_approved"] and not pilot["monthly_orders_enabled"]
    assert "plan" not in pilot["job"]
    store, broker = Store(settings), Alpaca(settings)
    await store.open()
    try:
        job = await store.get(KEY)
        assert job == pilot["job"] and job["cycle"] == CYCLE and job["max_buy_cash"] == "100"
        assert not await store.all("SELECT * FROM intents")
        assert not await store.one("SELECT id FROM controls WHERE state='pending' LIMIT 1")
        account, clock = await broker.account(), await broker.clock()
        positions, orders = await broker.positions(), await broker.recent_orders()
        assert PAPER_URL == "https://paper-api.alpaca.markets"
        assert account["status"] == "ACTIVE" and account["cash"] == "100000"
        assert not positions and not orders and not clock["is_open"]
        assert binding(account) == job["account_binding"]
        excluded = False
        try:
            async with store.leader("alpaca-" + account["id"]):
                pass
        except RuntimeError:
            excluded = True
        assert excluded
        rows = await store.all("SELECT value FROM kv WHERE key LIKE %s", ("etf-account-checkpoint:%",))
        assert len(rows) == 1
        checkpoint = rows[0]["value"]
        assert checkpoint["reconciliation"]["matched"]
        assert checkpoint["reconciliation"]["strategy_ownership_verified"]
        assert checkpoint["ledger"]["cash"] == "100000"
    finally:
        await store.close()
        await broker.close()
    print(
        json.dumps(
            {
                "checked_at": utcnow().isoformat(),
                "python": platform.python_version(),
                "architecture": platform.machine(),
                "paper_only": True,
                "ready": ready.json(),
                "owner": engine["owner"],
                "engine_state": engine["state"],
                "heartbeat": engine["heartbeat"],
                "armed_at": job["armed_at"],
                "pilot_state": job["state"],
                "pilot_job_sha256": fingerprint(job),
                "pilot_plan_present": False,
                "pilot_max_buy_cash": job["max_buy_cash"],
                "monthly_orders_enabled": False,
                "duplicate_account_writer_excluded": excluded,
                "cash": account["cash"],
                "positions": 0,
                "recent_orders": 0,
                "clock_open": clock["is_open"],
                "next_open": clock["next_open"],
                "ledger_sha256": fingerprint(checkpoint["ledger"]),
                "guard_sha256": fingerprint(checkpoint["guard"]),
                "training_policy": public["training_policy"],
                "broker_read_requests": 4,
                "broker_write_requests": 0,
                "source_sha256": hashlib.sha256(
                    Path("/app/src/stock_trader/etf_pilot.py").read_bytes()
                ).hexdigest(),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
