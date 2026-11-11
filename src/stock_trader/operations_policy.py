"""ADR-0082: lightweight shared policy; existing values and semantics preserved."""

from stock_trader.clock import parse_time
from stock_trader.etf_ledger import fingerprint

CONTRACT = {
    "decision": "ADR-0070",
    "poll_seconds": 60,
    "gate_max_age_seconds": 180,
    "disk_warning_bytes": 15 * 1024**3,
    "disk_critical_bytes": 5 * 1024**3,
    "disk_warning_fraction": 0.10,
    "disk_critical_fraction": 0.05,
    "backup_max_age_hours": 30,
    "account_max_age_seconds": 1200,
    "engine_max_age_seconds": 90,
    "daily_grace_seconds": 1200,
    "forced_exit": False,
    "require_all_fills_owned": True,
}
POLICY = fingerprint(CONTRACT)


def entry_allowed(status, now):
    try:
        age = (now - parse_time(status["heartbeat"])).total_seconds()
        return (
            status["policy_sha256"] == POLICY
            and status["additions_allowed"] is True
            and status["state"] in {"healthy", "warning"}
            and 0 <= age <= CONTRACT["gate_max_age_seconds"]
        )
    except KeyError, TypeError, ValueError:
        return False


def heartbeat_check(value, field, maximum, now):
    try:
        age = (now - parse_time(value[field])).total_seconds()
        good = value.get("state") != "failed" and 0 <= age <= maximum
        return {"severity": "ok" if good else "critical", "state": "fresh" if good else "stale"}
    except KeyError, TypeError, ValueError:
        return {"severity": "critical", "state": "missing"}
