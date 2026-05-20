"""Tests for the WebSocket serialization and bus pump."""

from __future__ import annotations

import json

from crew_os.core.models import Task, TraceContext
from crew_os.orchestration.bus import WILDCARD, MessageBus
from crew_os.web.ws import _pump, serialize_message


def test_serialize_pydantic_model() -> None:
    task = Task(description="x", trace=TraceContext.new_root())
    out = serialize_message(task)
    assert out["type"] == "Task"
    assert out["data"]["description"] == "x"


def test_serialize_raw_value() -> None:
    out = serialize_message({"hello": "world"})
    assert out["type"] == "raw"
    assert out["data"] == {"hello": "world"}


async def test_pump_forwards_bus_messages() -> None:
    bus = MessageBus()
    sub = await bus.subscribe(WILDCARD)
    sent: list[str] = []

    async def send(text: str) -> None:
        sent.append(text)
        if len(sent) >= 2:
            await sub.close()

    await bus.publish("tasks", Task(description="one", trace=TraceContext.new_root()))
    await bus.publish("tasks", Task(description="two", trace=TraceContext.new_root()))

    await _pump(sub, send)

    assert len(sent) == 2
    first = json.loads(sent[0])
    assert first["type"] == "Task"
    assert first["data"]["description"] == "one"
