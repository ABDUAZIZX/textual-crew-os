"""REST API for the dashboard.

Agent and task data are read live from the registry and store (cheap,
never stale). System/GPU metrics come from the cached sampler snapshot.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from pathlib import Path
from typing import Any, cast

from fastapi import APIRouter, HTTPException, Request

from crew_os.config import Settings
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


def _settings(request: Request) -> Settings:
    return cast("Settings", request.app.state.settings)


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


@router.get("/config")
async def get_config(request: Request) -> dict[str, Any]:
    """Expose a *safe* read-only view of runtime settings.

    Secrets (e.g. the Anthropic API key) are never returned; the key is
    surfaced only as a boolean ``anthropic_configured`` flag.
    """

    s = _settings(request)
    return {
        "ollama_host": s.ollama_host,
        "ollama_timeout": s.ollama_timeout,
        "web_host": s.web_host,
        "web_port": s.web_port,
        "log_level": s.log_level,
        "lab_mode": s.lab_mode,
        "anthropic_configured": s.anthropic_api_key is not None,
        "anthropic_model": s.anthropic_model,
        "data_dir": str(s.data_dir),
    }


def _read_audit_tail(path: Path, limit: int) -> list[dict[str, Any]]:
    """Return the last ``limit`` audit records, summarised. Memory-bounded."""

    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as f:
        for raw in deque(f, maxlen=limit):
            line = raw.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            event = rec.get("event", {})
            rows.append(
                {
                    "seq": rec.get("seq"),
                    "ts": rec.get("ts"),
                    "type": event.get("type"),
                    "severity": event.get("severity"),
                    "agent": event.get("agent_role"),
                }
            )
    return rows


@router.get("/audit")
async def get_audit(request: Request, limit: int = 100) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 1000))
    path = _settings(request).data_dir / "audit" / "audit.jsonl"
    return await asyncio.to_thread(_read_audit_tail, path, limit)
