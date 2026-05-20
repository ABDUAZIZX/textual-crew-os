"""WebSocket endpoint: streams bus messages to the dashboard.

On connect, the client receives a ``snapshot`` frame with the latest
metrics, then a live stream of bus messages (wildcard subscription).
Each frame is ``{"type": <class-or-'snapshot'/'raw'>, "data": ...}``.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any, cast

from fastapi import WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from crew_os.metrics.sampler import MetricsSampler
from crew_os.orchestration.bus import WILDCARD, MessageBus, Subscription


def serialize_message(message: Any) -> dict[str, Any]:
    if isinstance(message, BaseModel):
        return {"type": type(message).__name__, "data": message.model_dump(mode="json")}
    return {"type": "raw", "data": message}


async def _pump(sub: Subscription, send: Callable[[str], Awaitable[None]]) -> None:
    async for message in sub:
        await send(json.dumps(serialize_message(message)))


async def websocket_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    sampler = cast("MetricsSampler", websocket.app.state.sampler)
    bus = cast("MessageBus", websocket.app.state.bus)

    snap = sampler.latest()
    await websocket.send_text(
        json.dumps(
            {
                "type": "snapshot",
                "data": snap.model_dump(mode="json") if snap is not None else None,
            }
        )
    )

    sub = await bus.subscribe(WILDCARD)
    try:
        await _pump(sub, websocket.send_text)
    except WebSocketDisconnect:
        pass
    finally:
        await sub.close()
