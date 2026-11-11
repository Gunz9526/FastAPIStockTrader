"""ADR-0086: bounded GET-only monthly target research, isolated from trading."""

import argparse
import asyncio
import calendar
import hashlib
import json
import signal
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

from stock_trader.broker import PAPER_URL, Alpaca, BrokerError
from stock_trader.clock import NEW_YORK, parse_time, utcnow
from stock_trader.config import Settings
from stock_trader.equity_contract import (
    CONTRACT,
    EQUITIES,
    FIRST,
    KEY,
    LAST,
    LEADER,
    REGISTRY,
    ROOT,
    SYMBOLS,
    rank_targets,
    shift,
)
from stock_trader.etf_ledger import number
from stock_trader.logging import configure_logging, event
from stock_trader.research_seal import fingerprint, read, seal
from stock_trader.storage import Store


class BudgetYield(Exception):
    pass


class ResearchBlocked(ValueError):
    pass


class ReadOnlyAlpaca(Alpaca):
    async def request(self, method, path, *, data_api=False, params=None, payload=None):
        allowed = {"/v2/calendar": False, "/v2/assets": False, "/v2/stocks/bars": True}
        if method != "GET" or path not in allowed or allowed[path] is not data_api or payload is not None:
            raise ValueError("research client permits selected GET endpoints only")
        if PAPER_URL != "https://paper-api.alpaca.markets":
            raise ValueError("Paper endpoint required")
        return await super().request(method, path, data_api=data_api, params=params)


def sources():
    root = Path(__file__).parent
    names = [
        "equity_contract.py",
        "equity_notices.py",
        "equity_shadow.py",
        "research_seal.py",
        "broker.py",
        "api_budget.py",
        "clock.py",
        "config.py",
        "etf_ledger.py",
    ]
    return {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in names}


def session_times(row):
    day = date.fromisoformat(row["date"])
    opening = datetime.combine(day, time.fromisoformat(row["open"]), NEW_YORK)
    closing = datetime.combine(day, time.fromisoformat(row["close"]), NEW_YORK)
    if not opening < closing or not 0 < (closing - opening).total_seconds() <= 86400:
        raise ValueError("invalid research session hours")
    return opening.astimezone(UTC), closing.astimezone(UTC)


def validate_calendar(rows, month):
    days = [row["date"] for row in rows]
    if not rows or days != sorted(set(days)) or any(day[:7] != f"{month:%Y-%m}" for day in days):
        raise ValueError("invalid research calendar")
    for row in rows:
        session_times(row)
    return rows


def validate_bars(response, day):
    if response.get("next_page_token") or set(response.get("bars", {})) != set(SYMBOLS):
        raise ResearchBlocked("incomplete_anchor_symbols_or_page")
    result = {}
    for symbol, rows in response["bars"].items():
        if len(rows) != 1:
            raise ResearchBlocked("missing_or_duplicate_anchor_bar")
        row = rows[0]
        stamp = parse_time(row["t"]).astimezone(NEW_YORK)
        if stamp.date() != day or stamp.time() != time.min:
            raise ResearchBlocked("anchor_timestamp_mismatch")
        opening, high, low, close, volume = [number(row[k]) for k in ("o", "h", "l", "c", "v")]
        if low <= 0 or volume <= 0 or not low <= min(opening, close) <= max(opening, close) <= high:
            raise ResearchBlocked("invalid_anchor_ohlcv")
        result[symbol] = str(close)
    return result


def asset_class(row):
    if "class" not in row or row.get("asset_class", row["class"]) != row["class"]:
        raise ResearchBlocked("asset_class_schema_mismatch")
    return row["class"]


def cohort_from_assets(rows):
    if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
        raise ValueError("invalid asset snapshot")
    result = []
    for symbol in EQUITIES:
        matches = [
            r
            for r in rows
            if r.get("symbol") == symbol and r.get("status") == "active" and asset_class(r) == "us_equity"
        ]
        if len(matches) != 1 or type(matches[0].get("tradable")) is not bool or not matches[0]["id"]:
            raise ResearchBlocked("ambiguous_cohort_identity")
        result.append(
            {
                **{k: matches[0][k] for k in ("id", "symbol", "status", "tradable", "exchange")},
                "asset_class": asset_class(matches[0]),
            }
        )
    if len({r["id"] for r in result}) != len(EQUITIES):
        raise ResearchBlocked("duplicate_cohort_identity")
    return result


def eligibility(rows, cohort):
    if not isinstance(rows, list) or any(not isinstance(r, dict) or not r.get("id") for r in rows):
        raise ResearchBlocked("invalid_current_assets")
    by_id = {r["id"]: r for r in rows}
    if len(by_id) != len(rows):
        raise ResearchBlocked("duplicate_current_identity")
    result, details = {}, {}
    for old in cohort:
        current = by_id.get(old["id"])
        if not current or current.get("symbol") != old["symbol"] or asset_class(current) != "us_equity":
            raise ResearchBlocked("cohort_identity_or_symbol_changed")
        if type(current.get("tradable")) is not bool or current.get("status") not in {"active", "inactive"}:
            raise ResearchBlocked("invalid_current_eligibility")
        symbol = old["symbol"]
        result[symbol] = current["status"] == "active" and current["tradable"]
        details[symbol] = {
            **{k: current[k] for k in ("id", "symbol", "status", "tradable")},
            "asset_class": asset_class(current),
        }
    return result, details


class EquityShadow:
    def __init__(self, settings, store, reader, fence):
        self.settings, self.store, self.reader, self.fence = settings, store, reader, fence
        self.root = settings.data_dir / ROOT
        self.requests = 0
        self.fixed_now = None

    def now(self):
        return self.fixed_now or utcnow()

    async def status(self, state, **details):
        await self.fence()
        result = {
            "state": state,
            "heartbeat": self.now().isoformat(),
            "contract_sha256": fingerprint(CONTRACT),
            "orders_submitted": 0,
            "performance_calculated": False,
            "mode": "research_only",
            "api_requests_this_tick": self.requests,
            **details,
        }
        await self.store.put(KEY, result)
        return result

    async def fetch(self, path, spec):
        await self.fence()
        if path.exists():
            value = read(path)
            if value["request"] != spec or parse_time(value["observed_at"]) > self.now():
                raise ResearchBlocked("cached_request_or_time_changed")
        else:
            if self.requests >= CONTRACT["max_logical_gets_per_tick"]:
                raise BudgetYield
            self.requests += 1
            try:
                response = await self.reader.request(
                    "GET", spec["path"], data_api=spec["data_api"], params=spec["params"]
                )
                value = {
                    "request": spec,
                    "state": "received",
                    "response": response,
                    "observed_at": self.now().isoformat(),
                }
            except BrokerError as exc:
                value = {
                    "request": spec,
                    "state": "error",
                    "http_status": exc.status,
                    "observed_at": self.now().isoformat(),
                }
            await self.fence()
            seal(path, value)
        if value["state"] != "received":
            raise ResearchBlocked("cached_http_error")
        return value

    async def month(self, month):
        path = self.root / "calendar" / f"{month:%Y-%m}.json"
        if path.exists():
            saved = read(path)
            if saved["month"] != month.isoformat():
                raise ResearchBlocked("calendar_key_changed")
            if saved.get("shared_source"):
                shared = self.settings.data_dir / "etf-shadow/calendar" / path.name
                if (
                    shared.is_symlink()
                    or hashlib.sha256(shared.read_bytes()).hexdigest() != saved["shared_source"]
                ):
                    raise ResearchBlocked("shared_calendar_changed")
            return validate_calendar(saved["sessions"], month)
        shared = self.settings.data_dir / "etf-shadow/calendar" / path.name
        if shared.exists():
            original = read(shared)
            if original["month"] != month.isoformat():
                raise ResearchBlocked("shared_calendar_key_changed")
            rows = validate_calendar(original["sessions"], month)
            await self.fence()
            seal(
                path,
                {
                    "month": month.isoformat(),
                    "sessions": rows,
                    "shared_source": hashlib.sha256(shared.read_bytes()).hexdigest(),
                },
            )
        else:
            spec = {
                "path": "/v2/calendar",
                "data_api": False,
                "params": {
                    "start": month.isoformat(),
                    "end": month.replace(day=calendar.monthrange(month.year, month.month)[1]).isoformat(),
                },
            }
            value = await self.fetch(self.root / "responses/calendar" / path.name, spec)
            rows = validate_calendar(value["response"], month)
            await self.fence()
            seal(path, {"month": month.isoformat(), "sessions": rows})
        return rows

    async def registration(self):
        source = self.settings.data_dir / "research/equity-access-0080/assets.json"
        original = read(source)
        if (
            original["state"] != "received"
            or original["request"]["path"] != "/v2/assets"
            or original["request"]["params"] != {"asset_class": "us_equity"}
        ):
            raise ResearchBlocked("cohort_source_contract_changed")
        expected = {
            "contract": CONTRACT,
            "source_sha256": sources(),
            "assets_source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "assets_observed_at": original["observed_at"],
            "cohort": cohort_from_assets(original["response"]),
        }
        path = self.root / "registration.json"
        database = await self.store.get(REGISTRY)
        if path.exists():
            registration = read(path)
            if any(registration.get(k) != v for k, v in expected.items()):
                raise ResearchBlocked("registration_inputs_changed")
            if parse_time(registration["registered_at"]) > self.now():
                raise ResearchBlocked("registration_from_future")
        else:
            if database:
                raise ResearchBlocked("registration_file_lost")
            observed = parse_time(original["observed_at"])
            if not timedelta(0) <= self.now() - observed <= timedelta(days=7):
                raise ResearchBlocked("initial_cohort_snapshot_stale")
            if self.now().astimezone(NEW_YORK).date() >= FIRST:
                raise ResearchBlocked("late_initial_registration")
            registration = {
                **expected,
                "registered_at": self.now().isoformat(),
                "symbol_asof": self.now().astimezone(NEW_YORK).date().isoformat(),
            }
            await self.fence()
            seal(path, registration)
        # The precise first formation boundary uses the official calendar below.
        prior = await self.month(shift(FIRST, -1))
        formed_on = date.fromisoformat(prior[-1]["date"]) + timedelta(days=1)
        formation = datetime.combine(formed_on, time(6), NEW_YORK).astimezone(UTC)
        if parse_time(registration["registered_at"]) >= formation:
            raise ResearchBlocked("late_initial_registration")
        if database is not None and database != registration:
            raise ResearchBlocked("database_registration_changed")
        if database is None:
            await self.fence()
            await self.store.put(REGISTRY, registration)
        return registration

    async def anchor(self, cycle, month, registration):
        rows = await self.month(month)
        day = date.fromisoformat(rows[-1]["date"])
        # The day is historical signal input, never a newly evaluated return period.
        available = datetime.combine(day + timedelta(days=1), time(6), NEW_YORK).astimezone(UTC)
        if self.now() < available:
            return None
        opening = datetime.combine(day, time.min, NEW_YORK)
        ending = datetime.combine(day + timedelta(days=1), time.min, NEW_YORK) - timedelta(microseconds=1)
        result = {}
        for adjustment in CONTRACT["anchor_adjustments"]:
            spec = {
                "path": "/v2/stocks/bars",
                "data_api": True,
                "params": {
                    "symbols": ",".join(SYMBOLS),
                    "start": opening.astimezone(UTC).isoformat(),
                    "end": ending.astimezone(UTC).isoformat(),
                    "timeframe": "1Day",
                    "adjustment": adjustment,
                    "feed": "sip",
                    "asof": registration["symbol_asof"],
                    "currency": "USD",
                    "limit": 10000,
                },
            }
            path = self.root / "cycles" / cycle / f"{day}-{adjustment}.json"
            value = await self.fetch(path, spec)
            result[adjustment] = {
                "closes": validate_bars(value["response"], day),
                "payload_sha256": fingerprint(value),
            }
        return {"date": day.isoformat(), **result}

    async def tick(self, now=None):
        self.fixed_now = parse_time(now) if now is not None else None
        self.requests = 0
        registration = await self.registration()
        registration_sha = fingerprint(registration)
        horizon = min(shift(self.now().astimezone(NEW_YORK).date().replace(day=1), 1), LAST)
        cycle_month, records = FIRST, []
        try:
            while cycle_month <= horizon:
                cycle = f"{cycle_month:%Y-%m}"
                path = self.root / "targets" / f"{cycle}.json"
                if path.exists():
                    saved = read(path)
                    if saved["registration_sha256"] != registration_sha or saved["cycle"] != cycle:
                        raise ResearchBlocked("target_registration_changed")
                    recorded = parse_time(saved["recorded_at"])
                    if (
                        recorded > self.now()
                        or saved["state"] not in {"recorded", "missed"}
                        or saved["orders_submitted"] != 0
                        or saved["performance_calculated"] is not False
                    ):
                        raise ResearchBlocked("invalid_recorded_target")
                    if saved["state"] == "recorded" and not parse_time(
                        saved["formation_start"]
                    ) <= recorded < parse_time(saved["intended_open"]):
                        raise ResearchBlocked("late_recorded_target")
                    for name, digest in saved.get("input_file_sha256", {}).items():
                        source = self.root / "cycles" / cycle / name
                        if (
                            source.is_symlink()
                            or source.name != name
                            or hashlib.sha256(source.read_bytes()).hexdigest() != digest
                        ):
                            raise ResearchBlocked("target_input_changed")
                    records.append(saved)
                    cycle_month = shift(cycle_month, 1)
                    continue
                following = await self.month(cycle_month)
                prior = await self.month(shift(cycle_month, -1))
                opening, _ = session_times(following[0])
                formation = datetime.combine(
                    date.fromisoformat(prior[-1]["date"]) + timedelta(days=1), time(6), NEW_YORK
                ).astimezone(UTC)
                base = {
                    "cycle": cycle,
                    "execution_session": following[0]["date"],
                    "intended_open": opening.isoformat(),
                    "formation_start": formation.isoformat(),
                    "registration_sha256": registration_sha,
                    "orders_submitted": 0,
                    "performance_calculated": False,
                }
                if self.now() >= opening:
                    await self.fence()
                    saved = {
                        **base,
                        "state": "missed",
                        "reason": "no_timely_target",
                        "recorded_at": self.now().isoformat(),
                    }
                    seal(path, saved)
                    records.append(saved)
                    cycle_month = shift(cycle_month, 1)
                    continue
                earlier = await self.anchor(cycle, shift(cycle_month, -13), registration)
                later = await self.anchor(cycle, shift(cycle_month, -2), registration)
                if earlier is None or later is None:
                    return await self.status(
                        "waiting_signal_dates",
                        next_cycle=cycle,
                        next_open=opening.isoformat(),
                        targets_recorded=sum(r["state"] == "recorded" for r in records),
                        missed_targets=sum(r["state"] == "missed" for r in records),
                    )
                if self.now() < formation:
                    return await self.status(
                        "waiting_formation",
                        next_cycle=cycle,
                        next_open=opening.isoformat(),
                        formation_start=formation.isoformat(),
                        warmup_complete=True,
                        anchor_dates=[earlier["date"], later["date"]],
                        targets_recorded=sum(r["state"] == "recorded" for r in records),
                        missed_targets=sum(r["state"] == "missed" for r in records),
                        registration_sha256=registration_sha,
                    )
                spec = {"path": "/v2/assets", "data_api": False, "params": {"asset_class": "us_equity"}}
                assets = await self.fetch(self.root / "cycles" / cycle / "assets.json", spec)
                if parse_time(assets["observed_at"]) < formation:
                    raise ResearchBlocked("eligibility_snapshot_before_formation")
                eligible, details = eligibility(assets["response"], registration["cohort"])
                if self.now() >= opening:
                    # The next pass records a missed target without inventing a fill.
                    continue
                inputs = {
                    p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in sorted((self.root / "cycles" / cycle).glob("*.json"))
                }
                saved = {
                    **base,
                    "state": "recorded",
                    "recorded_at": self.now().isoformat(),
                    "anchor_dates": [earlier["date"], later["date"]],
                    "eligibility": details,
                    "input_file_sha256": inputs,
                    **rank_targets(earlier["split"]["closes"], later["split"]["closes"], eligible),
                }
                await self.fence()
                if self.now() >= opening:
                    continue
                seal(path, saved)
                records.append(saved)
                cycle_month = shift(cycle_month, 1)
            return await self.status(
                "observing",
                registration_sha256=registration_sha,
                targets_recorded=sum(r["state"] == "recorded" for r in records),
                missed_targets=sum(r["state"] == "missed" for r in records),
                latest_target=records[-1] if records else None,
            )
        except BudgetYield:
            return await self.status("catching_up", registration_sha256=registration_sha)

    async def step(self, now=None):
        if not self.settings.credentials_present:
            self.fixed_now = parse_time(now) if now is not None else None
            self.requests = 0
            return await self.status("waiting_keys")
        try:
            return await self.tick(now)
        except (ResearchBlocked, ValueError, OSError, KeyError, TypeError) as exc:
            return await self.status(
                "review_required",
                error=type(exc).__name__,
                reason=str(exc) if isinstance(exc, ResearchBlocked) else "research_validation_failed",
            )


async def health(settings):
    from psycopg import AsyncConnection

    async with await AsyncConnection.connect(**settings.db_kwargs()) as conn:
        row = await (await conn.execute("SELECT value FROM kv WHERE key=%s", (KEY,))).fetchone()
    value = row[0] if row else {}
    age = (utcnow() - parse_time(value["heartbeat"])).total_seconds() if value else float("inf")
    healthy = (
        0 <= age <= 1800
        and value.get("contract_sha256") == fingerprint(CONTRACT)
        and value.get("orders_submitted") == 0
        and value.get("state") in {"waiting_formation", "waiting_signal_dates", "catching_up", "observing"}
    )
    print(json.dumps({"healthy": healthy, "state": value.get("state", "missing"), "broker_requests": 0}))
    return healthy


async def main_async(args):
    settings = Settings(_secrets_dir="/run/secrets")
    if args.health:
        return await health(settings)
    store = Store(settings)
    await store.open()
    reader = ReadOnlyAlpaca(settings)
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    try:
        async with store.leader(LEADER) as leader:

            async def fence():
                await leader.execute("SELECT 1")

            worker = EquityShadow(settings, store, reader, fence)
            while not stop.is_set():
                result = await worker.step()
                from stock_trader.equity_notices import publish

                await publish(store, result, fence)
                if args.once:
                    print(json.dumps(result))
                    return result["state"] != "review_required"
                try:
                    await asyncio.wait_for(stop.wait(), timeout=CONTRACT["poll_seconds"])
                except TimeoutError:
                    pass
    finally:
        await reader.close()
        await store.close()
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--health", action="store_true")
    args = parser.parse_args()
    configure_logging()
    try:
        if not asyncio.run(main_async(args)):
            raise SystemExit(1)
    except Exception as exc:
        event("equity_research_error", error=type(exc).__name__)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
