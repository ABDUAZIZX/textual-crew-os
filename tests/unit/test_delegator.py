"""Tests for ``crew_os.orchestration.delegator`` - the full delegation flow."""

from __future__ import annotations

from pathlib import Path

import pytest

from crew_os.agents.base import BaseAgent
from crew_os.core.models import AgentRole, EventType, Task, TaskStatus, TraceContext
from crew_os.core.policy import PolicyEngine
from crew_os.memory.sqlite_store import SqliteStore
from crew_os.orchestration.bus import MessageBus
from crew_os.orchestration.delegator import TASK_TOPIC, Delegator
from crew_os.orchestration.registry import AgentRegistry
from crew_os.orchestration.supervisor_tier1 import SupervisorT1
from crew_os.orchestration.supervisor_tier2 import PlanResult, SupervisorMode, SupervisorT2
from crew_os.security.audit import AuditLogger
from crew_os.security.rate_limit import RateLimiter


class _CompletingAgent(BaseAgent):
    async def handle_task(self, task: Task) -> Task:
        return task.model_copy(update={"status": TaskStatus.COMPLETED, "result": {"ok": True}})


class _CrashingAgent(BaseAgent):
    async def handle_task(self, task: Task) -> Task:
        raise RuntimeError("agent exploded")


class _StubT2(SupervisorT2):
    def __init__(self) -> None:
        super().__init__()
        self.called = 0

    async def plan(self, task: Task) -> PlanResult:
        self.called += 1
        return PlanResult(text="PLAN: do x then y", source=SupervisorMode.LOCAL)


def _agent(tmp_path: Path, cls: type[BaseAgent], role: AgentRole) -> BaseAgent:
    return cls(
        role=role,
        model="m",
        policy=PolicyEngine(),
        rate_limiter=RateLimiter(),
        auditor=AuditLogger(tmp_path / f"{role.value}.jsonl"),
    )


@pytest.fixture
async def store(tmp_path: Path) -> SqliteStore:
    s = SqliteStore(tmp_path / "mem.db")
    await s.connect()
    yield s
    await s.close()


def _delegator(
    tmp_path: Path,
    store: SqliteStore,
    registry: AgentRegistry,
    *,
    threshold: int = 7,
    bus: MessageBus | None = None,
) -> tuple[Delegator, AuditLogger]:
    auditor = AuditLogger(tmp_path / "audit.jsonl")
    deleg = Delegator(
        registry=registry,
        tier1=SupervisorT1(escalation_threshold=threshold),
        tier2=_StubT2(),
        store=store,
        auditor=auditor,
        bus=bus,
    )
    return deleg, auditor


def _coding_task() -> Task:
    return Task(description="implement a small function", trace=TraceContext.new_root())


async def test_simple_task_routed_and_completed(tmp_path: Path, store: SqliteStore) -> None:
    reg = AgentRegistry()
    reg.register(_agent(tmp_path, _CompletingAgent, AgentRole.CODER))
    deleg, auditor = _delegator(tmp_path, store, reg)

    result = await deleg.delegate(_coding_task())
    assert result.routing.target_role == AgentRole.CODER
    assert result.plan is None
    assert result.task.status == TaskStatus.COMPLETED

    # Persisted.
    stored = await store.get_task(result.task.id)
    assert stored is not None
    assert stored.status == TaskStatus.COMPLETED

    types = [r.event["type"] for r in auditor.iter_records()]
    assert EventType.TASK_ASSIGNED in types
    assert EventType.TASK_COMPLETED in types


async def test_complex_task_escalates_to_t2(tmp_path: Path, store: SqliteStore) -> None:
    reg = AgentRegistry()
    reg.register(_agent(tmp_path, _CompletingAgent, AgentRole.CODER))
    # Low threshold forces escalation for the coding task.
    deleg, _ = _delegator(tmp_path, store, reg, threshold=0)

    result = await deleg.delegate(_coding_task())
    assert result.plan is not None
    assert result.plan.text.startswith("PLAN")
    # The plan is seeded into the task result before execution.
    stored = await store.get_task(result.task.id)
    assert stored is not None


async def test_missing_agent_marks_failed(tmp_path: Path, store: SqliteStore) -> None:
    reg = AgentRegistry()  # no agents registered
    deleg, auditor = _delegator(tmp_path, store, reg)

    result = await deleg.delegate(_coding_task())
    assert result.task.status == TaskStatus.FAILED
    assert result.task.error is not None
    assert "no agent registered" in result.task.error

    types = [r.event["type"] for r in auditor.iter_records()]
    assert EventType.TASK_FAILED in types


async def test_agent_exception_marks_failed(tmp_path: Path, store: SqliteStore) -> None:
    reg = AgentRegistry()
    reg.register(_agent(tmp_path, _CrashingAgent, AgentRole.CODER))
    deleg, _ = _delegator(tmp_path, store, reg)

    result = await deleg.delegate(_coding_task())
    assert result.task.status == TaskStatus.FAILED
    assert result.task.error is not None
    assert "agent exploded" in result.task.error

    stored = await store.get_task(result.task.id)
    assert stored is not None
    assert stored.status == TaskStatus.FAILED


async def test_publishes_to_bus(tmp_path: Path, store: SqliteStore) -> None:
    reg = AgentRegistry()
    reg.register(_agent(tmp_path, _CompletingAgent, AgentRole.CODER))
    bus = MessageBus()
    sub = await bus.subscribe(TASK_TOPIC)
    deleg, _ = _delegator(tmp_path, store, reg, bus=bus)

    result = await deleg.delegate(_coding_task())
    published = await sub.get(timeout=1.0)
    assert isinstance(published, Task)
    assert published.id == result.task.id
    assert published.status == TaskStatus.COMPLETED


async def test_audit_chain_valid_after_delegation(tmp_path: Path, store: SqliteStore) -> None:
    reg = AgentRegistry()
    reg.register(_agent(tmp_path, _CompletingAgent, AgentRole.CODER))
    deleg, auditor = _delegator(tmp_path, store, reg)
    await deleg.delegate(_coding_task())
    assert auditor.verify().valid is True
