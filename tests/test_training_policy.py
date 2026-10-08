from contextlib import asynccontextmanager

import numpy as np
import pandas as pd
import pytest
from pydantic import SecretStr

from stock_trader.cli import resume_research, train_job
from stock_trader.cost_study import fixed_trade_costs, validate_predictions
from stock_trader.modeling import STRATEGIES
from stock_trader.training_policy import load_policy, record_result


def report(number, accepted=False, final=False):
    value = {"created_at": f"run-{number}", "accepted": accepted, "data_end": "2026-10-01"}
    if final:
        value["test_metrics"] = {"net_return": -0.01}
    return value


def test_rejection_budget_duplicate_and_acceptance():
    first = record_result(None, report(1))
    assert first["consecutive_rejections"] == 1
    assert record_result(first, report(1)) == first
    second = record_result(first, report(2))
    assert second["state"] == "active"
    third = record_result(second, report(3))
    assert third["state"] == "research_required"
    assert third["consecutive_rejections"] == 3
    accepted = record_result(third, report(4, accepted=True))
    assert accepted["consecutive_rejections"] == 0
    assert accepted["state"] == "active"


def test_final_test_rejection_blocks_immediately():
    policy = record_result(None, report(1, final=True))
    assert policy["state"] == "research_required"
    assert policy["reason"] == "holdout_failed"
    assert policy["failed_holdout_end"] == "2026-10-01"


async def test_migration_is_persistent_and_idempotent(store):
    await store.put("training_status", {"state": "rejected", "report": report(1)})
    first = await load_policy(store)
    assert first["consecutive_rejections"] == 1
    assert await load_policy(store) == first
    await store.put("training_status", {"state": "failed", "error": "TimeoutError"})
    assert await load_policy(store) == first


class MemoryStore:
    def __init__(self, policy):
        self.values = {"training_policy": policy}
        self.alerts = []
        self.audits = []

    async def get(self, key, default=None):
        return self.values.get(key, default)

    async def put(self, key, value):
        self.values[key] = value

    async def alert(self, *args):
        self.alerts.append(args)

    async def audit(self, *args, **kwargs):
        self.audits.append((args, kwargs))

    @asynccontextmanager
    async def leader(self, identity):
        yield self


def configured(settings):
    return settings.model_copy(
        update={"alpaca_key": SecretStr("test-key"), "alpaca_secret": SecretStr("test-secret")}
    )


class Broker:
    def __init__(self, settings):
        pass

    async def close(self):
        pass


async def test_blocked_job_collects_without_fitting_or_orders(settings, monkeypatch):
    policy = record_result(record_result(record_result(None, report(1)), report(2)), report(3))
    store = MemoryStore(policy)
    collected = []

    async def collect(*args):
        collected.append(True)

    def forbidden(*args):
        raise AssertionError("blocked job must not read or fit a model")

    monkeypatch.setattr("stock_trader.cli.Alpaca", Broker)
    monkeypatch.setattr("stock_trader.cli.collect_history", collect)
    monkeypatch.setattr("stock_trader.cli.read_history", forbidden)
    monkeypatch.setattr("stock_trader.cli.train_and_evaluate", forbidden)
    await train_job(configured(settings), store)
    assert collected == [True]
    assert store.values["training_policy"] == policy
    assert store.values["training_status"]["state"] == "research_required"
    assert "last_training_maintenance" in store.values
    assert "last_training_completed" not in store.values


async def test_technical_failure_does_not_increment_rejections(settings, monkeypatch):
    policy = record_result(None, report(1))
    store = MemoryStore(policy)

    async def failed(*args):
        raise TimeoutError()

    monkeypatch.setattr("stock_trader.cli.Alpaca", Broker)
    monkeypatch.setattr("stock_trader.cli.collect_history", failed)
    with pytest.raises(TimeoutError):
        await train_job(configured(settings), store)
    assert store.values["training_policy"] == policy
    assert store.values["training_status"]["state"] == "failed"


async def test_resume_requires_record_and_preserves_spent_holdout(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "docs").mkdir()
    decision = tmp_path / "docs" / "new-research.md"
    decision.write_text("Registered new hypothesis and fresh evaluation period")
    store = MemoryStore(record_result(None, report(1, final=True)))
    with pytest.raises(ValueError, match="document"):
        await resume_research(store, str(tmp_path / "missing.md"), "Future dates after 2026-10-01")
    with pytest.raises(ValueError, match="period"):
        await resume_research(store, str(decision), "soon")
    policy = await resume_research(store, str(decision), "Future untouched period after 2026-10-01")
    assert policy["state"] == "active"
    assert policy["consecutive_rejections"] == 0
    assert policy["failed_holdout_end"] == "2026-10-01"
    assert len(store.audits) == 1
    assert "manual_pause" not in store.values


async def test_resumed_job_cannot_reuse_failed_holdout(settings, monkeypatch):
    policy = {**record_result(None, report(1, final=True)), "state": "active"}
    store = MemoryStore(policy)

    async def collected(*args):
        pass

    monkeypatch.setattr("stock_trader.cli.Alpaca", Broker)
    monkeypatch.setattr("stock_trader.cli.collect_history", collected)
    monkeypatch.setattr(
        "stock_trader.cli.read_history",
        lambda *args: pd.DataFrame(
            {"timestamp": pd.date_range("2026-01-01T15:00:00", periods=200, freq="D", tz="UTC")}
        ),
    )
    monkeypatch.setattr(
        "stock_trader.cli.train_and_evaluate", lambda *args: pytest.fail("spent holdout reused")
    )
    await train_job(configured(settings), store)
    assert store.values["training_status"]["state"] == "waiting_fresh_holdout"
    assert store.values["training_policy"] == policy


def test_fixed_cost_decomposition_and_break_even():
    trades = [{"qty": 10, "entry_price": 100, "exit_price": 102, "gross_pnl": 20}]
    value = fixed_trade_costs(trades, 10000)
    assert value["net_returns"]["0"] == 0.002
    assert value["net_returns"]["20"] == pytest.approx((20 - 1010 * 0.002) / 10000)
    assert value["break_even_bps"] == pytest.approx(20 / 1010 * 10000)
    assert fixed_trade_costs([], 10000)["break_even_bps"] is None


def test_prediction_reuse_rejects_row_mismatch_and_holdout():
    frame = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(["2026-01-01T15:00Z", "2026-01-01T15:05Z"]),
            "symbol": ["AAPL", "AAPL"],
            "feature_valid": [True, True],
            "date": ["2026-01-01", "2026-01-01"],
        }
    )
    saved = frame.copy()
    for family in STRATEGIES:
        saved[family] = np.nan
    validate_predictions(frame, saved, "2026-06-16")
    with pytest.raises(ValueError, match="keys"):
        validate_predictions(frame, saved.iloc[::-1], "2026-06-16")
    with pytest.raises(ValueError, match="holdout"):
        validate_predictions(frame, saved, "2026-01-01")
