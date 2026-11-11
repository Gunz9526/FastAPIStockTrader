"""ADR-0081: authenticated, minimal operational health; never an order permission."""

import asyncio
import json

import httpx

from stock_trader.clock import parse_time, utcnow
from stock_trader.config import Settings
from stock_trader.etf_funding import ACTIVE, job_key
from stock_trader.etf_ledger import fingerprint
from stock_trader.operations_policy import CONTRACT as OPS_CONTRACT
from stock_trader.operations_policy import POLICY, heartbeat_check

CONTRACT = {
    "decision": "ADR-0081",
    "schema_version": 1,
    "paper_only": True,
    "order_permission": False,
    "operations_policy_sha256": POLICY,
}
SHA = fingerprint(CONTRACT)


def assess(operations, engine, account, *, active=None, job=None, now=None):
    now = now or utcnow()
    operations, engine, account = [v if isinstance(v, dict) else {} for v in [operations, engine, account]]
    critical, warnings = [], []
    if (
        heartbeat_check(operations, "heartbeat", OPS_CONTRACT["gate_max_age_seconds"], now)["severity"]
        != "ok"
    ):
        critical.append("operations_stale")
    if operations.get("policy_sha256") != POLICY:
        critical.append("operations_policy")
    if (
        operations.get("state") not in {"healthy", "warning"}
        or operations.get("additions_allowed") is not True
    ):
        critical.append("operations_blocked")
    elif operations["state"] == "warning":
        warnings.append("operations_warning")
    if heartbeat_check(engine, "heartbeat", OPS_CONTRACT["engine_max_age_seconds"], now)["severity"] != "ok":
        critical.append("engine_stale")
    if engine.get("owner") != "etf_auto" or engine.get("paper") is not True:
        critical.append("engine_contract")
    if engine.get("state") in {"failed", "paper_auto_await_review", "paper_auto_await_reconciliation"}:
        critical.append("engine_review")
    if (
        heartbeat_check(account, "last_success_at", OPS_CONTRACT["account_max_age_seconds"], now)["severity"]
        != "ok"
    ):
        critical.append("account_stale")
    if account.get("reconciled") is not True:
        critical.append("account_ownership")
    if active and (
        not isinstance(job, dict) or job.get("cycle") != active or job.get("review_required") is True
    ):
        critical.append("active_cycle_review")
    return {
        "schema_version": 1,
        "paper_only": True,
        "contract_sha256": SHA,
        "observed_at": now.isoformat(),
        "state": "blocked" if critical else "warning" if warnings else "healthy",
        "issues": sorted(critical + warnings),
        "broker_requests": 0,
    }


async def load_health(store, now=None):
    active = await store.get(ACTIVE)
    return assess(
        await store.get("operations", {}),
        await store.get("engine_status", {}),
        await store.get("etf_account", {}),
        active=active,
        job=await store.get(job_key(active)) if active else None,
        now=now,
    )


async def probe():
    settings = Settings()
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            response = await client.get(
                "http://127.0.0.1:8000/health/operations",
                headers={"Authorization": "Bearer " + settings.api_token.get_secret_value()},
            )
        value = response.json()
        if (
            value["contract_sha256"] != SHA
            or value["schema_version"] != 1
            or value["paper_only"] is not True
            or value["state"] not in {"healthy", "warning", "blocked"}
            or response.status_code != (503 if value["state"] == "blocked" else 200)
            or not 0 <= (utcnow() - parse_time(value["observed_at"])).total_seconds() <= 30
        ):
            raise ValueError("health response validation failed")
        print(json.dumps(value))
        return 1 if value["state"] == "blocked" else 0
    except httpx.HTTPError, ValueError, KeyError, TypeError:
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "paper_only": True,
                    "state": "unavailable",
                    "issues": ["local_probe_failed"],
                    "observed_at": utcnow().isoformat(),
                }
            )
        )
        return 1


def main():
    raise SystemExit(asyncio.run(probe()))


if __name__ == "__main__":
    main()
