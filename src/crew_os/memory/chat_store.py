"""Async SQLite store for Team Chat history.

Separate from :class:`~crew_os.memory.sqlite_store.SqliteStore` (task
working memory): chat is a distinct, user-facing concern with its own
file (``chat_history.db``). Sessions group messages; each message records
who said it (``agent_id`` = a role value or ``"user"``), its role
(``user``/``assistant``), content, timestamp, and tokens consumed.
"""

from __future__ import annotations

from pathlib import Path
from types import TracebackType

import aiosqlite
from pydantic import BaseModel, ConfigDict

from crew_os.core.exceptions import WorkingMemoryError
from crew_os.core.models import utcnow

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    title       TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    agent_id    TEXT,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    timestamp   TEXT NOT NULL,
    tokens_used INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
"""


class StoredMessage(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: int
    session_id: str
    agent_id: str | None
    role: str
    content: str
    timestamp: str
    tokens_used: int


class SessionSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    title: str | None
    created_at: str
    updated_at: str
    message_count: int


class ChatStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._conn: aiosqlite.Connection | None = None

    async def __aenter__(self) -> ChatStore:
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
            raise WorkingMemoryError("chat store is not connected; call connect() first")
        return self._conn

    async def ensure_session(self, session_id: str, *, title: str | None = None) -> None:
        """Create the session row if absent; bump ``updated_at`` otherwise."""

        conn = self._require_conn()
        now = utcnow().isoformat()
        await conn.execute(
            """
            INSERT INTO sessions (id, title, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                updated_at=excluded.updated_at,
                title=COALESCE(sessions.title, excluded.title)
            """,
            (session_id, title, now, now),
        )
        await conn.commit()

    async def add_message(
        self,
        *,
        session_id: str,
        agent_id: str | None,
        role: str,
        content: str,
        tokens_used: int = 0,
    ) -> StoredMessage:
        conn = self._require_conn()
        ts = utcnow().isoformat()
        cur = await conn.execute(
            """
            INSERT INTO messages (session_id, agent_id, role, content, timestamp, tokens_used)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (session_id, agent_id, role, content, ts, tokens_used),
        )
        await conn.execute(
            "UPDATE sessions SET updated_at = ? WHERE id = ?",
            (ts, session_id),
        )
        await conn.commit()
        return StoredMessage(
            id=int(cur.lastrowid or 0),
            session_id=session_id,
            agent_id=agent_id,
            role=role,
            content=content,
            timestamp=ts,
            tokens_used=tokens_used,
        )

    async def get_messages(self, session_id: str) -> list[StoredMessage]:
        conn = self._require_conn()
        async with conn.execute(
            """
            SELECT id, session_id, agent_id, role, content, timestamp, tokens_used
            FROM messages WHERE session_id = ? ORDER BY id ASC
            """,
            (session_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [StoredMessage(**dict(r)) for r in rows]

    async def list_sessions(self, *, limit: int = 50) -> list[SessionSummary]:
        conn = self._require_conn()
        async with conn.execute(
            """
            SELECT s.id, s.title, s.created_at, s.updated_at,
                   COUNT(m.id) AS message_count
            FROM sessions s
            LEFT JOIN messages m ON m.session_id = s.id
            GROUP BY s.id
            ORDER BY s.updated_at DESC
            LIMIT ?
            """,
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
        return [SessionSummary(**dict(r)) for r in rows]
