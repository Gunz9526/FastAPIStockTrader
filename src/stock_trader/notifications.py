from datetime import timedelta
from urllib.parse import urlsplit

import httpx

from stock_trader.clock import utcnow
from stock_trader.config import Settings
from stock_trader.storage import Store


class Notifier:
    def __init__(self, settings: Settings, store: Store, transport=None):
        self.store = store
        self.url = settings.discord_webhook.get_secret_value()
        if self.url:
            parsed = urlsplit(self.url)
            if (
                parsed.scheme != "https"
                or parsed.hostname != "discord.com"
                or not parsed.path.startswith("/api/webhooks/")
                or parsed.fragment
            ):
                raise ValueError("invalid Discord webhook; expected discord.com HTTPS webhook")
        self.client = httpx.AsyncClient(timeout=10, transport=transport, follow_redirects=False)

    async def close(self):
        await self.client.aclose()

    async def flush(self):
        if not self.url:
            return
        # ADR-0070: the engine and independent monitor share one delivery queue.
        # Do not wait behind another sender or hold the account writer lock.
        async with self.store.pool.connection() as conn:
            lock = await (
                await conn.execute("SELECT pg_try_advisory_xact_lock(83499010070) AS acquired")
            ).fetchone()
            if not lock["acquired"]:
                return
            rows = await (
                await conn.execute(
                    "SELECT * FROM alerts WHERE state='pending' AND next_attempt<=now() ORDER BY created_at LIMIT 5"
                )
            ).fetchall()
            for row in rows:
                delay = min(3600, 2 ** min(row["attempts"] + 2, 11))
                state = "pending"
                try:
                    response = await self.client.post(
                        self.url,
                        params={"wait": "true"},
                        json={"content": row["content"], "allowed_mentions": {"parse": []}},
                    )
                    if 200 <= response.status_code < 300:
                        state = "sent"
                    elif response.status_code in (401, 403, 404):
                        state = "disabled"
                    elif response.status_code == 429:
                        delay = max(delay, min(3600, float(response.json().get("retry_after", delay))))
                except httpx.TransportError, ValueError:
                    pass
                await conn.execute(
                    "UPDATE alerts SET state=%s,attempts=attempts+1,next_attempt=%s,"
                    "sent_at=CASE WHEN %s='sent' THEN now() ELSE NULL END WHERE key=%s",
                    (state, utcnow() + timedelta(seconds=delay), state, row["key"]),
                )
