"""ADR-0095: read-only audit of actual future observations; no broker calls."""

import asyncio
import hashlib
import json
from datetime import UTC, date, datetime, time, timedelta

from stock_trader.clock import NEW_YORK, parse_time, utcnow
from stock_trader.config import Settings
from stock_trader.etf_shadow import eligible_through, read_sealed, session_times
from stock_trader.operational_study import CONTRACT as OPS_CONTRACT
from stock_trader.operational_study import PREFIX as OPS_PREFIX
from stock_trader.operational_study import quality
from stock_trader.performance import CONTRACT as PAPER_CONTRACT
from stock_trader.performance import measurement, metrics
from stock_trader.research_seal import fingerprint
from stock_trader.storage import Store


async def audit():
    settings = Settings()
    store = Store(settings)
    await store.pool.open(wait=True, timeout=30)
    try:
        async with store.pool.connection() as connection:
            await connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            records = await (await connection.execute("SELECT key,value FROM kv")).fetchall()
            rows = await (
                await connection.execute(
                    "SELECT o.id FROM broker_orders o JOIN intents i ON i.client_id=o.client_id "
                    "WHERE i.action IN ('etf_buy','etf_sell')"
                )
            ).fetchall()
    finally:
        await store.close()
    now = utcnow()
    values = {row["key"]: row["value"] for row in records}
    funding_sha = fingerprint(values["etf-auto-registration-0072"])
    for key, contract in [
        ("operational-study-registration-0081", OPS_CONTRACT),
        ("paper-study-registration-0075", PAPER_CONTRACT),
    ]:
        if values[key]["contract"] != contract or values[key]["funding_registration_sha256"] != funding_sha:
            raise ValueError("observation registration changed")
    root = settings.data_dir / "etf-shadow"
    sessions = {}
    calendar_files = {}
    for path in sorted((root / "calendar").glob("*.json")):
        value = read_sealed(path)
        if value["month"][:7] != path.stem:
            raise ValueError("observation calendar identity changed")
        calendar_files[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
        for row in value["sessions"]:
            if row["date"][:7] != path.stem or row["date"] in sessions:
                raise ValueError("duplicate observation session")
            if row["date"] >= "2026-10-19":
                sessions[row["date"]] = row
    closed = [row for _, row in sorted(sessions.items()) if session_times(row)[1] <= now]
    operational = []
    for row in closed:
        record = values.get(OPS_PREFIX + row["date"], {"session_sha256": fingerprint(row), "samples": {}})
        summary = quality(record, row)
        if any(parse_time(sample["observed_at"]) > now for sample in record["samples"].values()):
            raise ValueError("future operational sample")
        operational.append({**summary, "record_sha256": fingerprint(record)})
    stored_window = values["operational_observation"].get("window", [])
    if [{k: v for k, v in row.items() if k != "record_sha256"} for row in operational[-20:]] != stored_window:
        raise ValueError("operational summary differs from minute records")
    cps = [value for key, value in values.items() if key.startswith("etf-account-checkpoint:")]
    if (
        len(cps) != 1
        or not cps[0]["reconciliation"]["matched"]
        or not cps[0]["reconciliation"]["all_fills_owned"]
    ):
        raise ValueError("account not reconciled")
    approved = {row["id"] for row in rows}
    prefix = "paper-study-observation-0075:"
    paper_keys = sorted(key for key in values if key.startswith(prefix))
    cutoff = eligible_through(now)
    expected_dates = [day for day in sorted(sessions) if date.fromisoformat(day) <= cutoff][:253]
    if [key.removeprefix(prefix) for key in paper_keys] != expected_dates[: len(paper_keys)]:
        raise ValueError("Paper observation dates not a contiguous prefix")
    measurements, paper_proofs = [], []
    for key in paper_keys:
        day = key.removeprefix(prefix)
        path = root / "daily" / (day + ".json")
        daily = read_sealed(path)
        if daily["session"] != sessions[day] or daily["date"] != day:
            raise ValueError("Paper daily session changed")
        stored = values[key]
        available = datetime.combine(
            date.fromisoformat(day) + timedelta(days=1), time(6), NEW_YORK
        ).astimezone(UTC)
        if not available <= parse_time(stored["observed_at"]) <= now or date.fromisoformat(day) > cutoff:
            raise ValueError("Paper observation time changed")
        actual = measurement(cps[0], daily, hashlib.sha256(path.read_bytes()).hexdigest(), approved)
        if stored["measurement"] != actual:
            raise ValueError("Paper measurement changed")
        measurements.append(actual)
        paper_proofs.append(
            {
                "date": day,
                "observation_sha256": fingerprint(stored),
                "daily_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    status = values["performance_status"]
    calculated = metrics(measurements)
    if status["observations"] != len(measurements) or any(
        status[key] != value for key, value in calculated.items()
    ):
        raise ValueError("Paper summary differs from actual observations")
    return {
        "state": "passed",
        "checked_at": now.isoformat(),
        "broker_requests": 0,
        "database_writes": 0,
        "operational_sessions": operational,
        "paper_observations": paper_proofs,
        "paper_observation_count": len(measurements),
        "paper_return_intervals": calculated["return_intervals"],
        "measurements": measurements,
        "expected_eligible_paper_sessions": expected_dates,
        "calendar_file_sha256": calendar_files,
        "economic_total_return": "not_calculated",
        "performance_approved": False,
        "account_ledger_sha256": fingerprint(cps[0]["ledger"]),
    }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(audit()), indent=2))
