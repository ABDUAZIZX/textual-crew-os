"""Coder agent - generates code and runs it inside a bubblewrap sandbox.

Tools:

* ``write_file`` / ``read_file`` - confined to ``workspace_root``; any path
  that escapes the root (via ``..`` or absolute paths) is rejected.
* ``run_tests`` - executes a command inside the sandbox with the workspace
  bound writable and the network unshared. Marked ``requires_subprocess``
  so the policy engine gates it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from crew_os.agents.base import BaseAgent
from crew_os.core.models import AgentRole, Task, TaskStatus, ToolManifest, utcnow
from crew_os.core.policy import PolicyEngine
from crew_os.llm.model_manager import ModelManager
from crew_os.security.audit import AuditLogger
from crew_os.security.rate_limit import RateLimiter
from crew_os.security.sandbox import run_sandboxed

DEFAULT_CODER_MODEL = "qwen2.5-coder:7b-instruct-q4_K_M"
CODER_SYSTEM = (
    "You are a senior Python engineer. Produce correct, idiomatic, "
    "production-quality code. Output only code unless asked otherwise."
)
DEFAULT_TEST_COMMAND = ("/usr/bin/python3", "-m", "pytest", "-q")


class CoderAgent(BaseAgent):
    def __init__(
        self,
        *,
        workspace_root: Path,
        policy: PolicyEngine,
        rate_limiter: RateLimiter,
        auditor: AuditLogger,
        model: str = DEFAULT_CODER_MODEL,
        model_manager: ModelManager | None = None,
        sandbox_timeout: float = 60.0,
    ) -> None:
        super().__init__(
            role=AgentRole.CODER,
            model=model,
            policy=policy,
            rate_limiter=rate_limiter,
            auditor=auditor,
            model_manager=model_manager,
        )
        self._workspace_root = workspace_root
        self._sandbox_timeout = sandbox_timeout
        self._workspace_root.mkdir(parents=True, exist_ok=True)

        self.register_tool(ToolManifest(name="write_file"), self._tool_write_file)
        self.register_tool(ToolManifest(name="read_file"), self._tool_read_file)
        self.register_tool(
            ToolManifest(name="run_tests", requires_subprocess=True),
            self._tool_run_tests,
        )

    def _resolve(self, rel: str) -> Path:
        root = self._workspace_root.resolve()
        candidate = (root / rel).resolve()
        if not candidate.is_relative_to(root):
            raise ValueError(f"path {rel!r} escapes the workspace root")
        return candidate

    async def _tool_write_file(self, *, path: str, content: str) -> dict[str, Any]:
        target = self._resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return {"path": str(target), "bytes": len(content.encode("utf-8"))}

    async def _tool_read_file(self, *, path: str) -> str:
        return self._resolve(path).read_text(encoding="utf-8")

    async def _tool_run_tests(
        self, *, path: str = ".", command: list[str] | None = None
    ) -> dict[str, Any]:
        cwd = self._resolve(path)
        argv = list(command) if command else list(DEFAULT_TEST_COMMAND)
        result = await asyncio.to_thread(
            run_sandboxed,
            argv,
            timeout=self._sandbox_timeout,
            writable=[self._workspace_root.resolve()],
            network=False,
            cwd=cwd,
        )
        return {
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "truncated": result.truncated,
        }

    async def handle_task(self, task: Task) -> Task:
        code = await self.think(
            f"Write Python code for the following task:\n{task.description}",
            system=CODER_SYSTEM,
        )
        write_result = await self.invoke_tool(
            "write_file",
            {"path": f"{task.id}/solution.py", "content": code},
            task.trace.child(),
        )
        return task.model_copy(
            update={
                "status": TaskStatus.COMPLETED,
                "result": {"code": code, "file": write_result["path"]},
                "updated_at": utcnow(),
            }
        )
