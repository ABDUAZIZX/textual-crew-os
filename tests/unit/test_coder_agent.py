"""Tests for ``crew_os.agents.coder`` - sandboxed file + execution tools."""

from __future__ import annotations

from pathlib import Path

import pytest

from crew_os.agents.coder import CoderAgent
from crew_os.core.models import Task, TaskStatus, TraceContext
from crew_os.core.policy import PolicyEngine
from crew_os.llm.ollama_client import GenerateResult
from crew_os.security.audit import AuditLogger
from crew_os.security.rate_limit import RateLimiter
from crew_os.security.sandbox import is_available

bwrap_required = pytest.mark.skipif(not is_available(), reason="bwrap is not installed")


class FakeManager:
    def __init__(self, code: str = "print('hello')") -> None:
        self._code = code

    async def generate(
        self, model: str, prompt: str, *, system: str | None = None
    ) -> GenerateResult:
        return GenerateResult(model=model, response=self._code)


def _coder(tmp_path: Path, *, manager: FakeManager | None = None) -> CoderAgent:
    return CoderAgent(
        workspace_root=tmp_path / "ws",
        policy=PolicyEngine(),
        rate_limiter=RateLimiter(),
        auditor=AuditLogger(tmp_path / "audit.jsonl"),
        model_manager=manager,  # type: ignore[arg-type]
        sandbox_timeout=20.0,
    )


def _trace() -> TraceContext:
    return TraceContext.new_root()


async def test_write_then_read_roundtrip(tmp_path: Path) -> None:
    coder = _coder(tmp_path)
    await coder.invoke_tool("write_file", {"path": "a/b.py", "content": "x = 1\n"}, _trace())
    content = await coder.invoke_tool("read_file", {"path": "a/b.py"}, _trace())
    assert content == "x = 1\n"


async def test_write_reports_bytes(tmp_path: Path) -> None:
    coder = _coder(tmp_path)
    result = await coder.invoke_tool("write_file", {"path": "f.txt", "content": "héllo"}, _trace())
    assert result["bytes"] == len("héllo".encode())
    assert Path(result["path"]).exists()


@pytest.mark.parametrize("evil", ["../escape.py", "../../etc/passwd", "/etc/passwd"])
async def test_path_traversal_rejected(tmp_path: Path, evil: str) -> None:
    coder = _coder(tmp_path)
    with pytest.raises(ValueError, match="escapes"):
        await coder.invoke_tool("write_file", {"path": evil, "content": "x"}, _trace())


async def test_capabilities(tmp_path: Path) -> None:
    coder = _coder(tmp_path)
    assert coder.capabilities == frozenset({"write_file", "read_file", "run_tests"})


async def test_handle_task_generates_and_writes(tmp_path: Path) -> None:
    coder = _coder(tmp_path, manager=FakeManager(code="def f():\n    return 7\n"))
    task = Task(description="write f returning 7", trace=_trace())
    done = await coder.handle_task(task)
    assert done.status == TaskStatus.COMPLETED
    assert done.result is not None
    assert "return 7" in done.result["code"]
    assert Path(done.result["file"]).read_text() == "def f():\n    return 7\n"


@bwrap_required
async def test_run_tests_executes_in_sandbox(tmp_path: Path) -> None:
    coder = _coder(tmp_path)
    result = await coder.invoke_tool(
        "run_tests",
        {"path": ".", "command": ["/usr/bin/python3", "-c", "print('sandbox-ok')"]},
        _trace(),
    )
    assert result["returncode"] == 0
    assert "sandbox-ok" in result["stdout"]


@bwrap_required
async def test_run_tests_sandbox_has_no_network(tmp_path: Path) -> None:
    coder = _coder(tmp_path)
    result = await coder.invoke_tool(
        "run_tests",
        {
            "path": ".",
            "command": ["/usr/bin/getent", "hosts", "example.com"],
        },
        _trace(),
    )
    assert result["returncode"] != 0
