"""Async working-memory store backed by SQLite (WAL mode).

Holds two things:

* ``tasks`` - the full :class:`Task` serialised as JSON, with a few
  columns (``status``, ``trace_id``, ``parent_id``) lifted out for
  indexed querying.
* ``agent_memory`` - a per-agent key/value scratchpad (JSON values).

The audit log lives elsewhere (append-only JSONL with a hash chain);
this store is mutable working state and is intentionally separate.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import TracebackType
from typing import Any

import aiosqlite

from crew_os.core.exceptions import WorkingMemoryError
from crew_os.core.models import AgentRole, Task, TaskStatus, utcnow

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id          TEXT PRIMARY KEY,
    trace_id    TEXT NOT NULL,
    parent_id   TEXT,
    status      TEXT NOT NULL,
    assigned_to TEXT,
    updated_at  TEXT NOT NULL,
    data_json   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_trace  ON tasks(trace_id);

CREATE TABLE IF NOT EXISTS agent_memory (
    agent      TEXT NOT NULL,
    key        TEXT NOT NULL,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (agent, key)
);
"""


class SqliteStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._conn: aiosqlite.Connection | None = None

    async def __aenter__(self) -> SqliteStore:
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    async def connect(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL;")
        await self._conn.execute("PRAGMA foreign_keys=ON;")
        await self._conn.executescript(_SCHEMA)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    def _require_conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise WorkingMemoryError("store is not connected; call connect() first")
        return self._conn

    # ── tasks ───────────────────────────────────────────────────────

    async def save_task(self, task: Task) -> None:
        conn = self._require_conn()
        await conn.execute(
            """
            INSERT INTO tasks (id, trace_id, parent_id, status, assigned_to, updated_at, data_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                trace_id=excluded.trace_id,
                parent_id=excluded.parent_id,
                status=excluded.status,
                assigned_to=excluded.assigned_to,
                updated_at=excluded.updated_at,
                data_json=excluded.data_json
            """,
            (
                task.id,
                task.trace.trace_id,
                task.parent_id,
                task.status.value,
                task.assigned_to.value if task.assigned_to else None,
                task.updated_at.isoformat(),
                task.model_dump_json(),
            ),
        )
        await conn.commit()

    async def get_task(self, task_id: str) -> Task | None:
        conn = self._require_conn()
        async with conn.execute("SELECT data_json FROM tasks WHERE id = ?", (task_id,)) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return Task.model_validate_json(row["data_json"])

    async def list_tasks(
        self,
        *,
        status: TaskStatus | None = None,
        trace_id: str | None = None,
    ) -> list[Task]:
        conn = self._require_conn()
        clauses: list[str] = []
        params: list[Any] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status.value)
        if trace_id is not None:
            clauses.append("trace_id = ?")
            params.append(trace_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        # clauses contain only fixed, hardcoded predicates; values are bound via params.
        query = f"SELECT data_json FROM tasks {where} ORDER BY updated_at ASC"  # noqa: S608  # nosec B608
        async with conn.execute(query, params) as cur:
            rows = await cur.fetchall()
        return [Task.model_validate_json(r["data_json"]) for r in rows]

    async def update_task_status(
        self,
        task_id: str,
        status: TaskStatus,
        *,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> Task:
        task = await self.get_task(task_id)
        if task is None:
            raise WorkingMemoryError(f"task {task_id!r} not found")
        updated = task.model_copy(
            update={
                "status": status,
                "updated_at": utcnow(),
                "result": result if result is not None else task.result,
                "error": error if error is not None else task.error,
            }
        )
        await self.save_task(updated)
        return updated

    # ── agent scratch memory ─────────────────────────────────────────

    async def put_memory(self, agent: AgentRole, key: str, value: Any) -> None:
        conn = self._require_conn()
        await conn.execute(
            """
            INSERT INTO agent_memory (agent, key, value_json, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(agent, key) DO UPDATE SET
                value_json=excluded.value_json,
                updated_at=excluded.updated_at
            """,
            (agent.value, key, json.dumps(value), utcnow().isoformat()),
        )
        await conn.commit()

    async def get_memory(self, agent: AgentRole, key: str) -> Any | None:
        conn = self._require_conn()
        async with conn.execute(
            "SELECT value_json FROM agent_memory WHERE agent = ? AND key = ?",
            (agent.value, key),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return json.loads(row["value_json"])
