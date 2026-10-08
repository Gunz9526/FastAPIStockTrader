import json
import os
import time
from dataclasses import asdict, dataclass
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from stock_trader.clock import parse_time, utcnow
from stock_trader.config import Settings
from stock_trader.features import FEATURE_VERSION, FEATURES, feature_frame, labels
from stock_trader.modeling import (
    FAMILIES,
    STRATEGIES,
    NativeBundle,
    checksum,
    make_regressor,
    predict_regressor,
    save_models,
)
from stock_trader.risk import RiskState, entry_plan, loss_reason


@dataclass
class Metrics:
    net_return: float
    max_drawdown: float
    trades: int
    win_rate: float
    profit_factor: float
    rejected_limits: int
    daily_returns: list[float]


@dataclass
class Replay:
    index: pd.Index
    day_end: pd.Series
    batches: list


def prepare_replay(frame: pd.DataFrame) -> Replay:
    columns = ["symbol", "open", "high", "low", "close", "atr", "ma_distance", "relative_strength"]
    batches = []
    for timestamp, batch in frame.groupby("timestamp", sort=True):
        rows = {}
        for values in batch[columns].itertuples(index=True, name=None):
            row = dict(zip(columns, values[1:], strict=True))
            row["_index"] = values[0]
            rows[row["symbol"]] = row
        batches.append((timestamp, batch["date"].iloc[0], rows))
    return Replay(
        frame.index,
        frame.groupby("date")["timestamp"].max() + pd.Timedelta(minutes=5),
        batches,
    )


def simulate(
    frame: pd.DataFrame,
    predictions: pd.Series,
    *,
    threshold: float,
    horizon: int,
    settings: Settings,
    replay: Replay | None = None,
    execution: str = "legacy",
    trades: list | None = None,
    rule_signals: pd.Series | None = None,
    rule_priority: pd.Series | None = None,
    quote_spread_bps: float = 8.0,
) -> Metrics:
    """Conservative OHLC portfolio simulation, next-bar limit entry, shared capital and risk rules."""
    capital = settings.capital_limit
    if execution not in ("legacy", "timed_open"):
        raise ValueError("unknown execution contract")
    if not np.isfinite(quote_spread_bps) or quote_spread_bps < 0:
        raise ValueError("invalid simulated spread")
    if rule_priority is not None and rule_signals is None:
        raise ValueError("rule priority requires explicit rule signals")
    cash = capital
    peak = capital
    max_dd = 0.0
    positions = {}
    pending = {}
    results = []
    day_returns = []
    last_prices = {}
    day = None
    day_start = capital
    stopped = False
    permanent_stop = False
    rejected = 0
    last_exit = {}

    def close_position(symbol, position, exit_price, timestamp, reason, timing, fee_divisor=20_000):
        nonlocal cash
        proceeds = position["qty"] * exit_price * (1 - settings.round_trip_cost_bps / fee_divisor)
        net = proceeds - position["entry_cost"]
        cash += proceeds
        results.append(net)
        last_exit[symbol] = timestamp
        positions.pop(symbol)
        if trades is not None:
            gross = position["qty"] * (exit_price - position["entry"])
            trades.append(
                {
                    "symbol": symbol,
                    "signal_time": position["signal_time"].isoformat(),
                    "entry_time": position["entry_time"].isoformat(),
                    "exit_bar_start": timestamp.isoformat(),
                    "exit_time": (
                        timestamp + pd.Timedelta(minutes=5) if timing == "bar_close" else timestamp
                    ).isoformat(),
                    "exit_timing": timing,
                    "prediction": position["prediction"],
                    "qty": position["qty"],
                    "entry_price": position["entry"],
                    "exit_price": exit_price,
                    "stop_price": position["stop"],
                    "take_price": position["take"],
                    "reason": reason,
                    "gross_pnl": gross,
                    "explicit_cost": gross - net,
                    "net_pnl": net,
                    "gross_return": exit_price / position["entry"] - 1,
                    "missing_path_bars": position["missing_path_bars"],
                }
            )

    replay = replay or prepare_replay(frame)
    if not replay.index.equals(frame.index):
        raise ValueError("replay belongs to a different time interval")
    prediction_values = predictions.reindex(replay.index).to_dict()
    signal_values = (
        rule_signals.reindex(replay.index).fillna(False).astype(bool).to_dict()
        if rule_signals is not None
        else None
    )
    priority_values = (
        rule_priority.reindex(replay.index).fillna(-np.inf).to_dict()
        if rule_priority is not None
        else prediction_values
    )
    # Session close is supplied by calendar-aware research input; fallback is a conservative early close.
    day_end = replay.day_end
    for timestamp, current_day, prepared_rows in replay.batches:
        if current_day != day:
            if day is not None:
                day_returns.append(cash / day_start - 1)
            day = current_day
            day_start = cash
            stopped = permanent_stop
            pending.clear()
        rows = {
            symbol: {**row, "prediction": prediction_values[row["_index"]]}
            for symbol, row in prepared_rows.items()
        }
        for symbol, row in rows.items():
            last_prices[symbol] = row["close"]
        for symbol, intent in list(pending.items()):
            if timestamp != intent["time"]:
                if timestamp > intent["time"]:
                    pending.pop(symbol)
                    rejected += 1
                continue
            row = rows.get(symbol)
            pending.pop(symbol)
            if row is None or row["open"] > float(intent["plan"]["limit_price"]):
                rejected += 1
                continue
            plan = intent["plan"]
            fill = min(float(plan["limit_price"]), row["open"] * 1.0005)
            qty = int(plan["qty"])
            fee = settings.round_trip_cost_bps / 20_000
            if fill <= float(plan["stop_loss"]["stop_price"]) or cash < qty * fill * (1 + fee):
                rejected += 1
                continue
            cash -= qty * fill * (1 + fee)
            positions[symbol] = {
                "qty": qty,
                "entry": fill,
                "entry_cost": qty * fill * (1 + fee),
                "stop": float(plan["stop_loss"]["stop_price"]),
                "take": float(plan["take_profit"]["limit_price"]),
                "until": timestamp + pd.Timedelta(minutes=horizon * 5),
                "entry_time": timestamp,
                "signal_time": intent["signal_time"],
                "prediction": intent["prediction"],
                "missing_path_bars": 0,
                "last_path_time": timestamp - pd.Timedelta(minutes=5),
            }
        exposure = sum(p["qty"] * last_prices.get(s, p["entry"]) for s, p in positions.items())
        equity = cash + exposure
        peak = max(peak, equity)
        max_dd = max(max_dd, 1 - equity / peak)
        risk = RiskState(min(capital, equity), equity, day_start, peak, exposure, cash, len(positions))
        reason = loss_reason(risk, settings)
        if reason:
            stopped = True
            permanent_stop |= reason in ("max_drawdown", "invalid_equity")
            pending.clear()
        close_time = day_end[current_day]
        for symbol, position in list(positions.items()):
            row = rows.get(symbol)
            skipped = int((timestamp - position["last_path_time"]) / pd.Timedelta(minutes=5)) - 1
            position["missing_path_bars"] += max(0, skipped) + int(row is None)
            position["last_path_time"] = timestamp
            if row is None:
                continue
            exit_price = None
            reason, timing = None, "intrabar_unknown"
            if execution == "timed_open" and (
                timestamp >= position["until"] or timestamp >= close_time - pd.Timedelta(minutes=10)
            ):
                exit_price = row["open"] * 0.9995
                reason = "day_close" if timestamp >= close_time - pd.Timedelta(minutes=10) else "time"
                timing = "bar_open"
            # If both barriers occur inside one bar, choose the stop first.
            elif row["low"] <= position["stop"]:
                exit_price = min(position["stop"], row["open"]) * 0.9995
                reason = "stop"
            elif row["high"] >= position["take"]:
                exit_price = position["take"]
                reason = "take"
            elif (
                timestamp >= position["until"]
                or timestamp >= close_time - pd.Timedelta(minutes=10)
                or stopped
            ):
                exit_price = row["close"] * 0.9995
                reason = (
                    "risk"
                    if stopped
                    else ("day_close" if timestamp >= close_time - pd.Timedelta(minutes=10) else "time")
                )
                timing = "bar_close"
            if exit_price is not None:
                close_position(symbol, position, exit_price, timestamp, reason, timing)
        # Positions lacking bars at closing time receive a conservative last-price liquidation.
        if timestamp >= close_time - pd.Timedelta(minutes=5):
            for symbol, position in list(positions.items()):
                close_position(
                    symbol, position, last_prices[symbol], timestamp, "sparse_close", "last_mark", 10_000
                )
        exposure = sum(p["qty"] * last_prices[s] for s, p in positions.items())
        equity = cash + exposure
        peak = max(peak, equity)
        max_dd = max(max_dd, 1 - equity / peak)
        if stopped or timestamp >= close_time - pd.Timedelta(minutes=30):
            continue
        if signal_values is None and timestamp.minute % 15 != 10:
            continue
        ranked = (
            sorted(rows.items(), key=lambda item: (-priority_values[item[1]["_index"]], item[0]))
            if rule_priority is not None
            else sorted(rows.items(), key=lambda item: item[1]["prediction"], reverse=True)
        )
        for symbol, row in ranked:
            if (
                symbol in settings.benchmark_symbols
                or symbol in positions
                or symbol in pending
                or row["prediction"] < threshold
                or (signal_values is not None and not signal_values[row["_index"]])
                or (signal_values is None and (row["ma_distance"] <= 0 or row["relative_strength"] <= 0))
            ):
                continue
            if pd.isna(row["prediction"]) or (
                symbol in last_exit
                and timestamp - last_exit[symbol] < pd.Timedelta(minutes=settings.reentry_cooldown_minutes)
            ):
                continue
            reserved = sum(int(p["plan"]["qty"]) * float(p["plan"]["limit_price"]) for p in pending.values())
            risk = RiskState(
                min(capital, equity),
                equity,
                day_start,
                peak,
                exposure + reserved,
                cash - reserved,
                len(positions) + len(pending),
            )
            plan = entry_plan(
                symbol=symbol,
                bid=row["close"] * (1 - quote_spread_bps / 20_000),
                ask=row["close"] * (1 + quote_spread_bps / 20_000),
                atr=row["atr"],
                state=risk,
                settings=settings,
                expected_return=row["prediction"],
            )
            if plan:
                pending[symbol] = {
                    "plan": plan,
                    "time": timestamp + pd.Timedelta(minutes=5),
                    "signal_time": timestamp,
                    "prediction": row["prediction"],
                }
    if day is not None:
        # Force remaining marks into a final conservative liquidation, including sparse symbols.
        for symbol, position in list(positions.items()):
            close_position(
                symbol,
                position,
                last_prices[symbol],
                replay.batches[-1][0],
                "final_close",
                "last_mark",
                10_000,
            )
        day_returns.append(cash / day_start - 1)
    gains = sum(p for p in results if p > 0)
    losses = -sum(p for p in results if p < 0)
    return Metrics(
        cash / capital - 1,
        max_dd,
        len(results),
        sum(p > 0 for p in results) / max(1, len(results)),
        gains / max(losses, 0.01),
        rejected,
        day_returns,
    )


def model_contract(settings: Settings) -> dict:
    return {
        "simulation_contract": "timed-open-v2",
        "feature_version": FEATURE_VERSION,
        "features": FEATURES,
        "feed": "iex",
        "symbols": list(settings.symbols),
        "stop_atr": settings.stop_atr,
        "reward_risk": settings.reward_risk,
        "trade_risk": settings.trade_risk,
        "daily_loss_limit": settings.daily_loss_limit,
        "capital_limit": settings.capital_limit,
        "gross_exposure_limit": settings.gross_exposure_limit,
        "symbol_exposure_limit": settings.symbol_exposure_limit,
        "max_positions": settings.max_positions,
        "max_drawdown": settings.max_drawdown,
        "max_spread": settings.max_spread,
        "reentry_cooldown_minutes": settings.reentry_cooldown_minutes,
        "round_trip_cost_bps": settings.round_trip_cost_bps,
    }


def fit_model(frame, target, settings, family="lgbm"):
    valid = target.notna() & frame["feature_valid"] & ~frame["symbol"].isin(settings.benchmark_symbols)
    if valid.sum() < 5000:
        raise ValueError("insufficient eligible training rows")
    model = make_regressor(family, settings.training_threads)
    model.fit(frame.loc[valid, FEATURES], target.loc[valid])
    return model


def predict_available(model, frame):
    result = pd.Series(np.nan, index=frame.index)
    valid = frame["feature_valid"]
    if valid.any():
        result.loc[valid] = predict_regressor(model, frame.loc[valid, FEATURES])
    return result


def train_and_evaluate(bars: pd.DataFrame, settings: Settings, progress=None) -> dict:
    def emit(stage, **details):
        if progress:
            progress({"state": "training", "stage": stage, "updated_at": utcnow().isoformat(), **details})

    emit("features", rows=len(bars))
    if pd.to_datetime(bars["timestamp"], utc=True).max() > pd.Timestamp(utcnow()):
        raise ValueError("future historical data rejected")
    # Retain ALL raw bars for protective exit simulation, including invalid signal lookbacks.
    frame = feature_frame(bars, settings.benchmark_symbols, include_invalid=True)
    dates = sorted(frame["date"].unique())
    if len(dates) < 180:
        raise ValueError("need at least 180 trading sessions for chronological validation")
    test_start = int(len(dates) * 0.85)
    validation_start = int(len(dates) * 0.55)
    splits = np.linspace(validation_start, test_start, 4, dtype=int)
    candidates = []
    diagnostics = []
    validation = frame.loc[frame["date"].isin(dates[validation_start:test_start])]
    emit("prepare_replay", rows=len(validation))
    validation_replay = prepare_replay(validation)
    fold_frames = [
        validation.loc[validation["date"].isin(dates[a:b])]
        for a, b in zip(splits[:-1], splits[1:], strict=True)
    ]
    fold_replays = [prepare_replay(subset) for subset in fold_frames]
    for horizon in (6, 12, 24):
        target = labels(frame, horizon)
        oof = {family: pd.Series(np.nan, index=frame.index) for family in FAMILIES}
        timings = {family: 0.0 for family in FAMILIES}
        for fold, (a, b) in enumerate(zip(splits[:-1], splits[1:], strict=True), start=1):
            train = frame.loc[frame["date"] < dates[a]]
            validation = frame.loc[frame["date"].isin(dates[a:b])]
            # Whole-session split means the intraday labels cannot overlap split boundaries.
            for family in FAMILIES:
                emit(
                    "fit",
                    family=family,
                    horizon_minutes=horizon * 5,
                    fold=fold,
                    folds=3,
                    training_rows=len(train),
                    validation_rows=len(validation),
                )
                started = time.monotonic()
                model = fit_model(train, target.loc[train.index], settings, family)
                oof[family].loc[validation.index] = predict_available(model, validation)
                timings[family] += time.monotonic() - started
                del model
        # No learned weight search: use only strictly time-separated OOF predictions.
        oof["ensemble"] = (oof["lgbm"] + oof["xgb"] + oof["catboost"]) / 3
        validation = frame.loc[frame["date"].isin(dates[validation_start:test_start])]
        residuals = pd.DataFrame({f: oof[f] - target for f in FAMILIES}).dropna()
        diagnostics.append(
            {
                "horizon_minutes": horizon * 5,
                "fit_and_predict_seconds": timings,
                "oof_rmse": {
                    f: float(
                        np.sqrt(np.mean((oof[f].loc[residuals.index] - target.loc[residuals.index]) ** 2))
                    )
                    if len(residuals)
                    else None
                    for f in STRATEGIES
                },
                "oof_residual_correlation": residuals.corr().fillna(0).to_dict(),
            }
        )
        for family in STRATEGIES:
            for threshold in (0.003, 0.0045, 0.006):
                emit(
                    "portfolio_validation",
                    family=family,
                    horizon_minutes=horizon * 5,
                    threshold=threshold,
                    candidates_completed=len(candidates),
                    candidates_total=36,
                )
                metrics = simulate(
                    validation,
                    oof[family],
                    threshold=threshold,
                    horizon=horizon,
                    settings=settings,
                    replay=validation_replay,
                    execution="timed_open",
                )
                folds = []
                for subset, replay in zip(fold_frames, fold_replays, strict=True):
                    folds.append(
                        asdict(
                            simulate(
                                subset,
                                oof[family],
                                threshold=threshold,
                                horizon=horizon,
                                settings=settings,
                                replay=replay,
                                execution="timed_open",
                            )
                        )
                    )
                eligible = (
                    metrics.trades >= 60
                    and metrics.net_return > 0
                    and metrics.max_drawdown <= 0.08
                    and metrics.profit_factor >= 1.1
                    and sum(f["net_return"] > 0 for f in folds) >= 2
                )
                candidates.append(
                    {
                        "family": family,
                        "horizon": horizon,
                        "threshold": threshold,
                        "metrics": asdict(metrics),
                        "folds": folds,
                        "eligible": eligible,
                        "score": metrics.net_return - 0.5 * metrics.max_drawdown,
                    }
                )
    eligible = [c for c in candidates if c["eligible"]]
    report = {
        "created_at": utcnow().isoformat(),
        "data_start": dates[0],
        "data_end": dates[-1],
        "test_start": dates[test_start],
        "candidates": candidates,
        "model_diagnostics": diagnostics,
        "candidate_policy": "cpu-three-models-equal-average-v1",
        "simulation_contract": "timed-open-v2",
        "accepted": False,
        "limitations": [
            "Fixed contemporary universe: survivorship bias remains.",
            "OHLC fills and IEX spreads are assumptions, not NBBO executions.",
            "Historical revised bars cannot fully reproduce live information availability.",
        ],
    }
    if not eligible:
        report["reason"] = "no validation candidate passed; trading stays disabled"
        save_report(report, settings)
        return report
    selected = max(eligible, key=lambda c: c["score"])
    train = frame.loc[frame["date"] < dates[test_start]]
    test = frame.loc[frame["date"] >= dates[test_start]]
    target = labels(frame, selected["horizon"])
    models = {}
    families = FAMILIES if selected["family"] == "ensemble" else (selected["family"],)
    predictions = pd.Series(0.0, index=test.index)
    for family in families:
        emit("selected_fit", family=family, horizon_minutes=selected["horizon"] * 5)
        models[family] = fit_model(train, target.loc[train.index], settings, family)
        predictions += predict_available(models[family], test) / len(families)
    emit("untouched_test", family=selected["family"])
    test_replay = prepare_replay(test)
    metrics = simulate(
        test,
        predictions,
        threshold=selected["threshold"],
        horizon=selected["horizon"],
        settings=settings,
        replay=test_replay,
        execution="timed_open",
    )
    stress_settings = settings.model_copy(update={"round_trip_cost_bps": settings.round_trip_cost_bps * 1.5})
    stress = simulate(
        test,
        predictions,
        threshold=selected["threshold"],
        horizon=selected["horizon"],
        settings=stress_settings,
        replay=test_replay,
        execution="timed_open",
    )
    baseline = pd.Series(
        np.where((test["ret_3"] > 0) & (test["relative_strength"] > 0), 0.006, 0), index=test.index
    )
    baseline_metrics = simulate(
        test,
        baseline,
        threshold=0.0045,
        horizon=12,
        settings=settings,
        replay=test_replay,
        execution="timed_open",
    )
    report.update(
        selected=selected,
        test_metrics=asdict(metrics),
        stress_metrics=asdict(stress),
        baseline_metrics=asdict(baseline_metrics),
    )
    accepted = (
        metrics.trades >= 20
        and metrics.net_return > 0
        and metrics.max_drawdown <= 0.08
        and metrics.profit_factor >= 1.1
        and stress.net_return > 0
        and metrics.net_return > baseline_metrics.net_return
    )
    report["accepted"] = accepted
    if accepted:
        emit("before_promotion", family=selected["family"])
        # Freeze the model tested on the untouched holdout. Do not refit and claim that artifact was tested.
        version = utcnow().strftime("%Y%m%dT%H%M%SZ")
        directory = settings.artifact_dir / version
        directory.mkdir(parents=True, exist_ok=False)
        members = save_models(models, directory)
        metadata = {
            "format_version": 2,
            "version": version,
            "created_at": utcnow().isoformat(),
            "accepted": True,
            "trained_until": dates[test_start - 1],
            "validated_until": dates[-1],
            "threshold": selected["threshold"],
            "horizon": selected["horizon"],
            "contract": model_contract(settings),
            "report": report,
            "family": selected["family"],
            "members": members,
            "weights": [1 / len(members)] * len(members),
        }
        (directory / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2))
        temp = settings.artifact_dir / "current.tmp"
        temp.write_text(version)
        os.replace(temp, settings.artifact_dir / "current")
        report["version"] = version
    else:
        report["reason"] = "selected candidate failed untouched test or cost stress; previous model retained"
    save_report(report, settings)
    return report


def save_report(report: dict, settings: Settings):
    settings.report_dir.mkdir(parents=True, exist_ok=True)
    path = settings.report_dir / f"research-{utcnow().strftime('%Y%m%dT%H%M%S%f')}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))


class ActiveModel:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.metadata = None
        self.model = None

    def refresh(self):
        pointer = self.settings.artifact_dir / "current"
        if not pointer.exists():
            raise ValueError("no validated model")
        version = pointer.read_text().strip()
        if not version or Path(version).name != version:
            raise ValueError("invalid model pointer")
        directory = self.settings.artifact_dir / version
        metadata = json.loads((directory / "metadata.json").read_text())
        if metadata.get("accepted") is not True or metadata["contract"] != model_contract(self.settings):
            raise ValueError("model contract mismatch")
        if utcnow() - parse_time(metadata["created_at"]) > timedelta(days=self.settings.model_max_age_days):
            raise ValueError("model expired")
        if metadata.get("format_version", 1) == 1:
            members = [{"family": "lgbm", "file": "model.txt", "sha256": metadata["sha256"]}]
            weights = [1.0]
        elif metadata["format_version"] == 2:
            members, weights = metadata["members"], metadata["weights"]
            if not members:
                raise ValueError("empty model bundle")
            families = [m["family"] for m in members]
            family = metadata["family"]
            if (
                (family == "ensemble" and set(families) != set(FAMILIES))
                or (family != "ensemble" and families != [family])
                or not np.allclose(weights, [1 / len(members)] * len(members))
            ):
                raise ValueError("model selection contract mismatch")
        else:
            raise ValueError("unsupported model format")
        # Validate checksums every refresh, including already loaded models.
        for member in members:
            path = directory / member["file"]
            if (
                Path(member["file"]).name != member["file"]
                or path.is_symlink()
                or checksum(path) != member["sha256"]
            ):
                raise ValueError("model checksum mismatch")
        if not self.metadata or self.metadata["version"] != version:
            self.model = NativeBundle(directory, members, weights, self.settings.training_threads)
            self.metadata = metadata

    def predict(self, frame: pd.DataFrame) -> np.ndarray:
        if self.model is None:
            raise ValueError("model unavailable")
        output = self.model.predict(frame[FEATURES])
        if not np.isfinite(output).all():
            raise ValueError("nonfinite predictions")
        return output
