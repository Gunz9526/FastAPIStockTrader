"""ADR-0055: posted account cash, not research entitlements or strategy ownership."""

import copy

from stock_trader.etf_ledger import SHARE_STEP, ZERO, fingerprint, number
from stock_trader.etf_study import SYMBOLS

FIELDS = {
    "id",
    "activity_type",
    "date",
    "transaction_time",
    "order_id",
    "symbol",
    "side",
    "qty",
    "price",
    "net_amount",
    "type",
    "per_share_amount",
}
CASH_TYPES = {"DIV", "DIVNRA", "DIVFT", "DIVTW", "DIVFEE", "FEE", "CSD", "CSW"}


async def read_activities(broker, *, after=None):
    """Bounded full-history audit. Never advances a cursor after partial pagination."""
    rows, seen, token = [], set(), None
    for page in range(20):
        params = {"direction": "asc", "page_size": 100}
        if after is not None:
            params["after"] = after
        if token:
            params["page_token"] = token
        batch = await broker.request("GET", "/v2/account/activities", params=params)
        if not isinstance(batch, list) or len(batch) > 100:
            raise ValueError("invalid activity page")
        for row in batch:
            identity = row.get("id")
            if not identity or identity in seen:
                raise ValueError("repeated activity page or identity")
            seen.add(identity)
            rows.append({k: v for k, v in row.items() if k in FIELDS})
        if len(batch) < 100:
            return rows, page + 1
        token = batch[-1]["id"]
    raise ValueError("activity pagination incomplete")


class AccountLedger:
    def __init__(self, opening_cash, *, reviewed_journals=None):
        self.cash = number(opening_cash)
        if self.cash < 0:
            raise ValueError("negative opening cash")
        self.positions, self.events, self.cash_by_type = {}, {}, {}
        self.reviewed_journals = dict(reviewed_journals or {})

    def snapshot(self):
        return {
            "cash": str(self.cash),
            "positions": {s: str(q) for s, q in self.positions.items()},
            "events": dict(self.events),
            "reviewed_journals": dict(self.reviewed_journals),
            "cash_by_type": {k: str(v) for k, v in self.cash_by_type.items()},
        }

    @classmethod
    def restore(cls, snapshot):
        result = cls(snapshot["cash"], reviewed_journals=snapshot.get("reviewed_journals", {}))
        result.positions = {s: number(q) for s, q in snapshot["positions"].items()}
        if any(s not in SYMBOLS or q < 0 or q != q.quantize(SHARE_STEP) for s, q in result.positions.items()):
            raise ValueError("invalid restored positions")
        result.events = dict(snapshot["events"])
        result.cash_by_type = {k: number(v) for k, v in snapshot["cash_by_type"].items()}
        return result

    def apply(self, rows):
        draft = copy.deepcopy(self)
        for row in rows:
            draft._apply(row)
        self.__dict__.update(draft.__dict__)

    def _apply(self, raw):
        row = {k: v for k, v in raw.items() if k in FIELDS}
        identity, kind = row["id"], row["activity_type"]
        if not identity:
            raise ValueError("missing activity identity")
        digest = fingerprint(row)
        if identity in self.events:
            if self.events[identity] != digest:
                raise ValueError("changed activity requires correction review")
            return
        if kind == "FILL":
            symbol, side = row["symbol"], row["side"]
            quantity, price = number(row["qty"]), number(row["price"])
            if (
                symbol not in SYMBOLS
                or side not in ("buy", "sell")
                or quantity <= 0
                or price <= 0
                or quantity != quantity.quantize(SHARE_STEP)
                or row.get("type") not in ("fill", "partial_fill")
                or not row.get("order_id")
            ):
                raise ValueError("unsupported fill")
            delta = quantity if side == "buy" else -quantity
            self.positions[symbol] = self.positions.get(symbol, ZERO) + delta
            if self.positions[symbol] < 0:
                raise ValueError("short position or incomplete history")
            change = -delta * price
        elif kind in CASH_TYPES or (kind == "JNLC" and self.reviewed_journals.get(identity) == digest):
            change = number(row["net_amount"])
            if (kind == "CSD" and change < 0) or (kind == "CSW" and change > 0):
                raise ValueError("invalid cash transfer sign")
        else:
            raise ValueError("unsupported account activity")
        self.cash += change
        if self.cash < 0:
            raise ValueError("negative cash or incomplete history")
        self.cash_by_type[kind] = self.cash_by_type.get(kind, ZERO) + change
        self.events[identity] = digest

    def reconcile(self, *, cash, positions):
        actual = {s: number(q) for s, q in positions.items() if number(q) != 0}
        expected = {s: q for s, q in self.positions.items() if q != 0}
        difference = number(cash) - self.cash
        matched = abs(difference) <= number("0.01") and actual == expected
        return {
            "matched": matched,
            "cash_difference": str(difference),
            "positions_match": actual == expected,
            "activity_count": len(self.events),
            "strategy_ownership_verified": False,
            "cash_by_type": {k: str(v) for k, v in self.cash_by_type.items()},
        }
