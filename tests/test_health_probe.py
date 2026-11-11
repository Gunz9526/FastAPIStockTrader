import copy
import json
import stat
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from stock_trader.api import create_app
from stock_trader.external_probe import check, record, ssh_command
from stock_trader.health import SHA, assess
from stock_trader.operations import POLICY

NOW = datetime(2026, 10, 19, 14, tzinfo=UTC)


def good():
    return (
        {
            "state": "healthy",
            "heartbeat": NOW.isoformat(),
            "policy_sha256": POLICY,
            "additions_allowed": True,
        },
        {
            "state": "paper_auto_waiting_market",
            "heartbeat": NOW.isoformat(),
            "owner": "etf_auto",
            "paper": True,
        },
        {"state": "monitoring", "last_success_at": NOW.isoformat(), "reconciled": True},
    )


def test_first_health_import_never_loads_research_or_ml_libraries():
    script = """
import sys
import stock_trader.health
blocked = ['numpy', 'pandas', 'lightgbm', 'xgboost', 'catboost', 'stock_trader.research', 'stock_trader.etf_study']
assert not any(name in sys.modules for name in blocked), 'health imported research/ML libraries'
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "state", ["paper_auto_waiting_market", "paper_auto_paused", "paper_auto_additions_paused"]
)
def test_closed_paused_and_risk_stopped_are_not_availability_incidents(state):
    ops, engine, account = good()
    engine["state"] = state
    assert assess(ops, engine, account, now=NOW)["state"] == "healthy"


@pytest.mark.parametrize(
    "kind",
    [
        "ops_age",
        "policy",
        "ops_blocked",
        "engine_age",
        "contract",
        "review",
        "account_age",
        "ownership",
        "active",
    ],
)
def test_health_detects_stale_or_review_required_state(kind):
    ops, engine, account = good()
    kwargs = {}
    if kind == "ops_age":
        ops["heartbeat"] = (NOW - timedelta(seconds=181)).isoformat()
    elif kind == "policy":
        ops["policy_sha256"] = "different"
    elif kind == "ops_blocked":
        ops.update(state="blocked", additions_allowed=False)
    elif kind == "engine_age":
        engine["heartbeat"] = (NOW - timedelta(seconds=91)).isoformat()
    elif kind == "contract":
        engine["paper"] = False
    elif kind == "review":
        engine["state"] = "paper_auto_await_review"
    elif kind == "account_age":
        account["last_success_at"] = (NOW - timedelta(seconds=1201)).isoformat()
    elif kind == "ownership":
        account["reconciled"] = False
    else:
        kwargs = {"active": "test-cycle", "job": {"cycle": "test-cycle", "review_required": True}}
    value = assess(ops, engine, account, now=NOW, **kwargs)
    assert value["state"] == "blocked" and value["issues"] and value["broker_requests"] == 0


async def test_health_api_is_authenticated_minimal_uncached_and_uses_503(store, settings, monkeypatch):
    ops, engine, account = good()
    account.update(cash="123456", positions={"SPY": "secret-quantity"})
    for key, value in [("operations", ops), ("engine_status", engine), ("etf_account", account)]:
        await store.put(key, value)
    monkeypatch.setattr("stock_trader.health.utcnow", lambda: NOW)
    app = create_app(settings.model_copy(update={"api_token": SecretStr("health-test-token")}), store)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/health/operations")).status_code == 401
        headers = {"Authorization": "Bearer health-test-token"}
        response = await client.get("/health/operations", headers=headers)
        assert response.status_code == 200 and response.json()["state"] == "healthy"
        assert response.headers["cache-control"] == "no-store"
        assert "123456" not in response.text and "secret-quantity" not in response.text
        assert "health-test-token" not in response.text
        account["reconciled"] = False
        await store.put("etf_account", account)
        assert (await client.get("/health/operations", headers=headers)).status_code == 503


def runner(payload=None, code=0):
    payload = payload or assess(*good(), now=NOW)

    def run(command, **kwargs):
        assert kwargs["timeout"] == 30 and kwargs["check"] is False
        assert "-oBatchMode=yes" in command
        return SimpleNamespace(
            returncode=code, stdout=json.dumps(payload), stderr="private error and credential"
        )

    return run


def test_external_checker_keeps_exit_status_and_suppresses_remote_details():
    assert check("ubuntu@example.test", "/project", now=NOW, execute=runner())["state"] == "healthy"
    value = assess(*good(), now=NOW)
    value.update(state="blocked", issues=["account_ownership"])
    assert check("host", "/project", now=NOW, execute=runner(value, 1))["state"] == "blocked"
    mismatch = check("host", "/project", now=NOW, execute=runner(value, 0))
    assert mismatch["state"] == "unavailable" and "private" not in json.dumps(mismatch)
    assert check("host", "/project", now=NOW + timedelta(seconds=121), execute=runner())["issues"] == [
        "remote_observation_stale"
    ]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"state": "healthy", "issues": "private"},
        {"state": "healthy", "issues": [], "observed_at": "2026-10-19T14:00:00"},
    ],
)
def test_bad_remote_response_is_never_healthy(payload):
    assert (
        check("host", "/project", now=NOW, execute=runner(payload or {"invalid": True}))["state"]
        == "unavailable"
    )


def test_external_transport_timeout_and_ssh_options_are_bounded():
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 30)

    assert check("host", "/project", now=NOW, execute=timeout)["state"] == "unavailable"
    with pytest.raises(ValueError):
        ssh_command("-oProxyCommand=bad", "/project")
    with pytest.raises(ValueError):
        ssh_command("host", "relative/project")
    command = ssh_command("user@host", "/project with ' quote/$(literal)")
    assert command[-2] == "user@host" and "'" in command[-1]
    assert "StrictHostKeyChecking=no" not in " ".join(command)


def test_external_state_only_reports_changes_and_preserves_target_boundary(tmp_path):
    path = tmp_path / "probe.json"
    healthy = {"state": "healthy", "issues": []}
    failed = {"state": "unavailable", "issues": ["ssh_unreachable"]}
    assert record(path, "host", "/project", healthy, now=NOW) is None
    assert record(path, "host", "/project", healthy, now=NOW) is None
    assert record(path, "host", "/project", failed, now=NOW)["transition"] == 1
    assert record(path, "host", "/project", failed, now=NOW) is None
    assert record(path, "host", "/project", healthy, now=NOW)["transition"] == 2
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    before = path.read_bytes()
    with pytest.raises(ValueError):
        record(path, "other-host", "/project", copy.deepcopy(healthy), now=NOW)
    assert path.read_bytes() == before
    assert SHA


def test_a_slow_old_external_check_cannot_overwrite_a_newer_result(tmp_path):
    path = tmp_path / "state.json"
    new = {"state": "blocked", "issues": ["account_ownership"]}
    record(path, "host", "/project", new, now=NOW)
    before = path.read_bytes()
    assert (
        record(path, "host", "/project", {"state": "healthy", "issues": []}, now=NOW - timedelta(seconds=1))
        is None
    )
    assert path.read_bytes() == before


def test_overlapping_external_writers_serialize_and_keep_the_latest_sample(tmp_path):
    path = tmp_path / "state.json"

    def write(index):
        result = (
            {"state": "healthy", "issues": []}
            if index % 2 == 0
            else {"state": "unavailable", "issues": ["ssh_unreachable"]}
        )
        record(path, "host", "/project", result, now=NOW + timedelta(seconds=index))

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(write, range(20)))
    final = json.loads(path.read_text())
    assert final["checked_at"] == (NOW + timedelta(seconds=19)).isoformat()
    assert final["result"]["state"] == "unavailable"
