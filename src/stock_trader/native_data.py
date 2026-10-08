"""Read-only Alpaca research collection, isolated from the runtime history contract."""

import argparse
import asyncio
import json
from datetime import timedelta

import pandas as pd

from stock_trader.broker import Alpaca
from stock_trader.clock import parse_time, utcnow
from stock_trader.config import Settings
from stock_trader.data import atomic_parquet, normalized_bars
from stock_trader.modeling import checksum
from stock_trader.research_cache import completed_collection
from stock_trader.study import write_json


async def collect_native(settings, reference, directory):
    holdout = reference["holdout_start"]
    start = parse_time(reference["folds"][0]["train_start"] + "T00:00:00Z")
    end = parse_time(holdout + "T00:00:00Z")
    if start >= end or holdout != "2026-06-16":
        raise ValueError("ADR-0017 requires the registered development-only interval")
    contract = {
        "feed": "iex",
        "adjustment": "raw",
        "timeframe": "5Min",
        "symbols": list(settings.symbols),
        "start": start.isoformat(),
        "end_exclusive": end.isoformat(),
        "holdout_start": holdout,
        "purpose": "ADR-0017-development-only",
    }
    directory.mkdir(parents=True, exist_ok=True)
    manifest = directory / "manifest.json"
    if manifest.exists() and json.loads(manifest.read_text()) != contract:
        raise ValueError("native research data contract mismatch")
    if (cached := completed_collection(directory, contract)) is not None:
        calendar_path = directory / "calendar.json"
        if not calendar_path.is_file() or calendar_path.is_symlink():
            raise ValueError("completed native cache calendar missing")
        if "calendar_sha256" in cached and checksum(calendar_path) != cached["calendar_sha256"]:
            raise ValueError("completed native cache calendar checksum mismatch")
        # Older ADR-0017 snapshots hash parquet only; retain their recorded contract.
        return cached
    write_json(manifest, contract)
    broker = Alpaca(settings)
    pages = 0
    try:
        calendar = await broker.calendar(
            reference["folds"][0]["train_start"], (end - timedelta(days=1)).date().isoformat()
        )
        write_json(directory / "calendar.json", calendar)
        openings = {c["date"]: int(c["open"][:2]) * 60 + int(c["open"][3:5]) for c in calendar}
        closings = {c["date"]: int(c["close"][:2]) * 60 + int(c["close"][3:5]) for c in calendar}
        cursor = start
        while cursor < end:
            boundary = min(cursor + timedelta(days=28), end)
            chunks = []
            async for batch in broker.bars(
                settings.symbols, cursor.isoformat(), boundary.isoformat(), "5Min"
            ):
                pages += 1
                for symbol, rows in batch.items():
                    if symbol not in settings.symbols:
                        raise ValueError("unexpected source symbol")
                    if rows:
                        chunks.append(normalized_bars(symbol, rows))
            if chunks:
                frame = pd.concat(chunks, ignore_index=True)
                local = frame["timestamp"].dt.tz_convert("America/New_York")
                if (local.dt.strftime("%Y-%m-%d") >= holdout).any():
                    raise ValueError("sealed holdout returned by source")
                frame = frame.loc[
                    (frame["timestamp"] >= pd.Timestamp(cursor))
                    & (frame["timestamp"] < pd.Timestamp(boundary))
                ]
                minute = frame["timestamp"].dt.tz_convert("America/New_York")
                dates = minute.dt.strftime("%Y-%m-%d")
                clock_minutes = minute.dt.hour * 60 + minute.dt.minute
                regular = clock_minutes.ge(dates.map(openings)) & clock_minutes.lt(dates.map(closings))
                frame = frame.loc[regular]
                for (symbol, month), part in frame.groupby(
                    ["symbol", frame["timestamp"].dt.strftime("%Y-%m")]
                ):
                    path = directory / "bars" / symbol / f"{month}.parquet"
                    if path.exists():
                        part = pd.concat([pd.read_parquet(path), part]).drop_duplicates(
                            ["timestamp", "symbol"], keep="last"
                        )
                    atomic_parquet(part.sort_values("timestamp"), path)
            cursor = boundary
            progress = {
                "stage": "collect_native",
                "completed_through_exclusive": cursor.isoformat(),
                "pages": pages,
                "updated_at": utcnow().isoformat(),
            }
            write_json(directory / "progress.json", progress)
            print(json.dumps(progress), flush=True)
        files = sorted((directory / "bars").glob("*/*.parquet"))
        result = {
            "contract": contract,
            "completed_at": utcnow().isoformat(),
            "pages": pages,
            "files": len(files),
            "file_sha256": {str(p.relative_to(directory)): checksum(p) for p in files},
            "calendar_sha256": checksum(directory / "calendar.json"),
        }
        write_json(directory / "collection.json", result)
        return result
    finally:
        await broker.close()


def main():
    from pathlib import Path

    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    args = parser.parse_args()
    settings = Settings()
    reference = json.loads((args.source / "report.json").read_text())
    if reference["holdout_evaluated"] or reference["settings"] != settings.public_contract():
        raise ValueError("registered sealed settings/reference required")
    directory = settings.data_dir / "research" / "native-iex-5m-development"
    asyncio.run(collect_native(settings, reference, directory))


if __name__ == "__main__":
    main()
