"""Tests for ``crew_os.orchestration.supervisor_tier2`` - escalation + fallback."""

from __future__ import annotations

import pytest

from crew_os.config import Settings
from crew_os.core.exceptions import SupervisorError
from crew_os.core.models import Task, TraceContext
from crew_os.llm.ollama_client import GenerateResult
from crew_os.orchestration.supervisor_tier2 import (
    SupervisorMode,
    SupervisorT2,
    build_supervisor_t2,
)


def _task() -> Task:
    return Task(description="design a distributed system", trace=TraceContext.new_root())


# ── fakes ────────────────────────────────────────────────────────────


class _Block:
    def __init__(self, text: str) -> None:
        self.text = text


class _Resp:
    def __init__(self, text: str) -> None:
        self.content = [_Block(text)]


class _Messages:
    def __init__(self, *, text: str = "claude-plan", boom: bool = False) -> None:
        self._text = text
        self._boom = boom
        self.calls: list[dict[str, object]] = []

    async def create(self, **kwargs: object) -> _Resp:
        self.calls.append(kwargs)
        if self._boom:
            raise RuntimeError("anthropic down")
        return _Resp(self._text)


class _Claude:
    def __init__(self, *, text: str = "claude-plan", boom: bool = False) -> None:
        self.messages = _Messages(text=text, boom=boom)


class _LocalManager:
    def __init__(self, text: str = "local-plan") -> None:
        self._text = text
        self.calls: list[tuple[str, str]] = []

    async def generate(
        self, model: str, prompt: str, *, system: str | None = None
    ) -> GenerateResult:
        self.calls.append((model, prompt))
        return GenerateResult(model=model, response=self._text)


# ── mode resolution ──────────────────────────────────────────────────


def test_mode_claude_when_client_present() -> None:
    s = SupervisorT2(claude=_Claude())
    assert s.mode == SupervisorMode.CLAUDE


def test_mode_local_when_only_manager() -> None:
    s = SupervisorT2(model_manager=_LocalManager())  # type: ignore[arg-type]
    assert s.mode == SupervisorMode.LOCAL


def test_mode_unavailable_when_nothing() -> None:
    assert SupervisorT2().mode == SupervisorMode.UNAVAILABLE


# ── planning ─────────────────────────────────────────────────────────


async def test_plan_with_claude() -> None:
    claude = _Claude(text="step 1; step 2")
    s = SupervisorT2(claude=claude)
    result = await s.plan(_task())
    assert result.source == SupervisorMode.CLAUDE
    assert result.text == "step 1; step 2"
    # System prompt sent with cache_control for prompt caching.
    sent_system = claude.messages.calls[0]["system"]
    assert sent_system[0]["cache_control"] == {"type": "ephemeral"}  # type: ignore[index]


async def test_plan_local_only() -> None:
    mgr = _LocalManager("local steps")
    s = SupervisorT2(model_manager=mgr)  # type: ignore[arg-type]
    result = await s.plan(_task())
    assert result.source == SupervisorMode.LOCAL
    assert result.text == "local steps"
    assert mgr.calls[0][0] == "qwen3:14b"


async def test_claude_failure_degrades_to_local() -> None:
    warnings: list[str] = []
    s = SupervisorT2(
        claude=_Claude(boom=True),
        model_manager=_LocalManager("fallback plan"),  # type: ignore[arg-type]
        on_warning=warnings.append,
    )
    result = await s.plan(_task())
    assert result.source == SupervisorMode.LOCAL
    assert result.text == "fallback plan"
    assert warnings
    assert "falling back" in warnings[0]


async def test_claude_failure_without_fallback_raises() -> None:
    s = SupervisorT2(claude=_Claude(boom=True))
    with pytest.raises(SupervisorError, match="no local fallback"):
        await s.plan(_task())


async def test_unavailable_raises() -> None:
    s = SupervisorT2()
    with pytest.raises(SupervisorError, match="no supervisor backend"):
        await s.plan(_task())


def test_build_from_settings_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CREW_ANTHROPIC_API_KEY", raising=False)
    settings = Settings()
    mgr = _LocalManager()
    s = build_supervisor_t2(settings, model_manager=mgr)  # type: ignore[arg-type]
    assert s.mode == SupervisorMode.LOCAL
