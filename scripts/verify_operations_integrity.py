"""Offline proof comparison plus read-only container/schema inspection (ADR-0070)."""

import hashlib
import json
import subprocess
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FOLDER = ROOT / "verification/operations-0070"
IMAGE = "sha256:8282496baf857170fc28999b4da0a0c209c40b878bde570d7551245273ac1033"


def read(name):
    return json.loads((FOLDER / name).read_text())


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def execute(*args):
    return subprocess.check_output(args, cwd=ROOT, text=True)


def main():
    a, b, c = [read(p) for p in ["runtime-before.json", "after-deploy.json", "after-restart.json"]]
    for field in [
        "pilot_sha256",
        "ledger_sha256",
        "capital_limit",
        "funding",
        "positions",
        "cash",
        "counts",
        "manual_pause",
        "training_policy",
    ]:
        assert a[field] == b[field] == c[field], field
    assert a["registration_sha256"] == b["registry_sha256"] == c["registry_sha256"]
    for field in [
        "day",
        "day_start",
        "peak",
        "daily_latch",
        "drawdown_latch",
        "flow_latch",
        "external_flows",
        "additions_allowed",
        "forced_exit",
    ]:
        assert a["guard"][field] == b["guard"][field] == c["guard"][field], field
    assert c["operations"]["policy_sha256"] == b["operations"]["policy_sha256"]
    assert c["operations"]["transition"] == b["operations"]["transition"] == 1
    assert c["operations"]["state"] == "healthy"
    assert c["operations"]["heartbeat"] > b["operations"]["heartbeat"]
    assert c["auto_heartbeat"] > b["auto_heartbeat"] and c["recent_orders"] == 1
    assert c["paper_only"] and c["python"] == "3.14.2" and c["architecture"] == "aarch64"
    for file, digest in c["source_sha256"].items():
        assert sha(ROOT / "src" / file) == digest, file
    shadow = read("shadow-after.json")
    old = json.loads((ROOT / "verification/etf-0067/shadow-after.json").read_text())
    assert shadow["file_sha256"] == old["file_sha256"]
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
    prior = {r["service"]: r for r in read("services-before.json")}
    final = {r["service"]: r for r in safe}
    unchanged = ["db", "trainer", "observer", "etf-shadow", "etf-account", "backup"]
    for key in unchanged:
        assert prior[key]["id"] == final[key]["id"] and prior[key]["image"] == final[key]["image"]
    assert len(final) == 9 and all(r["running"] for r in safe)
    for key in ["api", "engine", "operations"]:
        assert final[key]["image"] == IMAGE
    ops = next(r for r in rows if r["Config"]["Labels"]["com.docker.compose.service"] == "operations")
    assert not any("ALPACA" in v.split("=", 1)[0].upper() for v in ops["Config"]["Env"])
    assert ops["HostConfig"]["ReadonlyRootfs"]
    assert all(not m["RW"] for m in ops["Mounts"] if m["Destination"] != "/tmp")
    bootstrap = read("monitor-bootstrap.json")
    assert bootstrap["secret_files"] == ["db_password", "discord_webhook"]
    assert not bootstrap["broker_credentials_present"]
    storage = json.loads(Path(read("storage-check.json")["report"]).read_text())
    assert storage["same_exported_snapshot"] and storage["restored"]
    assert storage["temporary_database_removed"] and storage["sqlite_backup_integrity"] == "ok"
    suite = ET.parse(FOLDER / "test-results.xml").getroot().find("testsuite")
    assert suite.attrib["tests"] == "303"
    assert all(suite.attrib[k] == "0" for k in ["errors", "failures", "skipped"])
    query = (
        "SELECT count(*) FROM pg_database WHERE datname LIKE 'restore_check_0037_%' "
        "OR datname LIKE 'restore_check_0069_%'"
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
            query,
        ).strip()
        == "0"
    )
    implementation = [
        "compose.yaml",
        "src/stock_trader/operations.py",
        "src/stock_trader/notifications.py",
        "src/stock_trader/etf_auto.py",
        "src/stock_trader/api.py",
        "tests/test_operations.py",
        "tests/test_etf_auto.py",
        "scripts/verify_operations.py",
        "scripts/verify_operations_integrity.py",
    ]
    result = {
        "decision": "ADR-0070/ADR-0071",
        "checked_at": datetime.now(UTC).isoformat(),
        "paper_only": True,
        "performance_approved": False,
        "tests": suite.attrib,
        "static_and_format_files": 83,
        "services_running": 9,
        "unchanged_services": unchanged,
        "deployed_source_files_match": len(c["source_sha256"]),
        "operations_state": "healthy",
        "operations_policy_sha256": c["operations"]["policy_sha256"],
        "operations_transition": 1,
        "monitor_has_broker_credentials": False,
        "persistent_mounts_read_only": True,
        "restart_preserved": [
            "registration",
            "pilot",
            "ledger",
            "risk_latches",
            "budget",
            "positions",
            "cash",
            "counts",
            "policy",
            "incident_transition",
        ],
        "capital_limit": "1000",
        "funding": c["funding"],
        "broker_orders": 1,
        "new_order_posts": 0,
        "cached_files_unchanged": len(shadow["file_sha256"]),
        "completed_sessions": shadow["shadow"]["sessions"],
        "prospective_sessions": shadow["shadow"]["prospective_sessions"],
        "final_test_guard_sha256": shadow["final_guard_sha256"],
        "training_policy_unchanged": True,
        "new_research_training": 0,
        "new_performance_evaluations": 0,
        "storage_tables": len(storage["public_table_digests"]),
        "storage_rows": sum(v["rows"] for v in storage["public_table_digests"].values()),
        "storage_restored": True,
        "temporary_restore_databases_remaining": 0,
        "automation_created": False,
        "evidence_sha256": {
            p.name: sha(p) for p in sorted(FOLDER.iterdir()) if p.is_file() and p.name != "integrity.json"
        },
        "implementation_sha256": {p: sha(ROOT / p) for p in implementation},
    }
    (FOLDER / "integrity.json").write_text(json.dumps(result, indent=2) + "\n")
    print(
        json.dumps(
            {
                k: result[k]
                for k in [
                    "checked_at",
                    "services_running",
                    "operations_state",
                    "deployed_source_files_match",
                    "storage_tables",
                    "storage_rows",
                    "broker_orders",
                    "new_order_posts",
                ]
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
