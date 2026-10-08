import asyncio
import json
from datetime import timedelta
from urllib.parse import quote

import httpx
import websockets

from stock_trader.api_budget import SharedRequestBudget, retry_delay
from stock_trader.clock import parse_time, utcnow
from stock_trader.config import Settings

PAPER_URL = "https://paper-api.alpaca.markets"
DATA_URL = "https://data.alpaca.markets"
PAPER_STREAM = "wss://paper-api.alpaca.markets/stream"
IEX_STREAM = "wss://stream.data.alpaca.markets/v2/iex"


class BrokerError(Exception):
    def __init__(self, status: int, code: str = ""):
        # Never include response body, request headers or URLs in public errors.
        self.status = status
        super().__init__(f"Alpaca status={status} code={code}")


class AmbiguousSubmission(Exception):
    pass


class Alpaca:
    def __init__(self, settings: Settings, transport=None):
        self.settings = settings
        self.client = httpx.AsyncClient(
            headers={
                "APCA-API-KEY-ID": settings.alpaca_key.get_secret_value(),
                "APCA-API-SECRET-KEY": settings.alpaca_secret.get_secret_value(),
            },
            timeout=httpx.Timeout(15, connect=10),
            transport=transport,
            follow_redirects=False,
        )
        self._request_lock = asyncio.Lock()
        self._last_request = 0.0
        # MockTransport tests do not consume the production host's request budget.
        self._budget = (
            None
            if isinstance(transport, httpx.MockTransport)
            else SharedRequestBudget(settings.data_dir / "runtime" / "alpaca-budget.sqlite3")
        )

    async def close(self):
        await self.client.aclose()

    async def request(self, method: str, path: str, *, data_api=False, params=None, payload=None):
        base = DATA_URL if data_api else PAPER_URL
        for attempt in range(4):
            if self._budget is not None:
                await self._budget.acquire("data" if data_api else "trading")
            else:
                async with self._request_lock:
                    loop = asyncio.get_running_loop()
                    await asyncio.sleep(max(0, 0.4 - (loop.time() - self._last_request)))
                    self._last_request = loop.time()
            try:
                response = await self.client.request(method, base + path, params=params, json=payload)
            except httpx.TransportError:
                if method != "GET":
                    raise AmbiguousSubmission("write result unknown") from None
                if attempt == 3:
                    raise BrokerError(0, "transport") from None
                await asyncio.sleep(min(2**attempt, 8))
                continue
            delay = retry_delay(
                response.headers.get("Retry-After"), 60 if response.status_code == 429 else 2**attempt
            )
            if response.status_code == 429 and self._budget is not None:
                await asyncio.to_thread(self._budget.cooldown, "data" if data_api else "trading", delay)
            if method == "GET" and (response.status_code == 429 or response.status_code >= 500):
                if attempt < 3:
                    await asyncio.sleep(delay)
                    continue
            if response.status_code >= 500 and method != "GET":
                raise AmbiguousSubmission("write result unknown")
            if response.status_code >= 400:
                try:
                    code = str(response.json().get("code", ""))
                except ValueError, AttributeError:
                    code = ""
                raise BrokerError(response.status_code, code)
            return response.json() if response.content else None
        raise BrokerError(0, "retry_exhausted")

    async def account(self):
        return await self.request("GET", "/v2/account")

    async def clock(self):
        return await self.request("GET", "/v2/clock")

    async def calendar(self, start: str, end: str):
        return await self.request("GET", "/v2/calendar", params={"start": start, "end": end})

    async def asset(self, symbol: str):
        return await self.request("GET", "/v2/assets/" + quote(symbol, safe=""))

    async def positions(self):
        return await self.request("GET", "/v2/positions")

    async def open_orders(self):
        return await self.request(
            "GET", "/v2/orders", params={"status": "open", "nested": "true", "limit": 500}
        )

    async def recent_orders(self, after=None):
        cutoff = after or (utcnow() - timedelta(days=7)).isoformat()
        until = None
        result = []
        while True:
            params = {"status": "all", "nested": "true", "limit": 500, "after": cutoff, "direction": "desc"}
            if until:
                params["until"] = until
            batch = await self.request("GET", "/v2/orders", params=params)
            result.extend(batch)
            if len(batch) < 500:
                break
            next_until = batch[-1]["submitted_at"] or batch[-1]["created_at"]
            if next_until == until:
                raise BrokerError(0, "order_pagination_stalled")
            until = next_until
        return result

    async def order_by_client(self, client_id: str):
        try:
            return await self.request(
                "GET", "/v2/orders:by_client_order_id", params={"client_order_id": client_id}
            )
        except BrokerError as exc:
            if exc.status == 404:
                return None
            raise

    async def order(self, order_id: str):
        return await self.request("GET", "/v2/orders/" + quote(order_id, safe=""), params={"nested": "true"})

    async def submit(self, payload: dict):
        return await self.request("POST", "/v2/orders", payload=payload)

    async def cancel(self, order_id: str):
        try:
            return await self.request("DELETE", "/v2/orders/" + quote(order_id, safe=""))
        except BrokerError as exc:
            if exc.status not in (404, 422):
                raise

    async def quotes(self, symbols):
        response = await self.request(
            "GET",
            "/v2/stocks/quotes/latest",
            data_api=True,
            params={"symbols": ",".join(symbols), "feed": "iex"},
        )
        return response.get("quotes", {})

    async def bars(self, symbols, start, end, timeframe="5Min"):
        page = None
        while True:
            params = {
                "symbols": ",".join(symbols),
                "start": start,
                "end": end,
                "timeframe": timeframe,
                "feed": "iex",
                "adjustment": "raw",
                "limit": 10000,
            }
            if page:
                params["page_token"] = page
            response = await self.request("GET", "/v2/stocks/bars", data_api=True, params=params)
            yield response.get("bars", {})
            page = response.get("next_page_token")
            if not page:
                break

    async def stream(self, *, orders=False):
        url = PAPER_STREAM if orders else IEX_STREAM
        async with websockets.connect(
            url, ping_interval=20, ping_timeout=20, max_size=8_000_000, close_timeout=5
        ) as socket:
            auth = {
                "action": "auth",
                "key": self.settings.alpaca_key.get_secret_value(),
                "secret": self.settings.alpaca_secret.get_secret_value(),
            }
            await socket.send(json.dumps(auth))
            authenticated = False
            async for raw in socket:
                data = json.loads(raw)
                messages = data if isinstance(data, list) else [data]
                for message in messages:
                    if orders:
                        if message.get("stream") == "authorization":
                            if message["data"]["status"] != "authorized":
                                raise BrokerError(401, "stream_auth")
                            authenticated = True
                            await socket.send(
                                json.dumps({"action": "listen", "data": {"streams": ["trade_updates"]}})
                            )
                            yield {"_status": "connected"}
                        elif authenticated and message.get("stream") == "trade_updates":
                            yield message["data"]
                    else:
                        if message.get("T") == "error":
                            raise BrokerError(403, str(message.get("code", "stream")))
                        if message.get("T") == "success" and message.get("msg") == "authenticated":
                            authenticated = True
                            await socket.send(
                                json.dumps(
                                    {
                                        "action": "subscribe",
                                        "bars": self.settings.symbols,
                                        "updatedBars": self.settings.symbols,
                                    }
                                )
                            )
                            yield {"_status": "connected"}
                        elif authenticated and message.get("T") in ("b", "u"):
                            # Timestamp must be timezone aware before entering the data pipeline.
                            parse_time(message["t"])
                            yield message
