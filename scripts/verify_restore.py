"""Restore a local snapshot into a disposable DB; never replace the trading DB."""

import json
import subprocess
import uuid
from pathlib import Path

root = Path(__file__).resolve().parents[1]
directory = root / "verification"
directory.mkdir(exist_ok=True)
database = "restore_check_" + uuid.uuid4().hex[:12]
prefix = ["docker", "compose", "exec", "-T", "db"]
path = directory / "restore-check.dump"


def execute(*args, **kwargs):
    return subprocess.run(prefix + list(args), cwd=root, check=True, **kwargs)


with path.open("wb") as file:
    execute("pg_dump", "-U", "trader", "-d", "trader", "-Fc", stdout=file)
execute("createdb", "-U", "trader", database)
try:
    with path.open("rb") as file:
        execute("pg_restore", "-U", "trader", "-d", database, "--exit-on-error", stdin=file)
    result = execute(
        "psql",
        "-U",
        "trader",
        "-d",
        database,
        "-At",
        "-c",
        "SELECT count(*) FROM schema_version",
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "1"
    result = execute(
        "psql",
        "-U",
        "trader",
        "-d",
        database,
        "-At",
        "-c",
        "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'",
        capture_output=True,
        text=True,
    )
    report = {
        "restored": True,
        "public_tables": int(result.stdout.strip()),
        "snapshot_bytes": path.stat().st_size,
        "production_database_replaced": False,
    }
    (directory / "restore.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report))
finally:
    execute("dropdb", "-U", "trader", database)
