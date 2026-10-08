import numpy as np
import pandas as pd
import pytest

from stock_trader.reversal_study import path_quality, rule_signals
from stock_trader.swing_study import prepare_swing, simulate_swing

from .test_swing_study import fixture_path


def test_rule_ranks_trade_universe_and_separates_priority_from_potential(settings):
    frame = pd.DataFrame(
        {
            "symbol": settings.symbols,
            "date": "2026-01-08",
            "feature_valid": True,
            "relative_ret_5": np.arange(30) / 100 - 0.15,
            "ret_5": -0.03,
            "ma_60": 0.05,
            "normalized_atr": 2.0,
            "normalized_close": 100.0,
        }
    )
    frame.loc[frame.symbol.isin(settings.benchmark_symbols), "relative_ret_5"] = -999
    potential, priority = rule_signals(frame, settings)
    assert list(frame.loc[potential.notna(), "symbol"]) == list(settings.trade_symbols[:5])
    assert priority.iloc[0] > priority.iloc[1]
    assert potential.iloc[0] == pytest.approx(0.08)
    frame.loc[0, "normalized_atr"] = 4
    altered, ranking = rule_signals(frame, settings)
    assert altered.iloc[0] == pytest.approx(0.16)
    pd.testing.assert_series_equal(priority, ranking)
    frame.loc[0, "ma_60"] = -0.05
    assert pd.isna(rule_signals(frame, settings)[0].iloc[0])


def test_appended_future_cross_section_cannot_change_past_signals(settings):
    frame = pd.DataFrame(
        {
            "symbol": settings.symbols,
            "date": "2026-01-08",
            "feature_valid": True,
            "relative_ret_5": np.arange(30) / 100 - 0.15,
            "ret_5": -0.03,
            "ma_60": 0.05,
            "normalized_atr": 2.0,
            "normalized_close": 100.0,
        }
    )
    before = rule_signals(frame, settings)
    future = frame.copy()
    future["date"] = "2026-01-09"
    future["relative_ret_5"] *= -1
    after = rule_signals(pd.concat([frame, future], ignore_index=True), settings)
    for a, b in zip(before, after, strict=True):
        pd.testing.assert_series_equal(a, b.iloc[: len(frame)])


def test_unfiltered_rule_never_calls_future_entry_filter_and_keeps_open_mark(settings, monkeypatch):
    frame, replay = fixture_path()
    predictions = pd.Series(np.nan, index=frame.index)
    predictions.iloc[0] = 0.03

    def forbidden(*args):
        raise AssertionError("future entry filter called")

    monkeypatch.setattr("stock_trader.swing_study.observable_window", forbidden)
    diagnostics, trades = {}, []
    metrics = simulate_swing(
        frame,
        predictions,
        0,
        0,
        settings,
        replay,
        [(replay.dates[0], replay.dates[-1])],
        trades,
        "opening_quote",
        "barrier",
        diagnostics,
        future_path_filter=False,
        quote_spread_bps=20,
    )
    assert metrics.trades == 0 and not trades
    assert len(diagnostics["open_positions"]) == 1
    assert diagnostics["open_positions"][0]["censored"]
    assert metrics.net_return == pytest.approx(
        diagnostics["unrealized_after_estimated_exit_cost"] / settings.capital_limit
    )
    assert metrics.net_return < 0
    with pytest.raises(AssertionError, match="future entry"):
        simulate_swing(
            frame,
            predictions,
            0,
            0,
            settings,
            replay,
            [(replay.dates[0], replay.dates[-1])],
            [],
            "opening_quote",
            "barrier",
            {},
        )


def test_rule_priority_is_independent_of_gross_barrier_distance(settings):
    frame, replay = fixture_path(symbols=("AAPL", "MSFT"))
    predictions, priorities = pd.Series(np.nan, index=frame.index), pd.Series(np.nan, index=frame.index)
    first = frame.date.eq(replay.dates[0])
    predictions.loc[first] = [0.10, 0.02]
    priorities.loc[first] = [1.0, 2.0]
    diagnostics = {}
    simulate_swing(
        frame,
        predictions,
        0,
        0,
        settings.model_copy(update={"max_positions": 1}),
        replay,
        [(replay.dates[0], replay.dates[-1])],
        [],
        "opening_quote",
        "barrier",
        diagnostics,
        rule_priority=priorities,
        future_path_filter=False,
        quote_spread_bps=20,
    )
    assert diagnostics["open_positions"][0]["symbol"] == "MSFT"


def test_calendar_path_audit_counts_missing_prices_without_suppressing_entry(settings):
    frame, replay = fixture_path()
    rows = []
    for symbol, path in replay.paths.items():
        for t, d, v in zip(path["time"], path["date"], path["values"], strict=True):
            rows.append(
                {
                    "symbol": symbol,
                    "date": d,
                    "timestamp": pd.Timestamp(t, tz="UTC"),
                    **dict(zip(("open", "high", "low", "close", "factor"), v, strict=True)),
                }
            )
    bars = pd.DataFrame(rows)
    missing = replay.openings[replay.dates[2]] + pd.Timedelta(minutes=5)
    bars = bars.loc[~bars.timestamp.eq(missing)]
    sparse = prepare_swing(bars, replay.dates, replay.openings)
    predictions = pd.Series(np.nan, index=frame.index)
    predictions.iloc[0] = 0.03
    diagnostics = {}
    simulate_swing(
        frame,
        predictions,
        0,
        0,
        settings,
        sparse,
        [(replay.dates[0], replay.dates[-1])],
        [],
        "opening_quote",
        "barrier",
        diagnostics,
        future_path_filter=False,
    )
    assert len(diagnostics["open_positions"]) == 1
    calendar = [{"date": d, "open": "09:30", "close": "09:40"} for d in replay.dates]
    audit = path_quality([], diagnostics, bars, calendar, bars.timestamp.max())
    assert audit == {"positions_with_missing_path": 1, "missing_path_bars": 1}


def test_invalid_replay_spread_rejected(settings):
    frame, replay = fixture_path()
    with pytest.raises(ValueError, match="spread"):
        simulate_swing(
            frame,
            pd.Series(np.nan, index=frame.index),
            0,
            0,
            settings,
            replay,
            [(replay.dates[0], replay.dates[-1])],
            [],
            quote_spread_bps=np.nan,
        )
