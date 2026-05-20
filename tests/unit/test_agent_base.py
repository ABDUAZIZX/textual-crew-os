"""Tests for ``crew_os.agents.base`` - the security pipeline around tools."""

from __future__ import annotations

from pathlib import Path

import pytest

from crew_os.agents.base import BaseAgent
from crew_os.core.exceptions import PolicyViolation, RateLimitExceeded
from crew_os.core.models import (
    AgentRole,
    EventType,
    Task,
    TaskStatus,
    ToolManifest,
    TraceContext,
)
from crew_os.core.policy import PolicyEngine
from crew_os.llm.ollama_client import GenerateResult
from crew_os.security.audit import AuditLogger
from crew_os.security.rate_limit import RateLimiter


class EchoAgent(BaseAgent):
    async def handle_task(self, task: Task) -> Task:
        return task.model_copy(update={"status": TaskStatus.COMPLETED})


class FakeManager:
    async def generate(
        self, model: str, prompt: str, *, system: str | None = None
    ) -> GenerateResult:
        return GenerateResult(model=model, response=f"echo:{prompt}")


def _agent(
    tmp_path: Path,
    *,
    role: AgentRole = AgentRole.CODER,
    rate_limiter: RateLimiter | None = None,
    model_manager: object | None = None,
    lab_mode_active: bool = False,
) -> tuple[EchoAgent, AuditLogger]:
    auditor = AuditLogger(tmp_path / "audit.jsonl")
    agent = EchoAgent(
        role=role,
        model="qwen2.5-coder:7b",
        policy=PolicyEngine(),
        rate_limiter=rate_limiter or RateLimiter(),
        auditor=auditor,
        model_manager=model_manager,  # type: ignore[arg-type]
        lab_mode_active=lab_mode_active,
    )
    return agent, auditor


async def _safe_tool(**_: object) -> str:
    return "done"


async def _danger_tool(**_: object) -> str:
    return "boom"


# ─────────────────────────────── registry ──────────────────────────────


async def test_register_and_capabilities(tmp_path: Path) -> None:
    agent, _ = _agent(tmp_path)
    agent.register_tool(ToolManifest(name="echo"), _safe_tool)
    assert agent.capabilities == frozenset({"echo"})


async def test_duplicate_registration_raises(tmp_path: Path) -> None:
    agent, _ = _agent(tmp_path)
    agent.register_tool(ToolManifest(name="echo"), _safe_tool)
    with pytest.raises(ValueError, match="already registered"):
        agent.register_tool(ToolManifest(name="echo"), _safe_tool)


# ─────────────────────────────── invocation ────────────────────────────


async def test_unregistered_tool_denied_and_audited(tmp_path: Path) -> None:
    agent, auditor = _agent(tmp_path)
    with pytest.raises(PolicyViolation, match="not registered"):
        await agent.invoke_tool("ghost", {}, TraceContext.new_root())
    types = [r.event["type"] for r in auditor.iter_records()]
    assert types == [EventType.TOOL_CALL_DENIED]


async def test_safe_tool_allowed_and_executes(tmp_path: Path) -> None:
    agent, auditor = _agent(tmp_path)
    agent.register_tool(ToolManifest(name="echo"), _safe_tool)
    result = await agent.invoke_tool("echo", {}, TraceContext.new_root())
    assert result == "done"
    types = [r.event["type"] for r in auditor.iter_records()]
    assert types == [EventType.TOOL_CALL_ALLOWED]


async def test_dangerous_tool_denied_for_coder(tmp_path: Path) -> None:
    agent, auditor = _agent(tmp_path, role=AgentRole.CODER)
    agent.register_tool(ToolManifest(name="rm", dangerous=True), _danger_tool)
    with pytest.raises(PolicyViolation):
        await agent.invoke_tool("rm", {}, TraceContext.new_root())
    types = [r.event["type"] for r in auditor.iter_records()]
    assert types == [EventType.TOOL_CALL_DENIED]


async def test_args_passed_to_tool(tmp_path: Path) -> None:
    agent, _ = _agent(tmp_path)

    async def adder(*, a: int, b: int) -> int:
        return a + b

    agent.register_tool(ToolManifest(name="add"), adder)
    out = await agent.invoke_tool("add", {"a": 2, "b": 3}, TraceContext.new_root())
    assert out == 5


async def test_rate_limit_blocks_second_call(tmp_path: Path) -> None:
    rl = RateLimiter()
    rl.configure("coder:echo", capacity=1, refill_per_sec=0.0001)
    agent, auditor = _agent(tmp_path, rate_limiter=rl)
    agent.register_tool(ToolManifest(name="echo"), _safe_tool)

    assert await agent.invoke_tool("echo", {}, TraceContext.new_root()) == "done"
    with pytest.raises(RateLimitExceeded):
        await agent.invoke_tool("echo", {}, TraceContext.new_root())

    types = [r.event["type"] for r in auditor.iter_records()]
    assert types == [EventType.TOOL_CALL_ALLOWED, EventType.RATE_LIMITED]


async def test_audit_chain_valid_after_mixed_calls(tmp_path: Path) -> None:
    agent, auditor = _agent(tmp_path)
    agent.register_tool(ToolManifest(name="echo"), _safe_tool)
    await agent.invoke_tool("echo", {}, TraceContext.new_root())
    with pytest.raises(PolicyViolation):
        await agent.invoke_tool("ghost", {}, TraceContext.new_root())
    assert auditor.verify().valid is True


# ─────────────────────────────── think ─────────────────────────────────


async def test_think_without_manager_raises(tmp_path: Path) -> None:
    agent, _ = _agent(tmp_path, model_manager=None)
    with pytest.raises(RuntimeError, match="no model_manager"):
        await agent.think("hi")


async def test_think_with_manager_returns_response(tmp_path: Path) -> None:
    agent, _ = _agent(tmp_path, model_manager=FakeManager())
    out = await agent.think("ping")
    assert out == "echo:ping"


async def test_handle_task_marks_completed(tmp_path: Path) -> None:
    agent, _ = _agent(tmp_path)
    task = Task(description="x", trace=TraceContext.new_root())
    done = await agent.handle_task(task)
    assert done.status == TaskStatus.COMPLETED
