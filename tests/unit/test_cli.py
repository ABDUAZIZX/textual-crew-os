"""Tests for the ``crew`` CLI via Click's CliRunner."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from crew_os import __version__
from crew_os.cli import composition, main
from crew_os.core.models import (
    AgentRole,
    Event,
    EventType,
    Severity,
    Task,
    TaskStatus,
    TraceContext,
)
from crew_os.orchestration.delegator import DelegationResult
from crew_os.orchestration.supervisor_tier1 import RoutingDecision, TaskCategory
from crew_os.security.audit import AuditLogger


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "data"
    monkeypatch.setenv("CREW_DATA_DIR", str(d))
    get_settings = main.get_settings
    get_settings.cache_clear()  # type: ignore[attr-defined]
    return d


# ─────────────────────────────── help / version ────────────────────────


def test_help(runner: CliRunner) -> None:
    result = runner.invoke(main.cli, ["--help"])
    assert result.exit_code == 0
    assert "run" in result.output
    assert "dashboard" in result.output
    assert "audit-llm" in result.output


def test_version(runner: CliRunner) -> None:
    result = runner.invoke(main.cli, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


# ─────────────────────────────── run (mocked crew) ─────────────────────


class _FakeDelegator:
    async def delegate(self, task: Task) -> object:
        routing = RoutingDecision(
            target_role=AgentRole.CODER,
            category=TaskCategory.CODING,
            complexity=3,
            escalate_to_t2=False,
            reason="test",
        )
        done = task.model_copy(
            update={"status": TaskStatus.COMPLETED, "result": {"code": "print(1)"}}
        )
        return DelegationResult(task=done, routing=routing, plan=None)


class _FakeCrew:
    def __init__(self) -> None:
        self.delegator = _FakeDelegator()
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


def test_run_command(runner: CliRunner, monkeypatch: pytest.MonkeyPatch, data_dir: Path) -> None:
    fake = _FakeCrew()

    async def _build(*_a: object, **_k: object) -> _FakeCrew:
        return fake

    monkeypatch.setattr(composition, "build_crew", _build)
    result = runner.invoke(main.cli, ["run", "implement a function"])
    assert result.exit_code == 0
    assert "coder" in result.output
    assert "completed" in result.output
    assert fake.closed is True


# ─────────────────────────────── logs / replay ─────────────────────────


def _seed_audit(data_dir: Path) -> str:
    audit = AuditLogger(data_dir / "audit" / "audit.jsonl")
    trace = TraceContext.new_root()
    audit.append(
        Event(
            type=EventType.TASK_ASSIGNED,
            severity=Severity.INFO,
            trace=trace,
            agent_role=AgentRole.CODER,
            payload={"task_id": "t1"},
        )
    )
    audit.append(
        Event(
            type=EventType.TASK_COMPLETED,
            severity=Severity.INFO,
            trace=trace,
            agent_role=AgentRole.CODER,
            payload={"task_id": "t1"},
        )
    )
    # An unrelated trace.
    audit.append(Event(type=EventType.RATE_LIMITED, trace=TraceContext.new_root()))
    return trace.trace_id


def test_logs_all(runner: CliRunner, data_dir: Path) -> None:
    _seed_audit(data_dir)
    result = runner.invoke(main.cli, ["logs"])
    assert result.exit_code == 0
    assert "task_assigned" in result.output
    assert "rate_limited" in result.output


def test_logs_filtered_by_trace(runner: CliRunner, data_dir: Path) -> None:
    trace_id = _seed_audit(data_dir)
    result = runner.invoke(main.cli, ["logs", "--trace", trace_id])
    assert result.exit_code == 0
    assert "task_assigned" in result.output
    assert "rate_limited" not in result.output


def test_logs_empty(runner: CliRunner, data_dir: Path) -> None:
    result = runner.invoke(main.cli, ["logs"])
    assert result.exit_code == 0
    assert "no matching" in result.output


def test_replay(runner: CliRunner, data_dir: Path) -> None:
    trace_id = _seed_audit(data_dir)
    result = runner.invoke(main.cli, ["replay", trace_id])
    assert result.exit_code == 0
    assert "Replay of trace" in result.output
    assert "task_assigned" in result.output
    assert "task_completed" in result.output


def test_replay_unknown_trace(runner: CliRunner, data_dir: Path) -> None:
    _seed_audit(data_dir)
    result = runner.invoke(main.cli, ["replay", "deadbeef" * 4])
    assert result.exit_code == 0
    assert "no events" in result.output


def test_audit_path_uses_data_dir(data_dir: Path) -> None:
    trace_id = _seed_audit(data_dir)
    records = main._read_audit()
    assert any((r["event"].get("trace") or {}).get("trace_id") == trace_id for r in records)
    assert json.dumps(records)  # serialisable
