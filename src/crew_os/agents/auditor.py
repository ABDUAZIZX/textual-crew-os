"""Auditor agent - wraps ``garak`` (LLM vulnerability scanner) safely.

Per the Stage-0 decision, garak runs from an *isolated virtualenv* via a
validated subprocess, so its heavy transitive dependencies never enter the
main environment. The subprocess is not bubblewrap-sandboxed because garak
must reach the local Ollama (loopback) and may fetch probe data.

Inputs are validated before they ever touch the argv: model names and
probe identifiers must match a strict character allowlist, and the call
uses :func:`run_safe` (argv list, no shell).
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from crew_os.agents.base import BaseAgent
from crew_os.core.exceptions import SubprocessError
from crew_os.core.models import AgentRole, Task, TaskStatus, ToolManifest, utcnow
from crew_os.core.policy import PolicyEngine
from crew_os.security.audit import AuditLogger
from crew_os.security.rate_limit import RateLimiter
from crew_os.security.subprocess_safe import run_safe

DEFAULT_AUDITOR_MODEL = "llama3.1:8b-instruct-q4_K_M"
_PROBE_RE = re.compile(r"^[A-Za-z0-9_.]+$")
_MODEL_RE = re.compile(r"^[A-Za-z0-9_.:\-/]+$")


def build_garak_argv(
    garak_python: Path,
    *,
    model_name: str,
    probes: Sequence[str],
    report_prefix: Path,
    model_type: str = "ollama",
) -> list[str]:
    """Build a validated garak argv. Pure function.

    Raises:
        ValueError: if the model name or any probe fails validation.
    """

    if not _MODEL_RE.match(model_name):
        raise ValueError(f"invalid model name: {model_name!r}")
    if not probes:
        raise ValueError("at least one probe is required")
    for probe in probes:
        if not _PROBE_RE.match(probe):
            raise ValueError(f"invalid probe identifier: {probe!r}")
    return [
        str(garak_python),
        "-m",
        "garak",
        "--model_type",
        model_type,
        "--model_name",
        model_name,
        "--probes",
        ",".join(probes),
        "--report_prefix",
        str(report_prefix),
    ]


class AuditorAgent(BaseAgent):
    def __init__(
        self,
        *,
        policy: PolicyEngine,
        rate_limiter: RateLimiter,
        auditor: AuditLogger,
        garak_python: Path,
        report_dir: Path,
        model: str = DEFAULT_AUDITOR_MODEL,
        garak_timeout: float = 1800.0,
    ) -> None:
        super().__init__(
            role=AgentRole.AUDITOR,
            model=model,
            policy=policy,
            rate_limiter=rate_limiter,
            auditor=auditor,
        )
        self._garak_python = garak_python
        self._report_dir = report_dir
        self._garak_timeout = garak_timeout
        self._report_dir.mkdir(parents=True, exist_ok=True)
        self.register_tool(
            ToolManifest(name="run_garak", requires_subprocess=True),
            self._tool_run_garak,
        )

    async def _tool_run_garak(
        self,
        *,
        model_name: str,
        probes: list[str],
        report_name: str = "garak_report",
    ) -> dict[str, Any]:
        if not _PROBE_RE.match(report_name):
            raise ValueError(f"invalid report name: {report_name!r}")
        report_prefix = self._report_dir / report_name
        argv = build_garak_argv(
            self._garak_python,
            model_name=model_name,
            probes=probes,
            report_prefix=report_prefix,
        )
        try:
            result = await asyncio.to_thread(run_safe, argv, timeout=self._garak_timeout)
        except SubprocessError as exc:
            return {"ok": False, "error": str(exc)}
        return {
            "ok": result.returncode == 0,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "report_prefix": str(report_prefix),
        }

    async def handle_task(self, task: Task) -> Task:
        return task.model_copy(
            update={
                "status": TaskStatus.COMPLETED,
                "result": {
                    "note": "auditor ready; invoke run_garak with model + probes",
                    "task": task.description,
                },
                "updated_at": utcnow(),
            }
        )
