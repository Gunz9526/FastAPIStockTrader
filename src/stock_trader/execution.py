import hashlib
from datetime import timedelta

from stock_trader.broker import AmbiguousSubmission, BrokerError
from stock_trader.clock import parse_time, utcnow

TERMINAL = {"filled", "canceled", "expired", "rejected", "replaced"}


def client_id(*parts) -> str:
    return "st-" + hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:40]


def flatten_orders(orders: list[dict]) -> list[dict]:
    result = {}
    for order in orders:
        result[order["id"]] = order
        for leg in order.get("legs") or []:
            result[leg["id"]] = leg
    return list(result.values())


class Executor:
    def __init__(self, broker, store, leader_connection):
        self.broker = broker
        self.store = store
        self.leader_connection = leader_connection

    async def fence(self):
        # This connection owns the account advisory lock. If it is lost, no further writes are allowed.
        await self.leader_connection.execute("SELECT 1")

    async def submit(self, payload: dict, *, identity: str, action: str):
        payload = {**payload, "client_order_id": identity}
        if not await self.store.prepare(identity, payload["symbol"], action, payload):
            return None
        return await self._send(payload)

    async def _send(self, payload: dict):
        identity = payload["client_order_id"]
        await self.fence()
        await self.store.intent_state(identity, "submitting")
        try:
            order = await self.broker.submit(payload)
        except AmbiguousSubmission:
            await self.store.intent_state(identity, "unknown")
            await self.store.alert(
                f"unknown-{identity}", f"주문 결과 확인 중: {payload['symbol']}. 중복 주문을 차단했습니다."
            )
            return None
        except BrokerError as exc:
            # 429 and duplicate client IDs are resolved through a read before being considered rejected.
            found = await self.broker.order_by_client(identity)
            if found:
                order = found
            elif exc.status in (400, 401, 403, 422):
                await self.store.intent_state(identity, "rejected")
                await self.store.alert(
                    f"rejected-{identity}", f"주문 거절: {payload['symbol']} (상태 {exc.status})"
                )
                return None
            else:
                await self.store.intent_state(identity, "unknown")
                return None
        await self.store.save_order(order)
        await self.store.intent_state(identity, "accepted")
        await self.store.audit(
            "order_submitted", client_id=identity, symbol=payload["symbol"], action=payload["side"]
        )
        return order

    async def recover(self):
        for row in await self.store.unresolved():
            order = await self.broker.order_by_client(row["client_id"])
            if order:
                await self.store.save_order(order)
                await self.store.intent_state(row["client_id"], "accepted")
            elif row["state"] == "prepared":
                # No send was possible before the persistent submitting transition.
                # Abandon stale entries AND exits; exits must recompute current quantity first.
                await self.store.intent_state(row["client_id"], "abandoned")
            # A submitting/unknown write plus a transient 404 is NOT proof of non-submission.
        return await self.store.unresolved()

    async def cancel(self, order_id: str):
        await self.fence()
        try:
            await self.broker.cancel(order_id)
        except AmbiguousSubmission:
            # Future reconciliation will observe final state; no replacement yet.
            pass

    async def expire_entries(self, orders: list[dict]):
        now = utcnow()
        for order in orders:
            if order.get("client_order_id", "").startswith("st-") and order["side"] == "buy":
                submitted = parse_time(order.get("submitted_at") or order["created_at"])
                if now - submitted > timedelta(seconds=90) and order["status"] not in TERMINAL:
                    await self.cancel(order["id"])

    async def close_symbol(self, symbol: str, reason: str, orders: list[dict], positions: list[dict]):
        unresolved = await self.store.unresolved()
        if any(row["symbol"] == symbol for row in unresolved):
            return False
        quantities = await self.store.owned_quantities()
        position = next((p for p in positions if p["symbol"] == symbol), None)
        if position is None:
            return True
        quantity = float(position["qty"])
        if abs(quantities.get(symbol, 0) - quantity) > 1e-6 or quantity <= 0 or not quantity.is_integer():
            await self.store.alert(
                f"ownership-{symbol}", f"포지션 소유권/수량 불일치: {symbol}. 자동 청산을 보류했습니다."
            )
            return False
        # Cancel entries AND protective exits. Then wait for terminal state and recompute actual quantity.
        active = [o for o in flatten_orders(orders) if o["symbol"] == symbol and o["status"] not in TERMINAL]
        if active:
            for order in active:
                root = await self.store.one(
                    "SELECT o.client_id,i.action FROM broker_orders o "
                    "LEFT JOIN intents i ON i.client_id=o.client_id WHERE o.id=%s",
                    (order["id"],),
                )
                if not root:
                    return False
                # A liquidation already in flight must finish before any further exit is sent.
                if root.get("action") == "exit":
                    return False
                await self.cancel(order["id"])
            return False
        # Key on latest entry ID and exit attempt count, never only on day or symbol.
        entries = await self.store.all(
            "SELECT id FROM broker_orders WHERE symbol=%s AND side='buy' "
            "AND filled_qty>0 ORDER BY (payload->>'created_at') DESC LIMIT 1",
            (symbol,),
        )
        if not entries:
            return False
        count = await self.store.one(
            "SELECT count(*) AS count FROM intents WHERE symbol=%s AND action='exit'", (symbol,)
        )
        identity = client_id("exit", entries[0]["id"], count["count"], reason)
        order = await self.submit(
            {
                "symbol": symbol,
                "side": "sell",
                "type": "market",
                "time_in_force": "day",
                "qty": str(int(quantity)),
            },
            identity=identity,
            action="exit",
        )
        return order is not None
