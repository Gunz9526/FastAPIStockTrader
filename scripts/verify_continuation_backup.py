"""ADR-0081: verify the newest managed backup in a disposable DB only."""

import hashlib
import json
import re
import stat
import uuid
from datetime import UTC, datetime
from pathlib import Path

from verify_daily_backup import EXPECTED, execute, fingerprint, query

ROOT = Path(__file__).resolve().parents[1]
FOLDER = ROOT / "verification/continuation-0079"


def main():
    manifests = sorted((ROOT / "backups").glob("paper-db-*.dump.json"))
    manifest = manifests[-1]
    metadata = json.loads(manifest.read_text())
    name = metadata["file"]
    assert re.fullmatch(r"paper-db-[0-9]{8}T[0-9]{6}Z-[A-Za-z0-9]{8}\.dump", name)
    assert metadata["state"] == "complete" and manifest.name == name + ".json"
    dump = ROOT / "backups" / name
    digest = hashlib.sha256(dump.read_bytes()).hexdigest()
    assert digest == metadata["sha256"] and dump.stat().st_size == metadata["bytes"]
    for path, mode in [(ROOT / "backups", 0o700), (dump, 0o600), (manifest, 0o600)]:
        assert not path.is_symlink() and stat.S_IMODE(path.stat().st_mode) == mode
    expected = json.loads((FOLDER / "after-restart.json").read_text())
    database = "restore_check_0081_" + uuid.uuid4().hex[:12]
    assert re.fullmatch(r"restore_check_0081_[0-9a-f]{12}", database)
    created = False
    try:
        execute("createdb", "-U", "trader", database)
        created = True
        with dump.open("rb") as stream:
            execute("pg_restore", "-U", "trader", "-d", database, "--exit-on-error", stdin=stream)
        tables = query(
            database, "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"
        ).splitlines()
        assert set(tables) == EXPECTED
        restored = {}
        for field, key in [
            ("pilot_sha256", "etf-pilot-job-0062"),
            ("initial_job_sha256", "etf-auto-cycle:paper-auto-initial-0068"),
            ("legacy_registry_sha256", "etf-auto-registration-0068"),
            ("registry_sha256", "etf-auto-registration-0072"),
            ("study_registration_sha256", "paper-study-registration-0075"),
            ("operational_registration_sha256", "operational-study-registration-0081"),
        ]:
            restored[field] = fingerprint(
                json.loads(query(database, f"SELECT value FROM kv WHERE key='{key}'"))
            )
            assert restored[field] == expected[field], field
        cps = json.loads(
            query(database, "SELECT jsonb_agg(value) FROM kv WHERE key LIKE 'etf-account-checkpoint:%'")
        )
        assert len(cps) == 1 and fingerprint(cps[0]["ledger"]) == expected["ledger_sha256"]
        assert all(
            cps[0]["reconciliation"][k] for k in ["matched", "strategy_ownership_verified", "all_fills_owned"]
        )
        assert query(database, "SELECT count(*) FROM controls WHERE request_key IS NOT NULL") == "0"
        assert (
            query(database, "SELECT count(*) FROM kv WHERE key LIKE 'operational-study-session-0081:%'")
            == "0"
        )
        report = {
            "decision": "ADR-0081",
            "checked_at": datetime.now(UTC).isoformat(),
            "backup": name,
            "sha256": digest,
            "bytes": metadata["bytes"],
            "restored": True,
            "restored_contracts": restored,
            "ledger_sha256": expected["ledger_sha256"],
            "table_rows": {t: int(query(database, f'SELECT count(*) FROM public."{t}"')) for t in tables},
            "production_database_replaced": False,
            "is_offsite_backup": False,
            "broker_requests": 0,
        }
    finally:
        if created:
            execute("dropdb", "-U", "trader", "--force", database)
    report["temporary_database_removed"] = True
    (FOLDER / "backup-restore.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"state": "failed", "error": type(exc).__name__}))
        raise SystemExit(1) from None
