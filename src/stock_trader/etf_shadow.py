"""ADR-0052/0053: persistent, GET-only daily data and unexecuted SPY targets."""

import asyncio
import calendar
import hashlib
import json
import math
import os
import signal
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from stock_trader.broker import Alpaca
from stock_trader.clock import NEW_YORK, parse_time, utcnow
from stock_trader.config import Settings
from stock_trader.etf_ledger import fingerprint
from stock_trader.etf_study import SYMBOLS
from stock_trader.logging import configure_logging, event
from stock_trader.storage import Store

START = date(2026, 10, 2)
CONTRACT = {
    "decision": "ADR-0052/0053",
    "start": START.isoformat(),
    "feed": "sip",
    "adjustment": "raw",
    "timeframe": "1Day",
    "symbols": list(SYMBOLS),
    "strategy": "spy_hold",
    "target_weights": {"SPY": "1"},
    "available_after": "next New York day 06:00",
    "poll_seconds": 900,
    "max_sessions_per_tick": 10,
    "orders_enabled": False,
    "purpose": "forward-operational-observation-not-performance-approval",
}


def read_sealed(path):
    if path.is_symlink():
        raise ValueError("symlink cache rejected")
    envelope = json.loads(path.read_text())
    if envelope["sha256"] != fingerprint(envelope["payload"]):
        raise ValueError("cache checksum mismatch")
    return envelope["payload"]


def seal(path, payload):
    """Atomic file commit. Production calls are serialized by the PG leader lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        if read_sealed(path) != payload:
            raise ValueError("immutable cache changed")
        return
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    try:
        with temporary.open("w") as stream:
            json.dump({"payload": payload, "sha256": fingerprint(payload)}, stream, allow_nan=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def eligible_through(now):
    local = parse_time(now).astimezone(NEW_YORK)
    return local.date() - timedelta(days=1 if local.hour >= 6 else 2)


def month_next(day):
    return (day.replace(day=28) + timedelta(days=4)).replace(day=1)


def session_times(row):
    day = date.fromisoformat(row["date"])
    opening = datetime.fromisoformat(f"{day}T{row['open']}").replace(tzinfo=NEW_YORK)
    closing = datetime.fromisoformat(f"{day}T{row['close']}").replace(tzinfo=NEW_YORK)
    if opening >= closing:
        raise ValueError("invalid calendar hours")
    return opening.astimezone(UTC), closing.astimezone(UTC)


def validate_bars(response, session):
    if response.get("next_page_token") or set(response.get("bars", {})) != set(SYMBOLS):
        raise ValueError("incomplete or unexpected daily symbols")
    day = session["date"]
    for rows in response["bars"].values():
        if len(rows) != 1:
            raise ValueError("missing or duplicate daily bar")
        row = rows[0]
        timestamp = parse_time(row["t"]).astimezone(NEW_YORK)
        if timestamp.date().isoformat() != day or timestamp.time().isoformat() != "00:00:00":
            raise ValueError("daily timestamp outside registered session")
        values = [float(row[k]) for k in ("o", "h", "l", "c", "v")]
        opening, high, low, close, volume = values
        if not all(math.isfinite(v) for v in values) or low <= 0 or volume <= 0:
            raise ValueError("invalid daily values")
        if not low <= opening <= high or not low <= close <= high:
            raise ValueError("invalid daily OHLC")
    return response["bars"]


class DailyReader:
    """Only the read operations used by this service are exposed."""

    def __init__(self, settings):
        self._client = Alpaca(settings)

    async def calendar(self, start, end):
        return await self._client.calendar(start, end)

    async def daily(self, day):
        start = datetime.combine(day, datetime.min.time(), NEW_YORK)
        # Alpaca includes the end instant; exclude the following session's midnight.
        end = datetime.combine(day + timedelta(days=1), datetime.min.time(), NEW_YORK) - timedelta(
            microseconds=1
        )
        return await self._client.request(
            "GET",
            "/v2/stocks/bars",
            data_api=True,
            params={
                "symbols": ",".join(SYMBOLS),
                "start": start.astimezone(UTC).isoformat(),
                "end": end.astimezone(UTC).isoformat(),
                "feed": "sip",
                "timeframe": "1Day",
                "adjustment": "raw",
                "limit": 1000,
            },
        )

    async def close(self):
        await self._client.close()


class Shadow:
    def __init__(self, settings, store, reader=None):
        self.settings, self.store = settings, store
        self.reader = reader or DailyReader(settings)
        self.directory = settings.data_dir / "etf-shadow"
        self.requests = 0

    def register(self, now):
        path = self.directory / "registration.json"
        if path.exists():
            registered = read_sealed(path)
            if registered["contract"] != CONTRACT:
                raise ValueError("shadow registration changed")
            return registered
        now = parse_time(now)
        registered = {
            "contract": CONTRACT,
            "registered_at": now.isoformat(),
            "prospective_from": (now.astimezone(NEW_YORK).date() + timedelta(days=1)).isoformat(),
            "source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        }
        seal(path, registered)
        return registered

    async def calendar_month(self, month):
        month = month.replace(day=1)
        path = self.directory / "calendar" / f"{month:%Y-%m}.json"
        if path.exists():
            saved = read_sealed(path)
            if saved["month"] != month.isoformat():
                raise ValueError("calendar cache key mismatch")
            rows = saved["sessions"]
        else:
            end = month.replace(day=calendar.monthrange(month.year, month.month)[1])
            rows = await self.reader.calendar(month.isoformat(), end.isoformat())
            self.requests += 1
        days = [r["date"] for r in rows]
        if not rows or days != sorted(set(days)) or any(d[:7] != f"{month:%Y-%m}" for d in days):
            raise ValueError("invalid monthly calendar")
        for row in rows:
            session_times(row)
        if not path.exists():
            seal(path, {"month": month.isoformat(), "sessions": rows})
        return rows

    async def ensure_day(self, session, now, *, fixed_time=True):
        day = date.fromisoformat(session["date"])
        if day < START or day > eligible_through(now):
            raise ValueError("sealed or unfinished session requested")
        path = self.directory / "daily" / f"{day}.json"
        if path.exists():
            saved = read_sealed(path)
            if (
                saved["date"] != day.isoformat()
                or saved["session"] != session
                or saved["contract_sha256"] != fingerprint(CONTRACT)
            ):
                raise ValueError("daily cache contract mismatch")
            validate_bars({"bars": saved["bars"]}, session)
            return False
        response = await self.reader.daily(day)
        self.requests += 1
        bars = validate_bars(response, session)
        seal(
            path,
            {
                "date": day.isoformat(),
                "session": session,
                "bars": bars,
                "collected_at": parse_time(now if fixed_time else utcnow()).isoformat(),
                "contract_sha256": fingerprint(CONTRACT),
            },
        )
        return True

    def decision(self, session, signal_day, registration, now, initial=False):
        opening, _ = session_times(session)
        timely = parse_time(now) < opening
        payload = {
            "strategy": "spy_hold",
            "target_weights": {"SPY": "1"},
            "kind": "initial" if initial else "monthly",
            "execution_session": session["date"],
            "intended_open": opening.isoformat(),
            "signal_session": signal_day,
            "recorded_at": parse_time(now).isoformat(),
            "timely": timely,
            "prospective": timely and session["date"] >= registration["prospective_from"],
            "mode": "observation_only",
            "orders_submitted": 0,
            "cash_buffer": "0.001",
            "cost_reserve": "0.002",
            "registration_sha256": fingerprint(registration),
        }
        if signal_day is not None:
            path = self.directory / "daily" / f"{signal_day}.json"
            read_sealed(path)
            payload["signal_data_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        path = self.directory / "decisions" / f"{session['date']}-{'initial' if initial else 'monthly'}.json"
        if path.exists():
            saved = read_sealed(path)
            for key in (
                "strategy",
                "target_weights",
                "execution_session",
                "signal_session",
                "registration_sha256",
            ):
                if saved[key] != payload[key]:
                    raise ValueError("decision input changed")
            if saved.get("signal_data_sha256") != payload.get("signal_data_sha256"):
                raise ValueError("decision input data changed")
            return saved
        seal(path, payload)
        return payload

    async def tick(self, now=None):
        fixed_time = now is not None
        now = parse_time(now or utcnow())
        self.requests = 0
        if not self.settings.credentials_present:
            await self.store.put(
                "etf_shadow",
                {"state": "waiting_alpaca_keys", "updated_at": now.isoformat(), "orders_submitted": 0},
            )
            return
        registration = self.register(now)
        cutoff = eligible_through(now)
        rows, months = [], []
        month = START.replace(day=1)
        horizon = month_next(now.astimezone(NEW_YORK).date())
        while month <= horizon:
            sessions = await self.calendar_month(month)
            rows.extend(sessions)
            months.append(sessions)
            month = month_next(month)
        eligible = [r for r in rows if START.isoformat() <= r["date"] <= cutoff.isoformat()]
        completed, added = [], 0
        for session in eligible:
            path = self.directory / "daily" / f"{session['date']}.json"
            if not path.exists() and added >= CONTRACT["max_sessions_per_tick"]:
                break
            added += int(await self.ensure_day(session, now, fixed_time=fixed_time))
            completed.append(session["date"])
        # Initial target is recorded before the next registered session, independent
        # of monthly trend features (SPY hold has none). Never invent a fill.
        observed_at = now if fixed_time else utcnow()
        first = next(r for r in rows if r["date"] >= registration["prospective_from"])
        initial = self.decision(first, None, registration, observed_at, initial=True)
        decisions = [initial]
        for current, following in zip(months, months[1:], strict=False):
            if current[-1]["date"] in completed:
                decisions.append(self.decision(following[0], current[-1]["date"], registration, observed_at))
        next_month = next(m[0] for m in months if m[0]["date"] > now.astimezone(NEW_YORK).date().isoformat())
        state = "catching_up" if len(completed) < len(eligible) else "observing"
        status = {
            "state": state,
            "updated_at": observed_at.isoformat(),
            "last_success_at": observed_at.isoformat(),
            "strategy": "spy_hold",
            "mode": "observation_only",
            "orders_submitted": 0,
            "sessions": len(completed),
            "latest_completed_session": completed[-1] if completed else None,
            "prospective_from": registration["prospective_from"],
            "prospective_sessions": sum(d >= registration["prospective_from"] for d in completed),
            "added_sessions": added,
            "api_requests_this_tick": self.requests,
            "initial_target": initial,
            "monthly_decisions": len(decisions) - 1,
            "next_monthly_open": session_times(next_month)[0].isoformat(),
            "next_poll_after": (observed_at + timedelta(seconds=900)).isoformat(),
            "performance_evaluated": False,
        }
        await self.store.put("etf_shadow", status)
        return status

    async def run(self, stop):
        async with self.store.leader("etf-shadow-0053") as leader:
            while not stop.is_set():
                await leader.execute("SELECT 1")
                try:
                    await self.tick()
                except Exception as exc:
                    previous = await self.store.get("etf_shadow", {})
                    await self.store.put(
                        "etf_shadow",
                        {
                            **previous,
                            "state": "failed",
                            "updated_at": utcnow().isoformat(),
                            "error": type(exc).__name__,
                            "orders_submitted": 0,
                        },
                    )
                    event("etf_shadow_error", error=type(exc).__name__)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=900)
                except TimeoutError:
                    pass


async def main_async():
    settings = Settings()
    store = Store(settings)
    await store.open()
    shadow = Shadow(settings, store)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    try:
        await shadow.run(stop)
    finally:
        await shadow.reader.close()
        await store.close()


def main():
    configure_logging()
    try:
        asyncio.run(main_async())
    except Exception as exc:
        event("etf_shadow_failed", error=type(exc).__name__)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
