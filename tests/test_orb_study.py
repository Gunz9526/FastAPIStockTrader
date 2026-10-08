from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from stock_trader.orb_study import ledger_quality, rejection_reasons, rule_frame
from stock_trader.research import Metrics, prepare_replay, simulate


def inputs():
    dates = [str(d.date()) for d in pd.bdate_range("2026-01-05", periods=17)]
    openings = {d: pd.Timestamp(d + " 09:30", tz="America/New_York").tz_convert("UTC") for d in dates}
    daily = pd.DataFrame({"date": dates, "symbol": "AAPL", "normalized_atr": 2.0, "factor": 1.0})
    rows = []
    for d in dates:
        for i in range(78):
            close = 100.5 if i == 0 else (100.8 if i == 3 else 101.2)
            rows.append(
                {
                    "date": d,
                    "symbol": "AAPL",
                    "timestamp": openings[d] + pd.Timedelta(minutes=5 * i),
                    "open": 100.0 if i == 0 else (100.5 if i == 1 else 101.2),
                    "high": 101.0 if i == 0 else 101.3,
                    "low": 99.9 if i == 0 else 101.1,
                    "close": close,
                    "volume": 2000 if d == dates[-1] else 1000,
                    "factor": 1.0,
                }
            )
    return daily, pd.DataFrame(rows), dates, openings


def run_day(frame, day, opening, settings, *, signals=None):
    part = frame.loc[frame.date.eq(day)]
    prepared = prepare_replay(part)
    prepared.day_end = pd.Series({day: opening + pd.Timedelta(hours=6, minutes=30)})
    ledger = []
    metrics = simulate(
        part,
        part.gross_target_potential,
        threshold=0,
        horizon=100_000,
        settings=settings,
        replay=prepared,
        execution="timed_open",
        trades=ledger,
        rule_signals=part.orb_rvol if signals is None else signals,
        rule_priority=part.opening_rvol,
        quote_spread_bps=20,
    )
    return metrics, ledger


def test_first_completed_breakout_and_prior_atr_only(settings):
    daily, bars, dates, openings = inputs()
    frame = rule_frame(daily, bars, dates, openings, settings)
    selected = frame.loc[frame.date.eq(dates[-1]) & frame.orb_rvol]
    assert len(selected) == 1
    assert selected.iloc[0].timestamp == openings[dates[-1]] + pd.Timedelta(minutes=5)
    assert selected.iloc[0].opening_rvol == 2
    assert selected.iloc[0].atr == pytest.approx(0.1)
    daily.loc[daily.date.eq(dates[-1]), "normalized_atr"] = 9999
    changed = rule_frame(daily, bars, dates, openings, settings)
    pd.testing.assert_frame_equal(frame, changed)
    # Appending a future session cannot change any earlier signal or risk scale.
    cutoff = dates[-2]
    before = rule_frame(
        daily.loc[daily.date.le(cutoff)], bars.loc[bars.date.le(cutoff)], dates[:-1], openings, settings
    )
    pd.testing.assert_frame_equal(before, frame.loc[frame.date.le(cutoff)].reset_index(drop=True))


def test_missing_opening_history_is_not_zero_filled(settings):
    daily, bars, dates, openings = inputs()
    bars = bars.loc[~(bars.date.eq(dates[-2]) & bars.timestamp.eq(openings[dates[-2]]))]
    frame = rule_frame(daily, bars, dates, openings, settings)
    assert not frame.loc[frame.date.eq(dates[-1]), ["orb_rvol", "orb_control"]].any().any()


def test_missing_bar_before_cross_does_not_trigger(settings):
    daily, bars, dates, openings = inputs()
    day = dates[-1]
    bars = bars.loc[~(bars.date.eq(day) & bars.timestamp.eq(openings[day] + pd.Timedelta(minutes=15)))]
    frame = rule_frame(daily, bars, dates, openings, settings)
    assert frame.loc[frame.date.eq(day), "orb_rvol"].sum() == 1


def test_opening_volume_uses_same_split_share_unit(settings):
    daily, bars, dates, openings = inputs()
    historical = bars.date.lt(dates[-1])
    bars.loc[historical, "factor"] = 0.1
    bars.loc[historical, ["open", "high", "low", "close"]] *= 0.1
    daily.loc[daily.date.lt(dates[-1]), "factor"] = 0.1
    bars.loc[bars.date.eq(dates[-1]), "volume"] = 10_000
    frame = rule_frame(daily, bars, dates, openings, settings)
    assert frame.loc[frame.date.eq(dates[-1]), "opening_rvol"].eq(1.0).all()


def test_rule_execution_next_bar_calendar_close_and_cost_ledger(settings):
    daily, bars, dates, openings = inputs()
    frame = rule_frame(daily, bars, dates, openings, settings)
    result, ledger = run_day(frame, dates[-1], openings[dates[-1]], settings)
    assert result.trades == 1
    trade = ledger[0]
    assert pd.Timestamp(trade["entry_time"]) == openings[dates[-1]] + pd.Timedelta(minutes=10)
    assert trade["reason"] == "day_close"
    assert pd.Timestamp(trade["exit_time"]) == openings[dates[-1]] + pd.Timedelta(minutes=380)
    assert result.net_return == pytest.approx(sum(t["net_pnl"] for t in ledger) / settings.capital_limit)
    assert trade["explicit_cost"] > 0
    assert not any(ledger_quality(ledger).values())
    blocked, _ = run_day(
        frame, dates[-1], openings[dates[-1]], settings, signals=pd.Series(False, index=frame.index)
    )
    assert blocked.trades == 0
    stressed, _ = run_day(
        frame, dates[-1], openings[dates[-1]], settings.model_copy(update={"round_trip_cost_bps": 30})
    )
    assert stressed.net_return < result.net_return


@pytest.mark.parametrize("mode", ["missing", "gap", "both_barriers"])
def test_next_bar_missing_or_gap_no_retry_and_stop_first(settings, mode):
    daily, bars, dates, openings = inputs()
    frame = rule_frame(daily, bars, dates, openings, settings)
    entry = frame.date.eq(dates[-1]) & frame.timestamp.eq(openings[dates[-1]] + pd.Timedelta(minutes=10))
    if mode == "missing":
        frame = frame.loc[~entry]
    elif mode == "gap":
        frame.loc[entry, "open"] = 105
    else:
        frame.loc[entry, ["high", "low"]] = [105, 99]
    result, ledger = run_day(frame, dates[-1], openings[dates[-1]], settings)
    if mode == "both_barriers":
        assert result.trades == 1
        assert ledger[0]["reason"] == "stop"
        assert ledger[0]["net_pnl"] < 0
    else:
        assert result.trades == 0
        assert result.rejected_limits == 1


def test_missing_held_path_blocks_approval_without_removing_trade(settings):
    daily, bars, dates, openings = inputs()
    frame = rule_frame(daily, bars, dates, openings, settings)
    missing = frame.date.eq(dates[-1]) & frame.timestamp.eq(openings[dates[-1]] + pd.Timedelta(minutes=60))
    _, ledger = run_day(frame.loc[~missing], dates[-1], openings[dates[-1]], settings)
    assert ledger[0]["missing_path_bars"] == 1
    quality = ledger_quality(ledger)
    good = Metrics(0.02, 0.01, 80, 0.6, 1.5, 0, [0.02])
    folds = [{"net_return": 0.01}] * 3
    assert rejection_reasons(good, folds, good, quality) == ["unverifiable_execution_path"]
    assert rejection_reasons(good, folds, good, {k: 0 for k in quality}) == []
    assert "nonpositive_30bps_stress" in rejection_reasons(
        good, folds, replace(good, net_return=-0.01), {k: 0 for k in quality}
    )


def test_malformed_daily_calendar_and_simulated_spread_rejected(settings):
    daily, bars, dates, openings = inputs()
    with pytest.raises(ValueError, match="continuity"):
        rule_frame(daily.iloc[1:], bars, dates, openings, settings)
    frame = rule_frame(daily, bars, dates, openings, settings)
    with pytest.raises(ValueError, match="spread"):
        simulate(
            frame,
            pd.Series(np.nan, index=frame.index),
            threshold=0,
            horizon=1,
            settings=settings,
            quote_spread_bps=np.nan,
        )
