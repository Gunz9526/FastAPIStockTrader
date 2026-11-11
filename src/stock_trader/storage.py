import hashlib
from contextlib import asynccontextmanager

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from stock_trader.config import Settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version integer PRIMARY KEY, applied_at timestamptz DEFAULT now());
CREATE TABLE IF NOT EXISTS kv (key text PRIMARY KEY, value jsonb NOT NULL, updated_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS intents (
  client_id text PRIMARY KEY, symbol text NOT NULL, action text NOT NULL,
  payload jsonb NOT NULL, state text NOT NULL DEFAULT 'prepared',
  created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS broker_orders (
  id text PRIMARY KEY, client_id text NOT NULL, symbol text NOT NULL,
  side text NOT NULL, status text NOT NULL, filled_qty numeric NOT NULL DEFAULT 0,
  filled_avg_price numeric NOT NULL DEFAULT 0, payload jsonb NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS broker_orders_client ON broker_orders(client_id);
CREATE TABLE IF NOT EXISTS controls (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, command text NOT NULL,
  state text NOT NULL DEFAULT 'pending', result jsonb,
  created_at timestamptz NOT NULL DEFAULT now(), completed_at timestamptz
);
ALTER TABLE controls ADD COLUMN IF NOT EXISTS payload jsonb NOT NULL DEFAULT '{}';
ALTER TABLE controls ADD COLUMN IF NOT EXISTS request_key text;
CREATE UNIQUE INDEX IF NOT EXISTS controls_request_key ON controls(request_key);
CREATE TABLE IF NOT EXISTS alerts (
  key text PRIMARY KEY, content text NOT NULL, state text NOT NULL DEFAULT 'pending',
  attempts integer NOT NULL DEFAULT 0, next_attempt timestamptz NOT NULL DEFAULT now(),
  created_at timestamptz NOT NULL DEFAULT now(), sent_at timestamptz
);
CREATE TABLE IF NOT EXISTS audit (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, kind text NOT NULL,
  payload jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO schema_version(version) VALUES(1) ON CONFLICT DO NOTHING;
INSERT INTO schema_version(version) VALUES(2) ON CONFLICT DO NOTHING;
"""


class Store:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.pool = AsyncConnectionPool(
            "", kwargs={**settings.db_kwargs(), "row_factory": dict_row}, min_size=1, max_size=4, open=False
        )

    async def open(self):
        await self.pool.open(wait=True, timeout=30)
        # Startup migration is serialized across API, engine and training workers.
        async with self.pool.connection() as conn:
            await conn.execute("SELECT pg_advisory_xact_lock(8349901)")
            await conn.execute(SCHEMA, prepare=False)

    async def close(self):
        await self.pool.close()

    async def one(self, sql: str, args=()):
        async with self.pool.connection() as conn:
            return await (await conn.execute(sql, args)).fetchone()

    async def all(self, sql: str, args=()):
        async with self.pool.connection() as conn:
            return await (await conn.execute(sql, args)).fetchall()

    async def execute(self, sql: str, args=()):
        async with self.pool.connection() as conn:
            await conn.execute(sql, args)

    async def get(self, key: str, default=None):
        row = await self.one("SELECT value FROM kv WHERE key=%s", (key,))
        return row["value"] if row else default

    async def put(self, key: str, value):
        await self.execute(
            "INSERT INTO kv(key,value) VALUES(%s,%s) ON CONFLICT(key) DO UPDATE "
            "SET value=excluded.value, updated_at=now()",
            (key, Jsonb(value)),
        )

    async def audit(self, kind: str, **payload):
        await self.execute("INSERT INTO audit(kind,payload) VALUES(%s,%s)", (kind, Jsonb(payload)))

    async def prepare(self, client_id: str, symbol: str, action: str, payload: dict) -> bool:
        row = await self.one(
            "INSERT INTO intents(client_id,symbol,action,payload) VALUES(%s,%s,%s,%s) "
            "ON CONFLICT DO NOTHING RETURNING client_id",
            (client_id, symbol, action, Jsonb(payload)),
        )
        return row is not None

    async def intent_state(self, client_id: str, state: str):
        await self.execute(
            "UPDATE intents SET state=%s,updated_at=now() WHERE client_id=%s", (state, client_id)
        )

    async def unresolved(self):
        return await self.all("SELECT * FROM intents WHERE state IN ('prepared','submitting','unknown')")

    async def save_order(self, order: dict, owner_client_id: str | None = None):
        client_id = owner_client_id or order.get("client_order_id", "")
        if not client_id.startswith("st-"):
            return
        # REST/stream snapshots can arrive out of order. Do not regress fills or terminal status.
        await self.execute(
            """
            INSERT INTO broker_orders(id,client_id,symbol,side,status,filled_qty,filled_avg_price,payload)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(id) DO UPDATE SET
              status=excluded.status, filled_qty=excluded.filled_qty,
              filled_avg_price=excluded.filled_avg_price,payload=excluded.payload,updated_at=now()
            WHERE excluded.filled_qty >= broker_orders.filled_qty
              AND (broker_orders.status NOT IN ('filled','canceled','expired','rejected','replaced')
                   OR excluded.status IN ('filled','canceled','expired','rejected','replaced'))
            """,
            (
                order["id"],
                client_id,
                order["symbol"],
                order["side"],
                order["status"],
                order.get("filled_qty") or "0",
                order.get("filled_avg_price") or "0",
                Jsonb(order),
            ),
        )
        for leg in order.get("legs") or []:
            await self.save_order(leg, client_id)

    async def owned_quantities(self) -> dict[str, float]:
        rows = await self.all(
            "SELECT symbol,sum(CASE WHEN side='buy' THEN filled_qty ELSE -filled_qty END) "
            "AS qty FROM broker_orders GROUP BY symbol"
        )
        return {row["symbol"]: float(row["qty"]) for row in rows}

    async def cash_flow(self) -> float:
        row = await self.one(
            "SELECT coalesce(sum(CASE WHEN side='sell' THEN filled_qty*filled_avg_price "
            "ELSE -filled_qty*filled_avg_price END),0) AS cash FROM broker_orders"
        )
        return float(row["cash"])

    async def alert(self, key: str, content: str):
        await self.execute(
            "INSERT INTO alerts(key,content) VALUES(%s,%s) ON CONFLICT DO NOTHING", (key, content[:1900])
        )

    @asynccontextmanager
    async def leader(self, identity: str):
        lock_id = int.from_bytes(hashlib.sha256(identity.encode()).digest()[:8], "big", signed=True)
        conn = await AsyncConnection.connect(**self.settings.db_kwargs(), autocommit=True)
        try:
            row = await (await conn.execute("SELECT pg_try_advisory_lock(%s)", (lock_id,))).fetchone()
            if not row[0]:
                raise RuntimeError("another engine owns this account")
            yield conn
        finally:
            await conn.close()
