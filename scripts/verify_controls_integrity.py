"""ADR-0076/0077: final evidence comparison, no broker requests or production writes."""

import hashlib
import json
import subprocess
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FOLDER = ROOT / "verification/controls-0072"
IMAGE = "sha256:42e2c0b664db7e120e4e1e3ec94020f34bf984a8965a67747d065d2b79d79af8"


def read(name):
    return json.loads((FOLDER / name).read_text())


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def execute(*args):
    return subprocess.check_output(args, cwd=ROOT, text=True)


def main():
    a, b, c = [read(p) for p in ["runtime-before.json", "after-deploy.json", "after-restart.json"]]
    for key in [
        "legacy_registry_sha256",
        "pilot_sha256",
        "ledger_sha256",
        "counts",
        "positions",
        "cash",
        "manual_pause",
        "training_policy",
    ]:
        assert a[key] == b[key] == c[key], key
    for key in ["registry_sha256", "study_registration_sha256", "funding", "capital_limit"]:
        assert b[key] == c[key], key
    assert c["capital_limit"] == "10000" and Decimal(c["funding"]["cash_used"]) == Decimal("99.70")
    for key in [
        "day",
        "day_start",
        "daily_latch",
        "drawdown_latch",
        "flow_latch",
        "external_flows",
        "additions_allowed",
        "forced_exit",
    ]:
        assert a["guard"][key] == b["guard"][key] == c["guard"][key], key
    assert Decimal(c["guard"]["peak"]) >= Decimal(a["guard"]["peak"])
    assert c["schema_versions"] == [1, 2] and c["reconciliation"]["all_fills_owned"]
    assert c["operations"]["state"] == b["operations"]["state"] == "healthy"
    assert c["operations"]["policy_sha256"] == b["operations"]["policy_sha256"]
    assert c["operations"]["heartbeat"] > b["operations"]["heartbeat"]
    assert c["auto_heartbeat"] > b["auto_heartbeat"] and c["recent_orders"] == 1
    assert c["paper_only"] and c["python"] == "3.14.2" and c["architecture"] == "aarch64"
    assert c["performance"]["state"] == "not_started" and c["performance"]["observations"] == 0
    for file, digest in c["source_sha256"].items():
        assert sha(ROOT / "src" / file) == digest, file
    shadow = read("shadow-after.json")
    old = json.loads((ROOT / "verification/operations-0070/shadow-after.json").read_text())
    for file, digest in old["file_sha256"].items():
        assert shadow["file_sha256"][file] == digest, file
    assert shadow["final_guard_sha256"] == old["final_guard_sha256"]
    assert not shadow["model_pointer_exists"] and c["training_policy"] == shadow["training_policy"]
    ids = execute("docker", "compose", "ps", "-q").split()
    rows = json.loads(execute("docker", "inspect", *ids))
    safe = [
        {
            "service": r["Config"]["Labels"]["com.docker.compose.service"],
            "id": r["Id"],
            "image": r["Image"],
            "running": r["State"]["Running"],
            "health": r["State"].get("Health", {}).get("Status"),
        }
        for r in rows
    ]
    (FOLDER / "services-after.json").write_text(json.dumps(safe, indent=2) + "\n")
    prior, final = [{r["service"]: r for r in data} for data in [read("services-before.json"), safe]]
    unchanged = ["db", "trainer", "observer", "etf-shadow", "backup"]
    for key in unchanged:
        assert prior[key]["id"] == final[key]["id"] and prior[key]["image"] == final[key]["image"]
    assert len(final) == 9 and all(r["running"] for r in safe) and final["api"]["health"] == "healthy"
    for key in ["api", "engine", "etf-account", "operations"]:
        assert final[key]["image"] == IMAGE
    ops = next(r for r in rows if r["Config"]["Labels"]["com.docker.compose.service"] == "operations")
    assert not any("ALPACA" in v.split("=", 1)[0].upper() for v in ops["Config"]["Env"])
    assert ops["HostConfig"]["ReadonlyRootfs"]
    assert all(not m["RW"] for m in ops["Mounts"] if m["Destination"] != "/tmp")
    assert sorted(
        m["Destination"].split("/")[-1] for m in ops["Mounts"] if m["Destination"].startswith("/run/secrets/")
    ) == ["db_password", "discord_webhook"]
    storage = json.loads(Path(read("storage-check.json")["report"]).read_text())
    assert storage["same_exported_snapshot"] and storage["restored"] and storage["temporary_database_removed"]
    assert storage["sqlite_backup_integrity"] == "ok"
    backup = read("backup-restore.json")
    assert (
        backup["restored"]
        and backup["temporary_database_removed"]
        and not backup["production_database_replaced"]
    )
    suite = ET.parse(FOLDER / "test-results.xml").getroot().find("testsuite")
    assert suite.attrib["tests"] == "326" and all(
        suite.attrib[k] == "0" for k in ["errors", "failures", "skipped"]
    )
    assert (
        execute(
            "docker",
            "compose",
            "exec",
            "-T",
            "db",
            "psql",
            "-X",
            "-U",
            "trader",
            "-d",
            "trader",
            "-Atq",
            "-c",
            "SELECT count(*) FROM pg_database WHERE datname LIKE 'restore_check_%'",
        ).strip()
        == "0"
    )
    now = datetime.now(UTC)
    summary = {
        "started_at": a["checked_at"],
        "completed_at": now.isoformat(),
        "elapsed_seconds": (now - datetime.fromisoformat(a["checked_at"])).total_seconds(),
        "scheduled_run": False,
        "actual_scheduled_runs": 0,
        "completed_tasks": [
            "BUDGET-0072",
            "CTRL-01",
            "CTRL-02",
            "PERF-01",
            "OPS-02",
            "SCHEDULE-ASSESSMENT",
            "RES-01A",
        ],
        "tests_passed": 326,
        "new_regression_cases": 23,
        "new_model_training": 0,
        "new_strategy_performance_evaluations": 0,
        "new_actual_order_posts": 0,
        "actual_advanced_controls": 0,
        "waiting": [
            "actual expanded Paper order",
            "actual month-start cycle",
            "20-session operational observation",
            "future performance observations",
            "offsite storage destination/credentials",
        ],
        "token_cost_measured": False,
    }
    (FOLDER / "development-summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    result = {
        "decision": "ADR-0072~0078",
        "checked_at": now.isoformat(),
        "paper_only": True,
        "performance_approved": False,
        "tests": suite.attrib,
        "services_running": 9,
        "unchanged_services": unchanged,
        "image": IMAGE,
        "deployed_source_files_match": len(c["source_sha256"]),
        "static_and_format_files": len(
            [p for folder in ["src", "tests", "scripts"] for p in (ROOT / folder).rglob("*.py")]
        ),
        "capital_limit": c["capital_limit"],
        "funding": c["funding"],
        "remaining_capital": str(Decimal(c["capital_limit"]) - Decimal(c["funding"]["cash_used"])),
        "broker_orders": 1,
        "new_order_posts": 0,
        "advanced_controls_applied": 0,
        "restart_preserved": [
            "new/old funding registration",
            "pilot",
            "ledger",
            "risk latches",
            "budget",
            "positions",
            "cash",
            "counts",
            "performance contract",
        ],
        "old_cached_files_preserved": len(old["file_sha256"]),
        "completed_sessions": shadow["shadow"]["sessions"],
        "prospective_sessions": shadow["shadow"]["prospective_sessions"],
        "final_test_guard_sha256": shadow["final_guard_sha256"],
        "storage_tables": len(storage["public_table_digests"]),
        "storage_rows": sum(v["rows"] for v in storage["public_table_digests"].values()),
        "storage_restored": True,
        "managed_backup_restored": True,
        "temporary_restore_databases_remaining": 0,
        "monitor_broker_credentials": False,
        "automation_created": False,
        "evidence_sha256": {
            p.name: sha(p) for p in sorted(FOLDER.iterdir()) if p.is_file() and p.name != "integrity.json"
        },
        "implementation_sha256": {
            str(p.relative_to(ROOT)): sha(p)
            for p in [
                ROOT / "compose.yaml",
                ROOT / "scripts/assess_equity_data.py",
                *sorted((ROOT / "scripts").glob("verify_controls*.py")),
            ]
        },
    }
    (FOLDER / "integrity.json").write_text(json.dumps(result, indent=2) + "\n")
    print(
        json.dumps(
            {
                key: result[key]
                for key in [
                    "checked_at",
                    "services_running",
                    "capital_limit",
                    "remaining_capital",
                    "deployed_source_files_match",
                    "storage_tables",
                    "storage_rows",
                    "new_order_posts",
                ]
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
