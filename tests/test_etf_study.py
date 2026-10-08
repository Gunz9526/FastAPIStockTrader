import json

import numpy as np
import pandas as pd
import pytest

from stock_trader.etf_study import (
    DATA_CONTRACT,
    SYMBOLS,
    collect,
    rebalance,
    simulate,
    targets,
    validate_frames,
)
from stock_trader.modeling import checksum


def prices():
    dates = pd.bdate_range("2016-01-01", "2019-12-31")
    return pd.DataFrame(
        {s: 100 * np.exp(np.arange(len(dates)) * (0.0001 + i * 0.00005)) for i, s in enumerate(SYMBOLS)},
        index=dates,
    )


def test_month_close_signal_next_month_and_12_month_warmup():
    frame = prices()
    schedule = targets(frame, "rotation_2")
    assert schedule.index.min() == pd.Timestamp("2017-02-01")
    assert schedule.loc["2018-01-01", "VNQ"] == 0.5
    assert schedule.loc["2018-01-01", "DBC"] == 0.5
    assert schedule.sum(axis=1).eq(1).all()
    delayed = targets(frame, "rotation_2", 1)
    assert delayed.index.min() == pd.Timestamp("2017-02-02")


def test_future_prices_cannot_change_past_allocations():
    frame = prices()
    baseline = targets(frame, "rotation_2")
    frame.loc["2018-01-01":, "SPY"] *= 10
    changed = targets(frame, "rotation_2")
    pd.testing.assert_frame_equal(baseline.loc[:"2018-01-01"], changed.loc[:"2018-01-01"])


def test_bearish_risky_assets_move_to_bills():
    frame = prices()
    frame.loc[:, list(SYMBOLS[:-1])] = 100 * np.exp(-np.arange(len(frame))[:, None] * 0.001)
    for strategy in ("trend_10m", "rotation_2"):
        schedule = targets(frame, strategy)
        assert schedule.BIL.eq(1).all()
        assert schedule.drop(columns="BIL").eq(0).all().all()


def test_costs_paid_before_allocating_and_capital_conserved():
    values = np.array([20_000, 0, 0, 0, 0, 80_000])
    weights = np.array([0, 0.5, 0.5, 0, 0, 0])
    desired, cash, fee, turnover = rebalance(values, 0, weights, 0.002)
    assert desired.sum() + cash + fee == pytest.approx(100_000)
    assert fee == pytest.approx(turnover * 0.002)
    assert cash >= 0 and desired.sum() <= 100_000
    assert desired[1] == pytest.approx(desired[2])
    with pytest.raises(ValueError, match="allocation"):
        rebalance(values, 0, weights * 2, 0.002)


def test_flat_market_costs_and_terminal_liquidation_reconcile():
    frame = prices() * 0 + 100
    result, curve, ledger = simulate(frame, frame, targets(frame, "balanced_hold"), 20)
    assert curve.iloc[-1] == pytest.approx(100_000 - sum(e["cost"] for e in ledger))
    assert result["net_return"] < 0
    stressed, _, _ = simulate(frame, frame, targets(frame, "balanced_hold"), 40)
    assert stressed["net_return"] < result["net_return"]
    assert ledger[-1]["type"] == "estimated_terminal_liquidation"


def test_order_executes_after_prior_month_signal_not_at_signal_close():
    closes = prices() * 0 + 100
    opens = closes.copy()
    opens.loc["2018-01-01", "SPY"] = 200
    result, curve, _ = simulate(opens, closes, targets(closes, "spy_hold"), 0)
    assert curve.loc["2018-01-01"] == 50_000
    assert result["net_return"] == -0.5


def test_calendar_missing_or_sealed_prices_rejected():
    rows = [
        {
            "date": "2016-01-04",
            "timestamp": pd.Timestamp("2016-01-04T05:00Z"),
            "symbol": s,
            "open": 100,
            "high": 101,
            "low": 99,
            "close": 100,
        }
        for s in SYMBOLS
    ]
    frame = pd.DataFrame(rows)
    calendar = [{"date": "2016-01-04"}]
    validate_frames(frame, frame, calendar)
    with pytest.raises(ValueError, match="calendar session"):
        validate_frames(frame, frame, [*calendar, {"date": "2016-01-05"}])
    with pytest.raises(ValueError, match="boundary"):
        validate_frames(frame, frame, [{"date": "2026-06-16"}])


async def test_completed_cache_never_constructs_broker(settings, tmp_path, monkeypatch):
    directory = tmp_path / "collection"
    directory.mkdir()
    (directory / "manifest.json").write_text(json.dumps(DATA_CONTRACT))
    sample = directory / "sample"
    sample.write_text("sealed")
    (directory / "collection.json").write_text(
        json.dumps({"contract": DATA_CONTRACT, "file_sha256": {"sample": checksum(sample)}})
    )

    def forbidden(*args):
        raise AssertionError("cache issued request")

    monkeypatch.setattr("stock_trader.etf_study.Alpaca", forbidden)
    assert (await collect(settings, directory))["new_requests"] == 0
    sample.write_text("changed")
    with pytest.raises(ValueError, match="checksum"):
        await collect(settings, directory)
