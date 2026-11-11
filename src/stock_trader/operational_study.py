"""ADR-0081: future regular-session operational samples; never a trading gate."""

import re
from datetime import date

from psycopg.types.json import Jsonb

from stock_trader.clock import NEW_YORK, parse_time, utcnow
from stock_trader.etf_funding import REGISTRY
from stock_trader.etf_ledger import fingerprint
from stock_trader.etf_shadow import read_sealed, session_times
from stock_trader.health import SHA as HEALTH_SHA

START = date(2026, 10, 19)
REGISTRATION = "operational-study-registration-0081"
PREFIX = "operational-study-session-0081:"
CONTRACT = {
    "decision": "ADR-0081",
    "start": START.isoformat(),
    "sample_seconds": 60,
    "review_window_sessions": 20,
    "minimum_coverage": "0.90",
    "maximum_gap_minutes": 5,
    "maximum_critical_samples": 0,
    "requires_monthly_completion": True,
    "health_contract_sha256": HEALTH_SHA,
    "automatic_promotion": False,
}


def quality(record, session):
    opening, closing = session_times(session)
    minutes = int((closing - opening).total_seconds() / 60)
    if not 1 <= minutes <= 1440 or record.get("session_sha256") != fingerprint(session):
        raise ValueError("operational session contract changed")
    samples = record["samples"]
    if not isinstance(samples, dict):
        raise ValueError("invalid operational samples")
    indices = set()
    critical = warnings = 0
    for key, value in samples.items():
        index = int(key)
        timestamp = parse_time(value["observed_at"])
        if str(index) != key or not 0 <= index < minutes or not opening <= timestamp < closing:
            raise ValueError("sample outside regular session")
        if int((timestamp - opening).total_seconds() // 60) != index:
            raise ValueError("sample time changed")
        if value["state"] not in {"healthy", "warning", "blocked"}:
            raise ValueError("invalid operational health")
        indices.add(index)
        critical += value["state"] == "blocked"
        warnings += value["state"] == "warning"
    gap = maximum = 0
    for index in range(minutes):
        gap = 0 if index in indices else gap + 1
        maximum = max(maximum, gap)
    observed = len(indices)
    # Exact integer comparison avoids a floating-point coverage boundary.
    good = observed * 10 >= minutes * 9 and maximum <= CONTRACT["maximum_gap_minutes"] and critical == 0
    return {
        "date": session["date"],
        "expected_minutes": minutes,
        "observed_minutes": observed,
        "critical_samples": critical,
        "warning_samples": warnings,
        "longest_gap_minutes": maximum,
        "coverage": observed / minutes,
        "initial_quality_passed": good,
    }


class OperationalStudy:
    def __init__(self, settings, store):
        self.settings, self.store = settings, store

    async def status(self, state, now, **details):
        value = {
            "state": state,
            "heartbeat": now.isoformat(),
            "contract": CONTRACT,
            "broker_requests": 0,
            "performance_approved": False,
            **details,
        }
        await self.store.put("operational_observation", value)
        return value

    async def tick(self, health, now=None):
        now = now or utcnow()
        funding = await self.store.get(REGISTRY)
        if not funding:
            return await self.status("waiting_funding", now)
        if (
            funding.get("paper") is not True
            or funding.get("capital_limit") != "10000"
            or funding.get("version") != 72
        ):
            return await self.status("review_required", now, reason="funding_contract_changed")
        expected = {"contract": CONTRACT, "funding_registration_sha256": fingerprint(funding)}
        registered = await self.store.get(REGISTRATION)
        if not registered:
            if now.astimezone(NEW_YORK).date() >= START:
                return await self.status("review_required", now, reason="late_registration")
            await self.store.put(REGISTRATION, {**expected, "registered_at": now.isoformat()})
        elif any(registered.get(k) != v for k, v in expected.items()):
            return await self.status("review_required", now, reason="registration_changed")
        if now.astimezone(NEW_YORK).date() < START:
            return await self.status("not_started", now, completed_sessions=0, observed_sessions=0)
        root = self.settings.data_dir / "etf-shadow/calendar"
        try:
            local = now.astimezone(NEW_YORK).date()
            if not (root / f"{local:%Y-%m}.json").exists():
                return await self.status("waiting_calendar", now)
            month = START.replace(day=1)
            while month <= local.replace(day=1):
                if not (root / f"{month:%Y-%m}.json").exists():
                    return await self.status("waiting_calendar", now)
                month = date(month.year + month.month // 12, month.month % 12 + 1, 1)
            sessions = {}
            for path in sorted(root.glob("*.json")):
                for row in read_sealed(path)["sessions"]:
                    day = date.fromisoformat(row["date"])
                    if f"{day:%Y-%m}" != path.stem or row["date"] in sessions:
                        raise ValueError("calendar session misplaced")
                    if START <= day <= local:
                        sessions[row["date"]] = row
            if health.get("contract_sha256") != HEALTH_SHA or health.get("observed_at") != now.isoformat():
                raise ValueError("health sample contract changed")
            for session in sessions.values():
                opening, closing = session_times(session)
                if not opening <= now < closing:
                    continue
                key = PREFIX + session["date"]
                minute = str(int((now - opening).total_seconds() // 60))
                async with self.store.pool.connection() as conn:
                    await conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (key,))
                    row = await (await conn.execute("SELECT value FROM kv WHERE key=%s", (key,))).fetchone()
                    record = row["value"] if row else {"session_sha256": fingerprint(session), "samples": {}}
                    quality(record, session)
                    if minute not in record["samples"]:
                        record["samples"][minute] = {k: health[k] for k in ["observed_at", "state", "issues"]}
                        await conn.execute(
                            "INSERT INTO kv(key,value) VALUES(%s,%s) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=now()",
                            (key, Jsonb(record)),
                        )
            closed = [s for _, s in sorted(sessions.items()) if session_times(s)[1] <= now]
            window = closed[-CONTRACT["review_window_sessions"] :]
            days = []
            for session in window:
                record = await self.store.get(
                    PREFIX + session["date"], {"session_sha256": fingerprint(session), "samples": {}}
                )
                days.append(quality(record, session))
            monthly = await self.store.all(
                "SELECT value FROM kv WHERE key LIKE %s", ("etf-auto-cycle:paper-auto-monthly-0068-%",)
            )
            monthly_completed = 0
            for row in monthly:
                value = row["value"]
                cycle = value.get("cycle", "")
                match = re.fullmatch(r"paper-auto-monthly-0068-(\d{4}-\d{2})", cycle)
                if not match or value.get("state") not in {
                    "complete_filled",
                    "complete_partial",
                    "complete_no_fill",
                    "budget_exhausted",
                }:
                    continue
                first = next((s for day, s in sorted(sessions.items()) if day.startswith(match[1])), None)
                if first and session_times(first)[0] <= now:
                    monthly_completed += 1
            ready = (
                len(days) == CONTRACT["review_window_sessions"]
                and all(d["initial_quality_passed"] for d in days)
                and monthly_completed > 0
            )
            return await self.status(
                "review_ready" if ready else "collecting",
                now,
                completed_sessions=len(closed),
                window=days,
                monthly_decisions_completed=monthly_completed,
                current_health=health["state"],
                observed_sessions=(
                    await self.store.one("SELECT count(*) AS n FROM kv WHERE key LIKE %s", (PREFIX + "%",))
                )["n"],
            )
        except OSError, ValueError, KeyError, TypeError, OverflowError:
            return await self.status("review_required", now, reason="observation_validation_failed")
