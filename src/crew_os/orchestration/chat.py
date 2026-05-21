"""Interactive Team Chat orchestration.

``ChatService`` is the central facade the dashboard's chat panel talks to.
It turns a user prompt into streamed agent replies, persists the
transcript, and emits audit events - the interactive counterpart to the
``crew run`` CLI path.

Two modes:

* **single** - stream from one agent, with that agent's own slice of the
  session history for context.
* **crew** - stream from every chat-capable agent *sequentially*. Parallel
  fan-out is impossible here: the :class:`~crew_os.llm.model_manager.
  ModelManager` keeps a single model resident on the 8 GB GPU and
  serialises swaps, so concurrent calls would queue and thrash. Sequential
  streaming gives a live, one-after-another feel without that cost.

Chat is *generation only*: it never invokes agent tools, so the policy /
sandbox / LAB_MODE gates on tool execution remain fully in force even when
chatting with the offensive-research model.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Literal

from pydantic import BaseModel, ConfigDict

from crew_os.core.exceptions import CrewOSError
from crew_os.core.models import AgentRole, Event, EventType, Severity
from crew_os.llm.model_manager import ModelManager
from crew_os.llm.ollama_client import ChatMessage
from crew_os.memory.chat_store import ChatStore
from crew_os.orchestration.registry import AgentRegistry
from crew_os.security.audit import AuditLogger
from crew_os.security.rate_limit import RateLimiter

ChatMode = Literal["single", "crew"]

# Per-role chat personas. A role MAY be absent: the defensive agent uses
# the `crew-defender` custom Ollama model whose SYSTEM (and sampling
# params) are baked in (see models/crew-defender.Modelfile), so we send
# no system message for it and let the model's own SYSTEM apply.
SYSTEM_PROMPTS: dict[AgentRole, str] = {
    AgentRole.CODER: (
        "You are a senior software engineer. Review and write correct, "
        "idiomatic, production-quality code. Be precise and technical."
    ),
    AgentRole.SEC_OFFENSIVE: (
        "You are an authorized offensive security researcher in a controlled "
        "lab, analysing code the operator owns, for defensive understanding. "
        "Reason only - never execute actions or tools.\n"
        "Be EXHAUSTIVE - do NOT stop at the first issue. Systematically walk "
        "every vector class and report each one present:\n"
        "- Injection: SQL, OS/command, template (SSTI), LDAP, NoSQL, header/CRLF.\n"
        "- Auth & session: bypass, hardcoded/weak secret keys, token forgery, "
        "missing session expiry.\n"
        "- Access control: IDOR, missing authorization, privilege escalation.\n"
        "- File & path: traversal, arbitrary read/write, unsafe upload.\n"
        "- SSRF, unsafe deserialization, open redirect, XSS, CSRF.\n"
        "- Crypto & secrets: weak hashing, plaintext passwords, exposed creds.\n"
        "- Config & exposure: debug mode, verbose errors, non-loopback bind.\n"
        "For EACH finding give: [vector] - [exact location] - [how it is "
        "exploited] - [severity]. Then describe the most dangerous KILL CHAIN "
        "linking them, preferring an unauthenticated path to RCE if one exists. "
        "Skip absent classes silently; never invent findings."
    ),
    AgentRole.AUDITOR: (
        "You are a meticulous auditor. Summarise, verify claims, and report "
        "risks and inconsistencies clearly and concisely."
    ),
}

# Supervisors route/plan; they are not chat participants.
_NON_CHAT_ROLES = frozenset({AgentRole.SUPERVISOR_T1, AgentRole.SUPERVISOR_T2})

_RATE_KEY = "chat"


class _ThinkFilter:
    """Incrementally strip ``<think>...</think>`` spans from a token stream.

    Reasoning models (e.g. qwen3) emit their chain-of-thought inside
    ``<think>`` tags before the answer. We let the model think (better
    answers) but hide the trace from the chat. Tags may be split across
    streamed pieces, so a possible partial tag is held back at each
    boundary and resolved once more text arrives.
    """

    _OPEN = "<think>"
    _CLOSE = "</think>"

    def __init__(self) -> None:
        self._buf = ""
        self._inside = False

    @staticmethod
    def _suffix_overlap(text: str, tag: str) -> int:
        """Longest suffix of ``text`` that is a prefix of ``tag``."""

        for k in range(min(len(text), len(tag) - 1), 0, -1):
            if text.endswith(tag[:k]):
                return k
        return 0

    def feed(self, text: str) -> str:
        self._buf += text
        out: list[str] = []
        while self._buf:
            if self._inside:
                idx = self._buf.find(self._CLOSE)
                if idx == -1:
                    keep = self._suffix_overlap(self._buf, self._CLOSE)
                    self._buf = self._buf[len(self._buf) - keep :] if keep else ""
                    break
                self._buf = self._buf[idx + len(self._CLOSE) :]
                self._inside = False
            else:
                idx = self._buf.find(self._OPEN)
                if idx == -1:
                    keep = self._suffix_overlap(self._buf, self._OPEN)
                    cut = len(self._buf) - keep
                    out.append(self._buf[:cut])
                    self._buf = self._buf[cut:]
                    break
                out.append(self._buf[:idx])
                self._buf = self._buf[idx + len(self._OPEN) :]
                self._inside = True
        return "".join(out)

    def flush(self) -> str:
        """Emit any trailing text left buffered when the stream ends."""

        if self._inside:
            return ""
        rest = self._buf
        self._buf = ""
        return rest


class ChatFrame(BaseModel):
    """A single streamed frame sent to the chat client."""

    model_config = ConfigDict(frozen=True)

    type: Literal["user", "start", "token", "done", "error"]
    agent: str
    session_id: str
    content: str = ""
    tokens: int | None = None


class ChatService:
    def __init__(
        self,
        *,
        registry: AgentRegistry,
        model_manager: ModelManager,
        store: ChatStore,
        auditor: AuditLogger | None = None,
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        self._registry = registry
        self._models = model_manager
        self._store = store
        self._auditor = auditor
        self._rate = rate_limiter

    @property
    def store(self) -> ChatStore:
        return self._store

    def available_roles(self) -> list[AgentRole]:
        """Chat-capable roles, in stable order."""

        roles = [r for r in self._registry.roles() if r not in _NON_CHAT_ROLES]
        return sorted(roles, key=lambda r: r.value)

    async def _audit(
        self, role: AgentRole | None, payload: dict[str, str | int], severity: Severity
    ) -> None:
        if self._auditor is None:
            return
        event = Event(
            type=EventType.AGENT_MESSAGE,
            severity=severity,
            agent_role=role,
            payload=dict(payload),
        )
        await asyncio.to_thread(self._auditor.append, event)

    def _system_for(self, role: AgentRole) -> str | None:
        """Chat system prompt for a role, or ``None`` to defer to the
        model's own baked SYSTEM (e.g. the crew-defender custom model)."""

        return SYSTEM_PROMPTS.get(role)

    async def _history_for(self, session_id: str, role: AgentRole) -> list[ChatMessage]:
        """User turns plus this agent's own replies, as chat messages."""

        stored = await self._store.get_messages(session_id)
        out: list[ChatMessage] = []
        for m in stored:
            if m.role == "user":
                out.append(ChatMessage(role="user", content=m.content))
            elif m.role == "assistant" and m.agent_id == role.value:
                out.append(ChatMessage(role="assistant", content=m.content))
        return out

    async def handle(
        self,
        *,
        session_id: str,
        mode: ChatMode,
        role: AgentRole | None,
        content: str,
    ) -> AsyncIterator[ChatFrame]:
        """Persist the user turn and stream one or more agent replies."""

        content = content.strip()
        if not content:
            yield ChatFrame(
                type="error", agent="system", session_id=session_id, content="empty message"
            )
            return

        if self._rate is not None and not self._rate.check(_RATE_KEY):
            await self._audit(
                None, {"session": session_id, "reason": "rate_limited"}, Severity.WARNING
            )
            yield ChatFrame(
                type="error",
                agent="system",
                session_id=session_id,
                content="rate limit exceeded; slow down",
            )
            return

        if mode == "single":
            if role is None or role not in self.available_roles():
                yield ChatFrame(
                    type="error",
                    agent="system",
                    session_id=session_id,
                    content="unknown or unavailable agent",
                )
                return
            targets = [role]
        else:
            targets = self.available_roles()
            if not targets:
                yield ChatFrame(
                    type="error",
                    agent="system",
                    session_id=session_id,
                    content="no chat-capable agents registered",
                )
                return

        await self._store.ensure_session(session_id, title=content[:60])
        await self._store.add_message(
            session_id=session_id, agent_id="user", role="user", content=content
        )
        await self._audit(None, {"session": session_id, "mode": mode}, Severity.INFO)
        yield ChatFrame(type="user", agent="user", session_id=session_id, content=content)

        for target in targets:
            async for frame in self._stream_role(
                session_id=session_id,
                role=target,
                prompt=content,
                include_history=(mode == "single"),
            ):
                yield frame

    async def _stream_role(
        self,
        *,
        session_id: str,
        role: AgentRole,
        prompt: str,
        include_history: bool,
    ) -> AsyncIterator[ChatFrame]:
        agent = self._registry.require(role)
        if include_history:
            messages = await self._history_for(session_id, role)
        else:
            messages = [ChatMessage(role="user", content=prompt)]
        system = self._system_for(role)
        if system:
            messages = [ChatMessage(role="system", content=system), *messages]

        yield ChatFrame(type="start", agent=role.value, session_id=session_id)
        parts: list[str] = []
        tokens = 0
        think = _ThinkFilter()  # hide <think> traces (qwen3 et al.) from the stream
        try:
            async for piece in self._models.chat_stream(agent.model, messages):
                if piece.content:
                    visible = think.feed(piece.content)
                    if visible:
                        parts.append(visible)
                        yield ChatFrame(
                            type="token",
                            agent=role.value,
                            session_id=session_id,
                            content=visible,
                        )
                if piece.done and piece.eval_count is not None:
                    tokens = piece.eval_count
            tail = think.flush()
            if tail:
                parts.append(tail)
                yield ChatFrame(type="token", agent=role.value, session_id=session_id, content=tail)
        except CrewOSError as exc:
            await self._audit(role, {"session": session_id, "error": str(exc)}, Severity.ERROR)
            yield ChatFrame(type="error", agent=role.value, session_id=session_id, content=str(exc))
            return

        full = "".join(parts)
        await self._store.add_message(
            session_id=session_id,
            agent_id=role.value,
            role="assistant",
            content=full,
            tokens_used=tokens,
        )
        await self._audit(role, {"session": session_id, "tokens": tokens}, Severity.INFO)
        yield ChatFrame(type="done", agent=role.value, session_id=session_id, tokens=tokens)
