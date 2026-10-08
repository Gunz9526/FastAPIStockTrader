"""Decimal raw-share research ledger. No broker connection or implied dividend cash."""

import copy
import hashlib
import json
from datetime import date
from decimal import Decimal

ZERO = Decimal(0)
SHARE_STEP = Decimal("0.000000001")


def number(value):
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("nonfinite monetary value")
    return result


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def iso(value):
    return date.fromisoformat(value).isoformat()


class CashLedger:
    def __init__(self, cash):
        self.cash = number(cash)
        if self.cash < 0:
            raise ValueError("negative initial cash")
        self.positions = {}
        self.receivables = {}
        self.events = {}
        self.journal = []
        self.day = None
        self.day_contract = None

    def snapshot(self):
        return {
            "cash": str(self.cash),
            "positions": {s: str(q) for s, q in self.positions.items()},
            "receivables": copy.deepcopy(self.receivables),
            "events": dict(self.events),
            "journal": copy.deepcopy(self.journal),
            "day": self.day,
            "day_contract": self.day_contract,
        }

    @classmethod
    def restore(cls, snapshot):
        ledger = cls(snapshot["cash"])
        ledger.positions = {s: number(q) for s, q in snapshot["positions"].items()}
        if any(q < 0 for q in ledger.positions.values()):
            raise ValueError("negative holding")
        for key in ("receivables", "events", "journal", "day", "day_contract"):
            setattr(ledger, key, copy.deepcopy(snapshot[key]))
        for entry in ledger.receivables.values():
            if number(entry["amount"]) < 0:
                raise ValueError("negative receivable")
            iso(entry["pay_date"])
        return ledger

    def open_day(self, day, actions=()):
        """All-or-nothing entitlement snapshot before any fills for this session."""
        day = iso(day)
        actions = sorted(actions, key=lambda a: a["id"])
        contract = fingerprint(actions)
        if self.day is not None and day <= self.day:
            if day == self.day and contract == self.day_contract:
                return
            raise ValueError("late or changed session actions")
        draft = copy.deepcopy(self)
        draft._open(day, actions)
        draft.day, draft.day_contract = day, contract
        self.__dict__.update(draft.__dict__)

    def _open(self, day, actions):
        if len({a["id"] for a in actions}) != len(actions):
            raise ValueError("duplicate corporate action")
        symbols = [a["symbol"] for a in actions]
        if len(set(symbols)) != len(symbols):
            raise ValueError("multiple same-day actions require explicit ordering")
        for action in actions:
            if iso(action["ex_date"]) != day or not action["id"] or action["id"] in self.events:
                raise ValueError("invalid or reused corporate action")
            symbol = action["symbol"]
            quantity = self.positions.get(symbol, ZERO)
            if action["kind"] == "cash_dividend":
                if action.get("currency") != "USD" or action.get("special") is not False:
                    raise ValueError("unsupported dividend semantics")
                rate, payable = number(action["rate"]), iso(action["pay_date"])
                if rate <= 0 or payable < day:
                    raise ValueError("invalid dividend rate or payable date")
                amount = quantity * rate
                self.receivables[action["id"]] = {
                    "symbol": symbol,
                    "amount": str(amount),
                    "pay_date": payable,
                }
                self.journal.append(
                    {
                        "type": "entitlement",
                        "date": day,
                        "id": action["id"],
                        "quantity": str(quantity),
                        "amount": str(amount),
                    }
                )
            elif action["kind"] == "split":
                ratio = number(action["ratio"])
                if ratio <= 0:
                    raise ValueError("invalid split ratio")
                changed = quantity * ratio
                if changed != changed.quantize(SHARE_STEP):
                    raise ValueError("split requires unsupported cash-in-lieu")
                self.positions[symbol] = changed
                self.journal.append(
                    {
                        "type": "split",
                        "date": day,
                        "id": action["id"],
                        "old_quantity": str(quantity),
                        "new_quantity": str(changed),
                    }
                )
            else:
                raise ValueError("unsupported corporate action")
            self.events[action["id"]] = fingerprint(action)
        for identity, entry in list(self.receivables.items()):
            if entry["pay_date"] <= day:
                self.cash += number(entry["amount"])
                self.journal.append(
                    {
                        "type": "dividend_payment",
                        "date": day,
                        "pay_date": entry["pay_date"],
                        "id": identity,
                        "amount": entry["amount"],
                    }
                )
                del self.receivables[identity]

    def fill(self, identity, symbol, side, quantity, price, fee=0):
        quantity, price, fee = map(number, (quantity, price, fee))
        event = {
            "type": "fill",
            "id": identity,
            "symbol": symbol,
            "side": side,
            "quantity": str(quantity),
            "price": str(price),
            "fee": str(fee),
        }
        digest = fingerprint(event)
        if identity in self.events:
            if self.events[identity] != digest:
                raise ValueError("changed event identity")
            return
        if self.day is None or not identity or quantity <= 0 or price <= 0 or fee < 0:
            raise ValueError("invalid fill")
        if quantity != quantity.quantize(SHARE_STEP) or side not in ("buy", "sell"):
            raise ValueError("invalid fill precision or side")
        current = self.positions.get(symbol, ZERO)
        delta = quantity if side == "buy" else -quantity
        cash = self.cash - delta * price - fee
        if current + delta < 0 or cash < 0:
            raise ValueError("fill requires borrowing or short sale")
        self.positions[symbol], self.cash = current + delta, cash
        self.events[identity] = digest
        self.journal.append({**event, "date": self.day, "cash_after": str(cash)})

    def equity(self, raw_prices):
        holdings = ZERO
        for symbol, quantity in self.positions.items():
            if quantity:
                price = number(raw_prices[symbol])
                if price <= 0:
                    raise ValueError("invalid raw valuation price")
                holdings += quantity * price
        return self.cash + holdings + sum((number(e["amount"]) for e in self.receivables.values()), ZERO)
