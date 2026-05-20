"""Tests for ``crew_os.core.models`` - validation and round-tripping."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from crew_os.core.models import (
    AgentRole,
    Event,
    EventType,
    Severity,
    Task,
    TaskStatus,
    ToolCall,
    ToolManifest,
    TraceContext,
    new_id,
    utcnow,
)


def test_new_id_is_32_lowercase_hex() -> None:
    out = new_id()
    assert len(out) == 32
    assert all(c in "0123456789abcdef" for c in out)
    assert new_id() != new_id()


def test_utcnow_is_tz_aware() -> None:
    now = utcnow()
    assert now.tzinfo is not None
    assert now.utcoffset().total_seconds() == 0  # type: ignore[union-attr]


def test_trace_root_and_child() -> None:
    root = TraceContext.new_root()
    assert root.parent_span_id is None
    child = root.child()
    assert child.trace_id == root.trace_id
    assert child.parent_span_id == root.span_id
    assert child.span_id != root.span_id


def test_trace_rejects_bad_lengths() -> None:
    with pytest.raises(ValidationError):
        TraceContext(trace_id="short", span_id=new_id())


def test_trace_is_frozen() -> None:
    root = TraceContext.new_root()
    with pytest.raises(ValidationError):
        root.trace_id = "x" * 32  # type: ignore[misc]


def test_tool_manifest_defaults() -> None:
    m = ToolManifest(name="noop")
    assert m.requires_network is False
    assert m.requires_subprocess is False
    assert m.requires_lab_mode is False
    assert m.dangerous is False


def test_tool_call_roundtrip() -> None:
    trace = TraceContext.new_root()
    call = ToolCall(
        tool_name="read_file",
        args={"path": "/tmp/x"},
        agent_role=AgentRole.CODER,
        trace=trace,
    )
    data = call.model_dump(mode="json")
    rebuilt = ToolCall.model_validate(data)
    assert rebuilt == call


def test_task_default_status_and_timestamps() -> None:
    task = Task(description="run tests", trace=TraceContext.new_root())
    assert task.status == TaskStatus.PENDING
    assert task.assigned_to is None
    assert task.created_at == task.updated_at or task.updated_at >= task.created_at


def test_event_json_serialization() -> None:
    ev = Event(
        type=EventType.TASK_CREATED,
        severity=Severity.INFO,
        agent_role=AgentRole.SUPERVISOR_T1,
        payload={"task_id": "abc"},
    )
    blob = ev.model_dump_json()
    data = json.loads(blob)
    assert data["type"] == "task_created"
    assert data["severity"] == "info"
    assert data["agent_role"] == "supervisor_t1"
    assert data["payload"] == {"task_id": "abc"}


def test_agent_role_enum_values_are_stable() -> None:
    # The wire format depends on these strings; renaming breaks the audit log.
    assert AgentRole.CODER == "coder"
    assert AgentRole.SEC_OFFENSIVE == "sec_offensive"
    assert AgentRole.SUPERVISOR_T2 == "supervisor_t2"
