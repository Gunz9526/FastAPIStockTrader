"""Read-only evidence for ADR-0058~0060, inside the deployed account container."""

import asyncio
import json
from decimal import Decimal

import httpx

from stock_trader.clock import utcnow
from stock_trader.config import Settings
from stock_trader.etf_ledger import fingerprint
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
    status = public["etf_account"]
    assert status["state"] == "monitoring" and status["mode"] == "read_only" and status["paper"]
    assert status["orders_submitted"] == 0 and status["reconciled"]
    store = Store(settings)
    await store.open()
    try:
        rows = await store.all("SELECT value FROM kv WHERE key LIKE %s", ("etf-account-checkpoint:%",))
        assert len(rows) == 1
        checkpoint = rows[0]["value"]
        assert (
            checkpoint["reconciliation"]["matched"]
            and checkpoint["reconciliation"]["strategy_ownership_verified"]
        )
        activities = checkpoint["rows"]
        assert len(activities) == 1
        assert all(checkpoint["ledger"]["events"][k] == fingerprint(v) for k, v in activities.items())
        amount = sum(Decimal(r["net_amount"]) for r in activities.values())
        assert amount == Decimal(checkpoint["ledger"]["cash"]) == Decimal(status["cash"]) == 100000
        excluded = False
        try:
            async with store.leader("etf-account-0059"):
                pass
        except RuntimeError:
            excluded = True
        assert excluded
        assert not await store.unresolved()
    finally:
        await store.close()
    print(
        json.dumps(
            {
                "checked_at": utcnow().isoformat(),
                "ready": ready.json(),
                "account": status,
                "probe": public["etf_paper_probe"],
                "training_policy": public["training_policy"],
                "checkpoint_sha256": fingerprint(checkpoint),
                "ledger_sha256": fingerprint(checkpoint["ledger"]),
                "guard_sha256": fingerprint(checkpoint["guard"]),
                "last_full_at": checkpoint["last_full_at"],
                "posted_cash_sum": str(amount),
                "duplicate_monitor_excluded": excluded,
                "broker_write_requests": 0,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
