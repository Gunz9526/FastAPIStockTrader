import numpy as np
import pandas as pd
import pytest

from stock_trader.features import FEATURES, feature_frame
from stock_trader.modeling import FAMILIES, make_regressor, predict_regressor, save_models
from stock_trader.regime_study import CONTEXT_FEATURES, context_frame, run_regime_study
from stock_trader.study import write_json

from .test_risk_data import make_bars


def history():
    frame = feature_frame(make_bars(days=30), include_invalid=True)
    days = {day: i for i, day in enumerate(sorted(frame["date"].unique()))}
    factor = frame["date"].map(days) * 0.002 + 1
    frame[["open", "high", "low", "close"]] *= factor.to_numpy()[:, None]
    return frame


def test_context_lags_daily_prices_and_future_sessions():
    frame = history()
    original, daily = context_frame(frame, ("SPY", "QQQ"))
    days = sorted(frame["date"].unique())
    day = days[24]
    previous = daily.loc[(daily["symbol"] == "AAPL") & daily["date"].isin(days[22:24])]
    expected = previous["observed_close"].iloc[1] / previous["observed_close"].iloc[0] - 1
    assert original.loc[original["date"].eq(day), "prev_session_return"].iloc[0] == pytest.approx(expected)
    modified = frame.copy()
    later = modified["date"] > day
    modified.loc[later, ["open", "high", "low", "close"]] *= 10
    newer, _ = context_frame(modified, ("SPY", "QQQ"))
    pd.testing.assert_frame_equal(original.loc[~later, CONTEXT_FEATURES], newer.loc[~later, CONTEXT_FEATURES])


def test_current_day_future_close_does_not_change_earlier_context():
    frame = history()
    day = sorted(frame["date"].unique())[24]
    original, _ = context_frame(frame, ("SPY", "QQQ"))
    modified = frame.copy()
    later = modified["date"].eq(day) & modified["minute_of_session"].ge(180)
    modified.loc[later, ["open", "high", "low", "close"]] *= 4
    newer, _ = context_frame(modified, ("SPY", "QQQ"))
    earlier = frame["date"].eq(day) & frame["minute_of_session"].lt(180)
    pd.testing.assert_frame_equal(
        original.loc[earlier, CONTEXT_FEATURES], newer.loc[earlier, CONTEXT_FEATURES]
    )


def test_missing_session_is_not_forward_filled_and_row_order_preserved():
    frame = history()
    days = sorted(frame["date"].unique())
    frame = frame.loc[~(frame["symbol"].eq("AAPL") & frame["date"].eq(days[23]))]
    frame = frame.sample(frac=1, random_state=42)
    output, _ = context_frame(frame, ("SPY", "QQQ"))
    assert output[["timestamp", "symbol"]].equals(frame[["timestamp", "symbol"]])
    after = output["symbol"].eq("AAPL") & output["date"].eq(days[24])
    assert output.loc[after, "prev_session_return"].isna().all()
    assert output.loc[after, "prev_5_session_return"].isna().all()
    assert not output.loc[after, "feature_valid"].any()


@pytest.mark.parametrize("family", FAMILIES)
def test_cpu_models_fit_and_save_extended_feature_schema(family, tmp_path):
    columns = [*FEATURES, *CONTEXT_FEATURES]
    rng = np.random.default_rng(42)
    x = pd.DataFrame(rng.normal(size=(500, len(columns))), columns=columns)
    model = make_regressor(family, 2)
    model.fit(x, x["prev_5_session_return"] * 0.01)
    predictions = predict_regressor(model, x.iloc[:5])
    assert np.isfinite(predictions).all()
    assert len(predictions) == 5
    members = save_models({family: model}, tmp_path)
    assert len(members[0]["sha256"]) == 64
    assert (tmp_path / members[0]["file"]).stat().st_size > 0


def test_regime_research_rejects_sealed_holdout(settings, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    write_json(
        source / "report.json",
        {
            "holdout_evaluated": False,
            "promoted": False,
            "settings": settings.public_contract(),
            "holdout_start": "2026-06-16",
        },
    )
    pd.DataFrame({"date": ["2026-06-16"]}).to_parquet(source / "development-features.parquet", index=False)
    with pytest.raises(ValueError, match="sealed holdout"):
        run_regime_study(settings, source)


def test_duplicate_context_bars_rejected():
    frame = history()
    with pytest.raises(ValueError, match="duplicate"):
        context_frame(pd.concat([frame, frame.iloc[:1]]), ("SPY", "QQQ"))
