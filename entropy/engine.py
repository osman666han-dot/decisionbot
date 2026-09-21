import asyncio
import logging
import time
from dataclasses import dataclass

import aiohttp

import config
from . import mixer
from .picker import pick
from .sources import Source

log = logging.getLogger("engine")
USER_AGENT = "DecisionBot/1.0 (https://t.me/kolokotol)"


@dataclass(slots=True)
class ThrowResult:
    request_id: str
    source: str
    events: list[str]
    t0_ns: int
    index: int
    digest: str


class Engine:
    def __init__(self, sources: list[Source]) -> None:
        self.sources = sources
        self._tasks: list[asyncio.Task] = []
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=15)
        self._session = aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}, timeout=timeout)
        self._tasks = [
            asyncio.create_task(s.run(self._session), name=f"src-{s.name}") for s in self.sources
        ]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._session:
            await self._session.close()

    async def throw(
        self,
        request_id: str,
        n_options: int,
        timeout: float | None = None,
        extra: int | None = None,
        extra_timeout: float | None = None,
    ) -> ThrowResult | None:
        """Ждём первое живое событие после вызова; его источник побеждает.
        Затем берём ещё `extra` событий этого же источника и считаем хэш."""
        timeout = config.LISTEN_TIMEOUT if timeout is None else timeout
        extra = config.EXTRA_EVENTS if extra is None else extra
        extra_timeout = config.EXTRA_TIMEOUT if extra_timeout is None else extra_timeout

        t0_ns = time.time_ns()
        queues = {s.name: s.subscribe() for s in self.sources}
        getters = {asyncio.ensure_future(q.get()): name for name, q in queues.items()}
        try:
            done, _pending = await asyncio.wait(
                getters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
            first = [t.result() for t in done if not t.cancelled() and t.exception() is None]
            if not first:
                return None
            winner = min(first, key=lambda e: e.recv_ns)
            events = [winner]
            q = queues[winner.source]

            loop = asyncio.get_running_loop()
            deadline = loop.time() + extra_timeout
            while len(events) < 1 + extra:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                try:
                    events.append(await asyncio.wait_for(q.get(), remaining))
                except asyncio.TimeoutError:
                    break

            canon = [e.canonical for e in events]
            base = mixer.build(request_id, t0_ns, winner.source, canon)
            index, digest = pick(base, n_options)
            return ThrowResult(request_id, winner.source, canon, t0_ns, index, digest)
        finally:
            for t in getters:
                t.cancel()
            for s in self.sources:
                s.unsubscribe(queues[s.name])
