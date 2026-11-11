"""ADR-0096: one-shot, date-versioned forecast calendar. No DB credentials needed."""

import asyncio
import fcntl
import json

from stock_trader.broker import PAPER_URL, Alpaca, BrokerError
from stock_trader.clock import NEW_YORK, parse_time, utcnow
from stock_trader.config import Settings
from stock_trader.research_seal import read, seal
from stock_trader.work_progress import CALENDAR_CONTRACT, CALENDAR_REQUEST, CALENDAR_ROOT, validate_calendar


class CalendarReader(Alpaca):
    async def request(self, method, path, *, data_api=False, params=None, payload=None):
        if (
            method != "GET"
            or path != "/v2/calendar"
            or data_api is not False
            or payload is not None
            or PAPER_URL != "https://paper-api.alpaca.markets"
        ):
            raise ValueError("forecast reader allows calendar GET only")
        return await super().request(method, path, data_api=False, params=params)


async def collect(settings, reader, now=None):
    fixed_now = parse_time(now) if now else None
    now = fixed_now or utcnow()
    directory = settings.data_dir / CALENDAR_ROOT
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / ".collector.lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        path = directory / "snapshots" / (now.astimezone(NEW_YORK).date().isoformat() + ".json")
        requests = 0
        if path.exists():
            value = read(path)
        else:
            if not settings.credentials_present:
                raise ValueError("calendar credentials missing")
            requests = 1
            value = {"contract": CALENDAR_CONTRACT, "request": CALENDAR_REQUEST}
            try:
                response = await reader.request("GET", "/v2/calendar", params=CALENDAR_REQUEST["params"])
                value.update(state="received", response=response)
            except BrokerError as exc:
                value.update(state="error", http_status=exc.status)
            value["observed_at"] = (fixed_now or utcnow()).isoformat()
            seal(path, value)
        if (
            value["contract"] != CALENDAR_CONTRACT
            or value["request"] != CALENDAR_REQUEST
            or parse_time(value["observed_at"]) > (fixed_now or utcnow())
        ):
            raise ValueError("calendar cache contract changed")
        if value["state"] != "received":
            raise ValueError("cached calendar HTTP error")
        rows = validate_calendar(value["response"])
        return {
            "state": "collected",
            "logical_gets": requests,
            "sessions": len(rows),
            "cache_file": str(path),
            "orders_submitted": 0,
        }


async def main_async():
    settings = Settings(_env_file=None, _secrets_dir="/run/secrets")
    reader = CalendarReader(settings)
    try:
        print(json.dumps(await collect(settings, reader)))
    finally:
        await reader.close()


if __name__ == "__main__":
    try:
        asyncio.run(main_async())
    except Exception as exc:
        print(json.dumps({"state": "review_required", "error": type(exc).__name__, "orders_submitted": 0}))
        raise SystemExit(1) from None
