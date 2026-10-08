from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from stock_trader.clock import trading_day
from stock_trader.config import Settings
from stock_trader.data import BarCache
from stock_trader.features import FEATURES, feature_frame, labels
from stock_trader.risk import RiskState, entry_plan, loss_reason


def risk(**changes):
    values = dict(
        capital=100000,
        equity=100000,
        day_start_equity=100000,
        peak_equity=100000,
        exposure=0,
        cash=100000,
        position_count=0,
    )
    values.update(changes)
    return RiskState(**values)


def test_size_respects_reserved_capital_and_stop_risk(settings):
    state = risk(exposure=59000, cash=1000)
    plan = entry_plan(
        symbol="AAPL", bid=100, ask=100.1, atr=0.5, state=state, settings=settings, expected_return=0.01
    )
    assert plan is not None
    qty = int(plan["qty"])
    entry = float(plan["limit_price"])
    assert qty * entry <= 980
    assert qty * (entry - float(plan["stop_loss"]["stop_price"]) + entry * 0.002) <= 250
    assert state.exposure + qty * entry <= 60000


@pytest.mark.parametrize(
    "change",
    [{"exposure": 60000}, {"position_count": 4}, {"equity": 98000}, {"cash": 0}, {"equity": float("nan")}],
)
def test_invalid_or_limited_account_cannot_enter(settings, change):
    assert (
        entry_plan(
            symbol="AAPL",
            bid=100,
            ask=100.1,
            atr=0.5,
            state=risk(**change),
            settings=settings,
            expected_return=0.01,
        )
        is None
    )


def test_drawdown_precedes_daily_loss(settings):
    assert loss_reason(risk(equity=89000), settings) == "max_drawdown"
    assert loss_reason(risk(equity=98499), settings) == "daily_loss"


def test_invalid_subscription_rejected():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, _secrets_dir=None, symbols=tuple("S" + chr(65 + i) for i in range(31)))


def minute_bars(start, count=5):
    return [
        {
            "t": (start + timedelta(minutes=i)).isoformat(),
            "o": 100 + i,
            "h": 101 + i,
            "l": 99 + i,
            "c": 100.5 + i,
            "v": 1000,
        }
        for i in range(count)
    ]


def test_closed_bar_delay_missing_minutes_and_corrections():
    start = datetime(2026, 10, 2, 14, 0, tzinfo=UTC)
    cache = BarCache()
    rows = minute_bars(start)
    cache.seed("AAPL", rows)
    assert cache.five_minute("AAPL", start + timedelta(minutes=5, seconds=34)).empty
    complete = cache.five_minute("AAPL", start + timedelta(minutes=5, seconds=35))
    assert complete.iloc[0]["volume"] == 5000
    rows[2]["v"] = 2000
    cache.seed("AAPL", [rows[2]])
    assert cache.five_minute("AAPL", start + timedelta(minutes=6)).iloc[0]["volume"] == 6000
    missing = BarCache()
    missing.seed("AAPL", rows[:2] + rows[3:])
    assert missing.five_minute("AAPL", start + timedelta(minutes=6)).empty


def make_bars(days=2, symbols=("AAPL", "SPY", "QQQ")):
    rows = []
    for date in pd.bdate_range("2026-01-05", periods=days):
        start = pd.Timestamp(str(date.date()) + " 09:30", tz="America/New_York")
        for symbol in symbols:
            for i in range(78):
                price = 100 * (1 + 0.0001 * i + 0.00003 * np.sin(i))
                rows.append(
                    {
                        "timestamp": start.tz_convert("UTC") + pd.Timedelta(minutes=i * 5),
                        "symbol": symbol,
                        "open": price,
                        "high": price * 1.001,
                        "low": price * 0.999,
                        "close": price * 1.0002,
                        "volume": 1000 + i * 10,
                    }
                )
    return pd.DataFrame(rows)


def test_features_do_not_change_when_future_is_appended():
    bars = make_bars()
    cutoff = bars["timestamp"].sort_values().iloc[120]
    before = feature_frame(bars.loc[bars["timestamp"] <= cutoff])
    after = feature_frame(bars)
    after = after.loc[after["timestamp"] <= cutoff].reset_index(drop=True)
    pd.testing.assert_frame_equal(
        before[["timestamp", "symbol", *FEATURES]], after[["timestamp", "symbol", *FEATURES]]
    )


def test_gap_invalidates_lookback_and_labels_never_cross_sessions():
    bars = make_bars()
    removed = bars.loc[(bars["symbol"] == "AAPL")].iloc[30]["timestamp"]
    bars = bars.loc[~((bars["symbol"] == "AAPL") & (bars["timestamp"] == removed))]
    frame = feature_frame(bars)
    interrupted = frame.loc[
        (frame["symbol"] == "AAPL") & frame["timestamp"].between(removed, removed + pd.Timedelta(minutes=65))
    ]
    assert interrupted.empty
    target = labels(frame, 12)
    for _, group in frame.groupby(["date", "symbol"]):
        assert target.loc[group.tail(12).index].isna().all()


def test_trading_day_uses_new_york_not_utc():
    assert trading_day(datetime(2026, 10, 3, 1, tzinfo=UTC)) == "2026-10-02"
