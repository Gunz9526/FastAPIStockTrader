import hashlib
import json
from datetime import timedelta

import lightgbm as lgb
import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from stock_trader.api import create_app
from stock_trader.clock import utcnow
from stock_trader.engine import Engine
from stock_trader.features import FEATURES, feature_frame
from stock_trader.notifications import Notifier
from stock_trader.research import ActiveModel, model_contract, prepare_replay, simulate, train_and_evaluate

from .test_risk_data import make_bars


class ApiStore:
    async def open(self):
        pass

    async def close(self):
        pass

    async def get(self, key, default=None):
        if key == "engine_status":
            return {"state": "waiting_alpaca_keys", "heartbeat": utcnow().isoformat()}
        return default

    async def one(self, sql, args=()):
        return {"id": 1}

    async def audit(self, *args, **kwargs):
        pass


def test_api_requires_auth_and_never_returns_secrets(settings):
    configured = settings.model_copy(
        update={
            "api_token": SecretStr("test-only-token"),
            "alpaca_key": SecretStr("test-only-key"),
            "discord_webhook": SecretStr(""),
        }
    )
    app = create_app(configured, ApiStore())
    with TestClient(app) as client:
        assert client.get("/health/live").status_code == 200
        readiness = client.get("/health/ready")
        assert readiness.status_code == 200
        assert readiness.json()["trading_ready"] is False
        assert client.get("/status").status_code == 401
        response = client.get("/status", headers={"Authorization": "Bearer test-only-token"})
        assert response.status_code == 200
        assert "test-only-key" not in response.text
        assert "test-only-token" not in response.text
        assert "alpaca_secret" not in response.json()["settings"]
        assert client.post("/controls", json={"command": "flatten"}).status_code == 401
        assert (
            client.post(
                "/controls", json={"command": "flatten"}, headers={"Authorization": "Bearer test-only-token"}
            ).status_code
            == 422
        )


def write_model(settings, created=None):
    directory = settings.artifact_dir / "test-model"
    directory.mkdir(parents=True)
    random = np.random.default_rng(7)
    x = pd.DataFrame(random.normal(size=(300, len(FEATURES))), columns=FEATURES)
    model = lgb.train(
        {"objective": "regression", "verbosity": -1, "num_threads": 2},
        lgb.Dataset(x, label=x["ret_3"] * 0.01),
        num_boost_round=10,
    )
    path = directory / "model.txt"
    model.save_model(str(path))
    metadata = {
        "version": "test-model",
        "created_at": (created or utcnow()).isoformat(),
        "accepted": True,
        "contract": model_contract(settings),
        "threshold": 0.0045,
        "horizon": 12,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }
    (directory / "metadata.json").write_text(json.dumps(metadata))
    (settings.artifact_dir / "current").write_text("test-model")
    return x, path


def test_real_cpu_lightgbm_artifact_and_checksum(settings):
    x, path = write_model(settings)
    active = ActiveModel(settings)
    active.refresh()
    assert np.isfinite(active.predict(x)).all()
    path.write_text(path.read_text() + "changed")
    with pytest.raises(ValueError, match="checksum"):
        active.refresh()


def test_expired_model_and_changed_risk_contract_are_rejected(settings):
    write_model(settings, created=utcnow() - timedelta(days=22))
    with pytest.raises(ValueError, match="expired"):
        ActiveModel(settings).refresh()
    changed = settings.model_copy(update={"stop_atr": 3})
    with pytest.raises(ValueError, match="contract"):
        ActiveModel(changed).refresh()


def test_no_prediction_yields_no_trades(settings):
    frame = feature_frame(make_bars(), include_invalid=True)
    result = simulate(
        frame, pd.Series(np.nan, index=frame.index), threshold=0.003, horizon=12, settings=settings
    )
    assert result.trades == 0
    assert result.net_return == 0


@pytest.mark.parametrize("gap_above_limit", [False, True])
@pytest.mark.parametrize("use_prepared_replay", [False, True])
def test_simulated_limit_gap_or_ambiguous_bar_is_conservative(settings, gap_above_limit, use_prepared_replay):
    bars = make_bars(days=1)
    aapl = bars["symbol"] == "AAPL"
    factor = 1 + np.arange(aapl.sum()) * 0.0003
    bars.loc[aapl, ["open", "high", "low", "close"]] *= factor[:, None]
    frame = feature_frame(bars, include_invalid=True)
    signals = frame.loc[
        (frame["symbol"] == "AAPL") & frame["feature_valid"] & (frame["timestamp"].dt.minute % 15 == 10)
    ]
    signal = signals.iloc[0]
    prediction = pd.Series(0.0, index=frame.index)
    prediction.loc[signal.name] = 0.02
    next_bar = (frame["symbol"] == "AAPL") & (
        frame["timestamp"] == signal["timestamp"] + pd.Timedelta(minutes=5)
    )
    if gap_above_limit:
        frame.loc[next_bar, "open"] = signal["close"] * 1.05
    else:
        # Both stop and take-profit touched on the entry bar: count the adverse stop first.
        frame.loc[next_bar, "high"] = signal["close"] * 1.03
        frame.loc[next_bar, "low"] = signal["close"] * 0.97
    replay = prepare_replay(frame) if use_prepared_replay else None
    metrics = simulate(frame, prediction, threshold=0.003, horizon=12, settings=settings, replay=replay)
    if gap_above_limit:
        assert metrics.trades == 0
        assert metrics.rejected_limits == 1
    else:
        assert metrics.trades == 1
        assert metrics.net_return < 0
    if replay:
        # A reused interval must not retain a previous candidate's buy signals or positions.
        empty = simulate(frame, prediction * 0, threshold=0.003, horizon=12, settings=settings, replay=replay)
        assert empty.trades == 0
        assert empty.net_return == 0
        with pytest.raises(ValueError, match="interval"):
            simulate(
                frame.iloc[1:], prediction, threshold=0.003, horizon=12, settings=settings, replay=replay
            )


def test_research_requires_enough_independent_sessions(settings):
    with pytest.raises(ValueError, match="180"):
        train_and_evaluate(make_bars(days=2), settings)


async def test_calendar_uses_early_close_and_dst(settings):
    class Broker:
        async def calendar(self, start, end):
            return [{"date": start, "close": "13:00"}]

    engine = Engine(settings, ApiStore())
    original = engine.broker
    engine.broker = Broker()
    summer = await engine.session_close(pd.Timestamp("2026-07-02T15:00:00Z").to_pydatetime())
    winter = await engine.session_close(pd.Timestamp("2026-11-27T15:00:00Z").to_pydatetime())
    assert summer.hour == 17
    assert winter.hour == 18
    await original.close()
    await engine.notifier.close()


def test_webhook_rejects_non_discord_destination(settings):
    configured = settings.model_copy(
        update={"discord_webhook": SecretStr("https://example.com/api/webhooks/x")}
    )
    with pytest.raises(ValueError, match="Discord"):
        Notifier(configured, ApiStore())


async def test_daily_loss_latch_survives_restart(store, settings):
    await store.put("risk_account", {"daily_latch": "daily_loss", "day": "2026-10-02"})
    # A fresh Store/engine uses persisted risk state, not process-local flags.
    engine = Engine(settings, store)
    assert (await engine.store.get("risk_account"))["daily_latch"] == "daily_loss"
    await engine.broker.close()
    await engine.notifier.close()
