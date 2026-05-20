"""REST API for the dashboard.

Agent and task data are read live from the registry and store (cheap,
never stale). System/GPU metrics come from the cached sampler snapshot.
"""

from __future__ import annotations

import time
from typing import Any, cast

from fastapi import APIRouter, HTTPException, Request

from crew_os.core.models import TaskStatus
from crew_os.memory.sqlite_store import SqliteStore
from crew_os.metrics.sampler import MetricsSampler, MetricsSnapshot
from crew_os.metrics.usage import UsageReport, UsageTracker
from crew_os.orchestration.registry import AgentRegistry

router = APIRouter(prefix="/api")


def _registry(request: Request) -> AgentRegistry:
    return cast("AgentRegistry", request.app.state.registry)


def _store(request: Request) -> SqliteStore:
    return cast("SqliteStore", request.app.state.store)


def _sampler(request: Request) -> MetricsSampler:
    return cast("MetricsSampler", request.app.state.sampler)


def _usage(request: Request) -> UsageTracker:
    return cast("UsageTracker", request.app.state.usage)


def _start_time(request: Request) -> float:
    return cast("float", request.app.state.start_time)


@router.get("/status")
async def get_status(request: Request) -> dict[str, Any]:
    store = _store(request)
    tasks = await store.list_tasks()
    by_status: dict[str, int] = {}
    for task in tasks:
        by_status[task.status.value] = by_status.get(task.status.value, 0) + 1
    in_progress = by_status.get(TaskStatus.IN_PROGRESS.value, 0)
    completed = by_status.get(TaskStatus.COMPLETED.value, 0)
    return {
        "status": "running" if in_progress > 0 else "idle",
        "uptime_seconds": round(time.monotonic() - _start_time(request), 1),
        "tasks_total": len(tasks),
        "tasks_completed": completed,
        "tasks_by_status": by_status,
        "agents_online": len(_registry(request)),
    }


@router.get("/agents")
async def get_agents(request: Request) -> list[dict[str, Any]]:
    registry = _registry(request)
    out: list[dict[str, Any]] = []
    for role in sorted(registry.roles(), key=lambda r: r.value):
        agent = registry.require(role)
        out.append(
            {
                "role": role.value,
                "model": agent.model,
                "capabilities": sorted(agent.capabilities),
            }
        )
    return out


@router.get("/tasks")
async def get_tasks(request: Request, status: TaskStatus | None = None) -> list[dict[str, Any]]:
    tasks = await _store(request).list_tasks(status=status)
    return [t.model_dump(mode="json") for t in tasks]


@router.get("/metrics", response_model=None)
async def get_metrics(request: Request) -> MetricsSnapshot:
    snapshot = _sampler(request).latest()
    if snapshot is None:
        raise HTTPException(status_code=503, detail="metrics not yet sampled")
    return snapshot


@router.get("/usage", response_model=None)
async def get_usage(request: Request) -> UsageReport:
    return _usage(request).report()
