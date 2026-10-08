"""Development-only IEX raw/split daily data for the registered swing research."""

import argparse
import asyncio
import json
from pathlib import Path

import pandas as pd

from stock_trader.broker import Alpaca, BrokerError
from stock_trader.clock import utcnow
from stock_trader.config import Settings
from stock_trader.data import atomic_parquet, normalized_bars
from stock_trader.modeling import checksum
from stock_trader.research_cache import completed_collection
from stock_trader.study import write_json


async def collect_daily(settings, source):
    reference = json.loads((source / "report.json").read_text())
    if (
        reference["holdout_evaluated"]
        or reference["settings"] != settings.public_contract()
        or reference["holdout_start"] != "2026-06-16"
    ):
        raise ValueError("registered sealed development reference required")
    start = reference["folds"][0]["train_start"]
    end = reference["holdout_start"]
    directory = settings.data_dir / "research" / "swing-daily-development"
    directory.mkdir(parents=True, exist_ok=True)
    contract = {
        "feed": "iex",
        "timeframe": "1Day",
        "adjustments": ["raw", "split"],
        "symbols": list(settings.symbols),
        "start": start,
        "end_exclusive_utc": end + "T00:00:00Z",
        "holdout_start": end,
        "purpose": "ADR-0020-development-only",
    }
    manifest = directory / "manifest.json"
    if manifest.exists() and json.loads(manifest.read_text()) != contract:
        raise ValueError("daily research contract mismatch")
    if (cached := completed_collection(directory, contract)) is not None:
        return cached
    write_json(manifest, contract)
    broker = Alpaca(settings)
    pages = 0
    try:
        for adjustment in contract["adjustments"]:
            token = None
            parts = []
            while True:
                params = {
                    "symbols": ",".join(settings.symbols),
                    "start": start + "T00:00:00Z",
                    "end": end + "T00:00:00Z",
                    "feed": "iex",
                    "timeframe": "1Day",
                    "adjustment": adjustment,
                    "limit": 10000,
                }
                if token:
                    params["page_token"] = token
                response = await broker.request("GET", "/v2/stocks/bars", data_api=True, params=params)
                pages += 1
                for symbol, rows in response.get("bars", {}).items():
                    if symbol not in settings.symbols:
                        raise ValueError("unexpected source symbol")
                    if rows:
                        parts.append(normalized_bars(symbol, rows))
                token = response.get("next_page_token")
                if not token:
                    break
            if not parts:
                raise ValueError("empty daily data")
            frame = (
                pd.concat(parts, ignore_index=True)
                .drop_duplicates(["timestamp", "symbol"], keep="last")
                .sort_values(["timestamp", "symbol"])
            )
            frame["date"] = frame["timestamp"].dt.tz_convert("America/New_York").dt.strftime("%Y-%m-%d")
            if frame["date"].max() >= end:
                raise ValueError("sealed holdout returned by daily source")
            frame = frame.loc[frame["date"] >= start]
            atomic_parquet(frame, directory / f"{adjustment}.parquet")
            print(
                json.dumps(
                    {"stage": "daily_collected", "adjustment": adjustment, "rows": len(frame), "pages": pages}
                ),
                flush=True,
            )
        available = True
        try:
            actions = await broker.request(
                "GET",
                "/v1/corporate-actions",
                data_api=True,
                params={
                    "symbols": ",".join(settings.symbols),
                    "types": "forward_split,reverse_split",
                    "start": start,
                    "end": "2026-06-15",
                    "limit": 1000,
                },
            )
            if actions.get("next_page_token"):
                raise ValueError("unexpected split pagination; extend the registered collector")
            write_json(directory / "corporate-actions.json", actions)
        except BrokerError as exc:
            if exc.status not in (403, 404):
                raise
            available = False
            write_json(directory / "corporate-actions.json", {"available": False, "status": exc.status})
        result = {
            "contract": contract,
            "completed_at": utcnow().isoformat(),
            "pages": pages,
            "corporate_actions_available": available,
            "file_sha256": {
                p.name: checksum(p)
                for p in (
                    directory / "raw.parquet",
                    directory / "split.parquet",
                    directory / "corporate-actions.json",
                )
            },
            "source_sha256": checksum(Path(__file__)),
        }
        write_json(directory / "collection.json", result)
        return result
    finally:
        await broker.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    asyncio.run(collect_daily(Settings(), parser.parse_args().source))


if __name__ == "__main__":
    main()
