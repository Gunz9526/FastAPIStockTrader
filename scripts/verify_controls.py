"""ADR-0072~0077 closed-session deployment audit; no valid control or order POST."""

import asyncio
import hashlib
import json
import platform
from pathlib import Path

import httpx

from stock_trader.broker import PAPER_URL, Alpaca
from stock_trader.clock import utcnow
from stock_trader.config import Settings
from stock_trader.etf_funding import ACTIVE, INITIAL, LEGACY_REGISTRY, LIMIT, REGISTRY, job_key
from stock_trader.etf_ledger import fingerprint, number
from stock_trader.etf_pilot import KEY as PILOT
from stock_trader.operations import POLICY, entry_allowed
from stock_trader.performance import CONTRACT
from stock_trader.performance import REGISTRATION as STUDY
from stock_trader.storage import Store


async def main():
    settings = Settings()
    store, broker = Store(settings), Alpaca(settings)
    await store.open()
    try:
        async with httpx.AsyncClient(base_url="http://api:8000", timeout=10) as client:
            assert (await client.get("/status")).status_code == 401
            assert (await client.get("/control-preview")).status_code == 401
            ready = await client.get("/health/ready")
            ready.raise_for_status()
            headers = {"Authorization": "Bearer " + settings.api_token.get_secret_value()}
            response = await client.get("/status", headers=headers)
            response.raise_for_status()
            public = response.json()
            response = await client.get("/control-preview", headers=headers)
            response.raise_for_status()
            reviewed = response.json()
            for command in ["reset-risk", "flatten", "reduce"]:
                assert (
                    await client.post("/controls", json={"command": command}, headers=headers)
                ).status_code == 422
        ops, auto, performance = public["operations"], public["etf_auto"], public["performance"]
        assert ops["policy_sha256"] == POLICY and ops["state"] == "healthy"
        assert entry_allowed(ops, utcnow()) and all(v["severity"] == "ok" for v in ops["checks"].values())
        assert auto["owner"] == "etf_auto" and auto["state"] == "waiting_market"
        assert not await store.get(ACTIVE) and not await store.unresolved()
        assert not await store.get(job_key(INITIAL)) and public["manual_pause"] is False
        assert reviewed["capital_limit"] == str(LIMIT) == "10000"
        assert performance["state"] == "not_started" and performance["observations"] == 0
        assert performance["contract"] == CONTRACT and not performance["performance_approved"]
        account, clock = await broker.account(), await broker.clock()
        positions, orders = await broker.positions(), await broker.recent_orders()
        assert not clock["is_open"] and PAPER_URL == "https://paper-api.alpaca.markets"
        assert number(account["cash"]) == number("99900.30") and len(orders) == len(positions) == 1
        assert orders[0]["status"] == "filled" and positions[0]["symbol"] == "SPY"
        assert number(positions[0]["qty"]) == number("0.127959082")
        excluded = []
        for identity in ["operations-0070", "alpaca-" + account["id"], "etf-account-0059"]:
            try:
                async with store.leader(identity):
                    pass
            except RuntimeError:
                excluded.append(True)
        assert excluded == [True, True, True]
        cps = await store.all("SELECT value FROM kv WHERE key LIKE %s", ("etf-account-checkpoint:%",))
        assert len(cps) == 1
        cp = cps[0]["value"]
        assert cp["reconciliation"]["matched"] and cp["reconciliation"]["all_fills_owned"]
        assert cp["reconciliation"]["strategy_ownership_verified"]
        registry, old, pilot, study = [
            await store.get(key) for key in [REGISTRY, LEGACY_REGISTRY, PILOT, STUDY]
        ]
        assert registry["capital_limit"] == str(LIMIT) and old["capital_limit"] == "1000"
        assert registry["predecessor_sha256"] == fingerprint(old)
        assert study["contract"] == CONTRACT and study["funding_registration_sha256"] == fingerprint(registry)
        counts = {
            t: (await store.one("SELECT count(*) AS n FROM " + t))["n"]
            for t in ["intents", "broker_orders", "controls"]
        }
        assert counts == {"intents": 1, "broker_orders": 1, "controls": 2}
        versions = [
            r["version"] for r in await store.all("SELECT version FROM schema_version ORDER BY version")
        ]
        assert versions == [1, 2]
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
            "legacy_registry_sha256": fingerprint(old),
            "registry_sha256": fingerprint(registry),
            "pilot_sha256": fingerprint(pilot),
            "ledger_sha256": fingerprint(cp["ledger"]),
            "reconciliation": cp["reconciliation"],
            "guard": cp["guard"],
            "training_policy": public["training_policy"],
            "manual_pause": public["manual_pause"],
            "counts": counts,
            "schema_versions": versions,
            "control_preview": reviewed,
            "performance": performance,
            "study_registration_sha256": fingerprint(study),
            "duplicate_writers_excluded": True,
            "broker_read_requests": 4,
            "broker_write_requests": 0,
            "advanced_requests_enqueued": 0,
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
