"""In-process async publish/subscribe message bus.

Topic-based, with a special ``"*"`` wildcard topic that receives every
message (used by the dashboard feed in a later stage).

Publishing never blocks: if a subscriber's bounded queue is full the
message is dropped for that subscriber and its ``dropped`` counter is
incremented. This keeps a slow consumer from stalling the hot path.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import defaultdict
from types import TracebackType
from typing import Any

WILDCARD = "*"

_CLOSED = object()


class Subscription:
    def __init__(self, topic: str, maxsize: int, bus: MessageBus) -> None:
        self.topic = topic
        self.dropped = 0
        self._queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=maxsize)
        self._bus = bus
        self._closed = False

    def _offer(self, message: Any) -> None:
        try:
            self._queue.put_nowait(message)
        except asyncio.QueueFull:
            self.dropped += 1

    def __aiter__(self) -> Subscription:
        return self

    async def __anext__(self) -> Any:
        item = await self._queue.get()
        if item is _CLOSED:
            raise StopAsyncIteration
        return item

    async def get(self, *, timeout: float | None = None) -> Any:  # noqa: ASYNC109 - deliberate ergonomic API
        if timeout is None:
            item = await self._queue.get()
        else:
            item = await asyncio.wait_for(self._queue.get(), timeout)
        if item is _CLOSED:
            raise StopAsyncIteration
        return item

    async def __aenter__(self) -> Subscription:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._bus._remove(self)
        try:
            self._queue.put_nowait(_CLOSED)
        except asyncio.QueueFull:
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
            self._queue.put_nowait(_CLOSED)


class MessageBus:
    def __init__(self, *, max_queue: int = 1000) -> None:
        self._subs: dict[str, set[Subscription]] = defaultdict(set)
        self._lock = asyncio.Lock()
        self._max = max_queue

    async def subscribe(self, topic: str) -> Subscription:
        sub = Subscription(topic, self._max, self)
        async with self._lock:
            self._subs[topic].add(sub)
        return sub

    async def publish(self, topic: str, message: Any) -> int:
        async with self._lock:
            targets: set[Subscription] = set(self._subs.get(topic, set()))
            if topic != WILDCARD:
                targets |= self._subs.get(WILDCARD, set())
        for sub in targets:
            sub._offer(message)
        return len(targets)

    async def subscriber_count(self, topic: str) -> int:
        async with self._lock:
            return len(self._subs.get(topic, set()))

    async def _remove(self, sub: Subscription) -> None:
        async with self._lock:
            self._subs.get(sub.topic, set()).discard(sub)

    async def close(self) -> None:
        async with self._lock:
            all_subs = [s for subs in self._subs.values() for s in subs]
            self._subs.clear()
        for sub in all_subs:
            await sub.close()
