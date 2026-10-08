import pytest

from stock_trader.daily_data import collect_daily
from stock_trader.modeling import checksum
from stock_trader.research_cache import completed_collection
from stock_trader.study import write_json


async def test_completed_daily_data_returns_without_api_client(settings, tmp_path, monkeypatch):
    source = tmp_path / "reference"
    source.mkdir()
    write_json(
        source / "report.json",
        {
            "holdout_evaluated": False,
            "settings": settings.public_contract(),
            "holdout_start": "2026-06-16",
            "folds": [{"train_start": "2024-10-03"}],
        },
    )
    directory = settings.data_dir / "research" / "swing-daily-development"
    directory.mkdir(parents=True)
    contract = {
        "feed": "iex",
        "timeframe": "1Day",
        "adjustments": ["raw", "split"],
        "symbols": list(settings.symbols),
        "start": "2024-10-03",
        "end_exclusive_utc": "2026-06-16T00:00:00Z",
        "holdout_start": "2026-06-16",
        "purpose": "ADR-0020-development-only",
    }
    write_json(directory / "manifest.json", contract)
    for name in ("raw.parquet", "split.parquet", "corporate-actions.json"):
        (directory / name).write_bytes(b"immutable fixture")
    result = {
        "contract": contract,
        "file_sha256": {
            name: checksum(directory / name)
            for name in ("raw.parquet", "split.parquet", "corporate-actions.json")
        },
    }
    write_json(directory / "collection.json", result)

    def no_api(*args):
        raise AssertionError("cache hit must not create an API client")

    monkeypatch.setattr("stock_trader.daily_data.Alpaca", no_api)
    assert await collect_daily(settings, source) == result
    (directory / "raw.parquet").write_bytes(b"corrupted")
    with pytest.raises(ValueError, match="checksum"):
        await collect_daily(settings, source)


def test_cache_rejects_external_path_and_missing_manifest(tmp_path):
    directory = tmp_path / "cache"
    directory.mkdir()
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside")
    contract = {"registered": True}
    write_json(
        directory / "collection.json",
        {
            "contract": contract,
            "file_sha256": {"../outside": checksum(outside)},
        },
    )
    with pytest.raises(ValueError, match="manifest"):
        completed_collection(directory, contract)
    write_json(directory / "manifest.json", contract)
    with pytest.raises(ValueError, match="checksum"):
        completed_collection(directory, contract)
