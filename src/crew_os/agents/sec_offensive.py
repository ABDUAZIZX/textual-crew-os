"""Offensive security agent - LAB_MODE only, multi-layer gated.

Protection layers, all enforced here on top of config-level gates 1 and 2
(env var + consent token) from :mod:`crew_os.config`:

1. The agent refuses to start a task unless ``lab_mode_active`` is True.
2. The first offensive task each session triggers the interactive
   confirmation gate (``lab_gate.request_lab_confirmation``). Denial aborts.
3. Its tools are declared ``requires_lab_mode`` + ``dangerous``, so the
   policy engine independently blocks them whenever LAB_MODE is off.

The confirmation function is injectable for testing; in production it reads
the operator's passphrase from a TTY.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from crew_os.agents.base import BaseAgent
from crew_os.core.exceptions import LabModeError
from crew_os.core.models import AgentRole, Task, TaskStatus, ToolManifest, utcnow
from crew_os.core.policy import PolicyEngine
from crew_os.llm.model_manager import ModelManager
from crew_os.security.audit import AuditLogger
from crew_os.security.lab_gate import request_lab_confirmation
from crew_os.security.rate_limit import RateLimiter

DEFAULT_OFFENSIVE_MODEL = (
    "hf.co/mradermacher/WhiteRabbitNeo-2.5-Qwen-2.5-Coder-7B-OBLITERATED-i1-GGUF:Q4_K_M"
)
OFFENSIVE_SYSTEM = (
    "You are an authorized offensive security researcher operating in an "
    "isolated lab. Only assist with testing against systems the operator owns. "
    "Document scope and intent."
)

ConfirmFn = Callable[..., bool]


class SecOffensiveAgent(BaseAgent):
    def __init__(
        self,
        *,
        policy: PolicyEngine,
        rate_limiter: RateLimiter,
        auditor: AuditLogger,
        model: str = DEFAULT_OFFENSIVE_MODEL,
        model_manager: ModelManager | None = None,
        lab_mode_active: bool = False,
        confirm_fn: ConfirmFn | None = None,
    ) -> None:
        super().__init__(
            role=AgentRole.SEC_OFFENSIVE,
            model=model,
            policy=policy,
            rate_limiter=rate_limiter,
            auditor=auditor,
            model_manager=model_manager,
            lab_mode_active=lab_mode_active,
        )
        self._confirm_fn: ConfirmFn = confirm_fn or request_lab_confirmation
        self._confirmed = False
        self.register_tool(
            ToolManifest(name="offensive_probe", requires_lab_mode=True, dangerous=True),
            self._tool_offensive_probe,
        )

    @property
    def confirmed(self) -> bool:
        return self._confirmed

    async def _ensure_confirmed(self, task: Task) -> None:
        if self._confirmed:
            return
        granted = await asyncio.to_thread(
            self._confirm_fn,
            scope="offensive task",
            target=task.description[:80],
            auditor=self._auditor,
            trace=task.trace,
        )
        if not granted:
            raise LabModeError("operator denied LAB_MODE confirmation")
        self._confirmed = True

    async def _tool_offensive_probe(self, *, target: str) -> str:
        return await self.think(
            f"Plan an authorized offensive probe against the lab target: {target}",
            system=OFFENSIVE_SYSTEM,
        )

    async def handle_task(self, task: Task) -> Task:
        if not self._lab_mode_active:
            raise LabModeError("offensive agent invoked without LAB_MODE active")
        await self._ensure_confirmed(task)
        plan = await self.think(
            f"Authorized offensive task in lab:\n{task.description}",
            system=OFFENSIVE_SYSTEM,
        )
        return task.model_copy(
            update={
                "status": TaskStatus.COMPLETED,
                "result": {"plan": plan},
                "updated_at": utcnow(),
            }
        )
