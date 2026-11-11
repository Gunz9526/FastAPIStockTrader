"""ADR-0056: one frozen ETF order per reconciliation step. Not wired to production."""

from stock_trader.allocation import freeze_plan
from stock_trader.clock import parse_time, utcnow
from stock_trader.etf_ledger import SHARE_STEP, ZERO, number
from stock_trader.etf_study import SYMBOLS
from stock_trader.execution import TERMINAL, client_id


def validate_snapshot(plan, payload, snapshot, now):
    """Technical entry gate only; does not implement a long-term drawdown policy."""
    stamp = parse_time(snapshot["asof"])
    if not 0 <= (now - stamp).total_seconds() <= 30:
        raise ValueError("stale account snapshot")
    if not 0 <= (now - parse_time(plan["snapshot_at"])).total_seconds() <= 30:
        raise ValueError("expired frozen plan")
    account = snapshot["account"]
    if (
        account.get("status") != "ACTIVE"
        or account.get("trading_blocked") is not False
        or account.get("account_blocked") is not False
        or not snapshot["market_open"]
        or not snapshot["reconciled"]
        or snapshot["open_orders"]
    ):
        raise ValueError("account not ready")
    cash, equity = number(account["cash"]), number(account["equity"])
    if cash < 0 or equity <= 0:
        raise ValueError("capital outside registered envelope")
    positions = {s: number(q) for s, q in snapshot["positions"].items() if number(q)}
    owned = {s: number(q) for s, q in snapshot["owned"].items() if number(q)}
    if positions != owned or any(q < 0 for q in positions.values()):
        raise ValueError("strategy ownership mismatch")
    quote = snapshot["quotes"][payload["symbol"]]
    if not 0 <= (now - parse_time(quote["timestamp"])).total_seconds() <= 30:
        raise ValueError("stale quote")
    bid, ask = number(quote["bid"]), number(quote["ask"])
    if bid <= 0 or ask < bid or (ask - bid) / ((ask + bid) / 2) > number("0.0025"):
        raise ValueError("invalid or wide quote")
    asset = snapshot["assets"][payload["symbol"]]
    if (
        asset.get("status") != "active"
        or asset.get("tradable") is not True
        or asset.get("fractionable") is not True
    ):
        raise ValueError("asset not ready")
    qty, price = number(payload["qty"]), number(payload["limit_price"])
    if qty <= 0 or price <= 0 or qty != qty.quantize(SHARE_STEP):
        raise ValueError("invalid order quantity or price")
    if payload["side"] == "buy":
        if snapshot.get("guard", {}).get("additions_allowed") is not True:
            raise ValueError("new investment paused")
        if equity - cash + qty * price > 100000:
            raise ValueError("investment capital cap exceeded")
        if qty * price * number("1.002") + equity * number("0.001") > cash:
            raise ValueError("insufficient confirmed cash")
        if ask > price:
            raise ValueError("quote moved outside frozen limit")
    elif payload["side"] == "sell":
        if qty > owned.get(payload["symbol"], ZERO) or bid < price:
            raise ValueError("sale no longer executable")
    else:
        raise ValueError("invalid side")


class AllocationRunner:
    def __init__(self, executor, snapshot_reader, *, enabled=False):
        self.executor, self.reader, self.enabled = executor, snapshot_reader, enabled

    async def step(self, plan, now=None):
        if not self.enabled:
            return {"state": "disabled", "submitted": False}
        store, broker = self.executor.store, self.executor.broker
        expected_side = "sell" if plan["state"] == "sell_first" else "buy"
        symbols = [p["symbol"] for p in plan["orders"]]
        if len(set(symbols)) != len(symbols) or not set(symbols).issubset(SYMBOLS):
            raise ValueError("invalid plan universe")
        for payload in plan["orders"]:
            if payload["side"] != expected_side or payload["client_order_id"] != client_id(
                "etf-0049", plan["cycle"], expected_side, payload["symbol"]
            ):
                raise ValueError("invalid frozen order identity")
        # Caller holds the SAME account leader lock used by the existing engine.
        await self.executor.fence()
        await freeze_plan(store, plan)
        if await self.executor.recover():
            return {"state": "await_unknown", "submitted": False}
        for payload in plan["orders"]:
            identity = payload["client_order_id"]
            intent = await store.one("SELECT state FROM intents WHERE client_id=%s", (identity,))
            if intent:
                order = await broker.order_by_client(identity)
                if order is None:
                    return {
                        "state": "needs_new_plan"
                        if intent["state"] in ("abandoned", "rejected")
                        else "await_unknown",
                        "submitted": False,
                    }
                if any(order.get(k) != payload[k] for k in ("symbol", "side", "client_order_id")):
                    raise ValueError("broker identity mismatch")
                await store.save_order(order)
                if order["status"] not in TERMINAL:
                    return {"state": "await_fill", "submitted": False}
                if order["status"] != "filled" or number(order["filled_qty"]) != number(payload["qty"]):
                    return {"state": "needs_new_plan", "submitted": False}
                continue
            if (
                payload.get("type") != "limit"
                or payload.get("time_in_force") != "day"
                or payload.get("extended_hours") is not False
            ):
                raise ValueError("unsupported ETF order")
            snapshot = await self.reader()
            validate_snapshot(plan, payload, snapshot, now if now is not None else utcnow())
            order = await self.executor.submit(payload, identity=identity, action="etf_" + payload["side"])
            return {"state": "await_fill" if order else "await_reconciliation", "submitted": True}
        return {"state": "phase_complete", "submitted": False}
