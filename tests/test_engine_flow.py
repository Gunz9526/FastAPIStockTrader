from datetime import timedelta

import httpx
import numpy as np
import pandas as pd
from pydantic import SecretStr

from stock_trader.engine import Engine
from stock_trader.execution import Executor
from stock_trader.notifications import Notifier
from stock_trader.research import train_and_evaluate

from .test_broker_execution import FakeBroker, Fence
from .test_risk_data import make_bars


class AccountBroker(FakeBroker):
    def __init__(self, now):
        super().__init__()
        self.now = now
        self.positions_data = []

    async def account(self):
        return {
            "id": "simulated-paper",
            "status": "ACTIVE",
            "equity": "100000",
            "cash": "100000",
            "buying_power": "100000",
            "trading_blocked": False,
            "account_blocked": False,
        }

    async def clock(self):
        return {
            "timestamp": self.now.isoformat(),
            "is_open": True,
            "next_open": (self.now + timedelta(days=1)).isoformat(),
        }

    async def calendar(self, start, end):
        return [{"date": start, "close": "16:00"}]

    async def recent_orders(self):
        return list(self.orders.values())

    async def open_orders(self):
        return [o for o in self.orders.values() if o["status"] not in {"filled", "canceled", "rejected"}]

    async def positions(self):
        return self.positions_data

    async def quotes(self, symbols):
        return {s: {"t": self.now.isoformat(), "bp": 100.15, "ap": 100.20} for s in symbols}


class Model:
    metadata = {"version": "simulated-validated-model", "threshold": 0.0045, "horizon": 12}

    def refresh(self):
        pass

    def predict(self, frame):
        return np.where(frame["symbol"] == "AAPL", 0.02, 0)


async def test_engine_entry_then_daily_loss_and_restart(store, settings, monkeypatch):
    settings.data_dir.mkdir(parents=True)
    now = pd.Timestamp("2026-10-02T14:45:40Z").to_pydatetime()
    monkeypatch.setattr("stock_trader.engine.utcnow", lambda: now)
    engine = Engine(settings, store)
    original = engine.broker
    broker = AccountBroker(now)
    engine.broker = broker
    engine.executor = Executor(broker, store, Fence())
    engine.model = Model()
    engine.assets = {"AAPL": {"tradable": True}}
    engine.cache.connected = True
    engine.last_backfill = now
    start = pd.Timestamp("2026-10-02T13:30:00Z")
    for symbol in ("AAPL", "SPY", "QQQ"):
        rows = []
        for minute in range(75):
            price = 100 + minute * (0.002 if symbol == "AAPL" else 0.0005)
            rows.append(
                {
                    "t": (start + pd.Timedelta(minutes=minute)).isoformat(),
                    "o": price,
                    "h": price + 0.10,
                    "l": price - 0.10,
                    "c": price + 0.01,
                    "v": 1000,
                }
            )
        engine.cache.seed(symbol, rows)
    await engine.tick()
    assert len(broker.submissions) == 1
    payload = broker.submissions[0]
    assert payload["symbol"] == "AAPL"
    assert payload["order_class"] == "bracket"
    await engine.tick()
    assert len(broker.submissions) == 1
    # Force an observed, owned, fully filled entry with a >1.5% portfolio loss.
    qty = int(payload["qty"])
    parent = broker.orders[payload["client_order_id"]]
    parent.update(
        status="filled",
        qty=str(qty),
        filled_qty=str(qty),
        filled_avg_price="100.21",
        filled_at=now.isoformat(),
    )
    await store.save_order(parent)
    broker.positions_data = [{"symbol": "AAPL", "qty": str(qty), "market_value": str(qty * 80)}]
    await engine.tick()
    assert (await store.get("risk_account"))["daily_latch"] == "daily_loss"
    assert (await store.get("engine_status"))["state"] == "risk_stopped"
    assert broker.submissions[-1]["side"] == "sell"
    total_submissions = len(broker.submissions)
    # Exit remains in flight; it must not be canceled and recreated on the next tick.
    await engine.tick()
    assert len(broker.submissions) == total_submissions
    assert (await store.get("risk_account"))["daily_latch"] == "daily_loss"
    restarted = Engine(settings, store)
    await restarted.broker.close()
    restarted.broker = broker
    restarted.executor = Executor(broker, store, Fence())
    await restarted.tick()
    assert (await store.get("engine_status"))["state"] == "risk_stopped"
    assert len(broker.submissions) == total_submissions
    await restarted.notifier.close()
    await original.close()
    await engine.notifier.close()


async def test_controls_persist_and_resume_does_not_clear_daily_loss(store, settings):
    engine = Engine(settings, store)
    await store.put("risk_account", {"daily_latch": "daily_loss"})
    await store.execute("INSERT INTO controls(command) VALUES('pause'),('resume')")
    await engine.controls()
    assert await store.get("manual_pause") is False
    assert (await store.get("risk_account"))["daily_latch"] == "daily_loss"
    rows = await store.all("SELECT state,result FROM controls ORDER BY id")
    assert [r["state"] for r in rows] == ["completed", "completed"]
    assert rows[0]["result"] == {"applied": "pause"}
    await engine.broker.close()
    await engine.notifier.close()


async def test_discord_rate_limit_retry_queue_and_no_mentions(store, settings):
    configured = settings.model_copy(
        update={"discord_webhook": SecretStr("https://discord.com/api/webhooks/test/test")}
    )
    calls = []

    async def handle(request):
        calls.append(request)
        return (
            httpx.Response(429, json={"retry_after": 20})
            if len(calls) == 1
            else httpx.Response(200, json={"id": "test"})
        )

    notifier = Notifier(configured, store, transport=httpx.MockTransport(handle))
    await store.alert("test-event", "모의 장애 알림")
    await store.alert("test-event", "중복 장애 알림")
    await notifier.flush()
    assert len(calls) == 1
    row = await store.one("SELECT * FROM alerts WHERE key='test-event'")
    assert row["state"] == "pending"
    assert row["attempts"] == 1
    await store.execute("UPDATE alerts SET next_attempt=now() WHERE key='test-event'")
    await notifier.flush()
    row = await store.one("SELECT * FROM alerts WHERE key='test-event'")
    assert row["state"] == "sent"
    assert b'"allowed_mentions":{"parse":[]}' in calls[1].content
    await notifier.close()


def test_full_chronological_training_pipeline_emits_report(settings):
    bars = make_bars(days=185, symbols=("AAPL", "NVDA", "SPY", "QQQ"))
    report = train_and_evaluate(bars, settings)
    assert len(report["candidates"]) == 36
    assert {c["family"] for c in report["candidates"]} == {"lgbm", "xgb", "catboost", "ensemble"}
    assert report["test_start"] > report["data_start"]
    assert list(settings.report_dir.glob("research-*.json"))
    # Synthetic data is never used to authorize production trading.
    assert report["accepted"] is False
    assert not (settings.artifact_dir / "current").exists()
