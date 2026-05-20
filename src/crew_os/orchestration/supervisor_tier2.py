"""Tier-2 supervisor: Claude Opus 4.7 with offline-first local fallback.

Resolution order:

1. If a Claude client is configured, plan with Claude. On *any* Claude
   failure (network, auth, rate limit), and if a local model manager is
   available, degrade gracefully to the local fallback model.
2. If no Claude client but a model manager exists, plan locally.
3. Otherwise raise :class:`SupervisorError`.

The Claude system prompt is sent with ``cache_control`` so repeated
escalations hit the prompt cache.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, cast

from crew_os.core.exceptions import SupervisorError
from crew_os.core.models import Task
from crew_os.llm.model_manager import ModelManager
from crew_os.metrics.usage import UsageTracker

if TYPE_CHECKING:
    from crew_os.config import Settings

SYSTEM_PROMPT = (
    "You are the senior supervisor of a local AI crew. You receive complex "
    "tasks that the fast local router escalated to you. Produce a concise, "
    "concrete execution plan: ordered steps, the agent role each step "
    "belongs to (coder, sec_defensive, sec_offensive, auditor), and explicit "
    "success criteria. Do not perform the work; plan it."
)

DEFAULT_LOCAL_MODEL = "qwen3:14b"
DEFAULT_MAX_TOKENS = 2048


class SupervisorMode(StrEnum):
    CLAUDE = "claude"
    LOCAL = "local"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class PlanResult:
    text: str
    source: SupervisorMode


class SupervisorT2:
    def __init__(
        self,
        *,
        claude: Any = None,
        model_manager: ModelManager | None = None,
        anthropic_model: str = "claude-opus-4-7",
        local_model: str = DEFAULT_LOCAL_MODEL,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        on_warning: Callable[[str], None] | None = None,
        usage_tracker: UsageTracker | None = None,
    ) -> None:
        self._claude = claude
        self._model_manager = model_manager
        self._anthropic_model = anthropic_model
        self._local_model = local_model
        self._max_tokens = max_tokens
        self._on_warning = on_warning
        self._usage = usage_tracker

    @property
    def mode(self) -> SupervisorMode:
        if self._claude is not None:
            return SupervisorMode.CLAUDE
        if self._model_manager is not None:
            return SupervisorMode.LOCAL
        return SupervisorMode.UNAVAILABLE

    def _warn(self, message: str) -> None:
        if self._on_warning is not None:
            self._on_warning(message)

    def _build_prompt(self, task: Task) -> str:
        return (
            f"Task id: {task.id}\nDescription:\n{task.description}\n\nProduce the execution plan."
        )

    async def _call_claude(self, prompt: str) -> str:
        resp = await self._claude.messages.create(
            model=self._anthropic_model,
            max_tokens=self._max_tokens,
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": prompt}],
        )
        usage = getattr(resp, "usage", None)
        if self._usage is not None and usage is not None:
            self._usage.record(
                self._anthropic_model,
                input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
                output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
            )
        return cast("str", resp.content[0].text)

    async def _call_local(self, prompt: str) -> str:
        if self._model_manager is None:  # pragma: no cover - guarded by callers
            raise SupervisorError("local fallback requested without a model manager")
        result = await self._model_manager.generate(self._local_model, prompt, system=SYSTEM_PROMPT)
        return result.response

    async def plan(self, task: Task) -> PlanResult:
        prompt = self._build_prompt(task)

        if self._claude is not None:
            try:
                text = await self._call_claude(prompt)
                return PlanResult(text=text, source=SupervisorMode.CLAUDE)
            except Exception as exc:  # any Claude failure degrades gracefully to local
                if self._model_manager is None:
                    raise SupervisorError(
                        f"Claude failed and no local fallback is configured: {exc}"
                    ) from exc
                self._warn(
                    f"Claude planning failed ({exc}); falling back to local {self._local_model}"
                )
                text = await self._call_local(prompt)
                return PlanResult(text=text, source=SupervisorMode.LOCAL)

        if self._model_manager is not None:
            text = await self._call_local(prompt)
            return PlanResult(text=text, source=SupervisorMode.LOCAL)

        raise SupervisorError(
            "no supervisor backend available: set ANTHROPIC_API_KEY or provide "
            "a local model manager"
        )


def build_supervisor_t2(
    settings: Settings,
    *,
    model_manager: ModelManager | None = None,
    on_warning: Callable[[str], None] | None = None,
    usage_tracker: UsageTracker | None = None,
) -> SupervisorT2:
    """Construct a :class:`SupervisorT2` from settings.

    The ``anthropic`` SDK is imported lazily only when an API key is set,
    so a fully offline install never needs the package at import time.
    """

    claude: Any = None
    if settings.anthropic_api_key is not None:
        from anthropic import AsyncAnthropic  # noqa: PLC0415 - optional, only with a key

        claude = AsyncAnthropic(api_key=settings.anthropic_api_key.get_secret_value())
    return SupervisorT2(
        claude=claude,
        model_manager=model_manager,
        anthropic_model=settings.anthropic_model,
        on_warning=on_warning,
        usage_tracker=usage_tracker,
    )
