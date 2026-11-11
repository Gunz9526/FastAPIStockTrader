"""Read-only production evidence; run inside the deployed app container."""

import argparse
import asyncio
import hashlib
import json
import platform
from pathlib import Path

import httpx

from stock_trader.broker import Alpaca
from stock_trader.clock import utcnow
from stock_trader.config import Settings
from stock_trader.etf_shadow import CONTRACT, START, validate_bars
from stock_trader.storage import Store


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def main(account_check):
    settings = Settings()
    async with httpx.AsyncClient(base_url="http://api:8000", timeout=10) as client:
        unauth = await client.get("/status")
        assert unauth.status_code == 401
        ready = await client.get("/health/ready")
        ready.raise_for_status()
        response = await client.get(
            "/status", headers={"Authorization": "Bearer " + settings.api_token.get_secret_value()}
        )
        response.raise_for_status()
        status = response.json()
    shadow = status["etf_shadow"]
    assert shadow["state"] == "observing" and shadow["orders_submitted"] == 0
    assert not shadow["performance_evaluated"]
    policy = status["training_policy"]
    assert policy["state"] == "research_required" and policy["failed_holdout_end"] == "2026-10-01"
    root = settings.data_dir / "etf-shadow"
    hashes, sessions = {}, []
    for path in sorted(root.rglob("*.json")):
        assert not path.is_symlink()
        envelope = json.loads(path.read_text())
        payload = envelope["payload"]
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        assert hashlib.sha256(encoded).hexdigest() == envelope["sha256"]
        hashes[str(path.relative_to(root))] = sha(path)
        if path.parent.name == "daily":
            assert payload["date"] >= START.isoformat()
            validate_bars({"bars": payload["bars"]}, payload["session"])
            sessions.append(payload["date"])
        elif path.parent.name == "decisions":
            assert payload["orders_submitted"] == 0 and payload["target_weights"] == {"SPY": "1"}
    assert len(sessions) == shadow["sessions"] and max(sessions) == shadow["latest_completed_session"]
    registration = json.loads((root / "registration.json").read_text())["payload"]
    assert registration["contract"] == CONTRACT
    guard = settings.report_dir / "final-test-consumed-0025.json"
    guard_sha = sha(guard)
    assert guard_sha == "d4b630dbf77191269fa4da5b6e5925779ce75d752978f2265aeca6d975eb1129"
    store = Store(settings)
    await store.open()
    excluded = False
    try:
        try:
            async with store.leader("etf-shadow-0053"):
                pass
        except RuntimeError:
            excluded = True
    finally:
        await store.close()
    assert excluded, "deployed worker must own leader lock"
    result = {
        "checked_at": utcnow().isoformat(),
        "python": platform.python_version(),
        "architecture": platform.machine(),
        "unauthenticated_status": unauth.status_code,
        "readiness": ready.json(),
        "training_policy": policy,
        "shadow": shadow,
        "registration": registration,
        "executed_source_sha256": sha(Path("/app/src/stock_trader/etf_shadow.py")),
        "file_sha256": hashes,
        "sessions": sessions,
        "duplicate_worker_excluded": excluded,
        "final_guard_sha256": guard_sha,
        "model_pointer_exists": (settings.artifact_dir / "current").exists(),
        "account_read_requests": 0,
    }
    assert not result["model_pointer_exists"]
    if account_check:
        broker = Alpaca(settings)
        try:
            account = await broker.account()
            positions = await broker.positions()
            orders = await broker.recent_orders()
            result["account"] = {
                "status": account["status"],
                "equity": float(account["equity"]),
                "positions": len(positions),
                "recent_orders": len(orders),
            }
            result["account_read_requests"] = 3
        finally:
            await broker.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--account", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args.account))
