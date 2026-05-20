"""Tests for ``crew_os.memory.sqlite_store`` (async aiosqlite)."""

from __future__ import annotations

from pathlib import Path

import pytest

from crew_os.core.exceptions import WorkingMemoryError
from crew_os.core.models import AgentRole, Task, TaskStatus, TraceContext
from crew_os.memory.sqlite_store import SqliteStore


@pytest.fixture
async def store(tmp_path: Path) -> SqliteStore:
    s = SqliteStore(tmp_path / "memory.db")
    await s.connect()
    yield s
    await s.close()


def _task(desc: str = "do work", **kw: object) -> Task:
    return Task(description=desc, trace=TraceContext.new_root(), **kw)


async def test_connect_enables_wal(store: SqliteStore, tmp_path: Path) -> None:
    conn = store._require_conn()
    async with conn.execute("PRAGMA journal_mode;") as cur:
        row = await cur.fetchone()
    assert row[0].lower() == "wal"


async def test_save_and_get_task_roundtrip(store: SqliteStore) -> None:
    task = _task(assigned_to=AgentRole.CODER)
    await store.save_task(task)
    got = await store.get_task(task.id)
    assert got is not None
    assert got.id == task.id
    assert got.description == task.description
    assert got.assigned_to == AgentRole.CODER
    # Full trace fidelity is preserved through the JSON column.
    assert got.trace == task.trace


async def test_get_missing_task_returns_none(store: SqliteStore) -> None:
    assert await store.get_task("nope") is None


async def test_save_is_upsert(store: SqliteStore) -> None:
    task = _task()
    await store.save_task(task)
    updated = task.model_copy(update={"description": "changed"})
    await store.save_task(updated)
    got = await store.get_task(task.id)
    assert got is not None
    assert got.description == "changed"
    assert len(await store.list_tasks()) == 1


async def test_list_tasks_by_status(store: SqliteStore) -> None:
    await store.save_task(_task("a"))
    done = _task("b").model_copy(update={"status": TaskStatus.COMPLETED})
    await store.save_task(done)
    pending = await store.list_tasks(status=TaskStatus.PENDING)
    completed = await store.list_tasks(status=TaskStatus.COMPLETED)
    assert [t.description for t in pending] == ["a"]
    assert [t.description for t in completed] == ["b"]


async def test_list_tasks_by_trace(store: SqliteStore) -> None:
    trace = TraceContext.new_root()
    t1 = Task(description="x", trace=trace)
    t2 = Task(description="y", trace=trace)
    other = _task("z")
    for t in (t1, t2, other):
        await store.save_task(t)
    same = await store.list_tasks(trace_id=trace.trace_id)
    assert {t.description for t in same} == {"x", "y"}


async def test_update_task_status_sets_result(store: SqliteStore) -> None:
    task = _task()
    await store.save_task(task)
    updated = await store.update_task_status(task.id, TaskStatus.COMPLETED, result={"out": 42})
    assert updated.status == TaskStatus.COMPLETED
    assert updated.result == {"out": 42}
    assert updated.updated_at >= task.updated_at
    reloaded = await store.get_task(task.id)
    assert reloaded is not None
    assert reloaded.status == TaskStatus.COMPLETED


async def test_update_missing_task_raises(store: SqliteStore) -> None:
    with pytest.raises(WorkingMemoryError, match="not found"):
        await store.update_task_status("ghost", TaskStatus.FAILED)


async def test_agent_memory_roundtrip(store: SqliteStore) -> None:
    await store.put_memory(AgentRole.CODER, "scratch", {"n": 1, "s": "hi"})
    assert await store.get_memory(AgentRole.CODER, "scratch") == {"n": 1, "s": "hi"}


async def test_agent_memory_upsert(store: SqliteStore) -> None:
    await store.put_memory(AgentRole.CODER, "k", "v1")
    await store.put_memory(AgentRole.CODER, "k", "v2")
    assert await store.get_memory(AgentRole.CODER, "k") == "v2"


async def test_agent_memory_missing_returns_none(store: SqliteStore) -> None:
    assert await store.get_memory(AgentRole.AUDITOR, "absent") is None


async def test_operations_before_connect_raise(tmp_path: Path) -> None:
    s = SqliteStore(tmp_path / "x.db")
    with pytest.raises(WorkingMemoryError, match="not connected"):
        await s.get_task("any")


async def test_context_manager(tmp_path: Path) -> None:
    async with SqliteStore(tmp_path / "ctx.db") as s:
        await s.save_task(_task())
        assert len(await s.list_tasks()) == 1
