"""Offline daily observations audit. No training, broker client or orders."""

import argparse
import hashlib
import json
import math
import re
import statistics
import tempfile
from collections import Counter
from datetime import date
from pathlib import Path

from stock_trader.clock import parse_time, trading_day, utcnow


def digest(data):
    return hashlib.sha256(data).hexdigest()


def describe(values):
    return {
        "count": len(values),
        "median": statistics.median(values) if values else None,
        "max": max(values) if values else None,
    }


def summarize_observations(rows, day):
    if not rows:
        raise ValueError("empty completed observations")
    quotes, schemas, unique = Counter(), Counter(), {}
    spreads, future_leads, timestamps = [], [], []
    repeats = price_checks = price_bad_count = volume_bad_count = 0
    for row in rows:
        timestamp = parse_time(row["updated_at"])
        if (
            row["day"] != day
            or trading_day(timestamp) != day
            or row.get("orders_submitted") != 0
            or row["feed"] != "iex"
        ):
            raise ValueError("observation day/feed/read-only contract mismatch")
        if timestamps and timestamp <= timestamps[-1]:
            raise ValueError("non-increasing snapshot times")
        timestamps.append(timestamp)
        schema = row.get("schema_version", 1)
        schemas[schema] += 1
        if schema not in (1, 2):
            raise ValueError("unsupported observation schema")
        if schema == 2:
            points = [
                parse_time(row[k])
                for k in ("poll_started_at", "quotes_received_at", "bars_received_at", "updated_at")
            ]
            if points != sorted(points):
                raise ValueError("observation receipt times out of order")
        for quote in row["quotes"]:
            quotes[quote["state"]] += 1
            if "spread_bps" in quote:
                value = float(quote["spread_bps"])
                if not math.isfinite(value) or value < 0:
                    raise ValueError("invalid observed spread")
                spreads.append(value)
            if quote["state"] == "future":
                value = -float(quote["age_seconds"])
                if not math.isfinite(value) or value <= 0:
                    raise ValueError("invalid future quote age")
                future_leads.append(value)
        within_snapshot = set()
        for bar in row["bars"]:
            if trading_day(parse_time(bar["bar_start"])) != day:
                raise ValueError("bar outside observation day")
            key = (bar["symbol"], bar["bar_start"])
            if key in within_snapshot:
                raise ValueError("duplicate bar within snapshot")
            within_snapshot.add(key)
            count = bar["observed_minutes"]
            if not isinstance(count, int) or not 0 <= count <= 5:
                raise ValueError("invalid minute count")
            delta = bar["aggregate_minus_native"]
            if delta is not None and not all(
                math.isfinite(float(delta[k])) for k in ("open", "high", "low", "close", "volume")
            ):
                raise ValueError("nonfinite bar comparison")
            price_bad = delta is not None and any(
                abs(delta[k]) > 1e-6 for k in ("open", "high", "low", "close")
            )
            volume_bad = delta is not None and abs(delta["volume"]) > 1e-6
            price_checks += int(delta is not None)
            price_bad_count += int(price_bad)
            volume_bad_count += int(volume_bad)
            signature = json.dumps([count, bar["native_close"], delta], sort_keys=True, allow_nan=False)
            if key not in unique:
                unique[key] = {
                    "max_minutes": count,
                    "last_minutes": count,
                    "signatures": set(),
                    "price_bad": False,
                    "volume_bad": False,
                    "delay": bar["first_seen_delay_seconds"],
                    "cold": bar["cold_start"],
                }
            else:
                repeats += 1
            v = unique[key]
            v["max_minutes"], v["last_minutes"] = max(v["max_minutes"], count), count
            v["signatures"].add(signature)
            v["price_bad"] |= price_bad
            v["volume_bad"] |= volume_bad
    spacing = [(b - a).total_seconds() for a, b in zip(timestamps, timestamps[1:], strict=False)]
    return {
        "day": day,
        "snapshots": len(rows),
        "first_snapshot": timestamps[0].isoformat(),
        "last_snapshot": timestamps[-1].isoformat(),
        "snapshot_spacing_seconds": describe(spacing),
        "schema_counts": {str(k): v for k, v in schemas.items()},
        "quote_states": dict(quotes),
        "quote_future_lead_seconds": describe(future_leads),
        "iex_spread_bps": describe(spreads),
        "quote_age_basis": "receipt for schema2; poll start for schema1 (legacy future misclassification possible)",
        "bar_observations": sum(len(row["bars"]) for row in rows),
        "unique_symbol_bar_keys": len(unique),
        "repeat_bar_observations": repeats,
        "price_comparisons": price_checks,
        "price_mismatch_observations": price_bad_count,
        "volume_mismatch_observations": volume_bad_count,
        "unique_bars_ever_complete": sum(v["max_minutes"] == 5 for v in unique.values()),
        "unique_bars_never_complete": sum(v["max_minutes"] < 5 for v in unique.values()),
        "unique_bars_last_complete": sum(v["last_minutes"] == 5 for v in unique.values()),
        "unique_bars_price_mismatch": sum(v["price_bad"] for v in unique.values()),
        "unique_bars_volume_mismatch": sum(v["volume_bad"] for v in unique.values()),
        "unique_comparison_signatures_changed": sum(len(v["signatures"]) > 1 for v in unique.values()),
        "first_seen_delay_non_cold_seconds": describe([v["delay"] for v in unique.values() if not v["cold"]]),
        "cold_start_unique_bars": sum(v["cold"] for v in unique.values()),
        "is_independent_trade_sample": False,
        "is_nbbo_or_measured_execution_cost": False,
    }


def audit_file(settings, source, day):
    if source.is_symlink():
        raise ValueError("observation symlinks are not audit inputs")
    stat = source.stat()
    signature = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    destination = settings.report_dir / f"market-quality-{day}"
    report_path = destination / "report.json"
    if report_path.is_file():
        existing = json.loads(report_path.read_text())
        if existing["source_stat"] != signature and digest(source.read_bytes()) != existing["source_sha256"]:
            raise ValueError("completed observation source changed")
        return existing, False
    if destination.exists():
        raise ValueError("incomplete immutable quality report")
    data = source.read_bytes()
    after = source.stat()
    if signature != {"size": after.st_size, "mtime_ns": after.st_mtime_ns}:
        raise ValueError("observation source changed during audit")
    rows = [json.loads(line) for line in data.decode().splitlines() if line.strip()]
    report = {
        "decision": "ADR-0032/ADR-0034",
        "created_at": utcnow().isoformat(),
        "source": str(source),
        "source_stat": signature,
        "source_sha256": digest(data),
        "code_sha256": digest(Path(__file__).read_bytes()),
        "summary": summarize_observations(rows, day),
        "new_fits": 0,
        "new_replays": 0,
        "external_requests": 0,
        "performance_holdout_evaluated": False,
        "promoted": False,
    }
    settings.report_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=settings.report_dir, prefix=".market-quality-") as tmp:
        folder = Path(tmp) / "complete"
        folder.mkdir()
        (folder / "observations.jsonl").write_bytes(data)
        (folder / "market_quality.py").write_bytes(Path(__file__).read_bytes())
        (folder / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
        folder.rename(destination)
    return report, True


def audit_completed_sessions(settings, as_of_day):
    date.fromisoformat(as_of_day)
    reports, errors, created = [], [], 0
    for source in sorted((settings.data_dir / "observations").glob("market-*.jsonl")):
        match = re.fullmatch(r"market-(\d{4}-\d{2}-\d{2})\.jsonl", source.name)
        if not match or match[1] >= as_of_day:
            continue
        day = match[1]
        try:
            date.fromisoformat(day)
            report, new = audit_file(settings, source, day)
            created += int(new)
            reports.append(
                {
                    "day": day,
                    "report": str(settings.report_dir / f"market-quality-{day}" / "report.json"),
                    "source_sha256": report["source_sha256"],
                    "summary": report["summary"],
                }
            )
        except Exception as exc:
            errors.append({"day": day, "error": type(exc).__name__})
    return {
        "state": "partial"
        if errors and reports
        else "failed"
        if errors
        else "complete"
        if reports
        else "waiting_completed_session",
        "updated_at": utcnow().isoformat(),
        "as_of_day": as_of_day,
        "audited_sessions": len(reports),
        "new_reports": created,
        "latest": reports[-1] if reports else None,
        "errors": errors,
        "new_fits": 0,
        "orders_submitted": 0,
    }


def main():
    from stock_trader.config import Settings

    parser = argparse.ArgumentParser()
    parser.add_argument("--as-of-day", required=True)
    result = audit_completed_sessions(Settings(), parser.parse_args().as_of_day)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False), flush=True)


if __name__ == "__main__":
    main()
