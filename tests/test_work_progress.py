import copy
import json
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from pydantic import SecretStr

from stock_trader.api import create_app
from stock_trader.broker import BrokerError
from stock_trader.equity_contract import CONTRACT as EQUITY_CONTRACT
from stock_trader.equity_forward import CONTRACT as FORWARD_CONTRACT
from stock_trader.operational_study import CONTRACT as OPS_CONTRACT
from stock_trader.performance import CONTRACT as PAPER_CONTRACT
from stock_trader.progress_calendar import CalendarReader, collect
from stock_trader.research_seal import fingerprint, seal
from stock_trader.work_progress import (
    CALENDAR_CONTRACT,
    CALENDAR_REQUEST,
    CALENDAR_ROOT,
    MONTHLY_PREFIX,
    OPS_PREFIX,
    PAPER_PREFIX,
    available,
    build_report,
    load_calendar,
    load_progress,
    session_times,
    validate_calendar,
)

NOW = datetime(2026, 10, 21, 7, 40, tzinfo=UTC)


def calendar_rows():
    # Synthetic calendar with explicit holidays/early closes for date-boundary tests.
    holidays = {
        "2026-11-26",
        "2026-12-25",
        "2027-01-01",
        "2027-01-18",
        "2027-02-15",
        "2027-03-26",
        "2027-05-31",
        "2027-06-18",
        "2027-07-05",
        "2027-09-06",
        "2027-11-25",
        "2027-12-24",
    }
    day, end, rows = date(2026, 10, 1), date(2027, 12, 31), []
    while day <= end:
        if day.weekday() < 5 and day.isoformat() not in holidays:
            rows.append(
                {
                    "date": day.isoformat(),
                    "open": "09:30",
                    "close": "13:00"
                    if day.isoformat() in {"2026-11-27", "2026-12-24", "2027-11-26"}
                    else "16:00",
                }
            )
        day += timedelta(days=1)
    return rows


def snapshot(rows=None):
    rows = rows or calendar_rows()
    funding = {"paper": True, "capital_limit": "10000", "version": 72}
    contracts = {
        "operational-study-registration-0081": OPS_CONTRACT,
        "paper-study-registration-0075": PAPER_CONTRACT,
        "equity-study-registration-0086": EQUITY_CONTRACT,
        "equity-forward-registration-0089": FORWARD_CONTRACT,
    }
    values = {
        key: {"contract": contract, "funding_registration_sha256": fingerprint(funding)}
        for key, contract in contracts.items()
    }
    values["etf-auto-registration-0072"] = funding
    values["etf_auto"] = {"state": "waiting_market"}
    window = [
        {
            "date": day,
            "expected_minutes": 390,
            "observed_minutes": count,
            "critical_samples": 0,
            "longest_gap_minutes": 390 - count,
            "initial_quality_passed": True,
        }
        for day, count in [("2026-10-19", 389), ("2026-10-20", 390)]
    ]
    values["operational_observation"] = {
        "contract": OPS_CONTRACT,
        "heartbeat": NOW.isoformat(),
        "state": "collecting",
        "completed_sessions": 2,
        "window": window,
        "monthly_decisions_completed": 0,
    }
    values["performance_status"] = {
        "contract": PAPER_CONTRACT,
        "heartbeat": NOW.isoformat(),
        "state": "collecting",
        "observations": 1,
        "return_intervals": 0,
        "baseline_date": "2026-10-19",
        "latest_session": "2026-10-19",
    }
    for status_key, registration, contract in [
        ("equity_shadow_0086", "equity-study-registration-0086", EQUITY_CONTRACT),
        ("equity_forward_0090", "equity-forward-registration-0089", FORWARD_CONTRACT),
    ]:
        values[status_key] = {
            "contract_sha256": fingerprint(contract),
            "registration_sha256": fingerprint(values[registration]),
            "heartbeat": NOW.isoformat(),
            "mode": "research_only",
            "orders_submitted": 0,
            "state": "waiting_formation" if status_key == "equity_shadow_0086" else "waiting_start",
        }
    values["equity_shadow_0086"]["formation_start"] = "2026-10-31T10:00:00Z"
    values["equity_forward_0090"]["observations"] = 0
    values[PAPER_PREFIX + "2026-10-19"] = {"date": "2026-10-19"}
    values[OPS_PREFIX + "2026-10-19"] = {}
    values[OPS_PREFIX + "2026-10-20"] = {}
    return values


def milestone(report, name):
    return next(row for row in report["milestones"] if row["id"] == name)


def test_current_progress_next_resume_and_dates_do_not_approve_observations():
    rows, values = calendar_rows(), snapshot()
    report = build_report(values, rows, NOW)
    assert report["state"] == "observing"
    assert report["next_resume_kst"] == "2026-10-21T19:30:00+09:00"
    assert report["counts"]["paper_observations"] == 1
    assert milestone(report, "operational_20")["earliest_kst"] == "2026-11-14T06:00:00+09:00"
    assert milestone(report, "paper_monthly")["earliest_kst"] == "2026-11-02T23:30:00+09:00"
    assert milestone(report, "equity_formation")["earliest_kst"] == "2026-10-31T19:00:00+09:00"
    assert milestone(report, "equity_first_data")["earliest_kst"] == "2026-11-03T20:00:00+09:00"
    assert all(row["observed_ready"] is False for row in report["milestones"])
    assert report["broker_requests"] == report["database_writes"] == 0
    assert report["automatic_promotion"] is False and values == snapshot()


def test_dst_early_close_holiday_and_not_naive_weekdays():
    rows = calendar_rows()
    row = next(r for r in rows if r["date"] == "2026-11-27")
    assert session_times(row)[1].isoformat() == "2026-11-27T18:00:00+00:00"
    assert available(row).isoformat() == "2026-11-28T11:00:00+00:00"
    assert "2026-11-26" not in {r["date"] for r in rows}
    report = build_report(snapshot(), rows, NOW)
    expected = [r for r in rows if r["date"] >= "2026-10-19"][126]
    assert (
        milestone(report, "paper_127")["earliest_kst"]
        == available(expected).astimezone(__import__("zoneinfo").ZoneInfo("Asia/Seoul")).isoformat()
    )


def test_bad_operational_day_pushes_twenty_session_lower_bound():
    values, rows = snapshot(), calendar_rows()
    bad = values["operational_observation"]["window"][-1]
    bad.update(observed_minutes=200, longest_gap_minutes=190, initial_quality_passed=False)
    report = build_report(values, rows, NOW)
    assert milestone(report, "operational_20")["earliest_kst"] == "2026-11-18T06:00:00+09:00"
    assert not milestone(report, "operational_20")["observed_ready"]


@pytest.mark.parametrize("change", ["stale", "future", "snapshot_count", "changed_baseline", "source_review"])
def test_stale_future_or_torn_snapshot_never_reports_ready(change):
    values = snapshot()
    if change in {"stale", "future"}:
        values["performance_status"]["heartbeat"] = (
            NOW + timedelta(seconds=1) if change == "future" else NOW - timedelta(minutes=6)
        ).isoformat()
    elif change == "snapshot_count":
        values.pop(PAPER_PREFIX + "2026-10-19")
    elif change == "changed_baseline":
        values["performance_status"]["baseline_date"] = "2026-10-20"
    else:
        values["equity_forward_0090"]["state"] = "review_required"
    report = build_report(values, calendar_rows(), NOW)
    assert report["state"] == "review_required" and not any(m["observed_ready"] for m in report["milestones"])
    assert report["next_resume_kst"] == "2026-10-21T16:40:00+09:00"


def test_future_paper_observation_cannot_be_counted_even_with_updated_status():
    values = snapshot()
    values[PAPER_PREFIX + "2026-10-20"] = {"date": "2026-10-20"}
    values["performance_status"].update(observations=2, return_intervals=1, latest_session="2026-10-20")
    with pytest.raises(ValueError, match="future Paper"):
        build_report(values, calendar_rows(), NOW)


def test_changed_registration_funding_and_quality_contracts_block():
    for change in ["contract", "funding", "quality"]:
        values = snapshot()
        if change == "contract":
            values["paper-study-registration-0075"]["contract"] = {"start": "2026-10-18"}
        elif change == "funding":
            values["etf-auto-registration-0072"]["capital_limit"] = "20000"
        else:
            values["operational_observation"]["window"][0]["critical_samples"] = 1
        with pytest.raises(ValueError):
            build_report(values, calendar_rows(), NOW)


def test_completed_monthly_decision_moves_reminder_and_expired_calendar_requests_refresh():
    values = snapshot()
    values[MONTHLY_PREFIX + "2026-11"] = {"state": "budget_exhausted"}
    report = build_report(values, calendar_rows(), NOW, {"refresh_recommended": True})
    assert milestone(report, "paper_monthly")["earliest_kst"] == "2026-12-01T23:30:00+09:00"
    assert report["next_resume_reason"] == "calendar_refresh"


@pytest.mark.parametrize("change", ["duplicate", "missing_month", "wrong_hours", "weekend"])
def test_invalid_calendar_rejected(change):
    rows = calendar_rows()
    if change == "duplicate":
        rows.insert(1, rows[0])
    elif change == "missing_month":
        rows = [r for r in rows if not r["date"].startswith("2027-03")]
    elif change == "wrong_hours":
        rows[0]["close"] = "08:00"
    else:
        rows[0]["date"] = "2026-10-03"
    with pytest.raises(ValueError):
        validate_calendar(rows)


class Reader:
    def __init__(self, status=None):
        self.calls = []
        self.status = status

    async def request(self, *args, **kwargs):
        self.calls.append((args, copy.deepcopy(kwargs)))
        if self.status:
            raise BrokerError(self.status, "redacted")
        return calendar_rows()


async def test_collector_date_vintages_cache_and_http_failure_never_repeats(settings):
    settings = settings.model_copy(
        update={"alpaca_key": SecretStr("test"), "alpaca_secret": SecretStr("test")}
    )
    reader = Reader()
    result = await collect(settings, reader, NOW)
    assert (
        result["logical_gets"] == 1
        and (await collect(settings, reader, NOW))["logical_gets"] == 0
        and len(reader.calls) == 1
    )
    rows, info = load_calendar(settings.data_dir, NOW)
    assert rows == calendar_rows() and info["execution_calendar_replaced"] is False
    reader.status = 403
    with pytest.raises(ValueError, match="HTTP"):
        await collect(settings, reader, NOW + timedelta(days=1))
    reader.status = None
    with pytest.raises(ValueError, match="HTTP"):
        await collect(settings, reader, NOW + timedelta(days=1))
    assert len(reader.calls) == 2
    with pytest.raises(ValueError):
        load_calendar(settings.data_dir, NOW + timedelta(days=1))


@pytest.mark.parametrize(
    "method,path,data_api,payload",
    [
        ("POST", "/v2/orders", False, None),
        ("GET", "/v2/account", False, None),
        ("GET", "/v2/calendar", True, None),
        ("GET", "/v2/calendar", False, {}),
        ("DELETE", "/v2/calendar", False, None),
    ],
)
async def test_collector_forbidden_transport(settings, method, path, data_api, payload):
    reader = CalendarReader(settings)
    try:
        with pytest.raises(ValueError):
            await reader.request(method, path, data_api=data_api, payload=payload)
    finally:
        await reader.close()


def test_progress_import_never_loads_models_or_network_clients():
    code = "import sys,json; import stock_trader.work_progress; print(json.dumps(sorted(set(sys.modules)&{'numpy','pandas','lightgbm','stock_trader.broker','stock_trader.operations','stock_trader.performance'})))"
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == []


def later_snapshot(now):
    rows, values = calendar_rows(), snapshot()
    for key in ["operational_observation", "performance_status", "equity_shadow_0086", "equity_forward_0090"]:
        values[key]["heartbeat"] = now.isoformat()
    study = [r for r in rows if r["date"] >= "2026-10-19"]
    closed = [r for r in study if session_times(r)[1] <= now]
    window = []
    for row in closed:
        minutes = int((session_times(row)[1] - session_times(row)[0]).total_seconds() // 60)
        values[OPS_PREFIX + row["date"]] = {}
        window.append(
            {
                "date": row["date"],
                "expected_minutes": minutes,
                "observed_minutes": minutes,
                "critical_samples": 0,
                "longest_gap_minutes": 0,
                "initial_quality_passed": True,
            }
        )
    values["operational_observation"].update(
        completed_sessions=len(closed),
        window=window[-20:],
        monthly_decisions_completed=1,
        state="review_ready",
    )
    eligible = [r for r in study if available(r) <= now][:253]
    for row in eligible:
        values[PAPER_PREFIX + row["date"]] = {"date": row["date"]}
    values["performance_status"].update(
        observations=len(eligible),
        return_intervals=len(eligible) - 1,
        latest_session=eligible[-1]["date"],
        state="review_ready" if len(eligible) >= 127 else "collecting",
    )
    return values


def test_observed_operational_qualification_requires_monthly_and_never_approves_trade():
    now = datetime(2026, 11, 14, 12, tzinfo=UTC)
    values = later_snapshot(now)
    report = build_report(values, calendar_rows(), now)
    assert milestone(report, "operational_20")["observed_ready"]
    assert not milestone(report, "paper_127")["observed_ready"] and report["automatic_promotion"] is False
    values["operational_observation"]["monthly_decisions_completed"] = 0
    assert not milestone(build_report(values, calendar_rows(), now), "operational_20")["observed_ready"]


def test_real_minimum_paper_and_candidate_dates_still_require_source_readiness():
    rows = calendar_rows()
    candidate_rows = [r for r in rows if r["date"] >= "2026-11-02"]
    now = available(candidate_rows[126]) + timedelta(hours=1)
    values = later_snapshot(now)
    report = build_report(values, rows, now)
    assert milestone(report, "paper_127")["observed_ready"]
    assert not milestone(report, "equity_127")["observed_ready"]
    values["equity_forward_0090"].update(
        observations=127, monthly_decisions=6, review_ready=True, state="observing"
    )
    assert milestone(build_report(values, rows, now), "equity_127")["observed_ready"]
    values["equity_forward_0090"]["monthly_decisions"] = 5
    assert not milestone(build_report(values, rows, now), "equity_127")["observed_ready"]


def test_past_due_missing_daily_reports_immediate_review_and_candidate_future_count_blocked():
    now = NOW + timedelta(hours=3)
    values = snapshot()
    for key in ["operational_observation", "performance_status", "equity_shadow_0086", "equity_forward_0090"]:
        values[key]["heartbeat"] = now.isoformat()
    report = build_report(values, calendar_rows(), now)
    assert (
        "paper_eligible_observation_missing" in report["issues"]
        and report["next_resume_reason"] == "source_review"
    )
    values = snapshot()
    values["equity_forward_0090"].update(observations=127, monthly_decisions=6, review_ready=True)
    with pytest.raises(ValueError, match="future candidate"):
        build_report(values, calendar_rows(), NOW)


async def test_api_auth_read_only_single_snapshot_and_missing_calendar(settings, store):
    settings = settings.model_copy(update={"api_token": SecretStr("test-progress")})
    values = snapshot()
    # Freeze report time via load_progress directly; API uses the actual current
    # clock so this test only requires a safe response and no side effects there.
    for key, value in values.items():
        await store.put(key, {"measurement": value} if key.startswith(PAPER_PREFIX) else value)
    root = settings.data_dir / CALENDAR_ROOT / "snapshots"
    seal(
        root / "2026-10-21.json",
        {
            "contract": CALENDAR_CONTRACT,
            "request": CALENDAR_REQUEST,
            "state": "received",
            "observed_at": NOW.isoformat(),
            "response": calendar_rows(),
        },
    )
    report = await load_progress(store, settings.data_dir, NOW)
    assert report["state"] == "observing"
    before = await store.all("SELECT key,value FROM kv ORDER BY key")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(settings, store)), base_url="http://test"
    ) as client:
        assert (await client.get("/progress")).status_code == 401
        response = await client.get("/progress", headers={"Authorization": "Bearer test-progress"})
        assert response.status_code in {200, 503} and response.headers["cache-control"] == "no-store"
        assert response.json()["broker_requests"] == response.json()["database_writes"] == 0
    assert before == await store.all("SELECT key,value FROM kv ORDER BY key")
    (root / "2026-10-21.json").unlink()
    blocked = await load_progress(store, settings.data_dir, NOW)
    assert blocked["state"] == "review_required" and not blocked["milestones"]
