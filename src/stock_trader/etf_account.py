"""ADR-0059: read-only broker adapter and atomic posted account checkpoints."""

import hashlib
from datetime import date, datetime, time, timedelta

from psycopg.types.json import Jsonb

from stock_trader.account_ledger import AccountLedger, read_activities
from stock_trader.clock import NEW_YORK, parse_time, utcnow
from stock_trader.etf_guard import observe_guard
from stock_trader.etf_ledger import ZERO, fingerprint, number
from stock_trader.etf_shadow import read_sealed

BOOTSTRAP_SOURCE = "312d7c2f88106082fbb812cfd975abc8d54277ab5b6dd687d72a16646aa88b32"
BOOTSTRAP_EVENT = "31de6f12fa105206688b4195a968e7f3dc9120493a8cf2d84742516efecddc37"


def reviewed_bootstrap(settings):
    root = settings.report_dir / "account-audit-0055"
    source = root / "result.json"
    if hashlib.sha256(source.read_bytes()).hexdigest() != BOOTSTRAP_SOURCE:
        raise ValueError("bootstrap source changed")
    corrected = read_sealed(root / "corrected.json")
    original = read_sealed(source)
    approval = corrected["ledger"]["reviewed_journals"]
    if (
        corrected["source_sha256"] != BOOTSTRAP_SOURCE
        or len(approval) != 1
        or set(approval.values()) != {BOOTSTRAP_EVENT}
        or len(original["activities"]) != 1
        or fingerprint(original["activities"][0]) != BOOTSTRAP_EVENT
    ):
        raise ValueError("unreviewed bootstrap")
    return approval


def economic_time(row):
    stamp = row.get("transaction_time") or row["date"]
    if len(stamp) == 10:
        stamp = datetime.combine(date.fromisoformat(stamp), time.min, NEW_YORK)
    return parse_time(stamp).isoformat(), row["id"]


def replay_checkpoint(previous, rows, *, full, approval, account, positions, order_ids, now):
    """Pure all-or-nothing merge/replay, including late economic activity."""
    old_rows = previous.get("rows", {}) if previous else {}
    incoming = {r["id"]: r for r in rows}
    if len(incoming) != len(rows) or (full and not set(old_rows).issubset(incoming)):
        raise ValueError("duplicate or deleted activity")
    for identity, row in incoming.items():
        if identity in old_rows and fingerprint(row) != fingerprint(old_rows[identity]):
            raise ValueError("corrected activity requires review")
    merged = incoming if full else {**old_rows, **incoming}
    ledger = AccountLedger(0, reviewed_journals=approval)
    ordered = sorted(merged.values(), key=economic_time)
    ledger.apply(ordered)
    reconciled = ledger.reconcile(cash=account["cash"], positions=positions)
    if not reconciled["matched"]:
        raise ValueError("posted account mismatch")
    owned = {}
    for row in ordered:
        if row["activity_type"] == "FILL" and row["order_id"] in order_ids:
            delta = number(row["qty"]) * (1 if row["side"] == "buy" else -1)
            owned[row["symbol"]] = owned.get(row["symbol"], ZERO) + delta
    actual = {s: number(q) for s, q in positions.items() if number(q)}
    owned = {s: q for s, q in owned.items() if q}
    if any(q < 0 for q in owned.values()):
        raise ValueError("strategy history requires short sale")
    reconciled["all_fills_owned"] = all(
        row["activity_type"] != "FILL" or row["order_id"] in order_ids for row in ordered
    )
    reconciled["strategy_ownership_verified"] = actual == owned and reconciled["all_fills_owned"]
    flows = sum((number(ledger.cash_by_type.get(k, ZERO)) for k in ("CSD", "CSW")), ZERO)
    guard = observe_guard(
        previous.get("guard") if previous else None,
        equity=account["equity"],
        day=now.astimezone(NEW_YORK).date().isoformat(),
        external_flows=flows,
    )
    return {
        "rows": merged,
        "ledger": ledger.snapshot(),
        "guard": guard,
        "owned": {s: str(q) for s, q in owned.items()},
        "reconciliation": reconciled,
        "last_full_at": now.isoformat() if full else previous["last_full_at"],
        "last_success_at": now.isoformat(),
    }


def quantities(rows):
    result = {r["symbol"]: r["qty"] for r in rows}
    if len(result) != len(rows):
        raise ValueError("duplicate account positions")
    return result


class AccountMonitor:
    def __init__(self, broker, store, approval):
        self.broker, self.store, self.approval = broker, store, approval

    async def sync(self, now=None):
        start = now or utcnow()
        before = await self.broker.account()
        identity = hashlib.sha256(before["id"].encode()).hexdigest()[:24]
        key = "etf-account-checkpoint:" + identity
        async with self.store.pool.connection() as conn:
            await conn.execute("SET LOCAL lock_timeout = '15s'")
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("etf-sync:" + key,))
            return await self._sync(conn, before, key, start, now)

    async def _sync(self, conn, before, key, start, now):
        row = await (await conn.execute("SELECT value FROM kv WHERE key=%s", (key,))).fetchone()
        previous = row["value"] if row else {}
        full = not previous or start - parse_time(previous["last_full_at"]) >= timedelta(hours=24)
        after = None if full else (start - timedelta(days=7)).isoformat()
        rows, pages = await read_activities(self.broker, after=after)
        positions = quantities(await self.broker.positions())
        open_orders = await self.broker.open_orders()
        final_positions = quantities(await self.broker.positions())
        account = await self.broker.account()
        if before["id"] != account["id"] or before["cash"] != account["cash"] or positions != final_positions:
            raise ValueError("account changed while reconciling")
        approved = await (
            await conn.execute(
                "SELECT o.id FROM broker_orders o JOIN intents i ON i.client_id=o.client_id "
                "WHERE i.action IN ('etf_buy','etf_sell')"
            )
        ).fetchall()
        checkpoint = replay_checkpoint(
            previous,
            rows,
            full=full,
            approval=self.approval,
            account=account,
            positions=final_positions,
            order_ids={r["id"] for r in approved},
            now=now or utcnow(),
        )
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (key,))
        current = await (await conn.execute("SELECT value FROM kv WHERE key=%s", (key,))).fetchone()
        if (current["value"] if current else {}) != previous:
            raise ValueError("concurrent checkpoint changed")
        await conn.execute(
            "INSERT INTO kv(key,value) VALUES(%s,%s) ON CONFLICT(key) DO UPDATE "
            "SET value=excluded.value,updated_at=now()",
            (key, Jsonb(checkpoint)),
        )
        return {
            "asof": start.isoformat(),
            "account": account,
            "positions": final_positions,
            "owned": checkpoint["owned"],
            "open_orders": open_orders,
            "reconciled": checkpoint["reconciliation"]["matched"]
            and checkpoint["reconciliation"]["strategy_ownership_verified"],
            "guard": checkpoint["guard"],
            "last_success_at": checkpoint["last_success_at"],
            "full_audit": full,
            "read_requests": pages + 5,
            "activity_count": len(checkpoint["rows"]),
            "funding": funding_summary(checkpoint, account, {r["id"] for r in approved}),
        }


def funding_summary(checkpoint, account, order_ids):
    buys, fees = ZERO, ZERO
    for row in checkpoint["rows"].values():
        if row["activity_type"] == "FILL" and row["order_id"] in order_ids and row["side"] == "buy":
            buys += number(row["qty"]) * number(row["price"])
        if row["activity_type"] == "FEE":
            fees += max(-number(row["net_amount"]), ZERO)
    initial = number(checkpoint["ledger"]["cash_by_type"].get("JNLC", ZERO))
    return {
        "buy_cash_debits": str(buys),
        "negative_fees": str(fees),
        "cash_used": str(max(buys + fees, initial - number(account["cash"]), ZERO)),
    }


async def market_snapshot(broker, account_snapshot):
    clock = await broker.clock()
    result = {
        **account_snapshot,
        "market_open": bool(clock["is_open"]),
        "clock": clock,
        "market_read_requests": 1,
    }
    if not result["market_open"]:
        return result
    symbols = sorted(set(result["positions"]) | {"SPY"})
    result["assets"] = {s: await broker.asset(s) for s in symbols}
    raw = await broker.quotes(symbols)
    result["quotes"] = {s: {"bid": q["bp"], "ask": q["ap"], "timestamp": q["t"]} for s, q in raw.items()}
    result["market_read_requests"] += len(symbols) + 1
    return result
