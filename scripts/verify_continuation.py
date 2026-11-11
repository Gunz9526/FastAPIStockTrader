"""ADR-0079~0081 GET-only closed-session audit; run inside the updated API."""

import asyncio
import hashlib
import json
import platform
import time
from pathlib import Path

import httpx

from stock_trader.broker import PAPER_URL, Alpaca
from stock_trader.clock import utcnow
from stock_trader.config import Settings
from stock_trader.etf_funding import ACTIVE, INITIAL, LEGACY_REGISTRY, REGISTRY, job_key
from stock_trader.etf_ledger import fingerprint, number
from stock_trader.etf_pilot import KEY as PILOT
from stock_trader.health import SHA
from stock_trader.operational_study import CONTRACT
from stock_trader.operational_study import REGISTRATION as OPERATIONAL
from stock_trader.operations import POLICY, entry_allowed
from stock_trader.performance import REGISTRATION as PERFORMANCE
from stock_trader.storage import Store


async def main():
    settings = Settings()
    store, broker = Store(settings), Alpaca(settings)
    await store.open()
    try:
        async with httpx.AsyncClient(base_url="http://api:8000", timeout=10) as client:
            assert (await client.get("/health/operations")).status_code == 401
            assert (await client.get("/status")).status_code == 401
            headers = {"Authorization": "Bearer " + settings.api_token.get_secret_value()}
            started = time.monotonic()
            response = await client.get("/health/operations", headers=headers)
            health_seconds = time.monotonic() - started
            assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
            health = response.json()
            assert health["state"] == "healthy" and health["contract_sha256"] == SHA
            assert not {"cash", "positions", "account", "orders"}.intersection(health)
            response = await client.get("/status", headers=headers)
            response.raise_for_status()
            status = response.json()
            ready = await client.get("/health/ready")
            ready.raise_for_status()
        assert status["operations"]["policy_sha256"] == POLICY
        assert entry_allowed(status["operations"], utcnow())
        assert status["etf_auto"]["state"] == "waiting_market" and status["manual_pause"] is False
        operational = status["operational_observation"]
        assert operational["state"] == "not_started" and operational["observed_sessions"] == 0
        assert operational["contract"] == CONTRACT
        assert status["performance"]["state"] == "not_started" and status["performance"]["observations"] == 0
        assert not await store.get(ACTIVE) and not await store.unresolved()
        account, clock = await broker.account(), await broker.clock()
        positions, orders = await broker.positions(), await broker.recent_orders()
        assert PAPER_URL == "https://paper-api.alpaca.markets" and not clock["is_open"]
        assert len(positions) == 1 and positions[0]["symbol"] == "SPY"
        assert number(positions[0]["qty"]) == number("12.918730681")
        assert len(orders) == 2 and all(o["status"] == "filled" for o in orders)
        cps = await store.all("SELECT value FROM kv WHERE key LIKE %s", ("etf-account-checkpoint:%",))
        assert len(cps) == 1
        cp = cps[0]["value"]
        assert all(
            cp["reconciliation"][k] for k in ["matched", "strategy_ownership_verified", "all_fills_owned"]
        )
        assert abs(number(cp["ledger"]["cash"]) - number(account["cash"])) <= number(".01")
        assert number(status["etf_account"]["funding"]["cash_used"]) <= 10000
        contracts = {
            key: await store.get(key)
            for key in [REGISTRY, LEGACY_REGISTRY, PILOT, job_key(INITIAL), PERFORMANCE, OPERATIONAL]
        }
        assert contracts[REGISTRY]["capital_limit"] == "10000"
        assert contracts[job_key(INITIAL)]["state"] == "complete_filled"
        assert contracts[OPERATIONAL]["funding_registration_sha256"] == fingerprint(contracts[REGISTRY])
        for identity in ["operations-0070", "alpaca-" + account["id"], "etf-account-0059"]:
            excluded = False
            try:
                async with store.leader(identity):
                    pass
            except RuntimeError:
                excluded = True
            assert excluded
        counts = {
            t: (await store.one("SELECT count(*) AS n FROM " + t))["n"]
            for t in ["intents", "broker_orders", "controls"]
        }
        assert counts == {"intents": 2, "broker_orders": 2, "controls": 2}
        result = {
            "checked_at": utcnow().isoformat(),
            "python": platform.python_version(),
            "architecture": platform.machine(),
            "paper_only": True,
            "ready": ready.json(),
            "health": health,
            "health_request_seconds": health_seconds,
            "operations": status["operations"],
            "operational_observation": operational,
            "performance": status["performance"],
            "auto": status["etf_auto"],
            "capital_limit": "10000",
            "funding": status["etf_account"]["funding"],
            "cash": account["cash"],
            "equity": account["equity"],
            "positions": {p["symbol"]: p["qty"] for p in positions},
            "recent_orders": len(orders),
            "next_open": clock["next_open"],
            "checkpoint": cp,
            "ledger_sha256": fingerprint(cp["ledger"]),
            "registry_sha256": fingerprint(contracts[REGISTRY]),
            "legacy_registry_sha256": fingerprint(contracts[LEGACY_REGISTRY]),
            "pilot_sha256": fingerprint(contracts[PILOT]),
            "initial_job_sha256": fingerprint(contracts[job_key(INITIAL)]),
            "study_registration_sha256": fingerprint(contracts[PERFORMANCE]),
            "operational_registration_sha256": fingerprint(contracts[OPERATIONAL]),
            "counts": counts,
            "manual_pause": status["manual_pause"],
            "training_policy": status["training_policy"],
            "duplicate_writers_excluded": True,
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
