"""ADR-0025: one frozen rule, a previously unused test, no runtime registration."""

import argparse
import asyncio
import json
from dataclasses import asdict
from pathlib import Path

import pandas as pd

from stock_trader.broker import Alpaca
from stock_trader.clock import utcnow
from stock_trader.config import Settings
from stock_trader.data import atomic_parquet, normalized_bars
from stock_trader.modeling import checksum
from stock_trader.study import write_json
from stock_trader.swing_study import (
    PRICE_COLUMNS,
    daily_features,
    prepare_swing,
    simulate_swing,
    split_factors,
    trend_predictions,
)

TEST_START = "2026-06-16"
TEST_END = "2026-10-01"
REQUEST_END = "2026-10-02T00:00:00Z"


def frozen_reference(source):
    stress = json.loads((source / "report.json").read_text())
    if (
        not stress.get("completed_at")
        or not stress.get("development_stress_passed")
        or stress["chosen"] != "trend_rule_10"
    ):
        raise ValueError("ADR-0024 frozen rule must first pass development stress")
    study_dir = Path(stress["source"])
    if checksum(study_dir / "report.json") != stress["source_report_sha256"]:
        raise ValueError("selected development report changed")
    reference = json.loads((study_dir / "report.json").read_text())
    if reference["holdout_start"] != TEST_START or reference["promoted"] or reference["holdout_evaluated"]:
        raise ValueError("previously unused final test required")
    return stress, reference


def contract(settings):
    return {
        "decision": "ADR-0025",
        "feed": "iex",
        "symbols": list(settings.symbols),
        "start": TEST_START,
        "end": TEST_END,
        "request_end_exclusive": REQUEST_END,
        "candidate": "trend_rule_10",
        "timeframes": ["1Day", "5Min"],
        "costs_bps": [20, 30],
    }


async def collect_final(settings, source):
    stress, reference = frozen_reference(source)
    if reference["settings"] != settings.public_contract():
        raise ValueError("final settings differ from selected study")
    root = settings.data_dir / "research" / "swing-final-0025"
    root.mkdir(parents=True, exist_ok=True)
    registered = contract(settings)
    manifest = root / "manifest.json"
    if manifest.exists() and json.loads(manifest.read_text()) != registered:
        raise ValueError("final input contract changed")
    if (root / "collection.json").exists():
        return root
    write_json(manifest, registered)
    broker = Alpaca(settings)
    pages = 0
    try:
        calendar = await broker.calendar(TEST_START, TEST_END)
        write_json(root / "calendar.json", calendar)
        for adjustment in ("raw", "split"):
            parts, token = [], None
            while True:
                params = {
                    "symbols": ",".join(settings.symbols),
                    "start": TEST_START + "T00:00:00Z",
                    "end": REQUEST_END,
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
                        raise ValueError("unknown final symbol")
                    if rows:
                        parts.append(normalized_bars(symbol, rows))
                token = response.get("next_page_token")
                if not token:
                    break
            if not parts:
                raise ValueError("no final daily observations")
            frame = pd.concat(parts, ignore_index=True).sort_values(["timestamp", "symbol"])
            frame["date"] = frame["timestamp"].dt.tz_convert("America/New_York").dt.strftime("%Y-%m-%d")
            if (
                frame["date"].min() < TEST_START
                or frame["date"].max() > TEST_END
                or frame.duplicated(["date", "symbol"]).any()
            ):
                raise ValueError("final daily boundaries or duplicates")
            atomic_parquet(frame, root / f"{adjustment}.parquet")
        parts = []
        async for response in broker.bars(settings.symbols, TEST_START + "T00:00:00Z", REQUEST_END, "5Min"):
            pages += 1
            for symbol, rows in response.items():
                if symbol not in settings.symbols:
                    raise ValueError("unknown final intraday symbol")
                if rows:
                    parts.append(normalized_bars(symbol, rows))
        if not parts:
            raise ValueError("no final intraday observations")
        bars = pd.concat(parts, ignore_index=True).sort_values(["timestamp", "symbol"])
        local = bars["timestamp"].dt.tz_convert("America/New_York")
        bars["date"] = local.dt.strftime("%Y-%m-%d")
        opening = {c["date"]: int(c["open"][:2]) * 60 + int(c["open"][3:5]) for c in calendar}
        closing = {c["date"]: int(c["close"][:2]) * 60 + int(c["close"][3:5]) for c in calendar}
        minutes = local.dt.hour * 60 + local.dt.minute
        bars = bars.loc[minutes.ge(bars["date"].map(opening)) & minutes.lt(bars["date"].map(closing))]
        if bars.duplicated(["timestamp", "symbol"]).any():
            raise ValueError("duplicate final intraday bars")
        atomic_parquet(bars, root / "native.parquet")
        actions = await broker.request(
            "GET",
            "/v1/corporate-actions",
            data_api=True,
            params={
                "symbols": ",".join(settings.symbols),
                "types": "forward_split,reverse_split",
                "start": "2024-10-03",
                "end": TEST_END,
                "limit": 1000,
            },
        )
        if actions.get("next_page_token"):
            raise ValueError("unregistered split pagination")
        write_json(root / "corporate-actions.json", actions)
        names = (
            "manifest.json",
            "calendar.json",
            "raw.parquet",
            "split.parquet",
            "native.parquet",
            "corporate-actions.json",
        )
        write_json(
            root / "collection.json",
            {
                "contract": registered,
                "completed_at": utcnow().isoformat(),
                "pages": pages,
                "daily_rows": len(frame),
                "native_rows": len(bars),
                "file_sha256": {n: checksum(root / n) for n in names},
                "source_sha256": checksum(Path(__file__)),
                "development_stress_sha256": checksum(source / "report.json"),
            },
        )
        print(
            json.dumps(
                {"stage": "collected", "pages": pages, "daily_rows": len(frame), "native_rows": len(bars)}
            ),
            flush=True,
        )
        return root
    finally:
        await broker.close()


def run_final(settings, source):
    stress, reference = frozen_reference(source)
    root = settings.data_dir / "research" / "swing-final-0025"
    collected = json.loads((root / "collection.json").read_text())
    if collected["contract"] != contract(settings) or collected["development_stress_sha256"] != checksum(
        source / "report.json"
    ):
        raise ValueError("final collection or frozen stress changed")
    for name, expected in collected["file_sha256"].items():
        path = root / name
        if path.is_symlink() or Path(name).name != name or checksum(path) != expected:
            raise ValueError("final collection checksum mismatch")
    consumed = settings.report_dir / "final-test-consumed-0025.json"
    if consumed.exists():
        raise ValueError("this final test has already been evaluated; do not reselect or repeat")
    calendar = json.loads((root / "calendar.json").read_text())
    dates = [c["date"] for c in calendar]
    if dates != sorted(set(dates)) or dates[0] != TEST_START or dates[-1] != TEST_END:
        raise ValueError("final calendar boundaries changed")
    development = settings.data_dir / "research" / "swing-daily-development"
    daily = pd.read_parquet(development / "raw.parquet")
    if checksum(development / "raw.parquet") != stress["input_sha256"][str(development / "raw.parquet")]:
        raise ValueError("past warm-up data changed")
    test = pd.read_parquet(root / "raw.parquet")
    if test["date"].min() < TEST_START or test["date"].max() > TEST_END:
        raise ValueError("final daily range changed")
    raw = pd.concat([daily, test], ignore_index=True).sort_values(["date", "symbol"]).reset_index(drop=True)
    factors = split_factors(raw, json.loads((root / "corporate-actions.json").read_text()))
    split = pd.read_parquet(root / "split.parquet").sort_values(["date", "symbol"]).reset_index(drop=True)
    adjusted = raw.loc[raw["date"].ge(TEST_START)].reset_index(drop=True)
    test_factors = factors.loc[raw["date"].ge(TEST_START)].reset_index(drop=True)
    if not adjusted[["date", "symbol"]].equals(split[["date", "symbol"]]):
        raise ValueError("final adjusted source keys differ")
    if any((adjusted[c] * test_factors - split[c]).abs().max() > 0.011 for c in PRICE_COLUMNS):
        raise ValueError("final exact split factor differs from provider")
    all_dates = sorted(set(daily["date"]) | set(dates))
    frame = daily_features(raw, factors, all_dates, settings.benchmark_symbols)
    bars = pd.read_parquet(root / "native.parquet")
    scale = raw[["date", "symbol"]].copy()
    scale["factor"] = factors
    bars = bars.merge(scale, on=["date", "symbol"], validate="many_to_one")
    if bars["factor"].isna().any():
        raise ValueError("final execution factor missing")
    for c in PRICE_COLUMNS:
        bars[c] *= bars["factor"]
    openings = {
        c["date"]: pd.Timestamp(c["date"] + " " + c["open"], tz="America/New_York").tz_convert("UTC")
        for c in calendar
    }
    replay = prepare_swing(bars, dates, openings)
    scores = trend_predictions(frame).where(frame["date"].ge(TEST_START))
    out = settings.report_dir / f"swing-final-{utcnow().strftime('%Y%m%dT%H%M%SZ')}"
    out.mkdir()
    (out / "source").mkdir()
    code = {}
    for name in (
        "swing_final.py",
        "swing_study.py",
        "swing_followup.py",
        "risk.py",
        "config.py",
        "modeling.py",
        "research.py",
        "study.py",
    ):
        path = Path(__file__).with_name(name)
        (out / "source" / name).write_bytes(path.read_bytes())
        code[name] = checksum(path)
    write_json(
        consumed,
        {
            "decision": "ADR-0025",
            "candidate": "trend_rule_10",
            "start": TEST_START,
            "end": TEST_END,
            "started_at": utcnow().isoformat(),
            "result_directory": str(out),
            "state": "evaluating",
            "code_sha256": code,
        },
    )
    report = {
        "created_at": utcnow().isoformat(),
        "decision": "ADR-0025",
        "candidate": "trend_rule_10",
        "source": str(source),
        "test_start": TEST_START,
        "test_end": TEST_END,
        "holding_days": 10,
        "holdout_evaluated": True,
        "promoted": False,
        "settings": settings.public_contract(),
        "collection_sha256": checksum(root / "collection.json"),
        "code_sha256": code,
        "daily_warmup_sha256": checksum(development / "raw.parquet"),
        "evaluations": [],
        "unobserved_calendar_sessions": [d for d in dates if not replay.batches[d]],
    }
    for cost in (20, 30):
        ledger = []
        scoped = settings.model_copy(update={"round_trip_cost_bps": cost})
        metrics = simulate_swing(
            frame, scores, 10, 0.005, scoped, replay, [(TEST_START, TEST_END)], ledger, "opening_quote"
        )
        filename = f"trades-{cost}.json"
        write_json(out / filename, ledger)
        report["evaluations"].append({"cost_bps": cost, "metrics": asdict(metrics), "trade_file": filename})
    main, stressed = [e["metrics"] for e in report["evaluations"]]
    report["passed"] = (
        main["trades"] >= 20
        and main["net_return"] > 0
        and main["max_drawdown"] <= 0.08
        and main["profit_factor"] >= 1.1
        and stressed["net_return"] > 0
    )
    report["completed_at"] = utcnow().isoformat()
    write_json(out / "report.json", report)
    write_json(
        consumed,
        {
            "decision": "ADR-0025",
            "candidate": "trend_rule_10",
            "start": TEST_START,
            "end": TEST_END,
            "completed_at": report["completed_at"],
            "result_directory": str(out),
            "state": "passed_research" if report["passed"] else "rejected",
            "result_sha256": checksum(out / "report.json"),
        },
    )
    print(
        json.dumps(
            {"stage": "final_complete", "passed": report["passed"], "evaluations": report["evaluations"]}
        ),
        flush=True,
    )
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--collect", action="store_true")
    args = parser.parse_args()
    if args.collect:
        asyncio.run(collect_final(Settings(), args.source))
    else:
        run_final(Settings(), args.source)


if __name__ == "__main__":
    main()
