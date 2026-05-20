"""Team Chat web surface: a streaming WebSocket plus history GET routes.

This is the dashboard's only *write* path. It is reachable on loopback
only (``Settings.web_host`` is validated to a loopback address) and every
turn is persisted and audited. The socket triggers model *generation*
only - never tool execution - so the policy/sandbox/LAB_MODE gates are
untouched. See ``SECURITY.md``.
"""

from __future__ import annotations

import json
from typing import Any, cast

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect

from crew_os.core.models import AgentRole
from crew_os.orchestration.chat import ChatMode, ChatService

chat_router = APIRouter(prefix="/api/chat")


def _chat_or_none(app: Any) -> ChatService | None:
    return cast("ChatService | None", getattr(app.state, "chat_service", None))


def _chat(request: Request) -> ChatService:
    chat = _chat_or_none(request.app)
    if chat is None:
        raise HTTPException(status_code=503, detail="chat service not available")
    return chat


@chat_router.get("/agents")
async def chat_agents(request: Request) -> list[str]:
    return [r.value for r in _chat(request).available_roles()]


@chat_router.get("/sessions")
async def chat_sessions(request: Request) -> list[dict[str, Any]]:
    sessions = await _chat(request).store.list_sessions()
    return [s.model_dump() for s in sessions]


@chat_router.get("/sessions/{session_id}")
async def chat_session_messages(request: Request, session_id: str) -> list[dict[str, Any]]:
    messages = await _chat(request).store.get_messages(session_id)
    return [m.model_dump() for m in messages]


def _parse_request(raw: str) -> tuple[str, ChatMode, AgentRole | None, str]:
    data = json.loads(raw)
    session_id = str(data.get("session_id") or "").strip()
    mode_raw = str(data.get("mode") or "single")
    mode: ChatMode = "crew" if mode_raw == "crew" else "single"
    content = str(data.get("content") or "")
    role: AgentRole | None = None
    agent = data.get("agent")
    if agent:
        try:
            role = AgentRole(str(agent))
        except ValueError:
            role = None
    if not session_id:
        raise ValueError("missing session_id")
    return session_id, mode, role, content


async def chat_websocket_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    chat = _chat_or_none(websocket.app)
    if chat is None:
        await websocket.send_text(
            json.dumps(
                {"type": "error", "agent": "system", "content": "chat service not available"}
            )
        )
        await websocket.close()
        return

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                session_id, mode, role, content = _parse_request(raw)
            except (ValueError, json.JSONDecodeError) as exc:
                await websocket.send_text(
                    json.dumps(
                        {"type": "error", "agent": "system", "content": f"bad request: {exc}"}
                    )
                )
                continue
            async for frame in chat.handle(
                session_id=session_id, mode=mode, role=role, content=content
            ):
                await websocket.send_text(frame.model_dump_json())
    except WebSocketDisconnect:
        return
