"""Живые потоки действий людей: блокчейн, биржа, Википедия.

Каждый источник держит постоянное соединение и раздаёт события подписчикам.
Подписчик появляется только в момент броска, поэтому видит лишь события,
пришедшие ПОСЛЕ нажатия.
"""
import asyncio
import json
import logging
import time
from dataclasses import dataclass

import aiohttp

log = logging.getLogger("sources")


@dataclass(slots=True)
class Event:
    source: str
    canonical: str
    recv_ns: int


class Source:
    name = "base"

    def __init__(self) -> None:
        self._subs: set[asyncio.Queue] = set()
        self.connected = False
        self.detail = ""
        self.last_event_at: float | None = None
        self.count = 0
        self._fails = 0

    def subscribe(self, maxsize: int = 64) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def publish(self, canonical: str) -> None:
        ev = Event(self.name, canonical, time.monotonic_ns())
        self.count += 1
        self.last_event_at = time.time()
        for q in list(self._subs):
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:
                pass

    def _up(self) -> None:
        self.connected = True
        self._fails = 0

    async def stream(self, session: aiohttp.ClientSession) -> None:
        raise NotImplementedError

    async def run(self, session: aiohttp.ClientSession) -> None:
        while True:
            try:
                await self.stream(session)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self._fails += 1
                log.warning("%s: %s: %s", self.name, type(e).__name__, e)
            self.connected = False
            await asyncio.sleep(min(30, 2 * self._fails) or 1)


class ChainSource(Source):
    """Неподтверждённые транзакции биткоина (blockchain.com WebSocket)."""

    name = "chain"
    URL = "wss://ws.blockchain.info/inv"

    async def stream(self, session):
        self.detail = "blockchain.com"
        async with session.ws_connect(
            self.URL, heartbeat=25, headers={"Origin": "https://www.blockchain.com"}
        ) as ws:
            await ws.send_json({"op": "unconfirmed_sub"})
            self._up()
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    if data.get("op") == "utx":
                        x = data.get("x") or {}
                        h = x.get("hash")
                        if h:
                            self.publish(f"btc:{h}:{x.get('time', '')}")
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break


class ExchangeSource(Source):
    """Сделки на бирже. Binance, при блокировке региона переключаемся на Coinbase, потом Kraken."""

    name = "exchange"

    def __init__(self) -> None:
        super().__init__()
        self._idx = 0
        self._endpoints = [
            ("binance", self._binance),
            ("coinbase", self._coinbase),
            ("kraken", self._kraken),
        ]

    async def stream(self, session):
        label, fn = self._endpoints[self._idx]
        self.detail = label
        try:
            await fn(session)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._idx = (self._idx + 1) % len(self._endpoints)
            raise

    async def _binance(self, session):
        url = "wss://stream.binance.com:9443/ws/btcusdt@trade"
        async with session.ws_connect(url, heartbeat=20) as ws:
            self._up()
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    d = json.loads(msg.data)
                    if d.get("e") == "trade":
                        self.publish(f"binance:{d['t']}:{d['p']}:{d['q']}:{d.get('T', '')}")
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break

    async def _coinbase(self, session):
        url = "wss://ws-feed.exchange.coinbase.com"
        async with session.ws_connect(url, heartbeat=20) as ws:
            await ws.send_json(
                {"type": "subscribe", "product_ids": ["BTC-USD"], "channels": ["matches"]}
            )
            self._up()
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    d = json.loads(msg.data)
                    if d.get("type") == "match":
                        self.publish(
                            f"coinbase:{d.get('trade_id')}:{d.get('price')}:{d.get('size')}:{d.get('time', '')}"
                        )
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break

    async def _kraken(self, session):
        url = "wss://ws.kraken.com/v2"
        async with session.ws_connect(url, heartbeat=20) as ws:
            await ws.send_json(
                {
                    "method": "subscribe",
                    "params": {"channel": "trade", "symbol": ["BTC/USD"], "snapshot": False},
                }
            )
            self._up()
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    d = json.loads(msg.data)
                    if d.get("channel") == "trade" and d.get("type") == "update":
                        for t in d.get("data", []):
                            self.publish(
                                f"kraken:{t.get('trade_id')}:{t.get('price')}:{t.get('qty')}:{t.get('timestamp', '')}"
                            )
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break


class WikiSource(Source):
    """Правки Википедии и её сестринских проектов (Wikimedia EventStreams, SSE)."""

    name = "wiki"
    URL = "https://stream.wikimedia.org/v2/stream/recentchange"

    async def stream(self, session):
        self.detail = "wikimedia"
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=90)
        async with session.get(
            self.URL, timeout=timeout, headers={"Accept": "text/event-stream"}
        ) as resp:
            resp.raise_for_status()
            self._up()
            async for raw in resp.content:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                try:
                    d = json.loads(line[5:])
                except ValueError:
                    continue
                rid = d.get("id") or d.get("log_id")
                if rid is None:
                    continue
                length = d.get("length") or {}
                self.publish(
                    f"wiki:{d.get('wiki', '')}:{rid}:{d.get('title', '')}:"
                    f"{d.get('user', '')}:{d.get('timestamp', '')}:{length.get('new', '')}"
                )


def default_sources() -> list[Source]:
    return [ChainSource(), ExchangeSource(), WikiSource()]
