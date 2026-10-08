import asyncio
import json
import shutil
from datetime import timedelta

import pandas as pd
from psycopg.types.json import Jsonb

from stock_trader.broker import Alpaca
from stock_trader.clock import NEW_YORK, parse_time, trading_day, utcnow
from stock_trader.config import Settings
from stock_trader.data import BarCache, backfill_cache
from stock_trader.execution import Executor, client_id, flatten_orders
from stock_trader.features import feature_frame
from stock_trader.logging import event
from stock_trader.notifications import Notifier
from stock_trader.research import ActiveModel
from stock_trader.risk import RiskState, entry_plan, loss_reason
from stock_trader.storage import Store


class Engine:
    def __init__(self, settings: Settings, store: Store):
        self.settings = settings
        self.store = store
        self.broker = Alpaca(settings)
        self.notifier = Notifier(settings, store)
        self.cache = BarCache()
        self.model = ActiveModel(settings)
        self.executor = None
        self.tasks = []
        self.assets = {}
        self.last_backfill = None
        self.last_reconcile = None
        self.last_signal = None
        self.calendar = {}
        self.order_stream_connected = False
        self.account_id = None

    async def status(self, state: str, **fields):
        await self.store.put(
            "engine_status",
            {"state": state, "heartbeat": utcnow().isoformat(), "feed": "iex", "paper": True, **fields},
        )

    async def stream_bars(self):
        delay = 1
        while True:
            try:
                self.cache.connected = False
                await backfill_cache(self.broker, self.settings, self.cache)
                self.last_backfill = utcnow()
                async for message in self.broker.stream():
                    self.cache.connected = True
                    if message.get("_status"):
                        continue
                    self.cache.update(message)
                    delay = 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.cache.connected = False
                event("data_stream_error", error=type(exc).__name__)
                await self.store.alert(
                    f"data-{trading_day(utcnow())}",
                    "IEX 시세 연결이 끊겼습니다. 신규 매수를 중단하고 복구를 시도합니다.",
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)

    async def stream_orders(self):
        delay = 1
        while True:
            try:
                self.order_stream_connected = False
                async for message in self.broker.stream(orders=True):
                    self.order_stream_connected = True
                    order = message.get("order")
                    if order:
                        root = await self.store.one(
                            "SELECT client_id FROM broker_orders WHERE id=%s", (order["id"],)
                        )
                        await self.store.save_order(order, root["client_id"] if root else None)
                    delay = 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.order_stream_connected = False
                event("order_stream_error", error=type(exc).__name__)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)

    async def session_close(self, now):
        day = trading_day(now)
        if day not in self.calendar:
            rows = await self.broker.calendar(day, day)
            self.calendar[day] = rows[0] if rows else None
        row = self.calendar[day]
        if row is None:
            return None
        return parse_time(pd.Timestamp(f"{day} {row['close']}", tz=NEW_YORK).to_pydatetime())

    async def reconcile(self, *, full=False):
        if full or not self.last_reconcile or utcnow() - self.last_reconcile > timedelta(seconds=30):
            rows = await self.broker.recent_orders()
            for row in rows:
                root = await self.store.one("SELECT client_id FROM broker_orders WHERE id=%s", (row["id"],))
                await self.store.save_order(row, root["client_id"] if root else None)
            # Keep old unresolved order snapshots current even if outside the seven-day REST window.
            older = await self.store.all(
                "SELECT id,client_id FROM broker_orders WHERE status NOT IN "
                "('filled','canceled','expired','rejected','replaced')"
            )
            for row in older:
                order = await self.broker.order(row["id"])
                await self.store.save_order(order, row["client_id"])
            self.last_reconcile = utcnow()
        orders = await self.broker.open_orders()
        for order in orders:
            root = await self.store.one("SELECT client_id FROM broker_orders WHERE id=%s", (order["id"],))
            await self.store.save_order(order, root["client_id"] if root else None)
        positions = await self.broker.positions()
        owned = await self.store.owned_quantities()
        mismatch = []
        actual = {p["symbol"]: float(p["qty"]) for p in positions}
        for symbol in actual.keys() | owned.keys():
            if abs(actual.get(symbol, 0) - owned.get(symbol, 0)) > 1e-6:
                mismatch.append(symbol)
        foreign_orders = []
        for order in flatten_orders(orders):
            known = await self.store.one("SELECT id FROM broker_orders WHERE id=%s", (order["id"],))
            if not known:
                foreign_orders.append(order["symbol"])
        unresolved = await self.executor.recover()
        return orders, positions, mismatch, foreign_orders, unresolved

    async def controls(self):
        rows = await self.store.all("SELECT * FROM controls WHERE state='pending' ORDER BY id LIMIT 10")
        for row in rows:
            command = row["command"]
            if command == "pause":
                await self.store.put("manual_pause", True)
            elif command == "resume":
                await self.store.put("manual_pause", False)
                # Resume does not clear daily loss. Maximum drawdown requires explicit reset-risk.
            elif command == "flatten":
                await self.store.put("manual_pause", True)
                await self.store.put("flatten_requested", True)
            elif command == "reset-risk":
                await self.store.put("drawdown_latch", None)
                risk = await self.store.get("risk_account", {})
                if risk:
                    risk["peak"] = risk.get("equity", risk["initial_capital"])
                    await self.store.put("risk_account", risk)
                await self.store.put("manual_pause", True)
            await self.store.execute(
                "UPDATE controls SET state='completed',completed_at=now(),result=%s WHERE id=%s",
                (Jsonb({"applied": command}), row["id"]),
            )
            await self.store.audit("control_applied", command=command, control_id=row["id"])

    async def risk_state(self, account, orders, positions) -> RiskState:
        stored = await self.store.get("risk_account")
        if not stored:
            initial = min(self.settings.capital_limit, float(account["equity"]))
            stored = {"account_id": account["id"], "initial_capital": initial, "peak": initial}
        if stored["account_id"] != account["id"]:
            raise ValueError("account changed; explicit account migration required")
        # Strategy P&L derives from persistent fills plus current marks, excluding cash transfers.
        market_value = sum(float(p["market_value"]) for p in positions)
        equity = stored["initial_capital"] + await self.store.cash_flow() + market_value
        day = trading_day(utcnow())
        if stored.get("day") != day:
            stored.update(day=day, day_start=equity, daily_latch=None)
        stored.update(peak=max(stored["peak"], equity), equity=equity)
        await self.store.put("risk_account", stored)
        pending = [o for o in orders if o["side"] == "buy"]
        reserved = sum(
            max(0, float(o["qty"]) - float(o.get("filled_qty") or 0)) * float(o.get("limit_price") or 0)
            for o in pending
        )
        exposure = abs(market_value) + reserved
        position_symbols = {p["symbol"] for p in positions} | {o["symbol"] for o in pending}
        return RiskState(
            min(stored["initial_capital"], equity),
            equity,
            stored["day_start"],
            stored["peak"],
            exposure,
            min(float(account["cash"]), float(account["buying_power"]), equity - exposure),
            len(position_symbols),
        )

    async def manage_exits(self, orders, positions, close, now, force_reason=None):
        # Expire limit entries even when models/data are unavailable.
        await self.executor.expire_entries(orders)
        for position in positions:
            symbol = position["symbol"]
            rows = await self.store.all(
                "SELECT i.payload,i.client_id,o.payload AS broker_payload FROM intents i "
                "JOIN broker_orders o ON i.client_id=o.client_id WHERE i.symbol=%s "
                "AND i.action='entry' AND o.side='buy' AND o.filled_qty>0 "
                "ORDER BY i.created_at DESC LIMIT 1",
                (symbol,),
            )
            if not rows:
                continue
            entry = rows[0]
            parent = entry["broker_payload"]
            held = parse_time(parent.get("filled_at") or parent.get("submitted_at") or parent["created_at"])
            policy = await self.store.get("position_policy-" + entry["client_id"], {"minutes": 60})
            reason = force_reason
            if close and now >= close - timedelta(minutes=10):
                reason = reason or "session_close"
            if now - held > timedelta(minutes=policy["minutes"]):
                reason = reason or "holding_time"
            # Bracket exits do not activate until the full entry fills: close partial positions promptly.
            if 0 < float(parent["filled_qty"]) < float(parent["qty"]) and now - held > timedelta(seconds=90):
                reason = reason or "partial_entry"
            protective = [
                o
                for o in flatten_orders(orders)
                if o["symbol"] == symbol
                and o["side"] == "sell"
                and o.get("type") in ("stop", "stop_limit")
                and o["status"] not in ("filled", "canceled", "expired", "rejected", "replaced")
            ]
            if not protective and now - held > timedelta(seconds=30):
                reason = reason or "unprotected_position"
            if reason:
                await self.executor.close_symbol(symbol, reason, orders, positions)

    async def signals(self, risk, orders, positions, now, close):
        if (
            not self.cache.connected
            or self.last_backfill is None
            or close is None
            or now >= close - timedelta(minutes=30)
        ):
            return "waiting_data_or_entry_window"
        frames = [self.cache.five_minute(symbol, now) for symbol in self.settings.symbols]
        frames = [f for f in frames if not f.empty]
        if not frames:
            return "waiting_bars"
        frame = feature_frame(pd.concat(frames), self.settings.benchmark_symbols)
        if frame.empty:
            return "warming_features"
        target = pd.Timestamp(now).floor("15min") - pd.Timedelta(minutes=5)
        # Wait 35 seconds for corrected minute bars; never trade an old or incomplete bar.
        if now - target.to_pydatetime() > timedelta(minutes=7):
            return "waiting_decision_boundary"
        latest = frame.loc[frame["timestamp"] == target].copy()
        if latest.empty or not set(self.settings.benchmark_symbols).issubset(latest["symbol"]):
            return "waiting_market_context"
        try:
            await asyncio.to_thread(self.model.refresh)
            latest["prediction"] = await asyncio.to_thread(self.model.predict, latest)
        except ValueError, OSError, KeyError, json.JSONDecodeError:
            return "waiting_validated_model"
        metadata = self.model.metadata
        bucket = f"{metadata['version']}:{target.isoformat()}"
        if self.last_signal == bucket:
            return "monitoring"
        self.last_signal = bucket
        await self.store.put(
            "latest_signals",
            latest[["symbol", "prediction", "relative_strength", "ma_distance"]].to_dict("records"),
        )
        held = {p["symbol"] for p in positions} | {o["symbol"] for o in flatten_orders(orders)}
        quotes = await self.broker.quotes(self.settings.trade_symbols)
        for row in latest.sort_values("prediction", ascending=False).to_dict("records"):
            symbol = row["symbol"]
            if (
                symbol in held
                or symbol not in self.assets
                or not self.assets[symbol].get("tradable")
                or symbol not in self.settings.trade_symbols
                or row["prediction"] < metadata["threshold"]
                or row["ma_distance"] <= 0
                or row["relative_strength"] <= 0
            ):
                continue
            exits = await self.store.one(
                "SELECT payload FROM broker_orders WHERE symbol=%s AND side='sell' "
                "AND filled_qty>0 ORDER BY (payload->>'filled_at') DESC NULLS LAST LIMIT 1",
                (symbol,),
            )
            if exits:
                filled_at = exits["payload"].get("filled_at") or exits["payload"].get("updated_at")
                if filled_at and now - parse_time(filled_at) < timedelta(
                    minutes=self.settings.reentry_cooldown_minutes
                ):
                    continue
            quote = quotes.get(symbol)
            if not quote or abs((now - parse_time(quote["t"])).total_seconds()) > 30:
                continue
            if abs(float(quote["ap"]) / row["close"] - 1) > 0.02:
                continue
            plan = entry_plan(
                symbol=symbol,
                bid=float(quote["bp"]),
                ask=float(quote["ap"]),
                atr=row["atr"],
                state=risk,
                settings=self.settings,
                expected_return=row["prediction"],
            )
            if not plan:
                continue
            identity = client_id(metadata["version"], symbol, target.isoformat(), "buy")
            # Persist the exit contract BEFORE sending. Model replacement never changes an open trade's contract.
            await self.store.put(
                "position_policy-" + identity,
                {"minutes": metadata["horizon"] * 5, "model": metadata["version"]},
            )
            await self.executor.submit(plan, identity=identity, action="entry")
            # Reserve funds even when the result is uncertain; the next loop reconciles actual state.
            notional = int(plan["qty"]) * float(plan["limit_price"])
            risk = RiskState(
                risk.capital,
                risk.equity,
                risk.day_start_equity,
                risk.peak_equity,
                risk.exposure + notional,
                risk.cash - notional,
                risk.position_count + 1,
            )
            held.add(symbol)
        return "monitoring"

    async def tick(self):
        now = utcnow()
        await self.controls()
        account = await self.broker.account()
        clock = await self.broker.clock()
        close = await self.session_close(now)
        if abs((now - parse_time(clock["timestamp"])).total_seconds()) > 30:
            await self.status("clock_skew")
            return
        orders, positions, mismatch, foreign, unresolved = await self.reconcile()
        if mismatch or foreign:
            if clock["is_open"]:
                safe = [p for p in positions if p["symbol"] not in set(mismatch + foreign)]
                await self.manage_exits(orders, safe, close, now, "account_mismatch")
            await self.status("ownership_mismatch", symbols=sorted(set(mismatch + foreign)))
            await self.store.alert(
                f"mismatch-{trading_day(now)}",
                "계좌에 전략 소유권이 확인되지 않는 주문/포지션이 있습니다. 자동 매수를 보류합니다.",
            )
            return
        risk = await self.risk_state(account, orders, positions)
        stored = await self.store.get("risk_account")
        reason = loss_reason(risk, self.settings)
        if reason == "daily_loss":
            stored["daily_latch"] = reason
            await self.store.put("risk_account", stored)
        elif reason:
            await self.store.put("drawdown_latch", reason)
        reason = await self.store.get("drawdown_latch") or stored.get("daily_latch")
        force = reason or ("manual_flatten" if await self.store.get("flatten_requested", False) else None)
        is_open = bool(clock["is_open"])
        if is_open:
            if force:
                for order in orders:
                    if order["side"] == "buy":
                        await self.executor.cancel(order["id"])
            await self.manage_exits(orders, positions, close, now, force)
        elif positions:
            await self.store.alert(
                f"overnight-{trading_day(now)}",
                "정규장 종료 후 잔여 포지션이 있습니다. 보호 상태를 확인하고 다음 정규장에 청산합니다.",
            )
        if force and not positions and not orders and not unresolved:
            await self.store.put("flatten_requested", False)
        if reason:
            await self.store.alert(
                f"risk-{trading_day(now)}-{reason}",
                f"위험 한도 중단: {reason}. 신규 매수 중단 및 전략 포지션 청산을 진행합니다.",
            )
        paused = await self.store.get("manual_pause", not self.settings.auto_start)
        low_disk = shutil.disk_usage(self.settings.data_dir).free < 2 * 1024**3
        if not is_open:
            state = "market_closed"
            if close and now >= close and not positions and not unresolved:
                await self.store.alert(
                    f"daily-report-{trading_day(now)}",
                    f"거래일 보고: {trading_day(now)} · 전략 평가액 ${risk.equity:,.2f} · 일간 손익 ${risk.equity - risk.day_start_equity:,.2f} · 잔여 포지션 0",
                )
        elif reason or force:
            state = "risk_stopped" if reason else "flattening"
        elif paused:
            state = "paused"
        elif (
            account.get("trading_blocked")
            or account.get("account_blocked")
            or account.get("status") != "ACTIVE"
        ):
            state = "account_blocked"
        elif unresolved:
            state = "unknown_orders"
        elif low_disk:
            state = "low_disk"
        else:
            state = await self.signals(risk, orders, positions, now, close)
        await self.status(
            state,
            equity=risk.equity,
            day_pnl=risk.equity - risk.day_start_equity,
            exposure=risk.exposure,
            positions=positions,
            open_orders=len(orders),
            unknown_orders=len(unresolved),
            market_open=is_open,
            session_close=close.isoformat() if close else None,
            next_open=clock["next_open"],
            data_connected=self.cache.connected,
            order_stream_connected=self.order_stream_connected,
            last_reconcile=self.last_reconcile.isoformat() if self.last_reconcile else None,
            model=self.model.metadata["version"] if self.model.metadata else None,
        )

    async def run(self, stop: asyncio.Event):
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        try:
            if not self.settings.credentials_present:
                while not stop.is_set():
                    await self.status("waiting_alpaca_keys")
                    await self.controls()
                    # No startup notification before keys exist: quiet idle mode.
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=10)
                    except TimeoutError:
                        pass
                return
            account = await self.broker.account()
            self.account_id = account["id"]
            async with self.store.leader("alpaca-" + self.account_id) as conn:
                self.executor = Executor(self.broker, self.store, conn)
                await self.reconcile(full=True)
                for symbol in self.settings.trade_symbols:
                    self.assets[symbol] = await self.broker.asset(symbol)
                self.tasks = [
                    asyncio.create_task(self.stream_bars()),
                    asyncio.create_task(self.stream_orders()),
                ]
                await self.store.alert(
                    f"engine-start-{trading_day(utcnow())}",
                    "자동 매매 엔진이 시작되었습니다. Paper 계좌 대조·자료·검증 모델 조건을 확인합니다.",
                )
                while not stop.is_set():
                    await self.executor.fence()
                    try:
                        await self.tick()
                    except Exception as exc:
                        event("engine_tick_error", error=type(exc).__name__)
                        await self.status("recovering", error=type(exc).__name__)
                        await self.store.alert(
                            f"engine-error-{trading_day(utcnow())}",
                            "거래 엔진 오류로 신규 매수를 보류하고 계좌 상태를 다시 대조합니다.",
                        )
                    await self.notifier.flush()
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=self.settings.poll_seconds)
                    except TimeoutError:
                        pass
        finally:
            for task in self.tasks:
                task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)
            await self.broker.close()
            await self.notifier.close()
