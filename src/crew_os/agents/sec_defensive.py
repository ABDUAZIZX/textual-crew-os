"""Defensive security agent.

Reads code/config and asks its model for a defensive review. No network,
no subprocess, no offensive capability - the policy engine would block
any of those for this role anyway, but the agent simply never exposes
such tools.
"""

from __future__ import annotations

from pathlib import Path

from crew_os.agents.base import BaseAgent
from crew_os.core.models import AgentRole, Task, TaskStatus, ToolManifest, utcnow
from crew_os.core.policy import PolicyEngine
from crew_os.llm.model_manager import ModelManager
from crew_os.security.audit import AuditLogger
from crew_os.security.rate_limit import RateLimiter

DEFAULT_DEFENSIVE_MODEL = "crew-defender"
DEFENSIVE_SYSTEM = (
    "You are a defensive security analyst. Identify vulnerabilities, "
    "misconfigurations, and hardening opportunities. Be concrete: name the "
    "issue, its impact, and a mitigation. Never produce offensive tooling."
)


class SecDefensiveAgent(BaseAgent):
    def __init__(
        self,
        *,
        policy: PolicyEngine,
        rate_limiter: RateLimiter,
        auditor: AuditLogger,
        read_root: Path,
        model: str = DEFAULT_DEFENSIVE_MODEL,
        model_manager: ModelManager | None = None,
    ) -> None:
        super().__init__(
            role=AgentRole.SEC_DEFENSIVE,
            model=model,
            policy=policy,
            rate_limiter=rate_limiter,
            auditor=auditor,
            model_manager=model_manager,
        )
        self._read_root = read_root
        self.register_tool(ToolManifest(name="read_file"), self._tool_read_file)

    def _resolve(self, rel: str) -> Path:
        root = self._read_root.resolve()
        candidate = (root / rel).resolve()
        if not candidate.is_relative_to(root):
            raise ValueError(f"path {rel!r} escapes the readable root")
        return candidate

    async def _tool_read_file(self, *, path: str) -> str:
        return self._resolve(path).read_text(encoding="utf-8")

    async def analyze(self, content: str, *, context: str = "") -> str:
        prompt = f"Review the following for security issues.\nContext: {context}\n\n{content}"
        return await self.think(prompt, system=DEFENSIVE_SYSTEM)

    async def handle_task(self, task: Task) -> Task:
        review = await self.think(
            f"Perform a defensive security review for:\n{task.description}",
            system=DEFENSIVE_SYSTEM,
        )
        return task.model_copy(
            update={
                "status": TaskStatus.COMPLETED,
                "result": {"review": review},
                "updated_at": utcnow(),
            }
        )
