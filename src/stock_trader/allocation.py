"""ADR-0049: read-only target allocation planner; never submits an order."""

from datetime import datetime
from decimal import ROUND_CEILING, ROUND_FLOOR

from psycopg.types.json import Jsonb

from stock_trader.etf_ledger import SHARE_STEP, ZERO, number
from stock_trader.etf_study import SYMBOLS
from stock_trader.execution import TERMINAL, client_id


def plan_allocation(
    *, cycle, weights, positions, owned, cash, quotes, assets, now, market_open, open_orders=(), unresolved=()
):
    """Return sell-only or cash-funded buy-only plans from a reconciled snapshot.

    The caller must persist/freeze a cycle, reconcile all previous intents, then
    obtain fresh positions/cash/quotes before generating any subsequent cycle.
    Unknown writes and partially filled orders block all new plans.
    """
    if not cycle or len(cycle) > 160 or now.tzinfo is None:
        raise ValueError("invalid cycle or clock")
    if not market_open:
        return {"state": "market_closed", "orders": []}
    if unresolved or any(o.get("status") not in TERMINAL for o in open_orders):
        return {"state": "await_reconciliation", "orders": []}
    weights = {s: number(w) for s, w in weights.items()}
    positions = {s: number(q) for s, q in positions.items()}
    owned = {s: number(q) for s, q in owned.items()}
    cash = number(cash)
    symbols = set(weights) | set(positions) | set(owned)
    if not symbols or not symbols.issubset(SYMBOLS):
        raise ValueError("unsupported allocation universe")
    if cash < 0 or any(w < 0 for w in weights.values()) or sum(weights.values()) > 1:
        raise ValueError("cash or weights require borrowing")
    if any(positions.get(s, ZERO) != owned.get(s, ZERO) or positions.get(s, ZERO) < 0 for s in symbols):
        raise ValueError("ownership mismatch")
    if any(q != q.quantize(SHARE_STEP) for q in positions.values()):
        raise ValueError("unsupported position precision")
    mids, limits = {}, {}
    for symbol in sorted(symbols):
        asset, quote = assets[symbol], quotes[symbol]
        if (
            asset.get("status") != "active"
            or asset.get("tradable") is not True
            or asset.get("fractionable") is not True
        ):
            raise ValueError("ETF not tradable/fractionable")
        timestamp = datetime.fromisoformat(quote["timestamp"])
        if timestamp.tzinfo is None or not 0 <= (now - timestamp).total_seconds() <= 30:
            raise ValueError("stale or future quote")
        bid, ask = number(quote["bid"]), number(quote["ask"])
        if bid <= 0 or ask < bid or bid < 1:
            raise ValueError("invalid ETF quote")
        mids[symbol] = (bid + ask) / 2
        limits[symbol] = {
            "sell": bid.quantize(number("0.01"), rounding=ROUND_FLOOR),
            "buy": ask.quantize(number("0.01"), rounding=ROUND_CEILING),
        }
    equity = cash + sum((positions.get(s, ZERO) * mids[s] for s in symbols), ZERO)
    target_capital = equity * number("0.999")
    targets = {
        s: (target_capital * weights.get(s, ZERO) / mids[s]).quantize(SHARE_STEP, rounding=ROUND_FLOOR)
        for s in symbols
    }
    sales = {s: positions.get(s, ZERO) - targets[s] for s in symbols if positions.get(s, ZERO) > targets[s]}
    # At least USD 1 notional avoids dust orders; zero-target liquidation remains explicit.
    sales = {s: q for s, q in sales.items() if q * limits[s]["sell"] >= 1 or weights.get(s, ZERO) == 0}
    side = "sell" if sales else "buy"
    quantities = sales or {s: max(targets[s] - positions.get(s, ZERO), ZERO) for s in symbols}
    budget = max(cash - equity * number("0.001"), ZERO)
    requested = sum((q * limits[s]["buy"] * number("1.002") for s, q in quantities.items()), ZERO)
    scale = min(number(1), budget / requested) if requested else ZERO
    orders = []
    reserved = ZERO
    for symbol, quantity in sorted(quantities.items()):
        if side == "buy":
            quantity = (quantity * scale).quantize(SHARE_STEP, rounding=ROUND_FLOOR)
        price = limits[symbol][side]
        if quantity <= 0 or (quantity * price < 1 and (side == "buy" or weights.get(symbol, ZERO) != 0)):
            continue
        if side == "buy":
            reserved += quantity * price * number("1.002")
        orders.append(
            {
                "symbol": symbol,
                "side": side,
                "type": "limit",
                "time_in_force": "day",
                "extended_hours": False,
                "qty": str(quantity),
                "limit_price": str(price),
                "client_order_id": client_id("etf-0049", cycle, side, symbol),
            }
        )
    if reserved > budget:
        raise ValueError("buy plan exceeds available cash")
    return {
        "state": "sell_first" if side == "sell" else "cash_funded_buys",
        "orders": orders,
        "cycle": cycle,
        "snapshot_at": now.isoformat(),
        "cash": str(cash),
        "reserved_buy_cash": str(reserved),
        "cash_buffer": str(equity * number("0.001")),
    }


async def freeze_plan(store, plan):
    """Persist a reviewed plan atomically; a changed plan needs a new cycle ID."""
    if not plan.get("orders") or plan["state"] not in ("sell_first", "cash_funded_buys"):
        raise ValueError("only actionable plans can be frozen")
    key = f"etf-plan:{plan['cycle']}:{plan['state']}"
    async with store.pool.connection() as conn:
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (key,))
        row = await (await conn.execute("SELECT value FROM kv WHERE key=%s", (key,))).fetchone()
        if row:
            if row["value"] != plan:
                raise ValueError("cycle already frozen with a different plan")
            return row["value"]
        await conn.execute("INSERT INTO kv(key,value) VALUES(%s,%s)", (key, Jsonb(plan)))
    return plan
