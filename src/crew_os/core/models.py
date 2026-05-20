"""Core Pydantic data models shared across the platform.

These types are intentionally small and immutable wherever possible.
They flow through every layer: agents emit them, the policy engine
inspects them, the audit log records them, the dashboard renders them.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


def new_id() -> str:
    """Return a fresh 32-character lowercase hex identifier."""

    return uuid.uuid4().hex


def utcnow() -> datetime:
    """Return the current UTC time as a tz-aware datetime."""

    return datetime.now(UTC)


# ─────────────────────────────── Enums ─────────────────────────────────


class AgentRole(StrEnum):
    SUPERVISOR_T1 = "supervisor_t1"
    SUPERVISOR_T2 = "supervisor_t2"
    CODER = "coder"
    SEC_DEFENSIVE = "sec_defensive"
    SEC_OFFENSIVE = "sec_offensive"
    AUDITOR = "auditor"


class TaskStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class EventType(StrEnum):
    TASK_CREATED = "task_created"
    TASK_ASSIGNED = "task_assigned"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"
    AGENT_MESSAGE = "agent_message"
    TOOL_CALL_REQUESTED = "tool_call_requested"
    TOOL_CALL_ALLOWED = "tool_call_allowed"
    TOOL_CALL_DENIED = "tool_call_denied"
    POLICY_VIOLATION = "policy_violation"
    RATE_LIMITED = "rate_limited"
    LAB_MODE_REQUESTED = "lab_mode_requested"
    LAB_MODE_GRANTED = "lab_mode_granted"
    LAB_MODE_DENIED = "lab_mode_denied"
    AUDIT_VERIFICATION = "audit_verification"
    AGENT_STARTED = "agent_started"
    AGENT_STOPPED = "agent_stopped"


class Severity(StrEnum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


# ─────────────────────────────── Trace ─────────────────────────────────


class TraceContext(BaseModel):
    """Distributed-trace style context propagated across agent calls."""

    model_config = ConfigDict(frozen=True)

    trace_id: str = Field(min_length=32, max_length=32)
    span_id: str = Field(min_length=32, max_length=32)
    parent_span_id: str | None = Field(default=None, min_length=32, max_length=32)

    @classmethod
    def new_root(cls) -> TraceContext:
        return cls(trace_id=new_id(), span_id=new_id())

    def child(self) -> TraceContext:
        return TraceContext(
            trace_id=self.trace_id,
            span_id=new_id(),
            parent_span_id=self.span_id,
        )


# ─────────────────────────────── Domain ────────────────────────────────


class ToolManifest(BaseModel):
    """Declared side-effects of a tool, evaluated by the policy engine."""

    model_config = ConfigDict(frozen=True)

    name: str
    description: str = ""
    requires_network: bool = False
    requires_subprocess: bool = False
    requires_lab_mode: bool = False
    dangerous: bool = False


class ToolCall(BaseModel):
    """An agent's request to invoke a named tool."""

    model_config = ConfigDict(frozen=True)

    tool_name: str
    args: dict[str, Any] = Field(default_factory=dict)
    agent_role: AgentRole
    trace: TraceContext


class Task(BaseModel):
    """A unit of work flowing through the orchestration layer."""

    id: str = Field(default_factory=new_id)
    trace: TraceContext
    parent_id: str | None = None
    description: str
    status: TaskStatus = TaskStatus.PENDING
    assigned_to: AgentRole | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    result: dict[str, Any] | None = None
    error: str | None = None


class AgentMessage(BaseModel):
    """A point-to-point message between two agents on the bus."""

    model_config = ConfigDict(frozen=True)

    from_agent: AgentRole
    to_agent: AgentRole
    content: str
    trace: TraceContext
    timestamp: datetime = Field(default_factory=utcnow)


class Event(BaseModel):
    """A discrete observable event - the unit of the audit log."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=new_id)
    type: EventType
    timestamp: datetime = Field(default_factory=utcnow)
    severity: Severity = Severity.INFO
    trace: TraceContext | None = None
    agent_role: AgentRole | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
