"""Tests for ``crew_os.orchestration.registry``."""

from __future__ import annotations

from pathlib import Path

import pytest

from crew_os.agents.base import BaseAgent
from crew_os.core.exceptions import AgentNotRegisteredError
from crew_os.core.models import AgentRole, Task, TaskStatus, ToolManifest
from crew_os.core.policy import PolicyEngine
from crew_os.orchestration.registry import AgentRegistry
from crew_os.security.audit import AuditLogger
from crew_os.security.rate_limit import RateLimiter


class _Agent(BaseAgent):
    async def handle_task(self, task: Task) -> Task:
        return task.model_copy(update={"status": TaskStatus.COMPLETED})


def _make(tmp_path: Path, role: AgentRole) -> _Agent:
    return _Agent(
        role=role,
        model="m",
        policy=PolicyEngine(),
        rate_limiter=RateLimiter(),
        auditor=AuditLogger(tmp_path / f"{role.value}.jsonl"),
    )


def test_register_and_get(tmp_path: Path) -> None:
    reg = AgentRegistry()
    coder = _make(tmp_path, AgentRole.CODER)
    reg.register(coder)
    assert reg.get(AgentRole.CODER) is coder
    assert reg.get(AgentRole.AUDITOR) is None
    assert AgentRole.CODER in reg
    assert len(reg) == 1


def test_duplicate_registration_raises(tmp_path: Path) -> None:
    reg = AgentRegistry()
    reg.register(_make(tmp_path, AgentRole.CODER))
    with pytest.raises(ValueError, match="already registered"):
        reg.register(_make(tmp_path, AgentRole.CODER))


def test_require_raises_for_missing(tmp_path: Path) -> None:
    reg = AgentRegistry()
    with pytest.raises(AgentNotRegisteredError, match="auditor"):
        reg.require(AgentRole.AUDITOR)


def test_roles_returns_all(tmp_path: Path) -> None:
    reg = AgentRegistry()
    reg.register(_make(tmp_path, AgentRole.CODER))
    reg.register(_make(tmp_path, AgentRole.AUDITOR))
    assert reg.roles() == frozenset({AgentRole.CODER, AgentRole.AUDITOR})


def test_with_capability(tmp_path: Path) -> None:
    reg = AgentRegistry()
    coder = _make(tmp_path, AgentRole.CODER)
    coder.register_tool(ToolManifest(name="write_code"), _noop)
    auditor = _make(tmp_path, AgentRole.AUDITOR)
    reg.register(coder)
    reg.register(auditor)
    assert reg.with_capability("write_code") == [coder]
    assert reg.with_capability("nonexistent") == []


async def _noop(**_: object) -> None:
    return None
