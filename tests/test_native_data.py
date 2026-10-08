import json

import pandas as pd
import pytest

from stock_trader.data import normalized_bars
from stock_trader.modeling import checksum
from stock_trader.native_data import collect_native
from stock_trader.regime_study import native_frame
from stock_trader.study import write_json


def bar(timestamp):
    return {"t": timestamp, "o": 100, "h": 101, "l": 99, "c": 100.5, "v": 50}


async def test_native_collection_calendar_isolation_and_no_holdout(settings, tmp_path, monkeypatch):
    calls = []

    class Broker:
        def __init__(self, settings):
            pass

        async def calendar(self, start, end):
            calls.append(("calendar", start, end))
            return [
                {"date": "2026-06-12", "open": "09:30", "close": "13:00"},
                {"date": "2026-06-15", "open": "09:30", "close": "16:00"},
            ]

        async def bars(self, symbols, start, end, timeframe):
            calls.append(("bars", start, end, timeframe))
            yield {
                "AAPL": [
                    bar("2026-06-12T16:55:00Z"),
                    bar("2026-06-12T17:05:00Z"),
                    bar("2026-06-15T19:55:00Z"),
                ]
            }

        async def close(self):
            pass

    monkeypatch.setattr("stock_trader.native_data.Alpaca", Broker)
    untouched = settings.data_dir / "manifest.json"
    untouched.parent.mkdir()
    untouched.write_text("original-runtime-data")
    destination = settings.data_dir / "research" / "sample"
    reference = {"holdout_start": "2026-06-16", "folds": [{"train_start": "2026-06-12"}]}
    result = await collect_native(settings, reference, destination)
    frame = pd.read_parquet(destination / "bars" / "AAPL" / "2026-06.parquet")
    assert len(frame) == 2
    assert not frame["timestamp"].eq(pd.Timestamp("2026-06-12T17:05Z")).any()
    assert untouched.read_text() == "original-runtime-data"
    assert calls[1][-1] == "5Min"
    assert result["contract"]["end_exclusive"] == "2026-06-16T00:00:00+00:00"
    assert result["file_sha256"]["bars/AAPL/2026-06.parquet"] == checksum(
        destination / "bars" / "AAPL" / "2026-06.parquet"
    )
    before = list(calls)
    assert await collect_native(settings, reference, destination) == result
    assert calls == before
    (destination / "calendar.json").write_text("[]")
    with pytest.raises(ValueError, match="calendar checksum"):
        await collect_native(settings, reference, destination)
    assert calls == before


async def test_native_collection_rejects_source_holdout(settings, tmp_path, monkeypatch):
    class Broker:
        def __init__(self, settings):
            pass

        async def calendar(self, *args):
            return []

        async def bars(self, *args):
            yield {"AAPL": [bar("2026-06-16T13:30Z")]}

        async def close(self):
            pass

    monkeypatch.setattr("stock_trader.native_data.Alpaca", Broker)
    with pytest.raises(ValueError, match="sealed holdout"):
        await collect_native(
            settings,
            {"holdout_start": "2026-06-16", "folds": [{"train_start": "2026-06-12"}]},
            tmp_path / "sample",
        )


def test_native_input_rejects_checksum_and_wrong_date_contract(settings, tmp_path):
    directory = tmp_path / "native"
    directory.mkdir()
    path = directory / "sample.parquet"
    normalized_bars("AAPL", [bar("2026-06-15T13:30Z")]).to_parquet(path, index=False)
    manifest = {
        "feed": "iex",
        "timeframe": "5Min",
        "adjustment": "raw",
        "symbols": list(settings.symbols),
        "holdout_start": "2026-06-16",
    }
    write_json(directory / "manifest.json", manifest)
    write_json(
        directory / "collection.json", {"contract": manifest, "file_sha256": {"sample.parquet": "incorrect"}}
    )
    with pytest.raises(ValueError, match="checksum"):
        native_frame(settings, directory, pd.DataFrame(), "2026-06-16")
    collection = json.loads((directory / "collection.json").read_text())
    collection["file_sha256"]["sample.parquet"] = checksum(path)
    write_json(directory / "collection.json", collection)
    with pytest.raises(ValueError, match="boundaries"):
        native_frame(settings, directory, pd.DataFrame({"date": ["2026-06-12"]}), "2026-06-16")
