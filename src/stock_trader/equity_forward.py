"""ADR-0089~0091 future research data and gated accounting; selected GETs only."""

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
from stock_trader.equity_contract import LAST, ROOT, SYMBOLS, shift
from stock_trader.equity_forward_ledger import compare
from stock_trader.equity_forward_review import coverage, normalized_actions, target_weights
from stock_trader.equity_shadow import session_times, validate_calendar
from stock_trader.equity_shadow import sources as target_sources
from stock_trader.etf_ledger import number
from stock_trader.logging import configure_logging, event
from stock_trader.research_seal import fingerprint, read, seal
from stock_trader.storage import Store

KEY = "equity_forward_0090"
REGISTRY = "equity-forward-registration-0089"
LEADER = "equity-forward-0091"
FORWARD_ROOT = "research/equity-forward-0089"
FIRST = date(2026, 11, 1)
CONTRACT = {
    "decision": "ADR-0089/0090/0091/0092/0093",
    "virtual_initial_capital": "10000",
    "one_way_cost_bps": ["10", "25"],
    "quantity_step": "0.000000001",
    "cash_interest": "0",
    "taxes": "not_modelled",
    "fill_proxy": "raw first regular minute open",
    "mark_proxy": "raw last regular minute close",
    "feed": "sip",
    "available": "next New York calendar date 06:00",
    "actions_lookback_days": 730,
    "actions_date_basis": "cumulative process_date, all types, data_quality all",
    "coverage": "explicit independent evidence review required, never auto-issued",
    "benchmark": "SPY monthly target exposure equal to candidate slot sum",
    "turnover_reserve": "investable_equity*(1-one_way_cost_rate)/(1+one_way_cost_rate)",
    "minimum_return_intervals": 126,
    "minimum_monthly_decisions": 6,
    "first_month": "2026-11-01",
    "last_month": "2027-10-01",
    "max_logical_gets_per_tick": 4,
    "max_transport_attempts_per_tick": 16,
    "poll_seconds": 900,
    "orders_enabled": False,
    "automatic_promotion": False,
    "mode": "research_only",
}


class BudgetYield(Exception):
    pass


class ForwardReader(Alpaca):
    async def request(self, method, path, *, data_api=False, params=None, payload=None):
        allowed = {"/v2/calendar": False, "/v2/stocks/bars": True, "/v1/corporate-actions": True}
        if (
            method != "GET"
            or path not in allowed
            or data_api is not allowed[path]
            or payload is not None
            or PAPER_URL != "https://paper-api.alpaca.markets"
        ):
            raise ValueError("forward research permits selected GET endpoints only")
        return await super().request(method, path, data_api=data_api, params=params)


def sources():
    names = [
        "equity_forward.py",
        "equity_forward_ledger.py",
        "equity_forward_review.py",
        "forward_notices.py",
    ]
    return {
        **target_sources(),
        **{name: hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest() for name in names},
    }


def minute_prices(response, stamp, field):
    if response.get("next_page_token") or set(response.get("bars", {})) != set(SYMBOLS):
        raise ValueError("incomplete regular minute symbols or pagination")
    result = {}
    for symbol, rows in response["bars"].items():
        if len(rows) != 1 or parse_time(rows[0]["t"]) != stamp:
            raise ValueError("wrong regular minute timestamp")
        opening, high, low, close, volume = [number(rows[0][k]) for k in ("o", "h", "l", "c", "v")]
        if low <= 0 or volume <= 0 or not low <= min(opening, close) <= max(opening, close) <= high:
            raise ValueError("invalid regular minute OHLCV")
        result[symbol] = str(number(rows[0][field]))
    return result


class ForwardStudy:
    def __init__(self, settings, store, reader, fence):
        self.settings, self.store, self.reader, self.fence = settings, store, reader, fence
        self.root = settings.data_dir / FORWARD_ROOT
        self.target_root = settings.data_dir / ROOT
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
            "mode": "research_only",
            "automatic_promotion": False,
            "performance_calculated": False,
            "observations": 0,
            "api_requests_this_tick": self.requests,
            **details,
        }
        await self.store.put(KEY, result)
        return result

    async def registration(self):
        target = read(self.target_root / "registration.json")
        if target["source_sha256"] != target_sources():
            raise ValueError("target source contract changed")
        path = self.root / "registration.json"
        database = await self.store.get(REGISTRY)
        if path.exists():
            value = read(path)
        else:
            if database is not None or self.now() >= datetime(2026, 11, 2, 14, 30, tzinfo=UTC):
                raise ValueError("missing or late forward registration")
            day = self.now().astimezone(NEW_YORK).date()
            value = {
                "contract": CONTRACT,
                "source_sha256": sources(),
                "target_registration_sha256": fingerprint(target),
                "registered_at": self.now().isoformat(),
                "process_start": (day - timedelta(days=730)).isoformat(),
                "bootstrap_end": (day - timedelta(days=1)).isoformat(),
                "symbol_asof": target["symbol_asof"],
            }
            await self.fence()
            seal(path, value)
        if (
            value["contract"] != CONTRACT
            or value["source_sha256"] != sources()
            or value["target_registration_sha256"] != fingerprint(target)
            or parse_time(value["registered_at"]) > self.now()
            or (database is not None and database != value)
        ):
            raise ValueError("forward registration changed")
        if database is None:
            await self.fence()
            await self.store.put(REGISTRY, value)
        return value

    async def fetch(self, path, spec):
        await self.fence()
        if path.exists():
            value = read(path)
        else:
            if self.requests >= CONTRACT["max_logical_gets_per_tick"]:
                raise BudgetYield
            self.requests += 1
            value = {"request": spec}
            try:
                value.update(
                    state="received",
                    response=await self.reader.request(
                        "GET", spec["path"], data_api=spec["data_api"], params=spec["params"]
                    ),
                )
            except BrokerError as exc:
                value.update(state="error", http_status=exc.status)
            value["observed_at"] = self.now().isoformat()
            await self.fence()
            seal(path, value)
        if value["request"] != spec or parse_time(value["observed_at"]) > self.now():
            raise ValueError("cached research request changed")
        if value["state"] != "received":
            raise ValueError("cached research HTTP error")
        return value

    async def actions(self, name, end, registration):
        spec = {
            "path": "/v1/corporate-actions",
            "data_api": True,
            "params": {
                "symbols": ",".join(SYMBOLS),
                "start": registration["process_start"],
                "end": end,
                "data_quality": "all",
                "sort": "asc",
                "limit": 1000,
            },
        }
        value = await self.fetch(self.root / "actions" / f"{name}.json", spec)
        response = value["response"]
        if response.get("next_page_token") or not isinstance(response.get("corporate_actions"), dict):
            raise ValueError("incomplete cumulative corporate actions")
        if any(not isinstance(rows, list) for rows in response["corporate_actions"].values()):
            raise ValueError("invalid corporate action schema")
        return value

    async def month(self, month):
        path = self.root / "calendar" / f"{month:%Y-%m}.json"
        if path.exists():
            saved = read(path)
            if saved["month"] != month.isoformat():
                raise ValueError("forward calendar changed")
            if saved.get("input_sha256"):
                original = self.target_root / "calendar" / path.name
                if (
                    original.is_symlink()
                    or hashlib.sha256(original.read_bytes()).hexdigest() != saved["input_sha256"]
                ):
                    raise ValueError("target calendar changed")
            return validate_calendar(saved["sessions"], month)
        original = self.target_root / "calendar" / path.name
        if original.exists():
            rows = validate_calendar(read(original)["sessions"], month)
            saved = {
                "month": month.isoformat(),
                "sessions": rows,
                "input_sha256": hashlib.sha256(original.read_bytes()).hexdigest(),
            }
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
            saved = {"month": month.isoformat(), "sessions": rows}
        await self.fence()
        seal(path, saved)
        return rows

    async def session(self, row, registration):
        day = row["date"]
        opening, closing = session_times(row)
        prices, inputs, times = {}, {}, []
        for name, stamp, field in (("open", opening, "o"), ("close", closing - timedelta(minutes=1), "c")):
            spec = {
                "path": "/v2/stocks/bars",
                "data_api": True,
                "params": {
                    "symbols": ",".join(SYMBOLS),
                    "timeframe": "1Min",
                    "adjustment": "raw",
                    "feed": "sip",
                    "start": stamp.isoformat(),
                    "end": (stamp + timedelta(minutes=1) - timedelta(microseconds=1)).isoformat(),
                    "asof": registration["symbol_asof"],
                    "currency": "USD",
                    "limit": 10000,
                },
            }
            value = await self.fetch(self.root / "prices" / day / f"{name}.json", spec)
            prices[name] = minute_prices(value["response"], stamp, field)
            inputs[name] = fingerprint(value)
            times.append(parse_time(value["observed_at"]))
        actions = await self.actions(day, day, registration)
        inputs["actions"] = fingerprint(actions)
        times.append(parse_time(actions["observed_at"]))
        manifest = {
            "session": day,
            "input_sha256": inputs,
            "observed_at": max(times).isoformat(),
            "registration_sha256": fingerprint(registration),
            "open": prices["open"],
            "close": prices["close"],
        }
        path = self.root / "sessions" / f"{day}.json"
        await self.fence()
        if path.exists():
            if read(path) != manifest:
                raise ValueError("session inputs changed")
        else:
            seal(path, manifest)
        return manifest, actions["response"]

    async def accounting(self, collected, registration):
        previous, previous_day, parent, observations, monthly = None, "2026-11-01", None, 0, 0
        blocked = None
        for row, first, manifest, response in collected:
            day = row["date"]
            path = self.root / "ledgers" / f"{day}.json"
            if blocked is not None:
                if path.exists():
                    raise ValueError("ledger exists after coverage gap")
                continue
            if not (self.root / "coverage" / f"{day}.json").exists():
                if path.exists():
                    raise ValueError("ledger coverage removed")
                blocked = day
                continue
            resolutions = read(self.root / "coverage" / f"{day}.json").get("resolutions", {})
            actions = normalized_actions(response, previous_day, day, resolutions)
            if fingerprint(
                normalized_actions(collected[-1][3], previous_day, day, resolutions)
            ) != fingerprint(actions):
                raise ValueError("later corporate action vintage changes prior economics")
            reviewed = coverage(self.root, day, manifest, fingerprint(registration), actions, self.now())
            target, target_sha = None, None
            if first:
                source = self.target_root / "targets" / f"{day[:7]}.json"
                value = read(source)
                if parse_time(value["intended_open"]) != session_times(row)[0]:
                    raise ValueError("target opening does not match regular calendar")
                target = target_weights(value, registration["target_registration_sha256"], day)
                for name, digest in value["input_file_sha256"].items():
                    source = self.target_root / "cycles" / day[:7] / name
                    if (
                        source.name != name
                        or source.is_symlink()
                        or hashlib.sha256(source.read_bytes()).hexdigest() != digest
                    ):
                        raise ValueError("target formation input changed")
                target_sha = fingerprint(value)
                monthly += 1
            ledgers = compare(previous, day, actions, manifest["open"], manifest["close"], target)
            record = {
                "session": day,
                "registration_sha256": fingerprint(registration),
                "parent_sha256": parent,
                "input_sha256": manifest["input_sha256"],
                "coverage_sha256": fingerprint(reviewed),
                "target_sha256": target_sha,
                "ledgers": ledgers,
                "orders_submitted": 0,
            }
            await self.fence()
            if path.exists():
                if read(path) != record:
                    raise ValueError("previous economic record changed")
            else:
                seal(path, record)
            previous, previous_day, parent = ledgers, day, fingerprint(record)
            observations += 1
        comparisons = {}
        if previous:
            for bps in ("10", "25"):
                legs = {}
                for name in ("candidate", "spy"):
                    ledger = previous[f"{name}_{bps}bps"]
                    legs[name] = {
                        "net_return_since_virtual_capital": str(number(ledger["equity"]) / 10000 - 1),
                        "cash": ledger["cash"],
                        "unpaid_receivables": str(
                            sum((number(e["amount"]) for e in ledger["receivables"].values()), number(0))
                        ),
                        "costs_paid": str(
                            sum(
                                (number(e["cost"]) for e in ledger["journal"] if e["type"] == "virtual_fill"),
                                number(0),
                            )
                        ),
                    }
                legs["candidate_minus_spy_return"] = str(
                    number(legs["candidate"]["net_return_since_virtual_capital"])
                    - number(legs["spy"]["net_return_since_virtual_capital"])
                )
                comparisons[bps + "bps"] = legs
        return {
            "observations": observations,
            "monthly_decisions": monthly,
            "performance_calculated": observations > 0,
            "review_ready": observations >= 127 and monthly >= 6 and blocked is None,
            "coverage_waiting_session": blocked,
            "last_equities": {key: ledger["equity"] for key, ledger in (previous or {}).items()},
            "conditional_economic_review_only": True,
            "comparison": comparisons,
        }

    async def tick(self, now=None):
        self.fixed_now = parse_time(now) if now is not None else None
        self.requests = 0
        registration = await self.registration()
        bootstrap = await self.actions("bootstrap", registration["bootstrap_end"], registration)
        counts = {kind: len(rows) for kind, rows in bootstrap["response"]["corporate_actions"].items()}
        base = {"registration_sha256": fingerprint(registration), "bootstrap_action_counts": counts}
        if self.now().astimezone(NEW_YORK).date() < date(2026, 11, 2):
            return await self.status("waiting_start", next_session="2026-11-02", **base)
        month, collected = FIRST, []
        try:
            while month <= min(self.now().astimezone(NEW_YORK).date().replace(day=1), LAST):
                rows = await self.month(month)
                for index, row in enumerate(rows):
                    day = date.fromisoformat(row["date"])
                    available = datetime.combine(day + timedelta(days=1), time(6), NEW_YORK).astimezone(UTC)
                    if self.now() < available:
                        continue
                    manifest, actions = await self.session(row, registration)
                    collected.append((row, index == 0, manifest, actions))
                month = shift(month, 1)
            accounting = await self.accounting(collected, registration)
            return await self.status(
                "waiting_coverage" if accounting["coverage_waiting_session"] else "observing",
                collected_sessions=len(collected),
                **accounting,
                **base,
            )
        except BudgetYield:
            return await self.status("catching_up", collected_sessions=len(collected), **base)

    async def step(self, now=None):
        self.fixed_now = parse_time(now) if now is not None else None
        self.requests = 0
        if not self.settings.credentials_present:
            return await self.status("waiting_keys")
        try:
            return await self.tick(now)
        except (ValueError, OSError, KeyError, TypeError, ArithmeticError) as exc:
            return await self.status(
                "review_required", error=type(exc).__name__, reason="forward_validation_failed"
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
        and value.get("automatic_promotion") is False
        and value.get("state") in {"waiting_start", "observing", "waiting_coverage", "catching_up"}
    )
    print(json.dumps({"healthy": healthy, "state": value.get("state", "missing"), "broker_requests": 0}))
    return healthy


async def main_async(args):
    settings = Settings(_secrets_dir="/run/secrets")
    if args.health:
        return await health(settings)
    store, reader, stop = Store(settings), ForwardReader(settings), asyncio.Event()
    await store.open()
    for sig in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    try:
        async with store.leader(LEADER) as leader:

            async def fence():
                await leader.execute("SELECT 1")

            worker = ForwardStudy(settings, store, reader, fence)
            while not stop.is_set():
                result = await worker.step()
                from stock_trader.forward_notices import publish

                await publish(store, result, fence)
                if args.once:
                    print(json.dumps(result))
                    return result["state"] not in {"review_required", "waiting_keys"}
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
    configure_logging()
    try:
        if not asyncio.run(main_async(parser.parse_args())):
            raise SystemExit(1)
    except Exception as exc:
        event("equity_forward_error", error=type(exc).__name__)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
