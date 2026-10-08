import asyncio
import multiprocessing
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from types import SimpleNamespace

import httpx
import pytest

from stock_trader import api_budget
from stock_trader.api_budget import SharedRequestBudget, retry_delay
from stock_trader.broker import Alpaca, BrokerError


def worker_acquire(path, start, results):
    readings = []
    real_clock = time.monotonic

    def observed_clock():
        now = real_clock()
        readings.append(now)
        return now

    # Measure the real atomic reservation, not later process scheduling/queue delivery.
    api_budget.time = SimpleNamespace(monotonic=observed_clock)
    budget = SharedRequestBudget(path)
    start.wait()
    asyncio.run(budget.acquire("data"))
    results.put(readings[-1])


def test_independent_processes_share_real_request_budget(tmp_path):
    context = multiprocessing.get_context("spawn")
    start, results = context.Event(), context.Queue()
    path = tmp_path / "budget.sqlite3"
    processes = [context.Process(target=worker_acquire, args=(path, start, results)) for _ in range(3)]
    for process in processes:
        process.start()
    start.set()
    times = sorted(results.get(timeout=20) for _ in processes)
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0
    assert min(b - a for a, b in zip(times, times[1:], strict=False)) >= 0.75
    snapshot = SharedRequestBudget(path).snapshot()
    assert snapshot["combined"]["request_attempts"] == 3
    assert snapshot["data"]["request_attempts"] == 3


def test_shared_cooldown_separates_trading_and_reset_on_reboot(tmp_path, monkeypatch):
    monkeypatch.setattr("stock_trader.api_budget.time.monotonic", lambda: 100.0)
    path = tmp_path / "budget.sqlite3"
    first, second = SharedRequestBudget(path), SharedRequestBudget(path)
    assert first.reserve("data") == 0
    first.cooldown("data", 120)
    assert second.reserve("data") == 120
    assert second.reserve("trading") == pytest.approx(0.4)
    monkeypatch.setattr("stock_trader.api_budget.time.monotonic", lambda: 101.0)
    assert second.reserve("trading") == 0
    assert second.snapshot()["data"]["http_429"] == 1
    assert second.snapshot()["combined"]["http_429"] == 1
    second.boot = "next-boot"
    monkeypatch.setattr("stock_trader.api_budget.time.monotonic", lambda: 2.0)
    assert second.reserve("data") == 0


def test_corrupt_budget_fails_instead_of_bypassing(tmp_path):
    path = tmp_path / "budget.sqlite3"
    path.write_bytes(b"broken database")
    with pytest.raises(sqlite3.DatabaseError):
        SharedRequestBudget(path)


def test_retry_after_long_seconds_http_date_and_invalid_header():
    assert retry_delay("120", 60) == 120
    value = format_datetime(datetime.now(UTC) + timedelta(seconds=90), usegmt=True)
    assert 88 <= retry_delay(value, 60) <= 90
    for value in (None, "invalid", "NaN", "inf"):
        assert retry_delay(value, 60) == 60


async def test_429_read_cooldown_shared_and_write_never_retried(settings, monkeypatch):
    calls, delays, cooldowns = [], [], []

    async def sleep(delay):
        delays.append(delay)

    class Budget:
        async def acquire(self, bucket):
            pass

        def cooldown(self, bucket, seconds):
            cooldowns.append((bucket, seconds))

    async def handle(request):
        calls.append(request.method)
        return httpx.Response(
            429 if len(calls) == 1 or request.method == "POST" else 200,
            headers={"Retry-After": "120"},
            json={},
        )

    monkeypatch.setattr("stock_trader.broker.asyncio.sleep", sleep)
    broker = Alpaca(settings, transport=httpx.MockTransport(handle))
    broker._budget = Budget()
    assert await broker.quotes(["AAPL"]) == {}
    assert delays == [120]
    assert cooldowns == [("data", 120)]
    with pytest.raises(BrokerError) as error:
        await broker.submit({"symbol": "AAPL"})
    assert error.value.status == 429
    assert calls == ["GET", "GET", "POST"]
    assert cooldowns[-1] == ("trading", 120)
    await broker.close()
