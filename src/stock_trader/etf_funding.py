"""ADR-0072: immutable successor to the original small Paper funding contract."""

from psycopg.types.json import Jsonb

from stock_trader.etf_ledger import fingerprint, number

LEGACY_REGISTRY = "etf-auto-registration-0068"
REGISTRY = "etf-auto-registration-0072"
ACTIVE = "etf-auto-active-0068"
INITIAL = "paper-auto-initial-0068"
LIMIT = number(10000)


def job_key(cycle):
    return "etf-auto-cycle:" + cycle


async def register_funding(store, account_binding, validation_sha256, now):
    legacy = await store.get(LEGACY_REGISTRY, {})
    expected = {
        "paper": True,
        "capital_limit": "1000",
        "account_binding": account_binding,
        "validation_sha256": validation_sha256,
        "strategy": "spy_hold",
        "version": 68,
    }
    if any(legacy.get(k) != v for k, v in expected.items()):
        raise ValueError("funding predecessor not verified")
    rows = await store.all("SELECT value FROM kv WHERE key LIKE %s", ("etf-account-checkpoint:%",))
    if len(rows) != 1 or number(rows[0]["value"]["ledger"]["cash_by_type"].get("JNLC", 0)) != 100000:
        raise ValueError("approved starting capital changed")
    if (
        not rows[0]["value"]["reconciliation"]["strategy_ownership_verified"]
        or rows[0]["value"]["reconciliation"].get("all_fills_owned") is not True
    ):
        raise ValueError("funding requires verified activity ownership")
    contract = {
        **expected,
        "capital_limit": str(LIMIT),
        "version": 72,
        "initial_approved_cash": "100000",
        "capital_fraction": "0.10",
        "predecessor_sha256": fingerprint(legacy),
    }
    prior = await store.get(REGISTRY)
    if prior:
        if any(prior.get(k) != v for k, v in contract.items()):
            raise ValueError("automatic funding contract changed")
        return
    if await store.get(ACTIVE) or await store.unresolved():
        raise ValueError("funding migration requires idle account writer")
    async with store.pool.connection() as conn:
        await conn.execute(
            "INSERT INTO kv(key,value) VALUES(%s,%s)",
            (REGISTRY, Jsonb({**contract, "registered_at": now.isoformat()})),
        )
        await conn.execute(
            "INSERT INTO audit(kind,payload) VALUES(%s,%s)",
            (
                "paper_funding_migration",
                Jsonb(
                    {"version": 72, "capital_limit": str(LIMIT), "predecessor_sha256": fingerprint(legacy)}
                ),
            ),
        )
