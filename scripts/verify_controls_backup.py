"""ADR-0076: restore the post-migration managed backup without touching production."""

import hashlib
import json
import re
import stat
import uuid
from datetime import UTC, datetime
from pathlib import Path

from verify_daily_backup import EXPECTED, execute, fingerprint, query

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "verification/controls-0072"


def main():
    folder = ROOT / "backups"
    manifest_path = sorted(folder.glob("paper-db-*.dump.json"))[-1]
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
    expected = json.loads((EVIDENCE / "after-restart.json").read_text())
    database = "restore_check_0076_" + uuid.uuid4().hex[:12]
    if not re.fullmatch(r"restore_check_0076_[0-9a-f]{12}", database):
        raise ValueError("unsafe restore database")
    created = False
    try:
        execute("createdb", "-U", "trader", database)
        created = True
        with dump.open("rb") as stream:
            execute("pg_restore", "-U", "trader", "-d", database, "--exit-on-error", stdin=stream)
        tables = query(
            database, "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"
        ).splitlines()
        if set(tables) != EXPECTED:
            raise ValueError("restored schema mismatch")
        restored = {}
        for field, key in [
            ("pilot_sha256", "etf-pilot-job-0062"),
            ("legacy_registry_sha256", "etf-auto-registration-0068"),
            ("registry_sha256", "etf-auto-registration-0072"),
            ("study_registration_sha256", "paper-study-registration-0075"),
        ]:
            restored[field] = fingerprint(
                json.loads(query(database, f"SELECT value FROM kv WHERE key='{key}'"))
            )
            if restored[field] != expected[field]:
                raise ValueError("restored immutable contract mismatch")
        cps = json.loads(
            query(database, "SELECT jsonb_agg(value) FROM kv WHERE key LIKE 'etf-account-checkpoint:%'")
        )
        if len(cps) != 1:
            raise ValueError("restored checkpoint count mismatch")
        cp = cps[0]
        restored["ledger_sha256"] = fingerprint(cp["ledger"])
        if restored["ledger_sha256"] != expected["ledger_sha256"]:
            raise ValueError("restored ledger mismatch")
        if not all(
            cp["reconciliation"][k] for k in ["matched", "strategy_ownership_verified", "all_fills_owned"]
        ):
            raise ValueError("restored account not reconciled")
        versions = query(database, "SELECT version FROM schema_version ORDER BY version").splitlines()
        columns = query(
            database, "SELECT column_name FROM information_schema.columns WHERE table_name='controls'"
        ).splitlines()
        if versions != ["1", "2"] or not {"payload", "request_key"}.issubset(columns):
            raise ValueError("restored control migration missing")
        if query(database, "SELECT count(*) FROM controls WHERE request_key IS NOT NULL") != "0":
            raise ValueError("unexpected actual advanced control")
        report = {
            "decision": "ADR-0076",
            "checked_at": datetime.now(UTC).isoformat(),
            "backup": name,
            "sha256": digest,
            "bytes": metadata["bytes"],
            "restored": True,
            "restored_contracts": restored,
            "schema_versions": versions,
            "controls_columns": sorted(columns),
            "table_rows": {
                table: int(query(database, f'SELECT count(*) FROM public."{table}"')) for table in tables
            },
            "permissions_verified": True,
            "production_database_replaced": False,
            "is_offsite_backup": False,
            "broker_requests": 0,
        }
    finally:
        if created:
            execute("dropdb", "-U", "trader", "--force", database)
    report["temporary_database_removed"] = True
    (EVIDENCE / "backup-restore.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"state": "failed", "error": type(exc).__name__}))
        raise SystemExit(1) from None
