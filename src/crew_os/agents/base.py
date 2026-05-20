"""Abstract agent base class.

A :class:`BaseAgent` owns a set of registered tools and mediates every
tool invocation through the security stack:

    rate limiter  ->  policy engine  ->  audit log  ->  execute

Subclasses implement :meth:`handle_task`. They may also call
:meth:`think` to talk to their assigned model via a
:class:`~crew_os.llm.model_manager.ModelManager`.

Audit writes are pushed to a worker thread (``asyncio.to_thread``) so the
fsync in :class:`AuditLogger` never blocks the event loop.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from crew_os.core.exceptions import PolicyViolation, RateLimitExceeded
from crew_os.core.models import (
    AgentRole,
    Event,
    EventType,
    Severity,
    Task,
    ToolCall,
    ToolManifest,
    TraceContext,
)
from crew_os.core.policy import Decision, PolicyEngine
from crew_os.llm.model_manager import ModelManager
from crew_os.security.audit import AuditLogger
from crew_os.security.rate_limit import RateLimiter

ToolFunc = Callable[..., Awaitable[Any]]


@dataclass(frozen=True)
class Tool:
    manifest: ToolManifest
    func: ToolFunc


class BaseAgent(ABC):
    def __init__(
        self,
        *,
        role: AgentRole,
        model: str,
        policy: PolicyEngine,
        rate_limiter: RateLimiter,
        auditor: AuditLogger,
        model_manager: ModelManager | None = None,
        lab_mode_active: bool = False,
    ) -> None:
        self._role = role
        self._model = model
        self._policy = policy
        self._rate_limiter = rate_limiter
        self._auditor = auditor
        self._model_manager = model_manager
        self._lab_mode_active = lab_mode_active
        self._tools: dict[str, Tool] = {}

    @property
    def role(self) -> AgentRole:
        return self._role

    @property
    def model(self) -> str:
        return self._model

    @property
    def capabilities(self) -> frozenset[str]:
        return frozenset(self._tools)

    def tool_manifest(self, name: str) -> ToolManifest | None:
        tool = self._tools.get(name)
        return tool.manifest if tool is not None else None

    # ── tool registry ───────────────────────────────────────────────

    def register_tool(self, manifest: ToolManifest, func: ToolFunc) -> None:
        if manifest.name in self._tools:
            raise ValueError(f"tool {manifest.name!r} already registered")
        self._tools[manifest.name] = Tool(manifest=manifest, func=func)

    async def _audit(self, event: Event) -> None:
        await asyncio.to_thread(self._auditor.append, event)

    async def invoke_tool(
        self,
        tool_name: str,
        args: dict[str, Any],
        trace: TraceContext,
    ) -> Any:
        """Invoke a registered tool through the full security pipeline.

        Raises:
            PolicyViolation: tool unregistered or denied by policy.
            RateLimitExceeded: per-(role, tool) budget exhausted.
        """

        call = ToolCall(tool_name=tool_name, args=args, agent_role=self._role, trace=trace)

        tool = self._tools.get(tool_name)
        if tool is None:
            await self._audit(
                Event(
                    type=EventType.TOOL_CALL_DENIED,
                    severity=Severity.WARNING,
                    trace=trace,
                    agent_role=self._role,
                    payload={"tool": tool_name, "reason": "tool not registered"},
                )
            )
            raise PolicyViolation(f"tool {tool_name!r} is not registered for {self._role}")

        rl_key = f"{self._role.value}:{tool_name}"
        if not self._rate_limiter.check(rl_key):
            await self._audit(
                Event(
                    type=EventType.RATE_LIMITED,
                    severity=Severity.WARNING,
                    trace=trace,
                    agent_role=self._role,
                    payload={"tool": tool_name, "key": rl_key},
                )
            )
            raise RateLimitExceeded(f"rate limit exceeded for {rl_key!r}")

        decision = self._policy.evaluate(call, tool.manifest, lab_mode_active=self._lab_mode_active)
        if decision.decision == Decision.DENY:
            await self._audit(
                Event(
                    type=EventType.TOOL_CALL_DENIED,
                    severity=Severity.WARNING,
                    trace=trace,
                    agent_role=self._role,
                    payload={
                        "tool": tool_name,
                        "rule": decision.rule,
                        "reason": decision.reason,
                    },
                )
            )
            raise PolicyViolation(f"{decision.rule}: {decision.reason}")

        await self._audit(
            Event(
                type=EventType.TOOL_CALL_ALLOWED,
                severity=Severity.INFO,
                trace=trace,
                agent_role=self._role,
                payload={"tool": tool_name, "rule": decision.rule},
            )
        )
        return await tool.func(**args)

    # ── model access ────────────────────────────────────────────────

    async def think(self, prompt: str, *, system: str | None = None) -> str:
        """Generate a completion from this agent's assigned model."""

        if self._model_manager is None:
            raise RuntimeError(f"agent {self._role} has no model_manager; cannot call think()")
        result = await self._model_manager.generate(self._model, prompt, system=system)
        return result.response

    # ── task handling ───────────────────────────────────────────────

    @abstractmethod
    async def handle_task(self, task: Task) -> Task:
        """Process a task and return its updated form. Implemented by subclasses."""
