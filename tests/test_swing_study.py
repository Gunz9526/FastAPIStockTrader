import numpy as np
import pandas as pd
import pytest

from stock_trader.risk import RiskState
from stock_trader.swing_study import (
    DAILY_FEATURES,
    daily_features,
    load_inputs,
    normalized_plan,
    path_exit,
    prepare_swing,
    protective_exit,
    purged_training,
    simulate_swing,
    split_factors,
    swing_labels,
    trend_predictions,
)


def fixture_path(symbols=("AAPL",), split=False, missing_open=False):
    dates = ["2026-01-08", "2026-01-09", "2026-01-12", "2026-01-13", "2026-01-14"]
    openings = {d: pd.Timestamp(d + " 14:30:00Z") for d in dates}
    daily, bars = [], []
    for day, date in enumerate(dates):
        for symbol in symbols:
            factor = 0.1 if split and day < 2 else 1.0
            daily.append(
                {
                    "date": date,
                    "symbol": symbol,
                    "normalized_close": 100.0,
                    "normalized_atr": 2.0,
                    "feature_valid": True,
                    "factor": factor,
                }
            )
            for minute in (0, 5):
                if missing_open and day == 1 and minute == 0:
                    continue
                bars.append(
                    {
                        "timestamp": openings[date] + pd.Timedelta(minutes=minute),
                        "date": date,
                        "symbol": symbol,
                        "factor": factor,
                        "open": 100.0,
                        "high": 101.0,
                        "low": 99.0,
                        "close": 100.0,
                    }
                )
    frame = pd.DataFrame(daily)
    replay = prepare_swing(pd.DataFrame(bars), dates, openings)
    return frame, replay


def run_fixture(settings, frame, replay, horizon=2, intervals=None):
    predictions = pd.Series(np.nan, index=frame.index)
    predictions.loc[frame["date"].eq(replay.dates[0])] = 0.03
    ledger = []
    metrics = simulate_swing(
        frame,
        predictions,
        horizon,
        0.005,
        settings,
        replay,
        intervals or [(replay.dates[0], replay.dates[-1])],
        ledger,
    )
    return metrics, ledger


def test_next_session_entry_weekend_and_time_exit_at_open(settings):
    frame, replay = fixture_path()
    target, end_dates = swing_labels(frame, 2, replay, settings)
    metrics, trades = run_fixture(settings, frame, replay)
    assert len(trades) == 1
    assert trades[0]["signal_date"] == "2026-01-08"
    assert trades[0]["entry_time"] == "2026-01-09T14:30:00+00:00"
    assert trades[0]["exit_time"] == "2026-01-13T14:30:00+00:00"
    assert trades[0]["reason"] == "time"
    assert end_dates.iloc[0] == "2026-01-13"
    assert target.iloc[0] == pytest.approx(trades[0]["normalized_exit"] / trades[0]["normalized_entry"] - 1)
    assert metrics.net_return == pytest.approx(sum(t["net_pnl"] for t in trades) / settings.capital_limit)


def test_missing_open_has_no_backdated_fill_or_label(settings):
    frame, replay = fixture_path(missing_open=True)
    targets, _ = swing_labels(frame, 2, replay, settings)
    metrics, trades = run_fixture(settings, frame, replay)
    assert pd.isna(targets.iloc[0])
    assert trades == [] and metrics.rejected_limits == 1


def test_gap_stop_and_ambiguous_bar_stop_priority():
    assert protective_exit((90, 120, 89, 100, 1), 96, 108) == (90 * 0.9995, "stop", "intrabar")
    assert protective_exit((100, 109, 95, 101, 1), 96, 108)[1] == "stop"
    assert path_exit((110, 120, 90, 100, 1), 96, 108) == (108, "take_gap", "bar_open")


def test_overnight_gap_keeps_previous_close_loss_baseline(settings):
    frame, replay = fixture_path()
    ts, rows = replay.batches["2026-01-12"][0]
    rows["AAPL"] = (50.0, 50.0, 49.0, 50.0, 1.0)
    metrics, trades = run_fixture(settings, frame, replay)
    assert trades[0]["reason"] == "daily_loss"
    assert trades[0]["exit_raw_price"] == pytest.approx(50 * 0.9995)
    assert metrics.daily_returns[2] < -0.015


def test_split_preserves_value_and_integer_entry_lots(settings):
    frame, replay = fixture_path(split=True)
    metrics, trades = run_fixture(settings, frame, replay)
    assert trades[0]["entry_raw_qty"] == int(trades[0]["entry_raw_qty"])
    assert trades[0]["exit_raw_qty"] == pytest.approx(10 * trades[0]["entry_raw_qty"])
    assert -0.001 < metrics.net_return < 0


def test_simultaneous_positions_share_capital_and_ignore_current_close_for_sizing(settings):
    frame, replay = fixture_path(symbols=("AAPL", "AMD", "MSFT", "META", "NVDA", "TSLA"))
    _, trades = run_fixture(settings, frame, replay)
    assert len(trades) == 4
    assert sum(t["normalized_qty"] * t["normalized_entry"] for t in trades) <= settings.capital_limit * 0.6
    initial_qty = [t["entry_raw_qty"] for t in trades]
    for _, rows in replay.batches["2026-01-09"]:
        for symbol, row in rows.items():
            rows[symbol] = (*row[:3], 100.1, row[4])
    _, changed = run_fixture(settings, frame, replay)
    assert [t["entry_raw_qty"] for t in changed] == initial_qty


def test_eval_end_does_not_force_short_holding(settings):
    frame, replay = fixture_path()
    metrics, ledger = run_fixture(settings, frame, replay, horizon=10)
    assert ledger == [] and metrics.net_return == 0
    _, ledger = run_fixture(settings, frame, replay, intervals=[("2026-01-08", "2026-01-12")])
    assert ledger == []


def test_purge_even_if_label_would_exit_early():
    frame = pd.DataFrame(
        {
            "date": ["2026-01-01"] * 3 + ["2026-01-06"],
            "symbol": ["AAPL", "AAPL", "SPY", "AAPL"],
            "feature_valid": True,
        }
    )
    mask = purged_training(
        frame,
        pd.Series([0.1] * 4),
        pd.Series(["2026-01-08", "2026-01-05", "2026-01-05", "2026-01-08"]),
        "2026-01-05",
        ("SPY", "QQQ"),
    )
    assert mask.tolist() == [False, True, False, False]


def test_exact_split_factor_and_future_feature_invariance():
    dates = pd.bdate_range("2025-01-01", periods=90).strftime("%Y-%m-%d").tolist()
    rows = []
    for date, n in zip(dates, range(90), strict=True):
        for symbol in ("AAPL", "SPY", "QQQ"):
            rows.append(
                {
                    "symbol": symbol,
                    "date": date,
                    "open": 100 + n,
                    "high": 102 + n,
                    "low": 99 + n,
                    "close": 101 + n,
                    "volume": 1000,
                }
            )
    raw = pd.DataFrame(rows)
    factors = split_factors(raw, {})
    before = daily_features(raw, factors, dates, ("SPY", "QQQ"))
    changed = raw.copy()
    changed.loc[changed["date"].gt(dates[70]), "close"] *= 2
    after = daily_features(changed, factors, dates, ("SPY", "QQQ"))
    pd.testing.assert_frame_equal(
        before.loc[before["date"].le(dates[70]), DAILY_FEATURES],
        after.loc[after["date"].le(dates[70]), DAILY_FEATURES],
    )
    events = {
        "corporate_actions": {
            "forward_splits": [{"symbol": "AAPL", "ex_date": dates[80], "old_rate": 1, "new_rate": 10}]
        }
    }
    actual = split_factors(raw, events)
    assert actual.loc[raw["symbol"].eq("AAPL") & raw["date"].lt(dates[80])].eq(0.1).all()
    # Future constant adjustment cancels in all dimensionless features before the event.
    normalized = daily_features(raw, actual, dates, ("SPY", "QQQ"))
    mask = before["date"].lt(dates[80])
    np.testing.assert_allclose(
        before.loc[mask, DAILY_FEATURES], normalized.loc[mask, DAILY_FEATURES], equal_nan=True, atol=1e-12
    )


def test_signal_plan_does_not_use_future_daily_prices(settings):
    state = RiskState(100000, 100000, 100000, 100000, 0, 100000, 0)
    signal = {"symbol": "AAPL", "normalized_close": 100.0, "normalized_atr": 2.0}
    plan = normalized_plan(signal, 1.0, state, settings, 0.03)
    assert plan["raw_qty"] >= 1 and plan["entry"] > 100
    assert plan == normalized_plan({**signal, "future_close": 5000}, 1.0, state, settings, 0.03)


def test_delayed_time_exit_uses_first_observed_open_and_ignores_that_bars_future(settings):
    frame, replay = fixture_path()
    replay.batches["2026-01-13"].pop(0)
    path = replay.paths["AAPL"]
    remove = path["time"] == replay.openings["2026-01-13"].value
    for name in ("time", "date", "values"):
        path[name] = path[name][~remove]
    timestamp, rows = replay.batches["2026-01-13"][0]
    rows["AAPL"] = (100.0, 200.0, 10.0, 150.0, 1.0)
    metrics, ledger = run_fixture(settings, frame, replay)
    assert ledger[0]["exit_time"] == "2026-01-13T14:35:00+00:00"
    assert ledger[0]["exit_raw_price"] == pytest.approx(100 * 0.9995)
    assert metrics.trades == 1


def test_load_rejects_unregistered_holdout_before_reading_bars(settings, tmp_path):
    import json

    reference = {
        "holdout_evaluated": False,
        "promoted": False,
        "settings": settings.public_contract(),
        "holdout_start": "2026-06-17",
    }
    (tmp_path / "report.json").write_text(json.dumps(reference))
    with pytest.raises(ValueError, match="holdout boundary"):
        load_inputs(settings, tmp_path)


def test_whole_session_gap_is_unlabeled_and_never_filled(settings):
    frame, replay = fixture_path()
    replay.batches["2026-01-12"] = []
    replay.paths["AAPL"]["observed_dates"].remove("2026-01-12")
    targets, _ = swing_labels(frame, 2, replay, settings)
    assert pd.isna(targets.iloc[0])
    metrics, ledger = run_fixture(settings, frame, replay)
    assert ledger == []
    assert len(metrics.daily_returns) == len(replay.dates)


def test_opening_quote_changes_entry_and_matches_label(settings):
    frame, replay = fixture_path()
    timestamp, rows = replay.batches["2026-01-09"][0]
    rows["AAPL"] = (101.0, 102.0, 100.0, 101.0, 1.0)
    path = replay.paths["AAPL"]
    path["values"] = path["values"].copy()
    path["values"][path["time"] == timestamp.value] = rows["AAPL"]
    predictions = pd.Series(np.nan, index=frame.index)
    predictions.iloc[0] = 0.03
    ledger = []
    simulate_swing(
        frame,
        predictions,
        2,
        0.005,
        settings,
        replay,
        [(replay.dates[0], replay.dates[-1])],
        ledger,
        "opening_quote",
    )
    targets, _ = swing_labels(frame, 2, replay, settings, "opening_quote")
    assert ledger[0]["entry_raw_price"] > 101
    assert targets.iloc[0] == pytest.approx(ledger[0]["normalized_exit"] / ledger[0]["normalized_entry"] - 1)
    _, old_ledger = run_fixture(settings, frame, replay)
    assert old_ledger == []


def test_opening_quote_above_two_percent_signal_close_is_rejected(settings):
    state = RiskState(100000, 100000, 100000, 100000, 0, 100000, 0)
    signal = {"symbol": "AAPL", "normalized_close": 100.0, "normalized_atr": 2.0}
    assert normalized_plan(signal, 1.0, state, settings, 0.03, 103.0) is None
    assert normalized_plan(signal, 1.0, state, settings, 0.03, 101.0) is not None


def test_baseline_is_a_lagged_trend_ranking_not_future_return():
    frame = pd.DataFrame(
        {
            "feature_valid": [True] * 4,
            "ma_20": [0.1, -0.1, 0.1, 0.1],
            "ret_20": [0.1, 0.1, -0.1, 0.1],
            "relative_ret_20": [0.2, 0.1, 0.1, -0.2],
        }
    )
    scores = trend_predictions(frame)
    assert scores.iloc[0] > scores.iloc[3]
    assert scores.iloc[1:3].isna().all()
