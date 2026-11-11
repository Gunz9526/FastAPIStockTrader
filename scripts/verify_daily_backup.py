"""ADR-0069: restore a completed managed backup into a disposable database.

Run on the Docker host after verify_etf_auto.py. Never replaces production,
prints database rows, or sends broker requests.
"""

import hashlib
import json
import re
import stat
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = {"schema_version", "kv", "intents", "broker_orders", "controls", "alerts", "audit"}


def execute(*args, **kwargs):
    return subprocess.run(
        ["docker", "compose", "exec", "-T", "db", *args],
        cwd=ROOT,
        check=True,
        timeout=180,
        stderr=subprocess.PIPE,
        **kwargs,
    )


def query(database, sql):
    return execute(
        "psql",
        "-X",
        "-U",
        "trader",
        "-d",
        database,
        "-Atq",
        "-v",
        "ON_ERROR_STOP=1",
        "-c",
        sql,
        stdout=subprocess.PIPE,
        text=True,
    ).stdout.strip()


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def main():
    folder = ROOT / "backups"
    manifests = sorted(folder.glob("paper-db-*.dump.json"))
    if not manifests:
        raise ValueError("no completed backup")
    manifest_path = manifests[-1]
    metadata = json.loads(manifest_path.read_text())
    name = metadata["file"]
    if not re.fullmatch(r"paper-db-[0-9]{8}T[0-9]{6}Z-[A-Za-z0-9]{8}\.dump", name):
        raise ValueError("invalid backup filename")
    dump = folder / name
    if metadata["state"] != "complete" or manifest_path.name != name + ".json":
        raise ValueError("incomplete backup")
    digest = hashlib.sha256(dump.read_bytes()).hexdigest()
    if digest != metadata["sha256"] or dump.stat().st_size != metadata["bytes"]:
        raise ValueError("backup digest mismatch")
    for path, mode in [(folder, 0o700), (dump, 0o600), (manifest_path, 0o600)]:
        if path.is_symlink() or stat.S_IMODE(path.stat().st_mode) != mode:
            raise ValueError("backup permissions mismatch")
    expected = json.loads((ROOT / "verification/etf-0067/auto-after-restart.json").read_text())
    database = "restore_check_0069_" + uuid.uuid4().hex[:12]
    if not re.fullmatch(r"restore_check_0069_[0-9a-f]{12}", database):
        raise ValueError("unsafe restore database")
    created = False
    try:
        execute("createdb", "-U", "trader", database)
        created = True
        with dump.open("rb") as stream:
            execute(
                "pg_restore",
                "-U",
                "trader",
                "-d",
                database,
                "--exit-on-error",
                "--no-owner",
                "--no-acl",
                stdin=stream,
            )
        tables = query(
            database, "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"
        ).splitlines()
        if set(tables) != EXPECTED:
            raise ValueError("restored public schema mismatch")
        restored = {}
        for field, key in [
            ("pilot_sha256", "etf-pilot-job-0062"),
            ("registration_sha256", "etf-auto-registration-0068"),
        ]:
            value = json.loads(query(database, f"SELECT value FROM kv WHERE key='{key}'"))
            restored[field] = fingerprint(value)
            if restored[field] != expected[field]:
                raise ValueError("restored immutable contract mismatch")
        checkpoints = json.loads(
            query(
                database,
                "SELECT jsonb_agg(value) FROM kv WHERE key LIKE 'etf-account-checkpoint:%'",
            )
        )
        if len(checkpoints) != 1:
            raise ValueError("restored account checkpoint mismatch")
        checkpoint = checkpoints[0]
        restored["ledger_sha256"] = fingerprint(checkpoint["ledger"])
        if restored["ledger_sha256"] != expected["ledger_sha256"]:
            raise ValueError("restored ledger mismatch")
        if not checkpoint["reconciliation"]["matched"]:
            raise ValueError("restored account not reconciled")
        activity_types = [row["activity_type"] for row in checkpoint["rows"].values()]
        if sorted(activity_types) != ["FEE", "FILL", "JNLC"]:
            raise ValueError("restored actual pilot activity missing")
        version = query(database, "SHOW server_version")
        if not version.startswith("17.7"):
            raise ValueError("restore version changed")
        counts = {table: int(query(database, f'SELECT count(*) FROM public."{table}"')) for table in tables}
        report = {
            "decision": "ADR-0069",
            "checked_at": datetime.now(UTC).isoformat(),
            "backup": name,
            "sha256": digest,
            "bytes": metadata["bytes"],
            "postgres_version": version,
            "restored": True,
            "restored_contracts": restored,
            "restored_activity_types": activity_types,
            "table_rows": counts,
            "permissions_verified": True,
            "production_database_replaced": False,
            "is_offsite_backup": False,
            "broker_requests": 0,
        }
    finally:
        if created:
            execute("dropdb", "-U", "trader", "--force", database)
    report["temporary_database_removed"] = True
    output = ROOT / "verification/etf-0067/backup-restore.json"
    output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"state": "failed", "error": type(exc).__name__}))
        raise SystemExit(1) from None
