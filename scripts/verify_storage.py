"""ADR-0037: compare an exported production snapshot with a disposable restore.

Run on the Docker host. Never restore into the production database, log rows,
reset the REST budget, or send broker requests.
"""

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import uuid
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ["docker", "compose", "exec", "-T", "db"]
EXPECTED_TABLES = {"schema_version", "kv", "intents", "broker_orders", "controls", "alerts", "audit"}


def table_query(table):
    if table not in EXPECTED_TABLES:
        raise ValueError("unreviewed public table")
    return (
        "SELECT jsonb_build_object('rows',count(*),'sha256',"
        "encode(sha256(convert_to(COALESCE(string_agg(value,E'\\n' "
        "ORDER BY value COLLATE \"C\"),''),'UTF8')),'hex')) "
        f'FROM (SELECT to_jsonb(t)::text AS value FROM public."{table}" AS t) r'
    )


def execute(*args, **kwargs):
    return subprocess.run(
        DB + list(args), cwd=ROOT, check=True, timeout=180, stderr=subprocess.PIPE, **kwargs
    )


def psql(database, query):
    result = execute(
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
        "SET TIME ZONE 'UTC'; " + query,
        capture_output=False,
        stdout=subprocess.PIPE,
        text=True,
    )
    return result.stdout.strip()


def main():
    started = datetime.now(UTC)
    folder = ROOT / "verification" / ("storage-0037-" + started.strftime("%Y%m%dT%H%M%SZ"))
    folder.mkdir(mode=0o700)
    database = "restore_check_0037_" + uuid.uuid4().hex[:12]
    if not re.fullmatch(r"restore_check_0037_[0-9a-f]{12}", database):
        raise ValueError("unsafe restore database name")
    # Credentials remain inside the engine container. Only digests and a selected
    # policy leave it. This transaction remains open until pg_dump completes.
    helper = """
import json,sys
from psycopg import connect
from stock_trader.config import Settings
with connect(**Settings().db_kwargs(),autocommit=True) as connection:
    connection.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
    connection.execute("SET LOCAL TIME ZONE 'UTC'")
    connection.execute("SET LOCAL statement_timeout='120s'")
    snapshot=connection.execute("SELECT pg_export_snapshot()").fetchone()[0]
    version=connection.execute("SHOW server_version").fetchone()[0]
    tables=[r[0] for r in connection.execute("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")]
    if set(tables)!=EXPECTED_TABLES:
        raise ValueError("unexpected public schema")
    manifest={table:connection.execute(table_query(table)).fetchone()[0] for table in tables}
    policy=connection.execute("SELECT value FROM kv WHERE key='training_policy'").fetchone()[0]
    policy={k:policy.get(k) for k in ('state','reason','failed_holdout_end','consecutive_rejections')}
    print(json.dumps({'snapshot':snapshot,'version':version,'tables':manifest,'policy':policy}),flush=True)
    if sys.stdin.readline().strip()!='release':
        raise ValueError("snapshot release not received")
    connection.rollback()
"""
    # Share the exact hashing query implementation with the source transaction.
    import inspect

    helper = "EXPECTED_TABLES=" + repr(EXPECTED_TABLES) + "\n" + inspect.getsource(table_query) + helper
    created = False
    holder = subprocess.Popen(
        ["docker", "compose", "exec", "-T", "engine", "python", "-c", helper],
        cwd=ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        source = json.loads(holder.stdout.readline())
        if not re.fullmatch(r"[0-9A-Fa-f]+-[0-9A-Fa-f]+-[0-9]+", source["snapshot"]):
            raise ValueError("invalid exported snapshot")
        dump = folder / "postgres.dump"
        descriptor = os.open(dump, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            execute(
                "pg_dump",
                "-U",
                "trader",
                "-d",
                "trader",
                "-Fc",
                "--snapshot=" + source["snapshot"],
                stdout=stream,
            )
        holder.stdin.write("release\n")
        holder.stdin.flush()
        holder.communicate(timeout=30)
        if holder.returncode != 0:
            raise RuntimeError("snapshot exporter failed")
        execute("createdb", "-U", "trader", database)
        created = True
        with dump.open("rb") as stream:
            execute("pg_restore", "-U", "trader", "-d", database, "--exit-on-error", stdin=stream)
        tables = psql(
            database, "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"
        ).splitlines()
        if set(tables) != EXPECTED_TABLES:
            raise ValueError("restored schema mismatch")
        restored = {table: json.loads(psql(database, table_query(table))) for table in tables}
        if restored != source["tables"]:
            raise ValueError("restored row digest mismatch")
        policy = json.loads(psql(database, "SELECT value FROM kv WHERE key='training_policy'"))
        policy = {key: policy.get(key) for key in source["policy"]}
        if policy != source["policy"]:
            raise ValueError("restored training policy mismatch")
        # This unowned test lock is acquired only in the disposable DB.
        if psql(database, "SELECT pg_try_advisory_xact_lock(83499010037)") != "t":
            raise ValueError("restored advisory lock unavailable")
        budget = folder / "alpaca-budget.sqlite3"
        descriptor = os.open(budget, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        uri = (ROOT / "data/runtime/alpaca-budget.sqlite3").as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=10)) as live:
            with closing(sqlite3.connect(budget)) as copy:
                live.backup(copy, pages=64, sleep=0.1)
                integrity = copy.execute("PRAGMA integrity_check").fetchall()
                rows = copy.execute(
                    "SELECT bucket,attempts,limited,waits FROM budget ORDER BY bucket"
                ).fetchall()
        if integrity != [("ok",)]:
            raise ValueError("budget snapshot integrity failure")
        counters = {
            bucket: {"attempts": attempts, "http_429": limited, "wait_events": waits}
            for bucket, attempts, limited, waits in rows
        }
        if set(counters) != {"combined", "data", "trading"}:
            raise ValueError("unexpected budget buckets")
        for key in ("attempts", "http_429"):
            if counters["combined"][key] != counters["data"][key] + counters["trading"][key]:
                raise ValueError("budget counter sum mismatch")
        report = {
            "decision": "ADR-0035/ADR-0037",
            "started_at": started.isoformat(),
            "checked_at": datetime.now(UTC).isoformat(),
            "postgres_version": source["version"],
            "public_table_digests": restored,
            "training_policy": policy,
            "same_exported_snapshot": True,
            "restored": True,
            "advisory_lock_available": True,
            "production_database_replaced": False,
            "temporary_database": database,
            "sqlite_backup_integrity": "ok",
            "sqlite_snapshot_counters": counters,
            "live_budget_replaced": False,
            "dump_bytes": dump.stat().st_size,
            "dump_sha256": hashlib.sha256(dump.read_bytes()).hexdigest(),
            "sqlite_snapshot_sha256": hashlib.sha256(budget.read_bytes()).hexdigest(),
            "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "is_offsite_backup": False,
            "is_automatically_scheduled": False,
        }
    finally:
        if holder.poll() is None:
            try:
                holder.communicate("release\n", timeout=10)
            except subprocess.TimeoutExpired:
                holder.kill()
                holder.communicate()
        if created:
            execute("dropdb", "-U", "trader", database)
    # No completion report exists if restore, comparison or cleanup failed.
    report["temporary_database_removed"] = True
    (folder / "verify_storage.py").write_bytes(Path(__file__).read_bytes())
    (folder / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {
                "report": str(folder / "report.json"),
                "tables": len(restored),
                "restored_rows_match": True,
                "sqlite_integrity": "ok",
                "temporary_database_removed": True,
            }
        )
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # DB errors may contain statements or row content; keep them out of logs.
        print(json.dumps({"state": "failed", "error": type(exc).__name__}))
        raise SystemExit(1) from None
