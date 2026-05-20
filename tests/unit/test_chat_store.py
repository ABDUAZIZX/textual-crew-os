"""Tests for ``crew_os.memory.chat_store.ChatStore``."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from crew_os.core.exceptions import WorkingMemoryError
from crew_os.memory.chat_store import ChatStore


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[ChatStore]:
    s = ChatStore(tmp_path / "chat_history.db")
    await s.connect()
    yield s
    await s.close()


async def test_add_and_get_messages_roundtrip(store: ChatStore) -> None:
    await store.ensure_session("s1", title="hello world")
    await store.add_message(session_id="s1", agent_id="user", role="user", content="hi")
    await store.add_message(
        session_id="s1", agent_id="coder", role="assistant", content="hey", tokens_used=4
    )
    msgs = await store.get_messages("s1")
    assert [m.content for m in msgs] == ["hi", "hey"]
    assert msgs[0].role == "user"
    assert msgs[1].agent_id == "coder"
    assert msgs[1].tokens_used == 4


async def test_list_sessions_orders_recent_first_and_counts(store: ChatStore) -> None:
    await store.ensure_session("a")
    await store.add_message(session_id="a", agent_id="user", role="user", content="x")
    await store.ensure_session("b")  # touched after a -> should sort first
    sessions = await store.list_sessions()
    assert [s.id for s in sessions] == ["b", "a"]
    by_id = {s.id: s for s in sessions}
    assert by_id["a"].message_count == 1
    assert by_id["b"].message_count == 0


async def test_ensure_session_keeps_first_title(store: ChatStore) -> None:
    await store.ensure_session("s", title="first")
    await store.ensure_session("s", title="second")
    sessions = await store.list_sessions()
    assert sessions[0].title == "first"


async def test_operations_require_connect(tmp_path: Path) -> None:
    s = ChatStore(tmp_path / "x.db")
    with pytest.raises(WorkingMemoryError):
        await s.add_message(session_id="s", agent_id="user", role="user", content="x")
