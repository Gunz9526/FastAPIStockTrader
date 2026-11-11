"""ADR-0089 research accounting. Pure functions; no broker, I/O or implicit fills."""

import copy
from datetime import date
from decimal import ROUND_DOWN

from stock_trader.etf_ledger import SHARE_STEP, ZERO, number
from stock_trader.research_seal import fingerprint


def initial(cash="10000"):
    cash = number(cash)
    if cash <= 0:
        raise ValueError("invalid research capital")
    return {
        "cash": str(cash),
        "positions": {},
        "receivables": {},
        "events": {},
        "retired": [],
        "journal": [],
        "day": None,
        "session_sha256": None,
    }


def iso(value):
    return date.fromisoformat(value).isoformat()


def mark(ledger, prices, *, receivables=True):
    cash = number(ledger["cash"])
    if cash < 0:
        raise ValueError("negative research cash")
    for symbol, value in ledger["positions"].items():
        quantity = number(value)
        if quantity < 0 or quantity != quantity.quantize(SHARE_STEP):
            raise ValueError("invalid research quantity")
        if quantity:
            price = number(prices[symbol])
            if price <= 0:
                raise ValueError("invalid raw mark")
            cash += quantity * price
    for entry in ledger["receivables"].values():
        amount = number(entry["amount"])
        if amount < 0:
            raise ValueError("negative research receivable")
        iso(entry["pay_date"])
        if receivables:
            cash += amount
    return cash


def actions_at_open(ledger, day, actions):
    ids, symbols = set(), set()
    for action in sorted(actions, key=lambda row: row["id"]):
        identity, symbol, kind = action["id"], action["symbol"], action["kind"]
        if not identity or identity in ids or symbol in symbols or identity in ledger["events"]:
            raise ValueError("duplicate or unordered corporate action")
        ids.add(identity)
        symbols.add(symbol)
        effective = iso(action["ex_date"])
        # Non-session events are processed before the next regular-session fills.
        if effective > day or (ledger["day"] is not None and effective <= ledger["day"]):
            raise ValueError("late or future corporate action")
        quantity = number(ledger["positions"].get(symbol, "0"))
        if kind == "split":
            ratio = number(action["ratio"])
            changed = quantity * ratio
            if ratio <= 0 or changed != changed.quantize(SHARE_STEP):
                raise ValueError("unsupported split or cash in lieu")
            ledger["positions"][symbol] = str(changed)
            ledger["journal"].append(
                {"type": "split", "id": identity, "date": effective, "quantity": str(changed)}
            )
        elif kind in {"cash_dividend", "cash_merger"}:
            if action.get("currency") != "USD" or (
                kind == "cash_dividend" and action.get("special") is not False
            ):
                raise ValueError("unsupported cash action semantics")
            rate, payable = number(action["rate"]), iso(action["pay_date"])
            if rate <= 0 or payable < effective:
                raise ValueError("invalid cash action terms")
            ledger["receivables"][identity] = {
                "symbol": symbol,
                "kind": kind,
                "amount": str(quantity * rate),
                "pay_date": payable,
            }
            ledger["journal"].append(
                {
                    "type": "entitlement",
                    "id": identity,
                    "date": effective,
                    "quantity": str(quantity),
                    "amount": str(quantity * rate),
                }
            )
            if kind == "cash_merger":
                ledger["positions"][symbol] = "0"
                ledger["retired"].append(symbol)
        else:
            raise ValueError("unsupported corporate action")
        ledger["events"][identity] = fingerprint(action)
    for identity, entry in list(ledger["receivables"].items()):
        if entry["pay_date"] <= day:
            ledger["cash"] = str(number(ledger["cash"]) + number(entry["amount"]))
            ledger["journal"].append({"type": "payment", "id": identity, "date": day, **entry})
            del ledger["receivables"][identity]


def rebalance(ledger, day, weights, prices, cost_bps):
    values = {symbol: number(weight) for symbol, weight in weights.items()}
    if any(w <= 0 or w > 1 for w in values.values()) or sum(values.values(), ZERO) > 1:
        raise ValueError("invalid long-only target")
    if set(values).intersection(ledger["retired"]):
        raise ValueError("target contains retired security")
    rate = number(cost_bps) / 10000
    if rate < 0 or rate > number(".01"):
        raise ValueError("invalid research cost assumption")
    capital = mark(ledger, prices, receivables=False) * (1 - rate) / (1 + rate)
    desired = {}
    for symbol, weight in values.items():
        price = number(prices[symbol])
        if price <= 0:
            raise ValueError("invalid raw fill proxy")
        desired[symbol] = (capital * weight / price).quantize(SHARE_STEP, rounding=ROUND_DOWN)
    deltas = {
        symbol: desired.get(symbol, ZERO) - number(ledger["positions"].get(symbol, "0"))
        for symbol in set(ledger["positions"]) | set(desired)
    }
    for symbol in sorted(deltas, key=lambda symbol: (deltas[symbol] >= 0, symbol)):
        delta = deltas[symbol]
        if not delta:
            continue
        price = number(prices[symbol])
        if price <= 0:
            raise ValueError("invalid raw fill proxy")
        fee = abs(delta) * price * rate
        cash = number(ledger["cash"]) - delta * price - fee
        if cash < 0:
            raise ValueError("research borrowing prohibited")
        ledger["cash"] = str(cash)
        ledger["positions"][symbol] = str(number(ledger["positions"].get(symbol, "0")) + delta)
        ledger["journal"].append(
            {
                "type": "virtual_fill",
                "date": day,
                "symbol": symbol,
                "quantity_delta": str(delta),
                "raw_proxy": str(price),
                "cost": str(fee),
                "cash_after": str(cash),
            }
        )


def advance(previous, day, actions, opening, closing, weights, cost_bps):
    """Return a new complete session; exceptions never mutate the caller's state."""
    day = iso(day)
    specification = fingerprint(
        {
            "day": day,
            "actions": actions,
            "open": opening,
            "close": closing,
            "weights": weights,
            "bps": cost_bps,
        }
    )
    if previous["day"] is not None and day <= previous["day"]:
        if day == previous["day"] and specification == previous["session_sha256"]:
            return copy.deepcopy(previous)
        raise ValueError("changed or out-of-order research session")
    draft = copy.deepcopy(previous)
    actions_at_open(draft, day, actions)
    mark(draft, opening)
    if weights is not None:
        rebalance(draft, day, weights, opening, cost_bps)
    draft["equity"] = str(mark(draft, closing))
    draft["day"], draft["session_sha256"] = day, specification
    return draft


def compare(previous, day, actions, opening, closing, target=None):
    """Identical assumptions for candidate and monthly target-exposure SPY control."""
    weights = target
    benchmark = {"SPY": str(sum(map(number, weights.values()), ZERO))} if weights else {}
    if weights is None:
        benchmark = None
    elif not number(benchmark.get("SPY", "0")):
        benchmark = {}
    outputs = {}
    for bps in ("10", "25"):
        for name, intended in (("candidate", weights), ("spy", benchmark)):
            key = f"{name}_{bps}bps"
            ledger = (previous or {}).get(key, initial())
            outputs[key] = advance(ledger, day, actions, opening, closing, intended, bps)
    return outputs
