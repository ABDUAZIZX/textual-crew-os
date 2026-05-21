"""Tests for ``crew_os.orchestration.chat.ChatService``.

The model layer is faked (its streaming behaviour is covered in
test_model_manager / test_ollama_client); these tests pin down the
service's orchestration: persistence, mode routing, sequential crew
fan-out, context windows, error surfacing, rate limiting, and audit.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from crew_os.agents.base import BaseAgent
from crew_os.core.exceptions import OllamaError
from crew_os.core.models import AgentRole, Task
from crew_os.core.policy import PolicyEngine
from crew_os.llm.ollama_client import ChatStreamPiece
from crew_os.memory.chat_store import ChatStore
from crew_os.orchestration.chat import ChatService, _ThinkFilter
from crew_os.orchestration.registry import AgentRegistry
from crew_os.security.audit import AuditLogger
from crew_os.security.rate_limit import RateLimiter


class _Agent(BaseAgent):
    async def handle_task(self, task: Task) -> Task:
        return task


class FakeModels:
    """Records (model, messages) per call; streams two tokens + done."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[str, list[tuple[str, str]]]] = []
        self.fail = fail

    async def chat_stream(
        self, model: str, messages: object, **_: object
    ) -> AsyncIterator[ChatStreamPiece]:
        msgs = [(m.role, m.content) for m in messages]  # type: ignore[attr-defined]
        self.calls.append((model, msgs))
        if self.fail:
            raise OllamaError("ollama unreachable")
        yield ChatStreamPiece(content="a")
        yield ChatStreamPiece(content="b")
        yield ChatStreamPiece(content="", done=True, eval_count=3)


_MODELS = {
    AgentRole.CODER: "coder-model",
    AgentRole.AUDITOR: "auditor-model",
    AgentRole.SEC_DEFENSIVE: "defensive-model",
}


def _registry(tmp_path: Path) -> AgentRegistry:
    reg = AgentRegistry()
    for role, model in _MODELS.items():
        reg.register(
            _Agent(
                role=role,
                model=model,
                policy=PolicyEngine(),
                rate_limiter=RateLimiter(),
                auditor=AuditLogger(tmp_path / "audit.jsonl"),
            )
        )
    return reg


@pytest.fixture
async def chat_store(tmp_path: Path) -> AsyncIterator[ChatStore]:
    s = ChatStore(tmp_path / "chat.db")
    await s.connect()
    yield s
    await s.close()


def _service(
    tmp_path: Path,
    chat_store: ChatStore,
    *,
    models: FakeModels | None = None,
    rate_limiter: RateLimiter | None = None,
    auditor: AuditLogger | None = None,
) -> ChatService:
    return ChatService(
        registry=_registry(tmp_path),
        model_manager=models or FakeModels(),  # type: ignore[arg-type]
        store=chat_store,
        auditor=auditor,
        rate_limiter=rate_limiter,
    )


async def test_available_roles_excludes_supervisors(tmp_path: Path, chat_store: ChatStore) -> None:
    svc = _service(tmp_path, chat_store)
    # sorted by value: auditor, coder, sec_defensive
    assert [r.value for r in svc.available_roles()] == ["auditor", "coder", "sec_defensive"]


async def test_single_streams_and_persists(tmp_path: Path, chat_store: ChatStore) -> None:
    models = FakeModels()
    svc = _service(tmp_path, chat_store, models=models)
    frames = [
        f
        async for f in svc.handle(
            session_id="s1", mode="single", role=AgentRole.CODER, content="hi"
        )
    ]
    assert frames[0].type == "user"
    assert [f.agent for f in frames if f.type == "start"] == ["coder"]
    done = [f for f in frames if f.type == "done"]
    assert len(done) == 1
    assert done[0].tokens == 3
    streamed = "".join(f.content for f in frames if f.type == "token")
    assert streamed == "ab"

    msgs = await chat_store.get_messages("s1")
    assert [m.role for m in msgs] == ["user", "assistant"]
    assert msgs[1].agent_id == "coder"
    assert msgs[1].content == "ab"
    assert msgs[1].tokens_used == 3
    # single mode hit exactly one model
    assert [c[0] for c in models.calls] == ["coder-model"]


async def test_crew_streams_every_agent_sequentially(tmp_path: Path, chat_store: ChatStore) -> None:
    models = FakeModels()
    svc = _service(tmp_path, chat_store, models=models)
    frames = [
        f async for f in svc.handle(session_id="s2", mode="crew", role=None, content="review")
    ]
    # one start per chat agent, in available-roles order
    assert [f.agent for f in frames if f.type == "start"] == [
        "auditor",
        "coder",
        "sec_defensive",
    ]
    # models invoked once each, in the same order (sequential)
    assert [c[0] for c in models.calls] == ["auditor-model", "coder-model", "defensive-model"]
    # one assistant row per agent + the single user row
    msgs = await chat_store.get_messages("s2")
    assert sum(1 for m in msgs if m.role == "assistant") == 3
    assert sum(1 for m in msgs if m.role == "user") == 1


async def test_crew_gives_each_agent_no_cross_history(
    tmp_path: Path, chat_store: ChatStore
) -> None:
    models = FakeModels()
    svc = _service(tmp_path, chat_store, models=models)
    _ = [f async for f in svc.handle(session_id="s", mode="crew", role=None, content="q")]
    for _model, msgs in models.calls:
        assert [role for role, _ in msgs] == ["system", "user"]


async def test_single_mode_includes_prior_history(tmp_path: Path, chat_store: ChatStore) -> None:
    models = FakeModels()
    svc = _service(tmp_path, chat_store, models=models)
    _ = [
        f
        async for f in svc.handle(
            session_id="s3", mode="single", role=AgentRole.CODER, content="first"
        )
    ]
    models.calls.clear()
    _ = [
        f
        async for f in svc.handle(
            session_id="s3", mode="single", role=AgentRole.CODER, content="second"
        )
    ]
    _model, msgs = models.calls[-1]
    roles = [role for role, _ in msgs]
    texts = [text for _, text in msgs]
    assert roles[0] == "system"
    assert "assistant" in roles  # the prior coder reply is in context
    assert "first" in texts
    assert "second" in texts


async def test_unknown_agent_single_errors(tmp_path: Path, chat_store: ChatStore) -> None:
    svc = _service(tmp_path, chat_store)
    frames = [
        f
        async for f in svc.handle(
            session_id="s", mode="single", role=AgentRole.SUPERVISOR_T1, content="hi"
        )
    ]
    assert frames[0].type == "error"
    assert await chat_store.get_messages("s") == []


async def test_empty_message_errors_and_persists_nothing(
    tmp_path: Path, chat_store: ChatStore
) -> None:
    svc = _service(tmp_path, chat_store)
    frames = [
        f
        async for f in svc.handle(
            session_id="s", mode="single", role=AgentRole.CODER, content="   "
        )
    ]
    assert frames[0].type == "error"
    assert await chat_store.get_messages("s") == []


async def test_ollama_failure_yields_error_and_no_assistant_row(
    tmp_path: Path, chat_store: ChatStore
) -> None:
    svc = _service(tmp_path, chat_store, models=FakeModels(fail=True))
    frames = [
        f
        async for f in svc.handle(session_id="s", mode="single", role=AgentRole.CODER, content="hi")
    ]
    assert any(f.type == "error" for f in frames)
    msgs = await chat_store.get_messages("s")
    assert [m.role for m in msgs] == ["user"]  # user kept, assistant not written


async def test_rate_limit_blocks_second_send(tmp_path: Path, chat_store: ChatStore) -> None:
    rl = RateLimiter()
    rl.configure("chat", capacity=1, refill_per_sec=0.001)
    svc = _service(tmp_path, chat_store, rate_limiter=rl)
    first = [
        f
        async for f in svc.handle(session_id="s", mode="single", role=AgentRole.CODER, content="a")
    ]
    assert not any(f.type == "error" for f in first)
    second = [
        f
        async for f in svc.handle(session_id="s", mode="single", role=AgentRole.CODER, content="b")
    ]
    assert second[0].type == "error"
    assert "rate" in second[0].content.lower()


async def test_audit_records_chat_events(tmp_path: Path, chat_store: ChatStore) -> None:
    auditor = AuditLogger(tmp_path / "chat_audit.jsonl")
    svc = _service(tmp_path, chat_store, auditor=auditor)
    _ = [
        f
        async for f in svc.handle(session_id="s", mode="single", role=AgentRole.CODER, content="hi")
    ]
    types = [r.event["type"] for r in auditor.iter_records()]
    assert "agent_message" in types


# ── <think> stream filtering (qwen3 et al.) ──────────────────────────


def test_think_filter_strips_complete_block() -> None:
    f = _ThinkFilter()
    assert f.feed("<think>reasoning here</think>the answer") == "the answer"
    assert f.flush() == ""


def test_think_filter_passthrough_without_tags() -> None:
    f = _ThinkFilter()
    assert f.feed("plain text ") == "plain text "
    assert f.feed("more") == "more"
    assert f.flush() == ""


def test_think_filter_handles_tags_split_across_pieces() -> None:
    f = _ThinkFilter()
    chunks = ["Hello <thi", "nk>hidden rea", "soning</thin", "k>World"]
    out = "".join(f.feed(c) for c in chunks) + f.flush()
    assert out == "Hello World"


def test_think_filter_flush_emits_trailing_partial_open() -> None:
    f = _ThinkFilter()
    assert f.feed("answer<thi") == "answer"  # partial tag held back
    assert f.flush() == "<thi"  # never completed -> emitted as-is at end
