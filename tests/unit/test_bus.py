"""Tests for ``crew_os.orchestration.bus``."""

from __future__ import annotations

import asyncio

import pytest

from crew_os.orchestration.bus import WILDCARD, MessageBus


async def test_publish_delivers_to_topic_subscriber() -> None:
    bus = MessageBus()
    sub = await bus.subscribe("tasks")
    delivered = await bus.publish("tasks", {"id": 1})
    assert delivered == 1
    assert await sub.get(timeout=1.0) == {"id": 1}


async def test_message_not_delivered_to_other_topic() -> None:
    bus = MessageBus()
    sub = await bus.subscribe("tasks")
    delivered = await bus.publish("events", {"x": 1})
    assert delivered == 0
    with pytest.raises(asyncio.TimeoutError):
        await sub.get(timeout=0.05)


async def test_wildcard_receives_all_topics() -> None:
    bus = MessageBus()
    star = await bus.subscribe(WILDCARD)
    await bus.publish("tasks", "a")
    await bus.publish("events", "b")
    assert await star.get(timeout=1.0) == "a"
    assert await star.get(timeout=1.0) == "b"


async def test_multiple_subscribers_each_receive() -> None:
    bus = MessageBus()
    s1 = await bus.subscribe("t")
    s2 = await bus.subscribe("t")
    delivered = await bus.publish("t", "msg")
    assert delivered == 2
    assert await s1.get(timeout=1.0) == "msg"
    assert await s2.get(timeout=1.0) == "msg"


async def test_async_iteration() -> None:
    bus = MessageBus()
    sub = await bus.subscribe("t")
    await bus.publish("t", 1)
    await bus.publish("t", 2)

    received: list[int] = []
    async for item in sub:
        received.append(item)
        if len(received) == 2:
            await sub.close()
    assert received == [1, 2]


async def test_full_queue_drops_and_counts() -> None:
    bus = MessageBus(max_queue=2)
    sub = await bus.subscribe("t")
    for i in range(5):
        await bus.publish("t", i)
    assert sub.dropped == 3
    assert await sub.get(timeout=1.0) == 0
    assert await sub.get(timeout=1.0) == 1


async def test_unsubscribe_stops_delivery() -> None:
    bus = MessageBus()
    sub = await bus.subscribe("t")
    await sub.close()
    delivered = await bus.publish("t", "x")
    assert delivered == 0


async def test_subscriber_count() -> None:
    bus = MessageBus()
    assert await bus.subscriber_count("t") == 0
    await bus.subscribe("t")
    await bus.subscribe("t")
    assert await bus.subscriber_count("t") == 2


async def test_close_terminates_iterators() -> None:
    bus = MessageBus()
    sub = await bus.subscribe("t")
    await bus.close()
    received = [item async for item in sub]
    assert received == []


async def test_context_manager_subscription() -> None:
    bus = MessageBus()
    async with await bus.subscribe("t") as sub:
        await bus.publish("t", "hi")
        assert await sub.get(timeout=1.0) == "hi"
    assert await bus.subscriber_count("t") == 0
