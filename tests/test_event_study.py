import numpy as np
import pandas as pd
import pytest

from stock_trader.event_study import event_labels
from stock_trader.swing_study import prepare_swing, purged_training
from tests.test_swing_study import fixture_path


def entry_frame():
    frame, replay = fixture_path()
    frame["ma_20"], frame["ret_20"] = 0.03, 0.05
    replay.paths["AAPL"]["values"] = replay.paths["AAPL"]["values"].copy()
    return frame, replay


def test_early_target_uses_actual_event_end_and_unknowns_remain_nan(settings):
    frame, replay = entry_frame()
    path = replay.paths["AAPL"]
    path["values"][3] = (100, 110, 99, 109, 1)
    labels = event_labels(frame, replay, settings)
    assert labels.iloc[0]["status"] == "closed"
    assert labels.iloc[0]["event_end"] == replay.dates[1]
    assert labels.iloc[0]["exit_reason"] == "take"
    assert labels.iloc[0]["holding_sessions"] == 0
    assert labels.iloc[0]["target"] > 0
    assert labels.iloc[1]["status"] == "censored_end"
    assert pd.isna(labels.iloc[1]["target"])
    assert labels.iloc[-1]["status"] == "no_next_session"


def test_event_later_than_ten_sessions_and_purge_on_actual_end(settings):
    dates = pd.bdate_range("2026-01-05", periods=25).strftime("%Y-%m-%d").tolist()
    openings = {d: pd.Timestamp(d + "T14:30:00Z") for d in dates}
    bars = pd.DataFrame(
        [
            {
                "symbol": "AAPL",
                "date": d,
                "timestamp": openings[d],
                "open": 100.0,
                "high": 110.0 if i == 22 else 101.0,
                "low": 99.0,
                "close": 100.0,
                "factor": 1.0,
            }
            for i, d in enumerate(dates)
        ]
    )
    frame = pd.DataFrame(
        [
            {
                "symbol": "AAPL",
                "date": d,
                "normalized_close": 100.0,
                "normalized_atr": 2.0,
                "feature_valid": True,
                "ma_20": 0.03,
                "ret_20": 0.05,
            }
            for d in dates
        ]
    )
    replay = prepare_swing(bars, dates, openings)
    labels = event_labels(frame, replay, settings)
    assert labels.iloc[0]["holding_sessions"] == 21
    assert labels.iloc[0]["event_end"] == dates[22]
    before = purged_training(
        frame, labels["target"], labels["event_end"], dates[20], settings.benchmark_symbols
    )
    after = purged_training(
        frame, labels["target"], labels["event_end"], dates[22], settings.benchmark_symbols
    )
    assert not before.iloc[0] and after.iloc[0]
    assert (labels.loc[after, "event_end"] <= dates[22]).all()


@pytest.mark.parametrize(
    "row, reason",
    [
        ((100, 110, 90, 100, 1), "stop"),
        ((90, 110, 89, 100, 1), "stop_gap"),
        ((110, 120, 90, 100, 1), "take_gap"),
    ],
)
def test_event_label_keeps_existing_stop_and_opening_gap_priority(settings, row, reason):
    frame, replay = entry_frame()
    replay.paths["AAPL"]["values"][4] = row
    label = event_labels(frame, replay, settings).iloc[0]
    assert label["exit_reason"] == reason
    assert label["target"] < 0 if "stop" in reason else label["target"] > 0


def test_missing_day_before_hit_is_unknown_but_later_gap_does_not_erase_known_label(settings):
    frame, replay = entry_frame()
    replay.paths["AAPL"]["values"][8] = (100, 110, 99, 109, 1)
    replay.paths["AAPL"]["observed_dates"].remove(replay.dates[2])
    label = event_labels(frame, replay, settings).iloc[0]
    assert label["status"] == "unobserved_before_exit" and np.isnan(label["target"])
    replay.paths["AAPL"]["values"][3] = (100, 110, 99, 109, 1)
    frame.loc[frame["date"].gt(replay.dates[0]), "normalized_atr"] = 50.0
    label = event_labels(frame, replay, settings).iloc[0]
    assert label["status"] == "closed" and label["event_end"] == replay.dates[1]


def test_missing_open_and_trend_gate_do_not_create_labels(settings):
    frame, replay = fixture_path(missing_open=True)
    frame["ma_20"], frame["ret_20"] = 0.03, 0.05
    labels = event_labels(frame, replay, settings)
    assert labels.iloc[0]["status"] == "missing_open" and pd.isna(labels.iloc[0]["target"])
    frame.loc[0, "ma_20"] = -0.01
    assert event_labels(frame, replay, settings).iloc[0]["status"] == "not_entry_eligible"
