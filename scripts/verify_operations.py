"""Read-only ADR-0070 deployment and restart audit during a closed session."""

import asyncio
import hashlib
import json
import platform
from pathlib import Path

import httpx

from stock_trader.broker import PAPER_URL, Alpaca
from stock_trader.clock import utcnow
from stock_trader.config import Settings
from stock_trader.etf_auto import ACTIVE, REGISTRY
from stock_trader.etf_ledger import fingerprint, number
from stock_trader.etf_pilot import KEY as PILOT
from stock_trader.operations import POLICY, entry_allowed
from stock_trader.storage import Store


async def main():
    settings = Settings()
    store, broker = Store(settings), Alpaca(settings)
    await store.open()
    try:
        async with httpx.AsyncClient(base_url="http://api:8000", timeout=10) as client:
            assert (await client.get("/status")).status_code == 401
            ready = await client.get("/health/ready")
            ready.raise_for_status()
            response = await client.get(
                "/status", headers={"Authorization": "Bearer " + settings.api_token.get_secret_value()}
            )
            response.raise_for_status()
            public = response.json()
        ops, auto = public["operations"], public["etf_auto"]
        assert ops["policy_sha256"] == POLICY and ops["state"] == "healthy"
        assert entry_allowed(ops, utcnow()) and all(v["severity"] == "ok" for v in ops["checks"].values())
        assert auto["owner"] == "etf_auto" and auto["state"] == "waiting_market"
        assert not await store.get(ACTIVE) and not await store.unresolved()
        account, clock = await broker.account(), await broker.clock()
        positions, orders = await broker.positions(), await broker.recent_orders()
        assert not clock["is_open"] and PAPER_URL == "https://paper-api.alpaca.markets"
        assert number(account["cash"]) == number("99900.30") and len(orders) == len(positions) == 1
        assert orders[0]["status"] == "filled" and positions[0]["symbol"] == "SPY"
        assert number(positions[0]["qty"]) == number("0.127959082")
        excluded = []
        for identity in ["operations-0070", "alpaca-" + account["id"]]:
            try:
                async with store.leader(identity):
                    pass
            except RuntimeError:
                excluded.append(True)
        assert excluded == [True, True]
        checkpoints = await store.all("SELECT value FROM kv WHERE key LIKE %s", ("etf-account-checkpoint:%",))
        assert len(checkpoints) == 1
        checkpoint = checkpoints[0]["value"]
        registry, pilot = await store.get(REGISTRY), await store.get(PILOT)
        assert registry["capital_limit"] == "1000" and pilot["state"] == "complete_filled"
        result = {
            "checked_at": utcnow().isoformat(),
            "python": platform.python_version(),
            "architecture": platform.machine(),
            "paper_only": True,
            "ready": ready.json(),
            "operations": ops,
            "auto_state": auto["state"],
            "auto_heartbeat": auto["heartbeat"],
            "capital_limit": registry["capital_limit"],
            "funding": public["etf_account"]["funding"],
            "positions": {p["symbol"]: p["qty"] for p in positions},
            "cash": account["cash"],
            "recent_orders": len(orders),
            "next_open": clock["next_open"],
            "registry_sha256": fingerprint(registry),
            "pilot_sha256": fingerprint(pilot),
            "ledger_sha256": fingerprint(checkpoint["ledger"]),
            "guard": checkpoint["guard"],
            "training_policy": public["training_policy"],
            "manual_pause": public["manual_pause"],
            "counts": {
                t: (await store.one("SELECT count(*) AS n FROM " + t))["n"]
                for t in ["intents", "broker_orders", "controls"]
            },
            "duplicate_monitor_excluded": True,
            "duplicate_account_writer_excluded": True,
            "broker_read_requests": 4,
            "broker_write_requests": 0,
            "source_sha256": {
                str(p.relative_to(Path("/app/src"))): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(Path("/app/src").rglob("*.py"))
            },
        }
        print(json.dumps(result, indent=2))
    finally:
        await broker.close()
        await store.close()


if __name__ == "__main__":
    asyncio.run(main())
