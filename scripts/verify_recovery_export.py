"""ADR-0083: restore an exported dump to a disposable DB, never production."""

import argparse
import json
import re
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

from verify_daily_backup import EXPECTED, execute, fingerprint, query
from verify_storage import table_query

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from stock_trader.recovery_bundle import verify  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--expected", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    path = args.bundle.absolute()
    if not path.resolve().is_relative_to((ROOT / "recovery_exports").resolve()):
        raise ValueError("export-only input required")
    result = verify(path)
    meta = json.loads((path / "manifest.json").read_text())["payload"]
    dump = path / "postgres" / meta["database_backup"]["file"]
    expected = json.loads(args.expected.read_text())
    database = "restore_check_0083_" + uuid.uuid4().hex[:12]
    if not re.fullmatch(r"restore_check_0083_[0-9a-f]{12}", database):
        raise ValueError("unsafe restore identity")
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
            ("initial_job_sha256", "etf-auto-cycle:paper-auto-initial-0068"),
            ("legacy_registry_sha256", "etf-auto-registration-0068"),
            ("registry_sha256", "etf-auto-registration-0072"),
            ("study_registration_sha256", "paper-study-registration-0075"),
            ("operational_registration_sha256", "operational-study-registration-0081"),
        ]:
            restored[field] = fingerprint(
                json.loads(query(database, f"SELECT value FROM kv WHERE key='{key}'"))
            )
            if restored[field] != expected[field]:
                raise ValueError("restored immutable registration mismatch")
        cps = json.loads(
            query(database, "SELECT jsonb_agg(value) FROM kv WHERE key LIKE 'etf-account-checkpoint:%'")
        )
        if len(cps) != 1 or fingerprint(cps[0]["ledger"]) != expected["ledger_sha256"]:
            raise ValueError("restored ledger mismatch")
        if not all(
            cps[0]["reconciliation"][key]
            for key in ("matched", "all_fills_owned", "strategy_ownership_verified")
        ):
            raise ValueError("restored ownership mismatch")
        digests = {table: json.loads(query(database, table_query(table))) for table in tables}
        for table, count in expected["counts"].items():
            if digests[table]["rows"] != count:
                raise ValueError("restored order or control count mismatch")
        report = {
            **result,
            "checked_at": datetime.now(UTC).isoformat(),
            "database_restored": True,
            "public_table_digests": digests,
            "restored_contracts": restored,
            "ledger_sha256": expected["ledger_sha256"],
            "production_database_replaced": False,
            "live_budget_replaced": False,
        }
    finally:
        if created:
            execute("dropdb", "-U", "trader", "--force", database)
    report["temporary_database_removed"] = True
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {
                "database_restored": True,
                "tables": len(digests),
                "rows": sum(r["rows"] for r in digests.values()),
                "temporary_database_removed": True,
                "production_database_replaced": False,
                "broker_requests": 0,
            }
        )
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"state": "failed", "error": type(exc).__name__}))
        raise SystemExit(1) from None
