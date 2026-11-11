"""ADR-0070: local operations checks, independent of broker connectivity."""

import asyncio
import hashlib
import json
import re
import shutil
import signal
import stat
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

from psycopg.types.json import Jsonb

from stock_trader.clock import NEW_YORK, utcnow
from stock_trader.config import Settings
from stock_trader.etf_ledger import fingerprint
from stock_trader.etf_shadow import START, eligible_through, read_sealed
from stock_trader.logging import configure_logging, event
from stock_trader.notifications import Notifier
from stock_trader.operations_policy import CONTRACT as CONTRACT
from stock_trader.operations_policy import POLICY as POLICY
from stock_trader.operations_policy import entry_allowed as entry_allowed
from stock_trader.operations_policy import heartbeat_check as heartbeat_check
from stock_trader.storage import Store

KEY = "operations"


def disk_check(usage):
    if usage.total <= 0 or not 0 <= usage.free <= usage.total:
        raise ValueError("invalid disk observation")
    fraction = usage.free / usage.total
    severity = (
        "critical"
        if usage.free < CONTRACT["disk_critical_bytes"] or fraction < CONTRACT["disk_critical_fraction"]
        else "warning"
        if usage.free < CONTRACT["disk_warning_bytes"] or fraction < CONTRACT["disk_warning_fraction"]
        else "ok"
    )
    return {"severity": severity, "state": "space", "free_bytes": usage.free, "total_bytes": usage.total}


class BackupCheck:
    def __init__(self, folder):
        self.folder, self.cached = Path(folder), None

    def check(self, now):
        try:
            manifests = sorted(self.folder.glob("paper-db-*.dump.json"))
            if not manifests:
                return {"severity": "critical", "state": "missing"}
            manifest = manifests[-1]
            metadata = json.loads(manifest.read_text())
            name = metadata["file"]
            if (
                metadata["state"] != "complete"
                or not re.fullmatch(r"paper-db-[0-9]{8}T[0-9]{6}Z-[A-Za-z0-9]{8}\.dump", name)
                or manifest.name != name + ".json"
                or not name.startswith("paper-db-" + metadata["created_at"] + "-")
            ):
                raise ValueError("invalid completed backup")
            dump = self.folder / name
            signature = []
            for path, mode in [(self.folder, 0o700), (manifest, 0o600), (dump, 0o600)]:
                info = path.stat()
                if path.is_symlink() or stat.S_IMODE(info.st_mode) != mode:
                    raise ValueError("invalid backup permissions")
                signature.append((str(path), info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns))
            if dump.stat().st_size <= 0 or dump.stat().st_size != metadata["bytes"]:
                raise ValueError("invalid backup size")
            if signature != self.cached:
                with dump.open("rb") as stream:
                    digest = hashlib.file_digest(stream, "sha256").hexdigest()
                if digest != metadata["sha256"]:
                    raise ValueError("invalid backup checksum")
                self.cached = signature
            created = datetime.strptime(metadata["created_at"], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
            age = (now - created).total_seconds()
            if age < -30:
                raise ValueError("future backup")
            stale = age > CONTRACT["backup_max_age_hours"] * 3600
            return {
                "severity": "critical" if stale else "ok",
                "state": "stale" if stale else "verified",
                "file": name,
                "sha256": metadata["sha256"],
                "bytes": metadata["bytes"],
                "created_at": created.isoformat(),
            }
        except OSError, ValueError, KeyError, TypeError:
            return {"severity": "critical", "state": "invalid"}


def daily_check(root, shadow, now):
    root = Path(root) / "etf-shadow"
    cutoff = eligible_through(now)
    months = {cutoff.replace(day=1), (cutoff.replace(day=1) - timedelta(days=1)).replace(day=1)}
    try:
        current = root / "calendar" / f"{cutoff:%Y-%m}.json"
        if not current.exists():
            return {"severity": "warning", "state": "calendar_missing"}
        sessions = []
        for month in sorted(months):
            path = root / "calendar" / f"{month:%Y-%m}.json"
            if path.exists():
                sessions.extend(date.fromisoformat(row["date"]) for row in read_sealed(path)["sessions"])
        expected = max((day for day in sessions if START <= day <= cutoff), default=None)
        if expected is None:
            return {"severity": "ok", "state": "not_due"}
        available = datetime.combine(expected + timedelta(days=1), time(6), NEW_YORK)
        grace = now < available + timedelta(seconds=CONTRACT["daily_grace_seconds"])
        reported = shadow.get("latest_completed_session")
        good = (
            shadow.get("state") == "observing"
            and reported is not None
            and expected <= date.fromisoformat(reported) <= cutoff
        )
        if not good:
            return {
                "severity": "ok" if grace else "warning",
                "state": "grace" if grace else "late",
                "expected_session": expected.isoformat(),
                "latest_session": reported,
            }
        payload = read_sealed(root / "daily" / f"{expected}.json")
        if payload["date"] != expected.isoformat():
            raise ValueError("daily date mismatch")
        return {"severity": "ok", "state": "current", "expected_session": expected.isoformat()}
    except OSError, ValueError, KeyError, TypeError:
        return {"severity": "critical", "state": "invalid"}


class Monitor:
    def __init__(self, settings, store, backup_dir=Path("/backups")):
        self.settings, self.store = settings, store
        self.backup = BackupCheck(backup_dir)

    async def publish(self, checks, now):
        issues = {k: v for k, v in checks.items() if v["severity"] != "ok"}
        signature = fingerprint({k: (v["state"], v["severity"]) for k, v in issues.items()})
        previous = await self.store.get(KEY, {})
        changed = previous.get("issue_sha256") != signature
        transition = previous.get("transition", 0) + int(changed)
        blocked = any(v["severity"] == "critical" for v in issues.values())
        status = {
            "state": "blocked" if blocked else "warning" if issues else "healthy",
            "heartbeat": now.isoformat(),
            "policy_sha256": POLICY,
            "contract": CONTRACT,
            "additions_allowed": not blocked,
            "checks": checks,
            "issues": sorted(issues),
            "issue_sha256": signature,
            "transition": transition,
            "broker_requests": 0,
        }
        async with self.store.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO kv(key,value) VALUES(%s,%s) ON CONFLICT(key) DO UPDATE "
                "SET value=excluded.value,updated_at=now()",
                (KEY, Jsonb(status)),
            )
            if changed and (issues or previous.get("issues")):
                summary = ", ".join(f"{k}:{v['state']}" for k, v in sorted(issues.items()))
                content = (
                    "운영 감시: 신규 매수 차단. "
                    if blocked
                    else "운영 감시: 경고. "
                    if issues
                    else "운영 감시: 회복. "
                ) + summary
                await conn.execute(
                    "INSERT INTO alerts(key,content) VALUES(%s,%s) ON CONFLICT DO NOTHING",
                    (f"operations-0070:{transition}", content),
                )
                await conn.execute(
                    "INSERT INTO audit(kind,payload) VALUES(%s,%s)",
                    ("operations_transition", Jsonb({"transition": transition, "issues": sorted(issues)})),
                )
        return status

    async def tick(self, now=None, usage=None):
        now = now or utcnow()
        engine = await self.store.get("engine_status", {})
        account = await self.store.get("etf_account", {})
        shadow = await self.store.get("etf_shadow", {})
        try:
            disk = disk_check(usage or shutil.disk_usage(self.backup.folder))
        except OSError, ValueError:
            disk = {"severity": "critical", "state": "unavailable"}
        checks = {
            "disk": disk,
            "backup": self.backup.check(now),
            "engine": heartbeat_check(engine, "heartbeat", CONTRACT["engine_max_age_seconds"], now),
            "account": heartbeat_check(account, "last_success_at", CONTRACT["account_max_age_seconds"], now),
            "daily": daily_check(self.settings.data_dir, shadow, now),
        }
        if account.get("reconciled") is False:
            checks["account"] = {"severity": "critical", "state": "ownership_unverified"}
        return await self.publish(checks, now)


async def run():
    settings = Settings(_env_file=None, _secrets_dir=Path("/run/secrets"))
    store, stop = Store(settings), asyncio.Event()
    await store.open()
    notifier = Notifier(settings, store)
    try:
        from stock_trader.health import load_health
        from stock_trader.operational_study import OperationalStudy
        from stock_trader.performance import Study

        study = Study(settings, store)
        operational = OperationalStudy(settings, store)
        monitor = Monitor(settings, store)
        for sig in (signal.SIGTERM, signal.SIGINT):
            asyncio.get_running_loop().add_signal_handler(sig, stop.set)
        async with store.leader("operations-0070") as leader:
            while not stop.is_set():
                await leader.execute("SELECT 1")
                try:
                    await monitor.tick()
                    await study.tick()
                    observed_at = utcnow()
                    await operational.tick(await load_health(store, observed_at), observed_at)
                    await notifier.flush()
                except Exception as exc:
                    event("operations_error", error=type(exc).__name__)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=CONTRACT["poll_seconds"])
                except TimeoutError:
                    pass
    finally:
        await notifier.close()
        await store.close()


def main():
    configure_logging()
    try:
        asyncio.run(run())
    except Exception as exc:
        event("operations_failed", error=type(exc).__name__)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
