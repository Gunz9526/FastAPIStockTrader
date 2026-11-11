"""Read-only deployed limited-Paper evidence, before the next regular session."""

import asyncio
import hashlib
import json
import platform
from pathlib import Path

import httpx

from stock_trader.broker import PAPER_URL, Alpaca
from stock_trader.clock import parse_time, utcnow
from stock_trader.config import Settings
from stock_trader.etf_auto import ACTIVE, REGISTRY
from stock_trader.etf_ledger import fingerprint, number
from stock_trader.etf_pilot import KEY, binding
from stock_trader.storage import Store


async def main():
    settings = Settings()
    async with httpx.AsyncClient(base_url="http://api:8000", timeout=10) as client:
        assert (await client.get("/status")).status_code == 401
        ready = await client.get("/health/ready")
        ready.raise_for_status()
        response = await client.get(
            "/status", headers={"Authorization": "Bearer " + settings.api_token.get_secret_value()}
        )
        response.raise_for_status()
        public = response.json()
    auto = public["etf_auto"]
    assert auto["owner"] == public["engine"]["owner"] == "etf_auto"
    assert auto["paper"] and auto["monthly_orders_enabled"] and not auto["performance_approved"]
    assert auto["state"] == "waiting_market" and not auto["active_cycle"]
    assert 0 <= (utcnow() - parse_time(auto["heartbeat"])).total_seconds() < 90
    store, broker = Store(settings), Alpaca(settings)
    await store.open()
    try:
        registry = await store.get(REGISTRY)
        pilot = await store.get(KEY)
        assert registry["capital_limit"] == "1000" and registry == auto["registration"]
        assert not await store.get(ACTIVE) and not await store.unresolved()
        account, clock = await broker.account(), await broker.clock()
        positions, orders = await broker.positions(), await broker.recent_orders()
        assert PAPER_URL == "https://paper-api.alpaca.markets" and not clock["is_open"]
        assert binding(account) == registry["account_binding"]
        assert len(orders) == 1 and orders[0]["status"] == "filled"
        assert pilot["state"] == "complete_filled" and len(positions) == 1
        assert number(positions[0]["qty"]) == number(pilot["filled_qty"])
        assert number(account["cash"]) == number("99900.30")
        excluded = False
        try:
            async with store.leader("alpaca-" + account["id"]):
                pass
        except RuntimeError:
            excluded = True
        assert excluded
        checkpoints = await store.all("SELECT value FROM kv WHERE key LIKE %s", ("etf-account-checkpoint:%",))
        assert len(checkpoints) == 1
        checkpoint = checkpoints[0]["value"]
        assert (
            checkpoint["reconciliation"]["matched"]
            and checkpoint["reconciliation"]["strategy_ownership_verified"]
        )
        assert public["etf_account"]["funding"]["cash_used"] == "99.7"
        result = {
            "checked_at": utcnow().isoformat(),
            "paper_only": True,
            "python": platform.python_version(),
            "architecture": platform.machine(),
            "ready": ready.json(),
            "state": auto["state"],
            "heartbeat": auto["heartbeat"],
            "registration_sha256": fingerprint(registry),
            "pilot_sha256": fingerprint(pilot),
            "ledger_sha256": fingerprint(checkpoint["ledger"]),
            "guard_sha256": fingerprint(checkpoint["guard"]),
            "capital_limit": "1000",
            "funding": public["etf_account"]["funding"],
            "monthly_orders_enabled": True,
            "active_cycle": None,
            "duplicate_account_writer_excluded": excluded,
            "cash": account["cash"],
            "positions": [{"symbol": p["symbol"], "qty": p["qty"]} for p in positions],
            "recent_orders": len(orders),
            "next_open": clock["next_open"],
            "training_policy": public["training_policy"],
            "broker_read_requests": 4,
            "broker_write_requests": 0,
            "source_sha256": {
                p: hashlib.sha256((Path("/app/src/stock_trader") / p).read_bytes()).hexdigest()
                for p in ["etf_auto.py", "etf_account.py", "etf_service.py", "api.py"]
            },
        }
        print(json.dumps(result, indent=2))
    finally:
        await broker.close()
        await store.close()


if __name__ == "__main__":
    asyncio.run(main())
