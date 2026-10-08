import numpy as np
import pandas as pd
import pytest

from stock_trader.features import feature_frame
from stock_trader.modeling import FAMILIES, make_regressor
from stock_trader.research import prepare_replay, simulate
from stock_trader.study import aligned_labels, model_configuration, summarize_trades, write_json

from .test_risk_data import make_bars


def sample():
    bars = make_bars(days=2)
    aapl = bars.symbol.eq("AAPL")
    bars.loc[aapl, ["open", "high", "low", "close"]] *= (1 + np.arange(aapl.sum()) * 0.0003)[:, None]
    frame = feature_frame(bars, include_invalid=True)
    signal = frame.loc[
        frame.symbol.eq("AAPL") & frame.feature_valid & frame.timestamp.dt.minute.mod(15).eq(10)
    ].iloc[0]
    predictions = pd.Series(0.0, index=frame.index)
    predictions.loc[signal.name] = 0.02
    return frame, signal, predictions


def test_real_model_settings_are_strict_json_serializable(tmp_path):
    import json

    path = tmp_path / "configs.json"
    write_json(path, {family: model_configuration(make_regressor(family, 2)) for family in FAMILIES})
    saved = json.loads(path.read_text())
    assert saved["xgb"]["missing"] == "nan"
    assert set(saved) == set(FAMILIES)


def test_timed_exit_and_aligned_target_match_unit_trade(settings):
    frame, signal, predictions = sample()
    trades = []
    metrics = simulate(
        frame,
        predictions,
        threshold=0.003,
        horizon=6,
        settings=settings,
        execution="timed_open",
        trades=trades,
    )
    assert metrics.trades == 1
    trade = trades[0]
    assert trade["reason"] == "time"
    assert pd.Timestamp(trade["exit_time"]) - pd.Timestamp(trade["entry_time"]) == pd.Timedelta(minutes=30)
    target = aligned_labels(frame, 6, settings)
    assert target.loc[signal.name] == pytest.approx(trade["gross_return"])
    summary = summarize_trades(trades, settings.capital_limit)
    assert summary["net_return_from_ledger"] == pytest.approx(metrics.net_return)
    assert summary["gross_return_fixed_trades"] - summary["explicit_cost_return"] == pytest.approx(
        metrics.net_return
    )
    legacy = []
    simulate(frame, predictions, threshold=0.003, horizon=6, settings=settings, trades=legacy)
    assert pd.Timestamp(legacy[0]["exit_time"]) - pd.Timestamp(legacy[0]["entry_time"]) == pd.Timedelta(
        minutes=35
    )


def test_alignment_rejects_missing_future_and_infeasible_limit(settings):
    frame, signal, _ = sample()
    gap_time = signal.timestamp + pd.Timedelta(minutes=15)
    gap = frame.loc[~(frame.symbol.eq("AAPL") & frame.timestamp.eq(gap_time))]
    assert pd.isna(aligned_labels(gap, 6, settings).loc[signal.name])
    entry = frame.symbol.eq("AAPL") & frame.timestamp.eq(signal.timestamp + pd.Timedelta(minutes=5))
    frame.loc[entry, "open"] = signal.close * 1.05
    assert pd.isna(aligned_labels(frame, 6, settings).loc[signal.name])


def test_aligned_same_bar_stop_priority_and_session_boundary(settings):
    frame, signal, predictions = sample()
    entry = frame.symbol.eq("AAPL") & frame.timestamp.eq(signal.timestamp + pd.Timedelta(minutes=5))
    frame.loc[entry, "high"] = signal.close * 1.03
    frame.loc[entry, "low"] = signal.close * 0.97
    target = aligned_labels(frame, 6, settings)
    trades = []
    simulate(
        frame,
        predictions,
        threshold=0.003,
        horizon=6,
        settings=settings,
        execution="timed_open",
        trades=trades,
    )
    assert trades[0]["reason"] == "stop"
    assert target.loc[signal.name] == pytest.approx(trades[0]["gross_return"])
    assert target.loc[signal.name] < 0
    last_session_rows = frame.loc[
        frame.date.eq(signal.date) & frame.timestamp.dt.tz_convert("America/New_York").dt.hour.ge(15)
    ]
    assert aligned_labels(last_session_rows, 24, settings).loc[last_session_rows.index].notna().any()
    late = frame.timestamp.dt.tz_convert("America/New_York").dt.time.ge(pd.Timestamp("15:30").time())
    assert target.loc[late].isna().all()


def test_unknown_execution_rejected(settings):
    frame, _, predictions = sample()
    with pytest.raises(ValueError, match="execution"):
        simulate(
            frame,
            predictions,
            threshold=0.003,
            horizon=6,
            settings=settings,
            execution="unknown",
            replay=prepare_replay(frame),
        )
