"""Delegation pipeline: route -> (optionally escalate) -> execute -> persist.

Steps:

1. Tier-1 routes the task (category, target role, escalate flag).
2. If escalation is flagged, Tier-2 produces a plan, attached to the task
   under ``result["plan"]``.
3. The target agent (from the registry) runs the task.
4. The task's final state is persisted and a lifecycle event is published.

Security note: routing to ``sec_offensive`` is *not* gated here. The
offensive agent's own tool calls are blocked by the policy engine unless
LAB_MODE is active - enforcement stays in one place (defense in depth).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from crew_os.core.models import (
    Event,
    EventType,
    Severity,
    Task,
    TaskStatus,
    utcnow,
)
from crew_os.memory.sqlite_store import SqliteStore
from crew_os.orchestration.bus import MessageBus
from crew_os.orchestration.registry import AgentRegistry
from crew_os.orchestration.supervisor_tier1 import RoutingDecision, SupervisorT1
from crew_os.orchestration.supervisor_tier2 import PlanResult, SupervisorT2
from crew_os.security.audit import AuditLogger

TASK_TOPIC = "tasks"


@dataclass(frozen=True)
class DelegationResult:
    task: Task
    routing: RoutingDecision
    plan: PlanResult | None


class Delegator:
    def __init__(
        self,
        *,
        registry: AgentRegistry,
        tier1: SupervisorT1,
        tier2: SupervisorT2,
        store: SqliteStore,
        auditor: AuditLogger,
        bus: MessageBus | None = None,
    ) -> None:
        self._registry = registry
        self._t1 = tier1
        self._t2 = tier2
        self._store = store
        self._auditor = auditor
        self._bus = bus

    async def _audit(self, event: Event) -> None:
        await asyncio.to_thread(self._auditor.append, event)

    async def _publish(self, task: Task) -> None:
        if self._bus is not None:
            await self._bus.publish(TASK_TOPIC, task)

    async def delegate(self, task: Task) -> DelegationResult:
        routing = self._t1.route(task)
        await self._audit(
            Event(
                type=EventType.TASK_ASSIGNED,
                trace=task.trace,
                agent_role=routing.target_role,
                payload={
                    "task_id": task.id,
                    "category": routing.category.value,
                    "complexity": routing.complexity,
                    "escalate": routing.escalate_to_t2,
                    "reason": routing.reason,
                },
            )
        )

        plan: PlanResult | None = None
        if routing.escalate_to_t2:
            plan = await self._t2.plan(task)
            await self._audit(
                Event(
                    type=EventType.TASK_ASSIGNED,
                    severity=Severity.INFO,
                    trace=task.trace,
                    agent_role=routing.target_role,
                    payload={
                        "task_id": task.id,
                        "escalated": True,
                        "plan_source": plan.source.value,
                    },
                )
            )

        agent = self._registry.get(routing.target_role)
        if agent is None:
            failed = task.model_copy(
                update={
                    "assigned_to": routing.target_role,
                    "status": TaskStatus.FAILED,
                    "error": f"no agent registered for role {routing.target_role}",
                    "updated_at": utcnow(),
                }
            )
            await self._store.save_task(failed)
            await self._audit(
                Event(
                    type=EventType.TASK_FAILED,
                    severity=Severity.ERROR,
                    trace=task.trace,
                    agent_role=routing.target_role,
                    payload={"task_id": task.id, "reason": failed.error},
                )
            )
            await self._publish(failed)
            return DelegationResult(task=failed, routing=routing, plan=plan)

        result_seed: dict[str, str] = {}
        if plan is not None:
            result_seed = {"plan": plan.text}
        assigned = task.model_copy(
            update={
                "assigned_to": routing.target_role,
                "status": TaskStatus.IN_PROGRESS,
                "result": result_seed or task.result,
                "updated_at": utcnow(),
            }
        )
        await self._store.save_task(assigned)

        try:
            done = await agent.handle_task(assigned)
        except Exception as exc:  # record failure, don't crash the orchestrator
            failed = assigned.model_copy(
                update={
                    "status": TaskStatus.FAILED,
                    "error": f"{type(exc).__name__}: {exc}",
                    "updated_at": utcnow(),
                }
            )
            await self._store.save_task(failed)
            await self._audit(
                Event(
                    type=EventType.TASK_FAILED,
                    severity=Severity.ERROR,
                    trace=task.trace,
                    agent_role=routing.target_role,
                    payload={"task_id": task.id, "error": failed.error},
                )
            )
            await self._publish(failed)
            return DelegationResult(task=failed, routing=routing, plan=plan)

        await self._store.save_task(done)
        await self._audit(
            Event(
                type=EventType.TASK_COMPLETED,
                trace=task.trace,
                agent_role=routing.target_role,
                payload={"task_id": task.id, "status": done.status.value},
            )
        )
        await self._publish(done)
        return DelegationResult(task=done, routing=routing, plan=plan)
