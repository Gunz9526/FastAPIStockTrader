import numpy as np
import pandas as pd
import pytest

from stock_trader.barrier_study import block_sensitivity, ledger_summary
from stock_trader.swing_study import simulate_swing
from tests.test_swing_study import fixture_path


def barrier_run(settings, frame, replay, policy="barrier"):
    predictions = pd.Series(np.nan, index=frame.index)
    predictions.loc[frame["date"].eq(replay.dates[0])] = 0.03
    ledger, diagnostics = [], {}
    metrics = simulate_swing(
        frame,
        predictions,
        2,
        0.005,
        settings,
        replay,
        [(replay.dates[0], replay.dates[-1])],
        ledger,
        "opening_quote",
        policy,
        diagnostics,
    )
    return metrics, ledger, diagnostics


def test_target_can_exit_after_old_time_limit_without_changing_target(settings):
    frame, replay = fixture_path()
    for _, rows in replay.batches[replay.dates[-1]]:
        rows["AAPL"] = (100.0, 110.0, 99.0, 109.0, 1.0)
    frame.loc[frame["date"].gt(replay.dates[1]), "normalized_atr"] = 50.0
    metrics, ledger, detail = barrier_run(settings, frame, replay)
    assert metrics.trades == 1 and ledger[0]["reason"] == "take"
    assert ledger[0]["exit_time"].startswith(replay.dates[-1])
    assert ledger[0]["planned_exit_date"] is None
    assert detail["open_positions"] == []
    _, timed, _ = barrier_run(settings, frame, replay, "timed")
    assert timed[0]["reason"] == "time" and timed[0]["exit_time"].startswith(replay.dates[3])


def test_early_target_does_not_wait_for_old_limit(settings):
    frame, replay = fixture_path()
    _, rows = replay.batches[replay.dates[1]][1]
    rows["AAPL"] = (100.0, 110.0, 99.0, 109.0, 1.0)
    _, ledger, _ = barrier_run(settings, frame, replay)
    assert ledger[0]["reason"] == "take" and ledger[0]["exit_time"].startswith(replay.dates[1])


def test_unclosed_position_is_censored_and_exit_cost_is_included(settings):
    frame, replay = fixture_path()
    metrics, ledger, detail = barrier_run(settings, frame, replay)
    assert ledger == [] and metrics.trades == 0 and metrics.profit_factor == 0
    position = detail["open_positions"][0]
    assert position["censored"] and position["holding_sessions"] == 3
    assert position["estimated_liquidation_value"] < position["marked_value_before_exit_cost"]
    assert metrics.net_return == pytest.approx(
        detail["unrealized_after_estimated_exit_cost"] / settings.capital_limit
    )
    assert np.prod(1 + np.array(metrics.daily_returns)) - 1 == pytest.approx(metrics.net_return)


def test_realized_and_open_pnl_share_one_capital_ledger(settings):
    frame, replay = fixture_path(symbols=("AAPL", "AMD"))
    _, rows = replay.batches[replay.dates[-1]][0]
    rows["AAPL"] = (100.0, 110.0, 99.0, 109.0, 1.0)
    metrics, ledger, detail = barrier_run(settings, frame, replay)
    assert len(ledger) == 1 and len(detail["open_positions"]) == 1
    expected = sum(t["net_pnl"] for t in ledger) + detail["unrealized_after_estimated_exit_cost"]
    assert metrics.net_return == pytest.approx(expected / settings.capital_limit)


def test_daily_risk_still_exits_before_targets(settings):
    frame, replay = fixture_path()
    _, rows = replay.batches[replay.dates[2]][0]
    rows["AAPL"] = (50.0, 50.0, 49.0, 50.0, 1.0)
    metrics, ledger, detail = barrier_run(settings, frame, replay)
    assert ledger[0]["reason"] == "daily_loss" and metrics.net_return < -0.015
    assert detail["open_positions"] == []


def test_barrier_requires_explicit_disclosure_of_unfinished_positions(settings):
    frame, replay = fixture_path()
    with pytest.raises(ValueError, match="diagnostics"):
        simulate_swing(
            frame,
            pd.Series(0.03, index=frame.index),
            2,
            0.005,
            settings,
            replay,
            [(replay.dates[0], replay.dates[-1])],
            [],
            "opening_quote",
            "barrier",
        )


def test_longer_window_missing_data_is_not_silently_bridged(settings):
    frame, replay = fixture_path()
    replay.batches[replay.dates[-1]] = []
    replay.paths["AAPL"]["observed_dates"].remove(replay.dates[-1])
    _, ledger, detail = barrier_run(settings, frame, replay)
    assert ledger == [] and detail["open_positions"] == []


def test_diagnosis_separates_gross_from_explicit_cost():
    trades = [
        {
            "reason": "stop",
            "symbol": "AAPL",
            "entry_time": "2026-01-01T14:30:00Z",
            "exit_time": "2026-01-02T14:30:00Z",
            "gross_pnl": -100,
            "net_pnl": -110,
            "explicit_cost": 10,
        },
        {
            "reason": "time",
            "symbol": "AMD",
            "entry_time": "2026-01-01T14:30:00Z",
            "exit_time": "2026-01-03T14:30:00Z",
            "gross_pnl": 40,
            "net_pnl": 30,
            "explicit_cost": 10,
        },
    ]
    result = ledger_summary(trades, 10000)
    assert result["gross_return_completed"] == -0.006
    assert result["explicit_cost_return_completed"] == 0.002
    assert result["net_return_completed"] == -0.008
    assert result["reasons"]["time"]["net_pnl"] == 30


def test_block_diagnostic_is_reproducible_and_constant_path_keeps_compounding():
    returns = [0.01] * 45
    result = block_sensitivity(returns, 20, repetitions=100)
    assert result == block_sensitivity(returns, 20, repetitions=100)
    assert result["percentile_2_5"] == pytest.approx(1.01**45 - 1)
    assert result["percentile_97_5"] == pytest.approx(1.01**45 - 1)
    assert result["is_future_profit_probability"] is False
    zeros = block_sensitivity([0.0] * 50, 40, repetitions=100)
    assert zeros["percentile_97_5"] == 0 and zeros["positive_resample_fraction"] == 0


def test_invalid_block_returns_are_rejected():
    for returns in ([], [np.nan], [-1.0], [-1.5]):
        with pytest.raises(ValueError):
            block_sensitivity(returns, 20)
