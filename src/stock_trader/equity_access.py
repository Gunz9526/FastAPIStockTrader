"""ADR-0080: bounded, cached data-access samples; no returns, model or orders."""

import argparse
import asyncio
import json
import math

from stock_trader.broker import Alpaca, BrokerError
from stock_trader.clock import parse_time, utcnow
from stock_trader.config import Settings
from stock_trader.etf_ledger import fingerprint
from stock_trader.etf_shadow import read_sealed, seal

ROOT = "research/equity-access-0080"
BAR_PARAMS = {
    "start": "2022-06-06T04:00:00Z",
    "end": "2022-06-11T03:59:59Z",
    "feed": "sip",
    "timeframe": "1Day",
    "adjustment": "raw",
    "limit": 100,
}
REQUESTS = [
    {"name": "assets", "path": "/v2/assets", "data_api": False, "params": {"asset_class": "us_equity"}},
    {
        "name": "rename_after",
        "path": "/v2/stocks/bars",
        "data_api": True,
        "params": {**BAR_PARAMS, "symbols": "META", "asof": "2022-06-10"},
    },
    {
        "name": "rename_before",
        "path": "/v2/stocks/bars",
        "data_api": True,
        "params": {**BAR_PARAMS, "symbols": "FB", "asof": "2022-06-08"},
    },
    {
        "name": "rename_literal",
        "path": "/v2/stocks/bars",
        "data_api": True,
        "params": {**BAR_PARAMS, "symbols": "FB,META", "asof": "-"},
    },
    {
        "name": "inactive_bars",
        "path": "/v2/stocks/bars",
        "data_api": True,
        "params": {
            **BAR_PARAMS,
            "start": "2022-10-24T04:00:00Z",
            "end": "2022-10-29T03:59:59Z",
            "symbols": "TWTR",
            "asof": "2022-10-26",
        },
    },
    {
        "name": "actions",
        "path": "/v1/corporate-actions",
        "data_api": True,
        "params": {
            "symbols": "AAPL,TWTR",
            "start": "2016-01-01",
            "end": "2022-12-31",
            "limit": 1000,
            "sort": "asc",
        },
    },
]
CONTRACT = {
    "decision": "ADR-0080",
    "requests": REQUESTS,
    "max_logical_gets": 6,
    "max_transport_attempts": 24,
    "strategy_evaluation": False,
    "orders_enabled": False,
}


async def collect(settings, broker, fence):
    await fence()
    root = settings.data_dir / ROOT
    registration = root / "registration.json"
    if registration.exists():
        if read_sealed(registration)["contract"] != CONTRACT:
            raise ValueError("data access contract changed")
    else:
        seal(registration, {"contract": CONTRACT, "registered_at": utcnow().isoformat()})
    fetched, cached = 0, 0
    outputs = {}
    for spec in REQUESTS:
        await fence()
        path = root / (spec["name"] + ".json")
        if path.exists():
            value = read_sealed(path)
            if value["request"] != spec:
                raise ValueError("cached request changed")
            cached += 1
        else:
            value = {"request": spec, "observed_at": utcnow().isoformat()}
            fetched += 1
            try:
                value.update(
                    state="received",
                    response=await broker.request(
                        "GET", spec["path"], data_api=spec["data_api"], params=spec["params"]
                    ),
                )
            except BrokerError as exc:
                value.update(state="error", http_status=exc.status)
            await fence()
            seal(path, value)
        outputs[spec["name"]] = {"state": value["state"], "payload_sha256": fingerprint(value)}
    collection = {"contract_sha256": fingerprint(CONTRACT), "outputs": outputs}
    seal(root / "collection.json", collection)
    return {"logical_gets": fetched, "cache_hits": cached, "new_orders": 0, "outputs": outputs}


def assessment(root):
    responses, sources, errors = {}, {}, {}
    for spec in REQUESTS:
        value = read_sealed(root / (spec["name"] + ".json"))
        if value["request"] != spec:
            raise ValueError("access assessment request changed")
        sources[spec["name"]] = fingerprint(value)
        if value["state"] == "error":
            errors[spec["name"]] = value["http_status"]
        else:
            responses[spec["name"]] = value["response"]
    result = {
        "decision": "ADR-0080",
        "analysis_version": 1,
        "source_sha256": sources,
        "full_market_point_in_time_ready": False,
        "new_broker_requests": 0,
        "strategy_evaluations": 0,
        "performance_approved": False,
    }
    if errors:
        return {**result, "state": "access_incomplete", "http_errors": errors}
    assets = responses["assets"]
    if not isinstance(assets, list) or any(
        not isinstance(row, dict) or "id" not in row or "symbol" not in row for row in assets
    ):
        raise ValueError("invalid assets response")
    result["assets"] = {
        "rows": len(assets),
        "duplicate_ids": len(assets) - len({r["id"] for r in assets}),
        "active": sum(r.get("status") == "active" for r in assets),
        "inactive": sum(r.get("status") == "inactive" for r in assets),
        "fields": sorted(set().union(*(set(r) for r in assets))),
    }
    bars = {}
    incomplete = []
    invalid = []
    for spec in REQUESTS[1:5]:
        name = spec["name"]
        response = responses[name]
        if response.get("next_page_token"):
            incomplete.append(name)
        bars[name] = response["bars"]
        for symbol, rows in bars[name].items():
            if symbol not in spec["params"]["symbols"].split(",") or len({r["t"] for r in rows}) != len(rows):
                raise ValueError("invalid sample symbol or duplicate bar")
            for row in rows:
                values = [float(row[k]) for k in ["o", "h", "l", "c", "v"]]
                o, h, low, c, v = values
                if (
                    not all(math.isfinite(x) for x in values)
                    or low <= 0
                    or v <= 0
                    or not low <= min(o, c) <= max(o, c) <= h
                ):
                    invalid.append(
                        {
                            "sample": name,
                            "symbol": symbol,
                            "timestamp": row["t"],
                            "reason": "invalid_ohlcv_for_research",
                        }
                    )
                stamp = parse_time(row["t"])
                if not parse_time(spec["params"]["start"]) <= stamp <= parse_time(spec["params"]["end"]):
                    raise ValueError("sample timestamp outside request")
    after, before = bars["rename_after"].get("META", []), bars["rename_before"].get("FB", [])
    literal = [r for rows in bars["rename_literal"].values() for r in rows]
    signatures = {}
    for row in literal:
        signatures.setdefault(row["t"], set()).add(fingerprint(row))
    result["rename"] = {
        "after_rows": len(after),
        "before_rows": len(before),
        "mapped_series_equal": bool(after) and after == before,
        "literal_rows": len(literal),
        "literal_unique_timestamps": len(signatures),
        "literal_alias_overlap": len(literal) - len(signatures),
        "conflicting_alias_timestamps": sum(len(v) > 1 for v in signatures.values()),
    }
    inactive = bars["inactive_bars"].get("TWTR", [])
    result["inactive_sample"] = {
        "symbol": "TWTR",
        "rows": len(inactive),
        "timestamps": [r["t"] for r in inactive],
    }
    response = responses["actions"]
    if response.get("next_page_token"):
        incomplete.append("actions")
    actions = response["corporate_actions"]
    dividends = actions.get("cash_dividends", [])
    result["actions"] = {
        "counts": {k: len(v) for k, v in actions.items()},
        "missing_dividend_payable_date": sum(not r.get("payable_date") for r in dividends),
        "missing_dividend_record_date": sum(not r.get("record_date") for r in dividends),
        "dividend_process_start": min((r["process_date"] for r in dividends), default=None),
        "dividend_process_end": max((r["process_date"] for r in dividends), default=None),
    }
    return {
        **result,
        "state": "sample_quality_failed" if invalid else "samples_checked_research_data_incomplete",
        "invalid_bars": invalid,
        "incomplete_pages": incomplete,
        "remaining_evidence": [
            "historical eligible universe and security type",
            "listing/delisting and identifier history completeness",
            "complete payment dates and cash events",
            "unused registered evaluation period",
        ],
    }


async def main_async():
    settings = Settings()
    from stock_trader.storage import Store

    store = Store(settings)
    broker = Alpaca(settings)
    await store.open()
    try:
        async with store.leader("equity-access-0080") as leader:

            async def fence():
                await leader.execute("SELECT 1")

            print(json.dumps(await collect(settings, broker, fence), indent=2))
    finally:
        await broker.close()
        await store.close()


def main():
    parser = argparse.ArgumentParser()
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--collect", action="store_true")
    action.add_argument("--assess", action="store_true")
    args = parser.parse_args()
    if args.collect:
        asyncio.run(main_async())
    else:
        root = Settings().data_dir / ROOT
        result = assessment(root)
        seal(root / "assessment.json", result)
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
