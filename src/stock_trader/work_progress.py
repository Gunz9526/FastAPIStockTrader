"""ADR-0096/0097: read-only progress and conditional dates, never a trading gate."""

from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from stock_trader.clock import NEW_YORK, parse_time, utcnow
from stock_trader.research_seal import fingerprint, read

SEOUL = ZoneInfo("Asia/Seoul")
CALENDAR_ROOT = "operations/progress-calendar-0096"
CALENDAR_CONTRACT = {
    "decision": "ADR-0096",
    "start": "2026-10-01",
    "end": "2027-12-31",
    "purpose": "conditional_resume_forecast_only",
    "replaces_execution_calendar": False,
    "one_logical_get_per_vintage": True,
}
CALENDAR_REQUEST = {
    "path": "/v2/calendar",
    "data_api": False,
    "params": {"start": "2026-10-01", "end": "2027-12-31", "date_type": "TRADING"},
}
CONTRACT = {
    "decision": "ADR-0095/0096/0097",
    "paper_only": True,
    "broker_requests": 0,
    "database_writes": 0,
    "automatic_promotion": False,
    "dates": "conditional calendar lower bounds, not observed qualification",
    "daily_resume_buffer_minutes": 30,
    "operational_resume_buffer_minutes": 10,
    "calendar_refresh_days": 30,
}
REGISTRATIONS = {
    "operational-study-registration-0081": "6ae42a80a072761e040dbb4705a712aea1107418bfff661761ecec6e51480e00",
    "paper-study-registration-0075": "784d705e099726ec15921b3e85f2353a234cdb13ae8e76b80695589d47601a85",
    "equity-study-registration-0086": "577812d0384a200777030a1782c221276b2568843aedaea408eafbaeeca763ce",
    "equity-forward-registration-0089": "85928895d3f3d8196c4a81ffb203451eb0993044865e89301c7fe891d6a2165b",
}
STATUS = {
    "operational_observation": ("operational-study-registration-0081", 300),
    "performance_status": ("paper-study-registration-0075", 300),
    "equity_shadow_0086": ("equity-study-registration-0086", 1800),
    "equity_forward_0090": ("equity-forward-registration-0089", 1800),
}
PAPER_PREFIX = "paper-study-observation-0075:"
OPS_PREFIX = "operational-study-session-0081:"
MONTHLY_PREFIX = "etf-auto-cycle:paper-auto-monthly-0068-"
TERMINAL = {"complete_filled", "complete_partial", "complete_no_fill", "budget_exhausted"}
KEYS = [*REGISTRATIONS, *STATUS, "etf-auto-registration-0072", "etf_auto"]


def session_times(row):
    day = date.fromisoformat(row["date"])
    opening = datetime.combine(day, time.fromisoformat(row["open"]), NEW_YORK).astimezone(UTC)
    closing = datetime.combine(day, time.fromisoformat(row["close"]), NEW_YORK).astimezone(UTC)
    if opening >= closing or not 0 < (closing - opening).total_seconds() <= 86400:
        raise ValueError("invalid progress session hours")
    return opening, closing


def available(row):
    return datetime.combine(
        date.fromisoformat(row["date"]) + timedelta(days=1), time(6), NEW_YORK
    ).astimezone(UTC)


def korean(stamp):
    return stamp.astimezone(SEOUL).isoformat() if stamp else None


def validate_calendar(rows):
    if not isinstance(rows, list) or not rows:
        raise ValueError("missing forecast calendar")
    days = [row["date"] for row in rows]
    if days != sorted(set(days)):
        raise ValueError("unsorted or duplicate calendar")
    months = {}
    for row in rows:
        day = date.fromisoformat(row["date"])
        if not CALENDAR_CONTRACT["start"] <= row["date"] <= CALENDAR_CONTRACT["end"] or day.weekday() >= 5:
            raise ValueError("forecast calendar date outside scope")
        session_times(row)
        months[day.strftime("%Y-%m")] = months.get(day.strftime("%Y-%m"), 0) + 1
    month, end = date(2026, 10, 1), date(2027, 12, 1)
    while month <= end:
        if not 15 <= months.get(month.strftime("%Y-%m"), 0) <= 23:
            raise ValueError("truncated forecast calendar month")
        month = date(month.year + month.month // 12, month.month % 12 + 1, 1)
    return rows


def load_calendar(data_dir, now):
    paths = sorted((Path(data_dir) / CALENDAR_ROOT / "snapshots").glob("*.json"))
    if not paths:
        raise ValueError("forecast calendar not collected")
    path = paths[-1]
    value = read(path)
    observed = parse_time(value["observed_at"])
    if (
        value["contract"] != CALENDAR_CONTRACT
        or value["request"] != CALENDAR_REQUEST
        or value["state"] != "received"
        or observed > now
        or path.stem != observed.astimezone(NEW_YORK).date().isoformat()
    ):
        raise ValueError("forecast calendar provenance mismatch")
    return validate_calendar(value["response"]), {
        "observed_at": observed.isoformat(),
        "payload_sha256": fingerprint(value),
        "refresh_recommended": now - observed > timedelta(days=CONTRACT["calendar_refresh_days"]),
        "execution_calendar_replaced": False,
    }


def integer(value):
    if type(value) is not int or value < 0:
        raise ValueError("invalid observation count")
    return value


def check_sources(values, now):
    funding = values["etf-auto-registration-0072"]
    if (
        funding.get("paper") is not True
        or funding.get("capital_limit") != "10000"
        or funding.get("version") != 72
    ):
        raise ValueError("funding contract changed")
    problems = []
    for key, digest in REGISTRATIONS.items():
        value = values[key]
        if fingerprint(value["contract"]) != digest:
            raise ValueError("observation contract changed")
        if key in {"operational-study-registration-0081", "paper-study-registration-0075"} and value[
            "funding_registration_sha256"
        ] != fingerprint(funding):
            raise ValueError("observation funding binding changed")
    for key, (registration, maximum_age) in STATUS.items():
        value = values[key]
        digest = fingerprint(values[registration]["contract"])
        if value.get("contract_sha256", fingerprint(value.get("contract"))) != digest:
            raise ValueError("status contract changed")
        age = (now - parse_time(value["heartbeat"])).total_seconds()
        if not 0 <= age <= maximum_age:
            problems.append(key + ":stale_or_future")
        if value.get("state") in {"review_required", "failed", "waiting_keys"}:
            problems.append(key + ":review_required")
        if key.startswith("equity_"):
            if value.get("orders_submitted") != 0 or value.get("mode") != "research_only":
                raise ValueError("research authority changed")
            if value.get("registration_sha256") != fingerprint(values[registration]):
                raise ValueError("research registration binding changed")
    return problems


def build_report(values, rows, now, calendar_info=None):
    now = parse_time(now)
    validate_calendar(rows)
    issues = check_sources(values, now)
    by_day = {row["date"]: row for row in rows}
    paper = values["performance_status"]
    ops = values["operational_observation"]
    research = values["equity_shadow_0086"]
    forward = values["equity_forward_0090"]
    paper_rows = [row for row in rows if row["date"] >= "2026-10-19"]
    candidate_rows = [row for row in rows if row["date"] >= "2026-11-02"]
    observations, intervals = integer(paper["observations"]), integer(paper["return_intervals"])
    dates = sorted(key.removeprefix(PAPER_PREFIX) for key in values if key.startswith(PAPER_PREFIX))
    expected = [row["date"] for row in paper_rows[:observations]]
    if dates != expected or intervals != max(0, observations - 1) or observations > 253:
        issues.append("paper_observation_snapshot_refreshing")
    if observations and (
        paper.get("baseline_date") != expected[0] or paper.get("latest_session") != expected[-1]
    ):
        issues.append("paper_observation_dates_changed")
    if any(available(by_day[day]) > now for day in dates):
        raise ValueError("future Paper observation")
    for key in values:
        if key.startswith(PAPER_PREFIX) and values[key]["date"] != key.removeprefix(PAPER_PREFIX):
            raise ValueError("Paper observation identity changed")
    closed = [row for row in paper_rows if session_times(row)[1] <= now]
    window = ops.get("window", [])
    if [r["date"] for r in window] != [r["date"] for r in closed[-20:]] or integer(
        ops["completed_sessions"]
    ) != len(closed):
        issues.append("operational_snapshot_refreshing")
    bad = []
    for row in window:
        session = by_day[row["date"]]
        minutes = int((session_times(session)[1] - session_times(session)[0]).total_seconds() // 60)
        count, critical, gap = (
            integer(row[k]) for k in ("observed_minutes", "critical_samples", "longest_gap_minutes")
        )
        good = count * 10 >= minutes * 9 and gap <= 5 and critical == 0
        if count > minutes or row["expected_minutes"] != minutes or row["initial_quality_passed"] is not good:
            raise ValueError("operational quality summary changed")
        if row["date"] not in {key.removeprefix(OPS_PREFIX) for key in values if key.startswith(OPS_PREFIX)}:
            if count:
                raise ValueError("operational sample record missing")
        if not good:
            bad.append(row["date"])
    monthly = integer(ops.get("monthly_decisions_completed", 0))
    operational_index = 19
    if bad:
        operational_index = max(
            operational_index, next(i for i, row in enumerate(paper_rows) if row["date"] == bad[-1]) + 20
        )
    firsts = list({row["date"][:7]: row for row in reversed(candidate_rows)}.values())
    firsts = sorted(firsts, key=lambda row: row["date"])
    monthly_row = next(
        (
            row
            for row in firsts
            if values.get(MONTHLY_PREFIX + row["date"][:7], {}).get("state") not in TERMINAL
        ),
        None,
    )
    milestones, resumes = [], []

    def milestone(identity, label, stamp, ready, condition, resume=None):
        milestones.append(
            {
                "id": identity,
                "label": label,
                "earliest_kst": korean(stamp),
                "observed_ready": bool(ready and not issues),
                "condition": condition,
            }
        )
        if not ready and resume is not None:
            resumes.append((max(now, resume), identity))

    next_paper = paper_rows[observations] if observations < min(253, len(paper_rows)) else None
    if next_paper:
        when = available(next_paper)
        if now >= when + timedelta(minutes=30):
            issues.append("paper_eligible_observation_missing")
        milestone(
            "next_paper_data",
            "다음 Paper 일봉/관측 확인",
            when,
            False,
            "자료 실제 수신·원장 대조 필요",
            when + timedelta(minutes=30),
        )
    next_session = next((row for row in rows if session_times(row)[1] > now), None)
    if next_session:
        milestone(
            "next_regular_session",
            "다음 정규장 운영 확인",
            session_times(next_session)[0],
            False,
            "현재 장중이면 종료 후 실제 분당 관측 확인",
            session_times(next_session)[1] + timedelta(minutes=10),
        )
    op_when = session_times(paper_rows[operational_index])[1] if operational_index < len(paper_rows) else None
    op_ready = ops["state"] == "review_ready" and len(window) == 20 and not bad and monthly > 0
    milestone(
        "operational_20",
        "실제20운영 세션 검토",
        op_when,
        op_ready,
        "연속20세션 품질 통과·월초 종료 결정1회, 자동 승인 없음",
        op_when + timedelta(minutes=10) if op_when else None,
    )
    if monthly_row:
        opening = session_times(monthly_row)[0]
        milestone(
            "paper_monthly",
            "SPY Paper 월초 결정 확인",
            opening,
            False,
            "남은 누적 예산·기존 위험/운영 조건, 체결 보장 아님",
            opening + timedelta(minutes=30),
        )
    formation = parse_time(research["formation_start"]) if research.get("formation_start") else None
    if formation:
        milestone(
            "equity_formation",
            "연구 월초 목표 형성 확인",
            formation,
            False,
            "실제 사전 기록 필요·개별주 주문 권한 없음",
            formation + timedelta(minutes=30),
        )
    candidate_count = integer(forward.get("observations", 0))
    candidate_monthly = integer(forward.get("monthly_decisions", 0))
    if candidate_count > sum(available(row) <= now for row in candidate_rows) or candidate_monthly > sum(
        available(row) <= now for row in firsts
    ):
        raise ValueError("future candidate observation or monthly decision")
    candidate_ready = (
        forward.get("review_ready") is True and candidate_count >= 127 and candidate_monthly >= 6
    )
    first_available = available(candidate_rows[0])
    if candidate_count == 0:
        milestone(
            "equity_first_data",
            "첫 연구 미래 자료·독립 정합성 검토",
            first_available,
            False,
            "원본·독립 근거·coverage 필요, 빈 응답으로 승인하지 않음",
            first_available + timedelta(minutes=30),
        )
    milestone(
        "paper_127",
        "Paper 최소127관측 검토",
        available(paper_rows[126]),
        paper["state"] == "review_ready" and observations >= 127 and intervals >= 126,
        "연속 실제 관측·게시 원장 기준, 경제적 총수익 승인 아님",
        available(paper_rows[126]) + timedelta(minutes=30),
    )
    candidate_when = max(available(candidate_rows[126]), available(firsts[5]))
    milestone(
        "equity_127",
        "새 후보 최소127관측·6월초 검토",
        candidate_when,
        candidate_ready,
        "실제 독립 coverage·비용/배당/SPY 대조 필요, 자동 채택 없음",
        candidate_when + timedelta(minutes=30),
    )
    if issues:
        resumes.append((now, "source_review"))
    if calendar_info and calendar_info.get("refresh_recommended"):
        resumes.append((now, "calendar_refresh"))
    # A data failure takes precedence over a due-date reminder at the same time.
    resumes.sort(key=lambda value: (value[0], value[1] != "source_review", value[1]))
    return {
        "state": "review_required" if issues else "observing",
        "as_of": now.isoformat(),
        "as_of_kst": korean(now),
        "contract_sha256": fingerprint(CONTRACT),
        **CONTRACT,
        "issues": issues,
        "calendar": calendar_info or {},
        "milestones": milestones,
        "counts": {
            "completed_operational_sessions": len(closed),
            "operational_good_in_window": len(window) - len(bad),
            "operational_window_required": 20,
            "monthly_decisions": monthly,
            "paper_observations": observations,
            "paper_return_intervals": intervals,
            "candidate_observations": candidate_count,
            "candidate_monthly_decisions": candidate_monthly,
        },
        "next_resume_kst": korean(resumes[0][0]) if resumes else None,
        "next_resume_reason": resumes[0][1] if resumes else None,
        "external_dependencies": [
            "서버 밖 감시 장소 미확정",
            "외부 백업 저장 위치/접근/암호화/보관 조건 미확정",
        ],
        "source_sha256": {key: fingerprint(values[key]) for key in [*REGISTRATIONS, *STATUS]},
    }


async def progress_snapshot(store):
    # One MVCC statement; select only dates for financial observation rows.
    rows = await store.all(
        "SELECT key, CASE WHEN key LIKE %s THEN jsonb_build_object('date',value->'measurement'->>'date') "
        "WHEN key LIKE %s THEN '{}'::jsonb ELSE value END AS value FROM kv "
        "WHERE key=ANY(%s) OR key LIKE %s OR key LIKE %s OR key LIKE %s",
        (
            PAPER_PREFIX + "%",
            OPS_PREFIX + "%",
            KEYS,
            PAPER_PREFIX + "%",
            OPS_PREFIX + "%",
            MONTHLY_PREFIX + "%",
        ),
    )
    return {row["key"]: row["value"] for row in rows}


async def load_progress(store, data_dir, now=None):
    values = await progress_snapshot(store)
    now = parse_time(now) if now else utcnow()
    try:
        rows, info = load_calendar(data_dir, now)
        return build_report(values, rows, now, info)
    except ValueError, KeyError, TypeError, OSError, ArithmeticError:
        return {
            "state": "review_required",
            "as_of": now.isoformat(),
            "as_of_kst": korean(now),
            **CONTRACT,
            "contract_sha256": fingerprint(CONTRACT),
            "issues": ["progress_inputs_unavailable_or_changed"],
            "milestones": [],
            "next_resume_kst": korean(now),
            "next_resume_reason": "source_review",
        }
