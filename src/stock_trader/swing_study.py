"""ADR-0021: sealed-development daily signals, RTH execution, shared overnight capital.

Research only. This module cannot register a model or submit an Alpaca order.
"""

import argparse
import json
import math
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from stock_trader.clock import utcnow
from stock_trader.config import Settings
from stock_trader.modeling import (
    FAMILIES,
    STRATEGIES,
    checksum,
    make_regressor,
    predict_regressor,
    save_models,
)
from stock_trader.research import Metrics
from stock_trader.risk import RiskState, entry_plan, loss_reason
from stock_trader.study import model_configuration, write_json

DAILY_FEATURES = [
    *[f"ret_{n}" for n in (1, 3, 5, 10, 20, 60)],
    "volatility_20",
    "atr_pct",
    "ma_20",
    "ma_60",
    "relative_volume_20",
    "range_pct",
    "close_position",
    "open_gap",
    "market_ret_5",
    "market_ret_20",
    "market_ret_60",
    "market_volatility_20",
    "relative_ret_5",
    "relative_ret_20",
    "relative_ret_60",
]
PRICE_COLUMNS = ["open", "high", "low", "close"]


def split_factors(raw, actions):
    """One normalized share unit; exact events, never daily rounded adjustment ratios."""
    factors = pd.Series(1.0, index=raw.index)
    groups = actions.get("corporate_actions", {})
    for kind in ("forward_splits", "reverse_splits"):
        for event in groups.get(kind, []):
            old, new = float(event["old_rate"]), float(event["new_rate"])
            if old <= 0 or new <= 0 or not math.isfinite(old / new):
                raise ValueError("invalid corporate split rate")
            if kind == "reverse_splits":
                raise ValueError("reverse split fractional settlement is outside this research contract")
            mask = raw["symbol"].eq(event["symbol"]) & raw["date"].lt(event["ex_date"])
            factors.loc[mask] *= old / new
    return factors


def daily_features(raw, factors, dates, benchmarks):
    if raw.duplicated(["date", "symbol"]).any():
        raise ValueError("duplicate daily bars")
    source = raw.copy()
    source["factor"] = factors
    parts = []
    for symbol, group in source.groupby("symbol", sort=True):
        day = group.set_index("date").reindex(dates)
        day["symbol"] = symbol
        for column in PRICE_COLUMNS:
            day["normalized_" + column] = day[column] * day["factor"]
        close = day["normalized_close"]
        previous = close.shift(1)
        for n in (1, 3, 5, 10, 20, 60):
            complete = close.notna().rolling(n + 1).sum().eq(n + 1)
            day[f"ret_{n}"] = (close / close.shift(n) - 1).where(complete)
        day["volatility_20"] = day["ret_1"].rolling(20).std()
        true_range = (
            pd.concat(
                [
                    day["normalized_high"] - day["normalized_low"],
                    (day["normalized_high"] - previous).abs(),
                    (day["normalized_low"] - previous).abs(),
                ],
                axis=1,
            )
            .max(axis=1)
            .where(previous.notna())
        )
        day["normalized_atr"] = true_range.rolling(14).mean()
        day["atr_pct"] = day["normalized_atr"] / close
        for n in (20, 60):
            day[f"ma_{n}"] = close / close.rolling(n).mean() - 1
        normalized_volume = day["volume"] / day["factor"]
        day["relative_volume_20"] = normalized_volume / normalized_volume.rolling(20).mean()
        span = day["normalized_high"] - day["normalized_low"]
        day["range_pct"] = span / close
        day["close_position"] = (close - day["normalized_low"]) / span.replace(0, np.nan)
        day["open_gap"] = day["normalized_open"] / previous - 1
        parts.append(day.reset_index())
    frame = pd.concat(parts, ignore_index=True).sort_values(["date", "symbol"]).reset_index(drop=True)
    columns = ["ret_5", "ret_20", "ret_60", "volatility_20"]
    benchmark = frame.loc[frame["symbol"].isin(benchmarks)].groupby("date")[columns]
    market = benchmark.mean().where(benchmark.count().eq(len(benchmarks)))
    market = market.rename(columns={c: "market_" + c for c in columns})
    frame = frame.join(market, on="date")
    for n in (5, 20, 60):
        frame[f"relative_ret_{n}"] = frame[f"ret_{n}"] - frame[f"market_ret_{n}"]
    frame["feature_valid"] = np.isfinite(frame[DAILY_FEATURES].to_numpy(dtype=float)).all(axis=1)
    return frame


def load_inputs(settings, source):
    reference = json.loads((source / "report.json").read_text())
    if (
        reference["holdout_evaluated"]
        or reference["promoted"]
        or reference["settings"] != settings.public_contract()
    ):
        raise ValueError("registered unconsumed reference required")
    holdout = reference["holdout_start"]
    if holdout != "2026-06-16":
        raise ValueError("unregistered holdout boundary")
    directory = settings.data_dir / "research" / "swing-daily-development"
    native_dir = settings.data_dir / "research" / "native-iex-5m-development"
    collected = json.loads((directory / "collection.json").read_text())
    native = json.loads((native_dir / "collection.json").read_text())
    if (
        collected["contract"]["holdout_start"] != holdout
        or collected["contract"]["symbols"] != list(settings.symbols)
        or not collected["corporate_actions_available"]
        or native["contract"]["holdout_start"] != holdout
        or native["contract"]["symbols"] != list(settings.symbols)
        or collected["contract"]["feed"] != "iex"
        or native["contract"]["feed"] != "iex"
    ):
        raise ValueError("daily/native development input contract mismatch")
    hashes = {str(source / "report.json"): checksum(source / "report.json")}
    for root, collection in ((directory, collected), (native_dir, native)):
        for filename, expected in collection["file_sha256"].items():
            path = root / filename
            if (
                path.is_symlink()
                or not path.resolve().is_relative_to(root.resolve())
                or checksum(path) != expected
            ):
                raise ValueError("research input checksum mismatch")
            hashes[str(path)] = expected
        for name in ("manifest.json", "collection.json"):
            path = root / name
            if json.loads((root / "manifest.json").read_text()) != collection["contract"]:
                raise ValueError("collection manifest mismatch")
            hashes[str(path)] = checksum(path)
    calendar_path = native_dir / "calendar.json"
    hashes[str(calendar_path)] = checksum(calendar_path)
    calendar = json.loads(calendar_path.read_text())
    dates = [c["date"] for c in calendar]
    if dates != sorted(set(dates)) or not dates or dates[-1] >= holdout:
        raise ValueError("sealed or unordered calendar")
    raw = pd.read_parquet(directory / "raw.parquet").sort_values(["date", "symbol"]).reset_index(drop=True)
    split = (
        pd.read_parquet(directory / "split.parquet").sort_values(["date", "symbol"]).reset_index(drop=True)
    )
    keys = ["date", "symbol", "timestamp"]
    if not raw[keys].equals(split[keys]) or raw["date"].max() >= holdout or set(raw["date"]) != set(dates):
        raise ValueError("daily data keys or sealed boundary mismatch")
    actions = json.loads((directory / "corporate-actions.json").read_text())
    factors = split_factors(raw, actions)
    quality = {}
    for c in PRICE_COLUMNS:
        difference = (raw[c] * factors - split[c]).abs()
        quality[c] = float(difference.max())
        if difference.max() > 0.011:
            raise ValueError("split source disagrees with exact event factors")
    bars = pd.concat([pd.read_parquet(native_dir / p) for p in native["file_sha256"]], ignore_index=True)
    bars = bars.sort_values(["timestamp", "symbol"]).reset_index(drop=True)
    bars["date"] = bars["timestamp"].dt.tz_convert("America/New_York").dt.strftime("%Y-%m-%d")
    if bars["date"].max() >= holdout or bars.duplicated(["timestamp", "symbol"]).any():
        raise ValueError("sealed or duplicate intraday bars")
    scale = raw[["date", "symbol"]].copy()
    scale["factor"] = factors
    bars = bars.merge(scale, on=["date", "symbol"], validate="many_to_one")
    if bars["factor"].isna().any():
        raise ValueError("intraday bar lacks daily corporate factor")
    for c in PRICE_COLUMNS:
        bars[c] *= bars["factor"]
    openings, closings = {}, {}
    for c in calendar:
        for name, mapping in (("open", openings), ("close", closings)):
            mapping[c["date"]] = pd.Timestamp(c["date"] + " " + c[name], tz="America/New_York").tz_convert(
                "UTC"
            )
    if (
        ~bars["timestamp"].ge(bars["date"].map(openings)) | ~bars["timestamp"].lt(bars["date"].map(closings))
    ).any():
        raise ValueError("non-RTH bar in execution input")
    frame = daily_features(raw, factors, dates, settings.benchmark_symbols)
    return frame, bars, dates, openings, reference, hashes, quality


def normalized_plan(signal, factor, state, settings, prediction, opening_price=None, quote_spread_bps=8):
    """Raw integer sizing/cent prices in the entry day's actual share unit."""
    close = signal["normalized_close"] / factor
    if opening_price is not None:
        current = opening_price / factor
        if (
            not math.isfinite(current)
            or current <= 0
            or current * (1 + quote_spread_bps / 20_000) > close * 1.02
        ):
            return None
        close = current
    plan = entry_plan(
        symbol=signal["symbol"],
        bid=close * (1 - quote_spread_bps / 20_000),
        ask=close * (1 + quote_spread_bps / 20_000),
        atr=signal["normalized_atr"] / factor,
        state=state,
        settings=settings,
        expected_return=prediction,
    )
    if plan is None:
        return None
    return {
        "qty": int(plan["qty"]) / factor,
        "raw_qty": int(plan["qty"]),
        "entry": float(plan["limit_price"]) * factor,
        "stop": float(plan["stop_loss"]["stop_price"]) * factor,
        "take": float(plan["take_profit"]["limit_price"]) * factor,
    }


def protective_exit(row, stop, take):
    opening, high, low = row[0], row[1], row[2]
    if low <= stop:
        return min(stop, opening) * 0.9995, "stop", "intrabar"
    if high >= take:
        return take, "take", "intrabar"
    return None


def path_exit(row, stop, take):
    if row[0] <= stop:
        return row[0] * 0.9995, "stop_gap", "bar_open"
    if row[0] >= take:
        return take, "take_gap", "bar_open"
    return protective_exit(row, stop, take)


@dataclass
class SwingReplay:
    dates: list
    openings: dict
    batches: dict
    paths: dict


def prepare_swing(bars, dates, openings):
    paths = {}
    for symbol, group in bars.groupby("symbol", sort=False):
        paths[symbol] = {
            "time": group["timestamp"].dt.as_unit("ns").astype("int64").to_numpy(),
            "date": group["date"].to_numpy(),
            "values": group[[*PRICE_COLUMNS, "factor"]].to_numpy(),
            "observed_dates": set(group["date"]),
        }
    batches = {d: [] for d in dates}
    for timestamp, group in bars.groupby("timestamp", sort=True):
        rows = {
            r[0]: r[1:]
            for r in group[["symbol", *PRICE_COLUMNS, "factor"]].itertuples(index=False, name=None)
        }
        batches[group["date"].iloc[0]].append((timestamp, rows))
    return SwingReplay(dates, openings, batches, paths)


def observable_window(replay, symbol, entry_index, end_index):
    path = replay.paths.get(symbol)
    return path is not None and all(
        d in path["observed_dates"] for d in replay.dates[entry_index : end_index + 1]
    )


def swing_labels(frame, horizon, replay, settings, entry_reference="previous_close"):
    targets = pd.Series(np.nan, index=frame.index)
    end_dates = pd.Series("", index=frame.index, dtype=object)
    lookup = {d: i for i, d in enumerate(replay.dates)}
    capital = settings.capital_limit
    state = RiskState(capital, capital, capital, capital, 0, capital, 0)
    for index, signal in frame.loc[frame["feature_valid"]].iterrows():
        day = lookup[signal["date"]]
        if day + 1 + horizon >= len(replay.dates):
            continue
        entry_day, end = replay.dates[day + 1], replay.dates[day + 1 + horizon]
        end_dates.loc[index] = end
        path = replay.paths.get(signal["symbol"])
        if not observable_window(replay, signal["symbol"], day + 1, day + 1 + horizon):
            continue
        start = np.searchsorted(path["time"], replay.openings[entry_day].value)
        finish = np.searchsorted(path["time"], replay.openings[end].value)
        if (
            start >= len(path["time"])
            or path["time"][start] != replay.openings[entry_day].value
            or finish >= len(path["time"])
            or path["date"][finish] != end
        ):
            continue
        plan = normalized_plan(
            signal,
            path["values"][start, 4],
            state,
            settings,
            1.0,
            path["values"][start, 0] if entry_reference == "opening_quote" else None,
        )
        if plan is None or path["values"][start, 0] * 1.0005 > plan["entry"]:
            continue
        values = path["values"][start:finish]
        hits = np.flatnonzero((values[:, 2] <= plan["stop"]) | (values[:, 1] >= plan["take"]))
        if len(hits):
            exit_price = path_exit(values[hits[0]], plan["stop"], plan["take"])[0]
        else:
            exit_price = path["values"][finish, 0] * 0.9995
        targets.loc[index] = exit_price / plan["entry"] - 1
    return targets, end_dates


def purged_training(frame, target, end_dates, train_end, benchmarks):
    return (
        frame["date"].le(train_end)
        & frame["feature_valid"]
        & target.notna()
        & end_dates.ne("")
        & end_dates.le(train_end)
        & ~frame["symbol"].isin(benchmarks)
    )


def simulate_swing(
    frame,
    predictions,
    horizon,
    threshold,
    settings,
    replay,
    intervals,
    trades,
    entry_reference="previous_close",
    exit_policy="timed",
    diagnostics=None,
    rule_priority=None,
    future_path_filter=True,
    quote_spread_bps=8,
):
    if exit_policy not in ("timed", "barrier") or (exit_policy == "barrier" and diagnostics is None):
        raise ValueError("barrier replay requires explicit open-position diagnostics")
    if not math.isfinite(quote_spread_bps) or quote_spread_bps < 0:
        raise ValueError("invalid simulated spread")
    if not future_path_filter and diagnostics is None:
        raise ValueError("unfiltered paths require explicit diagnostics")
    capital = settings.capital_limit
    cash, peak, previous_close, max_dd = capital, capital, capital, 0.0
    positions, prices, last_exit = {}, {}, {}
    price_times, price_factors = {}, {}
    exposure_sum = 0.0
    exposure_samples = 0
    max_exposure = 0.0
    max_positions_seen = 0
    full_slots = 0
    permanent = False
    daily_returns = []
    rejected = 0
    observations = {d: part for d, part in frame.groupby("date")}
    date_index = {d: i for i, d in enumerate(replay.dates)}
    fee = settings.round_trip_cost_bps / 20_000
    active = sorted(d for d in replay.dates if any(a <= d <= b for a, b in intervals))

    def state(day_start):
        exposure = sum(p["qty"] * prices[s] for s, p in positions.items())
        return RiskState(capital, cash + exposure, day_start, peak, exposure, cash, len(positions))

    def close(symbol, price, timestamp, factor, reason, timing):
        nonlocal cash
        position = positions.pop(symbol)
        proceeds = position["qty"] * price * (1 - fee)
        net = proceeds - position["entry_cost"]
        gross = position["qty"] * (price - position["entry"])
        cash += proceeds
        last_exit[symbol] = timestamp
        trades.append(
            {
                "symbol": symbol,
                "signal_date": position["signal_date"],
                "entry_time": position["entry_time"].isoformat(),
                "exit_bar_start": timestamp.isoformat(),
                "exit_time": (
                    timestamp + pd.Timedelta(minutes=5) if timing == "bar_close" else timestamp
                ).isoformat(),
                "exit_timing": timing,
                "planned_exit_date": position["end"],
                "reason": reason,
                "prediction": position["prediction"],
                "normalized_qty": position["qty"],
                "entry_raw_qty": position["raw_qty"],
                "exit_raw_qty": position["qty"] * factor,
                "entry_raw_price": position["entry"] / position["entry_factor"],
                "exit_raw_price": price / factor,
                "entry_factor": position["entry_factor"],
                "exit_factor": factor,
                "normalized_entry": position["entry"],
                "normalized_exit": price,
                "gross_pnl": gross,
                "explicit_cost": gross - net,
                "net_pnl": net,
            }
        )

    for current_day in active:
        day_start = previous_close
        stopped = permanent
        signal_index = date_index[current_day] - 1
        signal_day = replay.dates[signal_index] if signal_index >= 0 else ""
        available = observations.get(signal_day)
        candidates = []
        if available is not None:
            for index, signal in available.loc[available["feature_valid"]].iterrows():
                prediction = predictions.get(index, np.nan)
                if (
                    signal["symbol"] not in settings.benchmark_symbols
                    and math.isfinite(prediction)
                    and prediction >= threshold
                ):
                    priority = prediction if rule_priority is None else rule_priority.get(index, np.nan)
                    if math.isfinite(priority):
                        candidates.append(
                            (float(priority), signal["symbol"], signal.to_dict(), float(prediction))
                        )
        candidates.sort(key=lambda c: (-c[0], c[1]))
        if not replay.batches[current_day] and positions:
            raise ValueError("held positions across an unobservable session")
        end_index = date_index[current_day] + horizon
        end = replay.dates[end_index] if end_index < len(replay.dates) else ""
        interval = next(((a, b) for a, b in intervals if a <= signal_day <= b), None)
        can_schedule = bool(end and interval and end <= interval[1])
        if (
            can_schedule
            and not stopped
            and all(t != replay.openings[current_day] for t, _ in replay.batches[current_day])
        ):
            rejected += sum(symbol not in positions for _, symbol, _, _ in candidates)
        for timestamp, rows in replay.batches[current_day]:
            # Mark the current OPEN before sizing or stops; current closes are still unknown.
            for symbol, row in rows.items():
                prices[symbol] = row[0]
            opening_state = state(day_start)
            peak = max(peak, opening_state.equity)
            max_dd = max(max_dd, 1 - opening_state.equity / peak)
            reason = loss_reason(state(day_start), settings)
            if reason:
                stopped = True
                permanent |= reason == "max_drawdown"
            for symbol, position in list(positions.items()):
                row = rows.get(symbol)
                if row is None:
                    continue
                if stopped or (position["end"] is not None and current_day >= position["end"]):
                    close(
                        symbol,
                        row[0] * 0.9995,
                        timestamp,
                        row[4],
                        reason or ("risk" if stopped else "time"),
                        "bar_open",
                    )
                elif row[0] <= position["stop"]:
                    close(symbol, row[0] * 0.9995, timestamp, row[4], "stop_gap", "bar_open")
                elif row[0] >= position["take"]:
                    close(symbol, position["take"], timestamp, row[4], "take_gap", "bar_open")
            if timestamp == replay.openings[current_day] and not stopped:
                if can_schedule:
                    for _, symbol, signal, prediction in candidates:
                        observation_end = date_index[active[-1]] if exit_policy == "barrier" else end_index
                        if future_path_filter and not observable_window(
                            replay, symbol, date_index[current_day], observation_end
                        ):
                            continue
                        if symbol in positions:
                            continue
                        if len(positions) >= settings.max_positions:
                            full_slots += 1
                            continue
                        if (
                            symbol in last_exit
                            and (timestamp - last_exit[symbol]).total_seconds()
                            < settings.reentry_cooldown_minutes * 60
                        ):
                            continue
                        row = rows.get(symbol)
                        if row is None:
                            rejected += 1
                            continue
                        plan = normalized_plan(
                            signal,
                            row[4],
                            state(day_start),
                            settings,
                            prediction,
                            row[0] if entry_reference == "opening_quote" else None,
                            quote_spread_bps,
                        )
                        if plan is None:
                            continue
                        if row[0] * 1.0005 > plan["entry"]:
                            rejected += 1
                            continue
                        cost = plan["qty"] * plan["entry"] * (1 + fee)
                        if cost > cash:
                            rejected += 1
                            continue
                        cash -= cost
                        positions[symbol] = {
                            **plan,
                            "entry_cost": cost,
                            "entry_time": timestamp,
                            "signal_date": signal_day,
                            "prediction": prediction,
                            "entry_factor": row[4],
                            "end": end if exit_policy == "timed" else None,
                        }
                        prices[symbol] = row[0]
            max_positions_seen = max(max_positions_seen, len(positions))
            # Intrabar protective orders are checked after all opening decisions.
            for symbol, position in list(positions.items()):
                row = rows.get(symbol)
                if row is not None:
                    exit_value = protective_exit(row, position["stop"], position["take"])
                    if exit_value:
                        close(symbol, exit_value[0], timestamp, row[4], exit_value[1], exit_value[2])
            for symbol, row in rows.items():
                prices[symbol] = row[3]
                price_times[symbol] = timestamp + pd.Timedelta(minutes=5)
                price_factors[symbol] = row[4]
            marked = state(day_start)
            peak = max(peak, marked.equity)
            max_dd = max(max_dd, 1 - marked.equity / peak)
            reason = loss_reason(state(day_start), settings)
            if reason or stopped:
                stopped = True
                permanent |= reason == "max_drawdown"
                for symbol in list(positions):
                    row = rows.get(symbol)
                    if row is not None:
                        close(symbol, row[3] * 0.9995, timestamp, row[4], reason or "risk", "bar_close")
                marked = state(day_start)
                max_dd = max(max_dd, 1 - marked.equity / peak)
            exposure_sum += marked.exposure / capital
            exposure_samples += 1
            max_exposure = max(max_exposure, marked.exposure / capital)
            max_positions_seen = max(max_positions_seen, len(positions))
        previous_close = state(day_start).equity
        daily_returns.append(previous_close / day_start - 1)
    if positions and exit_policy == "timed":
        raise ValueError("positions remain beyond a registered evaluation boundary")
    results = [t["net_pnl"] for t in trades]
    open_positions = []
    for symbol, position in positions.items():
        value = position["qty"] * prices[symbol] * 0.9995 * (1 - fee)
        open_positions.append(
            {
                "symbol": symbol,
                "entry_time": position["entry_time"].isoformat(),
                "holding_sessions": date_index[active[-1]]
                - date_index[position["entry_time"].tz_convert("America/New_York").strftime("%Y-%m-%d")],
                "normalized_qty": position["qty"],
                "raw_qty": position["qty"] * price_factors[symbol],
                "normalized_entry": position["entry"],
                "normalized_stop": position["stop"],
                "normalized_take": position["take"],
                "last_observed_price": prices[symbol],
                "last_observed_at": price_times[symbol].isoformat(),
                "entry_cost": position["entry_cost"],
                "marked_value_before_exit_cost": position["qty"] * prices[symbol],
                "estimated_liquidation_value": value,
                "unrealized_after_estimated_exit_cost": value - position["entry_cost"],
                "censored": True,
            }
        )
    terminal_equity = cash + sum(p["estimated_liquidation_value"] for p in open_positions)
    unrealized = sum(p["unrealized_after_estimated_exit_cost"] for p in open_positions)
    if not np.isclose(sum(results) + unrealized, terminal_equity - capital, atol=1e-6):
        raise ValueError("swing capital and trade ledger disagree")
    if open_positions:
        daily_returns[-1] = terminal_equity / day_start - 1
        max_dd = max(max_dd, 1 - terminal_equity / peak)
    if diagnostics is not None:
        diagnostics.update(
            {
                "exit_policy": exit_policy,
                "open_positions": open_positions,
                "realized_pnl": sum(results),
                "unrealized_after_estimated_exit_cost": unrealized,
                "terminal_equity": terminal_equity,
                "estimated_terminal_exit_cost": sum(
                    p["marked_value_before_exit_cost"] - p["estimated_liquidation_value"]
                    for p in open_positions
                ),
                "average_exposure": exposure_sum / max(exposure_samples, 1),
                "max_exposure": max_exposure,
                "max_positions": max_positions_seen,
                "skipped_candidates_full_slots": full_slots,
                "future_path_filter": future_path_filter,
                "quote_spread_bps": quote_spread_bps,
            }
        )
    return Metrics(
        terminal_equity / capital - 1,
        max_dd,
        len(results),
        sum(x > 0 for x in results) / max(len(results), 1),
        sum(max(x, 0) for x in results) / max(sum(max(-x, 0) for x in results), 1e-8),
        rejected,
        daily_returns,
    )


def trend_predictions(frame):
    # A sorting score, not a calibrated return forecast.
    eligible = frame["feature_valid"] & frame["ma_20"].gt(0) & frame["ret_20"].gt(0)
    return (1 + frame["relative_ret_20"].clip(-0.5, 0.5)).where(eligible)


def swing_eligible(metrics, folds):
    return (
        metrics.trades >= 60
        and metrics.net_return > 0
        and metrics.max_drawdown <= 0.08
        and metrics.profit_factor >= 1.1
        and sum(f.net_return > 0 for f in folds) >= 2
    )


def run_swing_study(settings, source, entry_reference="previous_close"):
    if entry_reference not in ("previous_close", "opening_quote"):
        raise ValueError("unknown swing entry price contract")
    started = time.monotonic()
    prefix = "swing-quote-study" if entry_reference == "opening_quote" else "swing-study"
    out = settings.report_dir / f"{prefix}-{utcnow().strftime('%Y%m%dT%H%M%SZ')}"
    out.mkdir()
    for name in ("source", "models", "predictions", "trades"):
        (out / name).mkdir()
    code = {}
    for name in (
        "swing_study.py",
        "daily_data.py",
        "modeling.py",
        "risk.py",
        "research.py",
        "study.py",
        "config.py",
    ):
        path = Path(__file__).with_name(name)
        (out / "source" / name).write_bytes(path.read_bytes())
        code[name] = checksum(path)

    def progress(stage, **fields):
        value = {"stage": stage, "updated_at": utcnow().isoformat(), **fields}
        write_json(out / "progress.json", value)
        print(json.dumps(value), flush=True)

    progress("inputs")
    frame, bars, dates, openings, reference, hashes, quality = load_inputs(settings, source)
    frame.to_parquet(out / "development-daily-features.parquet", index=False)
    progress("prepare_replay", daily_rows=len(frame), intraday_rows=len(bars))
    replay = prepare_swing(bars, dates, openings)
    del bars
    report = {
        "created_at": utcnow().isoformat(),
        "source": str(source),
        "decision": "ADR-0022" if entry_reference == "opening_quote" else "ADR-0021",
        "holdout_start": reference["holdout_start"],
        "holdout_evaluated": False,
        "promoted": False,
        "simulation_contract": "daily-opening-quote-swing-v1"
        if entry_reference == "opening_quote"
        else "daily-next-rth-open-swing-v1",
        "entry_reference": entry_reference,
        "settings": settings.public_contract(),
        "features": DAILY_FEATURES,
        "folds": reference["folds"],
        "rows": {
            "development": len(frame),
            "valid": int(frame["feature_valid"].sum()),
            "sessions": len(dates),
        },
        "split_rounding_max_abs_difference": quality,
        "model_configuration": {
            f: model_configuration(make_regressor(f, settings.training_threads)) for f in FAMILIES
        },
        "input_sha256": hashes,
        "code_sha256": code,
        "fits": [],
        "candidates": [],
        "baselines": [],
        "assumptions": [
            "IEX-only prices",
            "no dividend credit",
            "opening OHLC execution proxy",
            "missing opening bar means unfilled",
            "unobserved holdings marks remain stale",
            "stop priority when intrabar order is ambiguous",
            "fixed 20bps round-trip modeled fee",
            "future execution-data availability restricts research sample, not an executable trading filter",
        ],
        "unobserved_calendar_sessions": [d for d in dates if not replay.batches[d]],
        "unobservable_windows": {},
    }
    intervals = [(f["validation_start"], f["validation_end"]) for f in reference["folds"]]
    validation = frame.loc[frame["date"] >= intervals[0][0]]
    for horizon in (2, 5, 10):
        progress("labels", holding_days=horizon)
        target, end_dates = swing_labels(frame, horizon, replay, settings, entry_reference)
        report["unobservable_windows"][str(horizon)] = sum(
            not observable_window(
                replay, row.symbol, dates.index(row.date) + 1, dates.index(row.date) + 1 + horizon
            )
            for row in frame.loc[
                frame["feature_valid"] & ~frame["symbol"].isin(settings.benchmark_symbols)
            ].itertuples()
            if dates.index(row.date) + 1 + horizon < len(dates)
        )
        oof = {f: pd.Series(np.nan, index=frame.index) for f in FAMILIES}
        for fold, spec in enumerate(reference["folds"], start=1):
            mask = purged_training(frame, target, end_dates, spec["train_end"], settings.benchmark_symbols)
            unpurged = (
                frame["date"].le(spec["train_end"])
                & frame["feature_valid"]
                & target.notna()
                & ~frame["symbol"].isin(settings.benchmark_symbols)
            )
            valid = (
                frame["date"].between(spec["validation_start"], spec["validation_end"])
                & frame["feature_valid"]
            )
            if mask.sum() < 1500:
                raise ValueError("insufficient purged daily training rows")
            for family in FAMILIES:
                progress("fit", holding_days=horizon, fold=fold, family=family, training_rows=int(mask.sum()))
                model = make_regressor(family, settings.training_threads)
                model.fit(frame.loc[mask, DAILY_FEATURES], target.loc[mask])
                oof[family].loc[valid] = predict_regressor(model, frame.loc[valid, DAILY_FEATURES])
                directory = out / "models" / f"{horizon}-{fold}-{family}"
                directory.mkdir()
                report["fits"].append(
                    {
                        "family": family,
                        "holding_days": horizon,
                        "fold": fold,
                        "training_rows": int(mask.sum()),
                        "purged_rows": int(unpurged.sum() - mask.sum()),
                        "max_label_end": end_dates.loc[mask].max(),
                        "validation_rows": int(valid.sum()),
                        "members": save_models({family: model}, directory),
                    }
                )
                del model
        oof["ensemble"] = (oof["lgbm"] + oof["xgb"] + oof["catboost"]) / 3
        saved = validation[["date", "symbol", "feature_valid"]].copy()
        saved["label"] = target.loc[validation.index]
        saved["label_planned_end"] = end_dates.loc[validation.index]
        for family in STRATEGIES:
            saved[family] = oof[family].loc[validation.index]
        saved.to_parquet(out / "predictions" / f"{horizon}.parquet", index=False)
        for family in STRATEGIES:
            for threshold in (0.005, 0.010, 0.015):
                progress(
                    "evaluate",
                    holding_days=horizon,
                    family=family,
                    threshold=threshold,
                    completed=len(report["candidates"]),
                    total=36,
                )
                ledger = []
                metrics = simulate_swing(
                    frame,
                    oof[family],
                    horizon,
                    threshold,
                    settings,
                    replay,
                    intervals,
                    ledger,
                    entry_reference,
                )
                folds = [
                    simulate_swing(
                        frame,
                        oof[family],
                        horizon,
                        threshold,
                        settings,
                        replay,
                        [interval],
                        [],
                        entry_reference,
                    )
                    for interval in intervals
                ]
                filename = f"{family}-{horizon}-{threshold:.3f}.json"
                write_json(out / "trades" / filename, ledger)
                eligible = swing_eligible(metrics, folds)
                report["candidates"].append(
                    {
                        "family": family,
                        "holding_days": horizon,
                        "threshold": threshold,
                        "metrics": asdict(metrics),
                        "folds": [asdict(f) for f in folds],
                        "trade_file": filename,
                        "eligible": eligible,
                        "ledger_net_return": sum(t["net_pnl"] for t in ledger) / settings.capital_limit,
                        "score": metrics.net_return - 0.5 * metrics.max_drawdown,
                    }
                )
        if entry_reference == "opening_quote":
            progress("baseline", holding_days=horizon)
            scores = trend_predictions(frame).where(frame["date"].ge(intervals[0][0]))
            ledger = []
            metrics = simulate_swing(
                frame, scores, horizon, 0.005, settings, replay, intervals, ledger, entry_reference
            )
            folds = [
                simulate_swing(
                    frame, scores, horizon, 0.005, settings, replay, [interval], [], entry_reference
                )
                for interval in intervals
            ]
            filename = f"trend-baseline-{horizon}.json"
            write_json(out / "trades" / filename, ledger)
            report["baselines"].append(
                {
                    "family": "trend_rule",
                    "holding_days": horizon,
                    "metrics": asdict(metrics),
                    "folds": [asdict(f) for f in folds],
                    "eligible": swing_eligible(metrics, folds),
                    "trade_file": filename,
                    "score": metrics.net_return - 0.5 * metrics.max_drawdown,
                }
            )
        write_json(out / "report.json", report)
    report["completed_at"] = utcnow().isoformat()
    report["elapsed_seconds"] = time.monotonic() - started
    write_json(out / "report.json", report)
    progress(
        "complete",
        fits=len(report["fits"]),
        candidates=len(report["candidates"]),
        eligible=sum(c["eligible"] for c in report["candidates"]),
    )
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument(
        "--entry-reference", choices=("previous_close", "opening_quote"), default="previous_close"
    )
    args = parser.parse_args()
    run_swing_study(Settings(), args.source, args.entry_reference)


if __name__ == "__main__":
    main()
