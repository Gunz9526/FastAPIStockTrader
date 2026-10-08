import json
import os
from datetime import timedelta
from pathlib import Path

import pandas as pd

from stock_trader.broker import Alpaca
from stock_trader.clock import parse_time, utcnow
from stock_trader.config import Settings

COLUMNS = ["timestamp", "symbol", "open", "high", "low", "close", "volume"]


def normalized_bars(symbol: str, rows: list[dict]) -> pd.DataFrame:
    frame = pd.DataFrame(rows)
    if frame.empty:
        return pd.DataFrame(columns=COLUMNS)
    frame = frame.rename(
        columns={"t": "timestamp", "o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"}
    )
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    frame["symbol"] = symbol
    for column in COLUMNS[2:]:
        frame[column] = pd.to_numeric(frame[column], errors="raise")
    valid = (
        (frame["low"] > 0)
        & (frame["low"] <= frame["open"])
        & (frame["low"] <= frame["close"])
        & (frame["high"] >= frame["open"])
        & (frame["high"] >= frame["close"])
        & (frame["volume"] >= 0)
    )
    return frame.loc[valid, COLUMNS].drop_duplicates(["symbol", "timestamp"], keep="last")


def atomic_parquet(frame: pd.DataFrame, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(f".{os.getpid()}.tmp")
    frame.to_parquet(temp, index=False)
    os.replace(temp, path)


class BarCache:
    """Keep original minute bars, then aggregate only complete, closed five-minute buckets."""

    def __init__(self):
        self.frames: dict[str, pd.DataFrame] = {}
        self.connected = False
        self.last_event = None

    def seed(self, symbol: str, rows: list[dict]):
        frame = normalized_bars(symbol, rows)
        existing = self.frames.get(symbol, pd.DataFrame(columns=COLUMNS))
        parts = [f for f in (existing, frame) if not f.empty]
        if parts:
            self.frames[symbol] = (
                pd.concat(parts)
                .drop_duplicates("timestamp", keep="last")
                .sort_values("timestamp")
                .tail(3000)
                .reset_index(drop=True)
            )

    def update(self, bar: dict):
        self.seed(bar["S"], [bar])
        self.last_event = utcnow()

    def five_minute(self, symbol: str, now=None) -> pd.DataFrame:
        now = now or utcnow()
        frame = self.frames.get(symbol)
        if frame is None or frame.empty:
            return pd.DataFrame(columns=COLUMNS)
        frame = frame.set_index("timestamp")
        grouped = frame.resample("5min", label="left", closed="left")
        bars = grouped.agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
        # Missing IEX minute bars mean unknown continuity; never fabricate a tradable bucket.
        bars = bars.loc[grouped["close"].count() == 5].dropna()
        bars = bars.loc[bars.index + pd.Timedelta(minutes=5, seconds=35) <= pd.Timestamp(now)]
        bars["symbol"] = symbol
        return bars.reset_index()[COLUMNS]


async def backfill_cache(broker: Alpaca, settings: Settings, cache: BarCache):
    now = utcnow()
    start = now - timedelta(days=4)
    # Basic historical access may exclude recent minutes. The live stream fills the remaining gap.
    # After startup, incomplete lookbacks stay blocked until enough live minutes accumulate.
    end = now - timedelta(minutes=16)
    async for batch in broker.bars(settings.symbols, start.isoformat(), end.isoformat(), timeframe="1Min"):
        for symbol, rows in batch.items():
            cache.seed(symbol, rows)


async def collect_history(broker: Alpaca, settings: Settings):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    marker = settings.data_dir / "history_watermark.txt"
    manifest_path = settings.data_dir / "manifest.json"
    contract = {
        "feed": "iex",
        "adjustment": "raw",
        "symbols": list(settings.symbols),
        "aggregation": "complete-five-minute-v1",
    }
    if manifest_path.exists() and json.loads(manifest_path.read_text()) != contract:
        raise ValueError("history contract changed; use a new DATA_DIR for a new universe")
    manifest_path.write_text(json.dumps(contract, indent=2))
    end = utcnow().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(minutes=20)
    start = utcnow() - timedelta(days=settings.history_days)
    if marker.exists():
        start = max(start, parse_time(marker.read_text().strip()) - timedelta(days=2))
    while start < end:
        chunk_end = min(start + timedelta(days=14), end)
        buckets: dict[str, list] = {}
        # Match runtime aggregation: collect minutes and require complete five-minute buckets.
        async for batch in broker.bars(settings.symbols, start.isoformat(), chunk_end.isoformat(), "1Min"):
            for symbol, rows in batch.items():
                buckets.setdefault(symbol, []).extend(rows)
        for symbol, rows in buckets.items():
            cache = BarCache()
            # Historical chunk must not be truncated to the runtime rolling buffer.
            cache.frames[symbol] = normalized_bars(symbol, rows).sort_values("timestamp")
            frame = cache.five_minute(symbol, chunk_end + timedelta(minutes=6))
            for month, part in frame.groupby(frame["timestamp"].dt.strftime("%Y-%m")):
                path = settings.data_dir / "bars" / symbol / f"{month}.parquet"
                if path.exists():
                    part = pd.concat([pd.read_parquet(path), part]).drop_duplicates("timestamp", keep="last")
                atomic_parquet(part.sort_values("timestamp"), path)
        temp = marker.with_suffix(".tmp")
        temp.write_text(chunk_end.isoformat())
        os.replace(temp, marker)
        start = chunk_end


def read_history(settings: Settings) -> pd.DataFrame:
    manifest = json.loads((settings.data_dir / "manifest.json").read_text())
    if manifest["feed"] != "iex" or manifest["symbols"] != list(settings.symbols):
        raise ValueError("historical feed/universe mismatch")
    parts = [pd.read_parquet(path) for path in sorted((settings.data_dir / "bars").glob("*/*.parquet"))]
    if not parts:
        raise ValueError("no historical IEX bars; collect history first")
    return (
        pd.concat(parts)
        .drop_duplicates(["symbol", "timestamp"], keep="last")
        .sort_values(["timestamp", "symbol"])
        .reset_index(drop=True)
    )
