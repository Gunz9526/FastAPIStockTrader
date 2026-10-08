"""Read-only market observations; deliberately independent of models and order submission."""

import asyncio
import json
import math
import re
import signal
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from stock_trader.broker import Alpaca
from stock_trader.clock import parse_time, utcnow
from stock_trader.config import Settings
from stock_trader.data import normalized_bars
from stock_trader.logging import configure_logging, event
from stock_trader.market_quality import audit_completed_sessions
from stock_trader.storage import Store


def next_poll_delay(elapsed):
    # A cycle taking a full interval has missed its slot; don't issue a burst.
    return 60 - elapsed if elapsed < 60 else 60


def quote_observation(symbol, quote, now):
    if not quote:
        return {"symbol": symbol, "state": "missing"}
    try:
        bid, ask = float(quote["bp"]), float(quote["ap"])
        age = (now - parse_time(quote["t"])).total_seconds()
        if not all(math.isfinite(v) for v in (bid, ask, age)) or bid <= 0 or ask < bid:
            raise ValueError("invalid quote")
        state = "future" if age < 0 else "stale" if age > 30 else "fresh"
        return {
            "symbol": symbol,
            "state": state,
            "quote_time": parse_time(quote["t"]).isoformat(),
            "bid": bid,
            "ask": ask,
            "age_seconds": age,
            "spread_bps": (ask - bid) / ((ask + bid) / 2) * 10000,
        }
    except KeyError, ValueError, TypeError:
        return {"symbol": symbol, "state": "invalid"}


def compare_bars(symbol, minute_rows, native_rows, now, opening, closing, seen, cold_start):
    minutes = normalized_bars(symbol, minute_rows)
    native = normalized_bars(symbol, native_rows)
    if native.empty:
        return None
    eligible = (
        native["timestamp"].ge(pd.Timestamp(opening))
        & native["timestamp"].lt(pd.Timestamp(closing))
        & (native["timestamp"] + pd.Timedelta(minutes=5, seconds=35)).le(pd.Timestamp(now))
    )
    native = native.loc[eligible]
    if native.empty:
        return None
    latest = native.sort_values("timestamp").iloc[-1]
    start = latest["timestamp"]
    end = start + pd.Timedelta(minutes=5)
    part = minutes.loc[minutes["timestamp"].ge(start) & minutes["timestamp"].lt(end)].sort_values("timestamp")
    identity = symbol + ":" + start.isoformat()
    if identity not in seen:
        seen[identity] = {"at": now.isoformat(), "cold_start": cold_start}
    first = seen[identity]
    deltas = None
    if not part.empty:
        aggregate = {
            "open": part["open"].iloc[0],
            "high": part["high"].max(),
            "low": part["low"].min(),
            "close": part["close"].iloc[-1],
            "volume": part["volume"].sum(),
        }
        deltas = {field: float(aggregate[field] - latest[field]) for field in aggregate}
    return {
        "symbol": symbol,
        "bar_start": start.isoformat(),
        "age_after_end_seconds": (now - end.to_pydatetime()).total_seconds(),
        "first_seen_delay_seconds": (parse_time(first["at"]) - end.to_pydatetime()).total_seconds(),
        "cold_start": first["cold_start"],
        "observed_minutes": len(part),
        "native_close": float(latest["close"]),
        "aggregate_minus_native": deltas,
    }


def append_observation(directory, day, snapshot):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / f"market-{day}.jsonl").open("a") as stream:
        stream.write(json.dumps(snapshot, ensure_ascii=False, allow_nan=False) + "\n")
    cutoff = datetime.fromisoformat(day).date() - timedelta(days=30)
    for path in directory.glob("market-*.jsonl"):
        match = re.fullmatch(r"market-(\d{4}-\d{2}-\d{2})\.jsonl", path.name)
        if match and not path.is_symlink():
            try:
                date = datetime.fromisoformat(match[1]).date()
            except ValueError:
                continue
            if date < cutoff:
                path.unlink()


class Observer:
    def __init__(self, settings, store, broker=None):
        self.settings = settings
        self.store = store
        self.broker = broker or Alpaca(settings)
        self.sessions = {}

    async def tick(self, now=None):
        fixed_time = now is not None
        now = now or utcnow()
        day = now.astimezone(ZoneInfo("America/New_York")).date().isoformat()
        previous = await self.store.get("market_observation", {})
        count = previous.get("samples", 0) if previous.get("day") == day else 0
        status = {
            "day": day,
            "updated_at": now.isoformat(),
            "samples": count,
            "feed": "iex",
            "orders_submitted": 0,
        }
        if not self.settings.credentials_present:
            await self.store.put("market_observation", {**status, "state": "waiting_alpaca_keys"})
            return
        clock = await self.broker.clock()
        if not clock["is_open"]:
            await self.store.put(
                "market_observation", {**status, "state": "market_closed", "next_open": clock["next_open"]}
            )
            try:
                quality = await asyncio.to_thread(audit_completed_sessions, self.settings, day)
            except Exception as exc:
                quality = {"state": "failed", "updated_at": now.isoformat(), "error": type(exc).__name__}
            await self.store.put("market_quality", quality)
            return
        if day not in self.sessions:
            calendar = await self.broker.calendar(day, day)
            if len(calendar) != 1 or calendar[0]["date"] != day:
                raise ValueError("calendar mismatch")
            session = calendar[0]
            self.sessions = {
                day: (
                    datetime.fromisoformat(day + "T" + session["open"])
                    .replace(tzinfo=ZoneInfo("America/New_York"))
                    .astimezone(UTC),
                    datetime.fromisoformat(day + "T" + session["close"])
                    .replace(tzinfo=ZoneInfo("America/New_York"))
                    .astimezone(UTC),
                )
            }
        opening, closing = self.sessions[day]
        if not opening <= now < closing:
            await self.store.put("market_observation", {**status, "state": "outside_calendar_session"})
            return
        quotes = await self.broker.quotes(self.settings.symbols)
        quotes_received_at = now if fixed_time else utcnow()
        series = {}
        for timeframe in ("1Min", "5Min"):
            rows = {}
            async for batch in self.broker.bars(
                self.settings.symbols, (now - timedelta(minutes=20)).isoformat(), now.isoformat(), timeframe
            ):
                for symbol, values in batch.items():
                    if symbol in self.settings.symbols:
                        rows.setdefault(symbol, []).extend(values)
            series[timeframe] = rows
        bars_received_at = now if fixed_time else utcnow()
        saved_seen = await self.store.get("market_observation_seen", {})
        seen = saved_seen.get("bars", {}) if saved_seen.get("day") == day else {}
        observations = [
            quote_observation(s, quotes.get(s), quotes_received_at) for s in self.settings.symbols
        ]
        bars = []
        for symbol in self.settings.symbols:
            item = compare_bars(
                symbol,
                series["1Min"].get(symbol, []),
                series["5Min"].get(symbol, []),
                bars_received_at,
                opening,
                closing,
                seen,
                count == 0,
            )
            if item:
                bars.append(item)
        snapshot = {
            **status,
            "schema_version": 2,
            "poll_started_at": now.isoformat(),
            "quotes_received_at": quotes_received_at.isoformat(),
            "bars_received_at": bars_received_at.isoformat(),
            "updated_at": (now if fixed_time else utcnow()).isoformat(),
            "state": "observing",
            "samples": count + 1,
            "quotes": observations,
            "bars": bars,
            "fresh_quotes": sum(q["state"] == "fresh" for q in observations),
            "confirmed_symbols": len(bars),
            "incomplete_minute_buckets": sum(b["observed_minutes"] < 5 for b in bars),
            "price_mismatches": sum(
                b["aggregate_minus_native"] is not None
                and any(abs(b["aggregate_minus_native"][f]) > 1e-6 for f in ("open", "high", "low", "close"))
                for b in bars
            ),
        }
        await asyncio.to_thread(append_observation, self.settings.data_dir / "observations", day, snapshot)
        await self.store.put("market_observation_seen", {"day": day, "bars": seen})
        await self.store.put("market_observation", snapshot)

    async def run(self, stop):
        async with self.store.leader("market-observer") as leader:
            while not stop.is_set():
                loop = asyncio.get_running_loop()
                poll_started = loop.time()
                await leader.execute("SELECT 1")
                try:
                    await self.tick()
                except Exception as exc:
                    previous = await self.store.get("market_observation", {})
                    await self.store.put(
                        "market_observation",
                        {
                            **previous,
                            "state": "failed",
                            "updated_at": utcnow().isoformat(),
                            "error": type(exc).__name__,
                        },
                    )
                    await self.store.alert(
                        f"observation-error-{utcnow().date()}",
                        "시세 관찰 작업 실패. 매매 경로는 변경하지 않고 관찰을 재시도합니다.",
                    )
                    event("observation_error", error=type(exc).__name__)
                try:
                    # No catch-up bursts: a delayed cycle starts a new cadence.
                    elapsed = loop.time() - poll_started
                    await asyncio.wait_for(stop.wait(), timeout=next_poll_delay(elapsed))
                except TimeoutError:
                    pass


async def main_async():
    settings = Settings()
    store = Store(settings)
    await store.open()
    observer = Observer(settings, store)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    try:
        await observer.run(stop)
    finally:
        await observer.broker.close()
        await store.close()


def main():
    configure_logging()
    try:
        asyncio.run(main_async())
    except Exception as exc:
        event("observer_failed", error=type(exc).__name__)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
