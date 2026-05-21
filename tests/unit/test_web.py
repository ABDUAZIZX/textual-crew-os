"""Tests for the FastAPI dashboard backend (REST + CORS)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from crew_os.agents.base import BaseAgent
from crew_os.core.models import AgentRole, Task, TaskStatus, ToolManifest, TraceContext
from crew_os.core.policy import PolicyEngine
from crew_os.memory.sqlite_store import SqliteStore
from crew_os.metrics.gpu import GpuMetrics
from crew_os.metrics.sampler import MetricsSampler
from crew_os.metrics.system import SystemMetrics
from crew_os.orchestration.bus import MessageBus
from crew_os.orchestration.registry import AgentRegistry
from crew_os.security.audit import AuditLogger
from crew_os.security.rate_limit import RateLimiter
from crew_os.web.app import create_app
from crew_os.web.routes import _read_audit_tail


class _Agent(BaseAgent):
    async def handle_task(self, task: Task) -> Task:
        return task.model_copy(update={"status": TaskStatus.COMPLETED})


def _fake_sampler() -> MetricsSampler:
    return MetricsSampler(
        interval=0.05,
        system_fn=lambda: SystemMetrics(
            cpu_percent=5.0,
            ram_percent=40.0,
            ram_used_mb=25000,
            ram_total_mb=64000,
            disk_read_bytes=0,
            disk_write_bytes=0,
            timestamp="t",
        ),
        gpu_fn=lambda: GpuMetrics(available=True, name="RTX 2060 SUPER", vram_total_mb=8192),
        gpu_power_fn=lambda: 90.0,
        rapl_fn=lambda: None,
    )


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    store = SqliteStore(tmp_path / "mem.db")
    await store.connect()

    registry = AgentRegistry()
    coder = _Agent(
        role=AgentRole.CODER,
        model="qwen2.5-coder:7b",
        policy=PolicyEngine(),
        rate_limiter=RateLimiter(),
        auditor=AuditLogger(tmp_path / "audit.jsonl"),
    )
    coder.register_tool(ToolManifest(name="write_file"), _noop)
    registry.register(coder)

    app = create_app(
        registry=registry,
        store=store,
        bus=MessageBus(),
        sampler=_fake_sampler(),
    )

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            c._store = store  # type: ignore[attr-defined]
            yield c
    await store.close()


async def _noop(**_: object) -> None:
    return None


async def test_status_idle(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "idle"
    assert body["agents_online"] == 1
    assert body["tasks_total"] == 0
    assert body["uptime_seconds"] >= 0


async def test_status_running_when_in_progress(client: httpx.AsyncClient) -> None:
    store: SqliteStore = client._store  # type: ignore[attr-defined]
    task = Task(description="x", trace=TraceContext.new_root())
    await store.save_task(task.model_copy(update={"status": TaskStatus.IN_PROGRESS}))
    resp = await client.get("/api/status")
    assert resp.json()["status"] == "running"


async def test_agents_endpoint(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/agents")
    assert resp.status_code == 200
    agents = resp.json()
    assert len(agents) == 1
    assert agents[0]["role"] == "coder"
    assert "write_file" in agents[0]["capabilities"]


async def test_tasks_endpoint_and_filter(client: httpx.AsyncClient) -> None:
    store: SqliteStore = client._store  # type: ignore[attr-defined]
    await store.save_task(Task(description="a", trace=TraceContext.new_root()))
    done = Task(description="b", trace=TraceContext.new_root()).model_copy(
        update={"status": TaskStatus.COMPLETED}
    )
    await store.save_task(done)

    all_tasks = (await client.get("/api/tasks")).json()
    assert len(all_tasks) == 2
    completed = (await client.get("/api/tasks?status=completed")).json()
    assert len(completed) == 1
    assert completed[0]["description"] == "b"


async def test_metrics_endpoint(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/metrics")
    assert resp.status_code == 200
    body = resp.json()
    assert body["gpu"]["name"] == "RTX 2060 SUPER"
    assert body["gpu_watts"] == 90.0
    assert body["system"]["cpu_percent"] == 5.0


async def test_cors_allows_localhost_origin(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/status", headers={"Origin": "http://127.0.0.1:8765"})
    assert resp.headers.get("access-control-allow-origin") == "http://127.0.0.1:8765"


async def test_cors_blocks_foreign_origin(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/status", headers={"Origin": "http://evil.example.com"})
    assert "access-control-allow-origin" not in resp.headers


async def test_ws_route_registered(client: httpx.AsyncClient) -> None:
    paths = {getattr(r, "path", None) for r in client._transport.app.routes}  # type: ignore[attr-defined]
    assert "/ws" in paths


async def test_usage_endpoint(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/usage")
    assert resp.status_code == 200
    body = resp.json()
    assert "session" in body
    assert "week" in body
    assert body["session"]["total_tokens"] == 0
    assert body["currency"] == "USD"


async def test_config_endpoint_exposes_safe_subset(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/config")
    assert resp.status_code == 200
    body = resp.json()
    # Expected safe fields are present.
    assert body["ollama_host"].startswith("http://127.0.0.1")
    assert isinstance(body["ollama_timeout"], int)
    assert body["web_host"] in {"127.0.0.1", "localhost", "::1"}
    assert isinstance(body["anthropic_configured"], bool)
    # The secret itself must never be exposed, under any key.
    assert "anthropic_api_key" not in body
    assert "api_key" not in body
    assert all("secret" not in k.lower() for k in body)


async def test_audit_endpoint_returns_list(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/audit?limit=5")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def _audit_line(seq: int, etype: str, severity: str, agent: str | None) -> str:
    return json.dumps(
        {
            "seq": seq,
            "ts": f"t{seq}",
            "event": {"type": etype, "severity": severity, "agent_role": agent},
        }
    )


def test_read_audit_tail_summarises_and_bounds(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    lines = [
        _audit_line(0, "task_created", "info", "coder"),
        "not json - skipped",
        _audit_line(1, "agent_message", "info", None),
        _audit_line(2, "rate_limited", "warning", "auditor"),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # limit=4 keeps all lines; the malformed one is parsed-and-skipped.
    rows = _read_audit_tail(path, limit=4)
    assert len(rows) == 3
    assert rows[-1] == {
        "seq": 2,
        "ts": "t2",
        "type": "rate_limited",
        "severity": "warning",
        "agent": "auditor",
    }
    # limit bounds the tail.
    assert [r["seq"] for r in _read_audit_tail(path, limit=1)] == [2]


def test_read_audit_tail_missing_file(tmp_path: Path) -> None:
    assert _read_audit_tail(tmp_path / "nope.jsonl", limit=10) == []


async def test_chat_ws_route_registered(client: httpx.AsyncClient) -> None:
    paths = {getattr(r, "path", None) for r in client._transport.app.routes}  # type: ignore[attr-defined]
    assert "/ws/chat" in paths


async def test_chat_routes_503_without_service(client: httpx.AsyncClient) -> None:
    # The fixture builds the app without a chat_service.
    assert (await client.get("/api/chat/agents")).status_code == 503
    assert (await client.get("/api/chat/sessions")).status_code == 503


async def test_dashboard_html_served(client: httpx.AsyncClient) -> None:
    resp = await client.get("/")
    assert resp.status_code == 200
    assert "Crew Control Center" in resp.text
    assert "/static/js/dashboard.js" in resp.text


async def test_static_avatar_served(client: httpx.AsyncClient) -> None:
    resp = await client.get("/static/avatars/coder.svg")
    assert resp.status_code == 200
    assert "svg" in resp.text
