"""ADR-0079~0081 final offline proof and read-only container inspection."""

import hashlib
import json
import subprocess
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
FOLDER = ROOT / "verification/continuation-0079"


def read(name):
    return json.loads((FOLDER / name).read_text())


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def execute(*args):
    return subprocess.check_output(args, cwd=ROOT, text=True)


def main():
    before, deployed, final = [
        read(name) for name in ["runtime-before.json", "after-deploy.json", "after-restart.json"]
    ]
    values = before["values"]
    contracts = {
        "registry_sha256": "etf-auto-registration-0072",
        "legacy_registry_sha256": "etf-auto-registration-0068",
        "pilot_sha256": "etf-pilot-job-0062",
        "initial_job_sha256": "etf-auto-cycle:paper-auto-initial-0068",
        "study_registration_sha256": "paper-study-registration-0075",
    }
    original_cp = before["checkpoints"][0]["value"]
    for proof in [deployed, final]:
        assert proof["counts"] == before["counts"] == {"intents": 2, "broker_orders": 2, "controls": 2}
        assert proof["positions"] == before["positions"]
        assert (
            proof["manual_pause"] == values["manual_pause"]
            and proof["training_policy"] == values["training_policy"]
        )
        for field, key in contracts.items():
            assert proof[field] == fingerprint(values[key]), field
        cp = proof["checkpoint"]
        for key, row in original_cp["rows"].items():
            assert cp["rows"][key] == row
        added = [r for key, r in cp["rows"].items() if key not in original_cp["rows"]]
        assert all(r["activity_type"] == "FEE" and Decimal(r["net_amount"]) < 0 for r in added)
        assert cp["ledger"]["cash_by_type"]["FILL"] == original_cp["ledger"]["cash_by_type"]["FILL"]
        assert Decimal(proof["funding"]["cash_used"]) <= 10000
        for field in ["flow_latch", "drawdown_latch", "forced_exit", "external_flows"]:
            assert cp["guard"][field] == original_cp["guard"][field], field
        assert Decimal(cp["guard"]["peak"]) >= Decimal(original_cp["guard"]["peak"])
        if cp["guard"]["day"] == original_cp["guard"]["day"]:
            assert cp["guard"]["day_start"] == original_cp["guard"]["day_start"]
            assert cp["guard"]["daily_latch"] == original_cp["guard"]["daily_latch"]
        else:
            assert cp["guard"]["day"] > original_cp["guard"]["day"] and cp["guard"]["daily_latch"] is False
        assert (
            cp["guard"]["day"]
            == datetime.fromisoformat(cp["last_success_at"])
            .astimezone(ZoneInfo("America/New_York"))
            .date()
            .isoformat()
        )
        assert (
            proof["operational_observation"]["state"] == "not_started"
            and proof["operational_observation"]["observed_sessions"] == 0
        )
        assert proof["health"]["state"] == "healthy" and proof["performance"]["observations"] == 0
        assert proof["paper_only"] and proof["python"] == "3.14.2" and proof["architecture"] == "aarch64"
    assert deployed["operational_registration_sha256"] == final["operational_registration_sha256"]
    assert (
        read("after-deploy-initial-0081.json")["operational_registration_sha256"]
        == final["operational_registration_sha256"]
    )
    assert final["operations"]["heartbeat"] > deployed["operations"]["heartbeat"]
    assert final["auto"]["heartbeat"] > deployed["auto"]["heartbeat"]
    assert final["operations"]["policy_sha256"] == values["operations"]["policy_sha256"]
    for file, digest in final["source_sha256"].items():
        assert sha(ROOT / "src" / file) == digest, file
    previous = json.loads((ROOT / "verification/controls-0072/after-restart.json").read_text())
    unchanged_modules = [
        "api_budget",
        "etf_auto",
        "etf_account",
        "etf_funding",
        "etf_guard",
        "execution",
        "etf_execution",
        "allocation",
        "broker",
        "storage",
        "notifications",
    ]
    for name in unchanged_modules:
        key = "stock_trader/" + name + ".py"
        assert final["source_sha256"][key] == previous["source_sha256"][key], key
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
    initial, current = [{r["service"]: r for r in data} for data in [read("services-before.json"), safe]]
    unchanged_services = ["db", "engine", "etf-account", "trainer", "observer", "etf-shadow", "backup"]
    for key in unchanged_services:
        assert initial[key]["id"] == current[key]["id"] and initial[key]["image"] == current[key]["image"]
    image = read("image.json")["id"]
    assert current["api"]["image"] == current["operations"]["image"] == image
    assert len(safe) == 9 and all(r["running"] for r in safe) and current["api"]["health"] == "healthy"
    ops = next(r for r in rows if r["Config"]["Labels"]["com.docker.compose.service"] == "operations")
    assert not any("ALPACA" in v.split("=", 1)[0].upper() for v in ops["Config"]["Env"])
    assert ops["HostConfig"]["ReadonlyRootfs"] and all(
        not m["RW"] for m in ops["Mounts"] if m["Destination"] != "/tmp"
    )
    suite = ET.parse(FOLDER / "test-results.xml").getroot().find("testsuite")
    assert suite.attrib["tests"] == "365" and all(
        suite.attrib[k] == "0" for k in ["errors", "failures", "skipped"]
    )
    cold = ET.parse(FOLDER / "cold-import-old.xml").getroot().find("testsuite")
    assert cold.attrib["failures"] == "1"
    import_check = read("cold-import-new.json")
    assert import_check["research_modules_loaded"] is False
    assert import_check["health_sha256"] == final["health"]["contract_sha256"]
    assert deployed["health_request_seconds"] < 10 and final["health_request_seconds"] < 10
    first, restart = read("equity-first.json"), read("equity-restart.json")
    assert first["logical_gets"] == 6 and restart["logical_gets"] == 0 and restart["cache_hits"] == 6
    assert first["outputs"] == restart["outputs"]
    assessment = read("equity-assessment.json")
    assert assessment["state"] == "sample_quality_failed" and len(assessment["invalid_bars"]) == 3
    assert not assessment["full_market_point_in_time_ready"] and assessment["strategy_evaluations"] == 0
    assert read("expanded-fill.json")["actual_expanded_fill_verified"]
    assert read("probe-after-restart.json")["state"] == "healthy"
    shadow = read("shadow-after.json")
    old = json.loads((ROOT / "verification/controls-0072/shadow-after.json").read_text())
    for name, digest in old["file_sha256"].items():
        assert shadow["file_sha256"][name] == digest
    assert shadow["final_guard_sha256"] == old["final_guard_sha256"] and not shadow["model_pointer_exists"]
    storage = json.loads(Path(read("storage-check.json")["report"]).read_text())
    assert storage["restored"] and storage["same_exported_snapshot"] and storage["temporary_database_removed"]
    assert storage["sqlite_backup_integrity"] == "ok"
    backup = read("backup-restore.json")
    assert (
        backup["restored"]
        and backup["temporary_database_removed"]
        and not backup["production_database_replaced"]
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
            "SELECT count(*) FROM pg_database WHERE datname LIKE 'restore_check_%' OR datname LIKE 'test_%'",
        ).strip()
        == "0"
    )
    now = datetime.now(UTC)
    result = {
        "decision": "ADR-0079~0082",
        "checked_at": now.isoformat(),
        "started_at": before["checked_at"],
        "elapsed_seconds": (now - datetime.fromisoformat(before["checked_at"])).total_seconds(),
        "tests": suite.attrib,
        "static_and_format_files": len(
            [p for folder in ["src", "tests", "scripts"] for p in (ROOT / folder).rglob("*.py")]
        ),
        "services_running": 9,
        "unchanged_services": unchanged_services,
        "deployed_image": image,
        "deployed_source_files_match": len(final["source_sha256"]),
        "unchanged_trading_modules": unchanged_modules,
        "paper_only": True,
        "capital_limit": "10000",
        "funding": final["funding"],
        "remaining_capital": str(10000 - Decimal(final["funding"]["cash_used"])),
        "cash": final["cash"],
        "positions": final["positions"],
        "existing_orders": 2,
        "new_order_posts": 0,
        "actual_advanced_controls": 0,
        "preserved_contracts": list(contracts),
        "new_operational_contract_preserved": True,
        "external_monitor_deployed": False,
        "external_probe_local_check": True,
        "first_health_seconds": {
            "after_deploy": deployed["health_request_seconds"],
            "after_restart": final["health_request_seconds"],
        },
        "fresh_health_import_seconds": import_check["import_seconds"],
        "health_import_loads_research": False,
        "future_operating_sessions": 0,
        "future_performance_observations": 0,
        "new_research_training": 0,
        "new_strategy_evaluations": 0,
        "access_logical_gets": 6,
        "access_cache_restart_gets": 0,
        "access_sample_quality_approved": False,
        "old_cached_files_preserved": len(old["file_sha256"]),
        "storage_tables": len(storage["public_table_digests"]),
        "storage_rows": sum(v["rows"] for v in storage["public_table_digests"].values()),
        "snapshot_restored": True,
        "managed_backup_restored": True,
        "temporary_databases_remaining": 0,
        "scheduled_run": False,
        "token_cost_measured": False,
        "completed": [
            "OBS-01",
            "RES-01B access samples and readiness diagnosis",
            "OPS-03 endpoint and external probe preparation",
            "OPS-04 future operating observation registration",
            "deployment/restart/recovery",
        ],
        "waiting": [
            "actual monthly decision",
            "20 future operating sessions",
            "127 future performance sessions",
            "external host and offsite backup",
            "full equity research data and frozen evaluation",
        ],
        "access_file_sha256": {
            str(p.relative_to(ROOT)): sha(p)
            for p in sorted((ROOT / "data/research/equity-access-0080").glob("*.json"))
        },
        "evidence_sha256": {
            p.name: sha(p) for p in sorted(FOLDER.iterdir()) if p.is_file() and p.name != "integrity.json"
        },
        "script_sha256": {
            str(p.relative_to(ROOT)): sha(p)
            for p in [
                ROOT / "compose.yaml",
                *sorted((ROOT / "scripts").glob("verify_continuation*.py")),
                ROOT / "scripts/verify_expanded_fill.py",
            ]
        },
    }
    (FOLDER / "integrity.json").write_text(json.dumps(result, indent=2) + "\n")
    print(
        json.dumps(
            {
                k: result[k]
                for k in [
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
