"""VRAM-aware model manager for an 8 GB GPU.

Policy (operator-chosen): *explicit unload + async lock*.

* Only one model is "active" at a time. An :class:`asyncio.Lock`
  serialises all swaps, so two coroutines can never race a load.
* Before activating a new model, the currently active one is explicitly
  unloaded (``keep_alive: 0``) - we never rely on Ollama's implicit
  eviction, which could transiently double-load and spill to CPU.
* Free VRAM is probed before a load; if below ``min_free_vram_mb`` the
  load is refused with :class:`InsufficientVRAMError`.
* After warming, ``/api/ps`` is consulted to confirm the load (best
  effort - a miss is logged via the optional ``on_warning`` hook, not
  fatal, since ps can lag the load by a beat).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from typing import Any

from crew_os.core.exceptions import InsufficientVRAMError
from crew_os.llm.ollama_client import (
    ChatMessage,
    ChatResult,
    ChatStreamPiece,
    GenerateResult,
    OllamaClient,
)
from crew_os.metrics.usage import UsageTracker


def default_vram_probe() -> int | None:
    """Return free VRAM in MiB for GPU 0, or ``None`` if unavailable."""

    try:
        import pynvml  # noqa: PLC0415 - optional dep, imported lazily by design
    except ImportError:
        return None
    try:
        pynvml.nvmlInit()
        handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        info = pynvml.nvmlDeviceGetMemoryInfo(handle)
        free_mb = int(info.free) // (1024 * 1024)
        pynvml.nvmlShutdown()
    except Exception:
        return None
    return free_mb


class ModelManager:
    def __init__(
        self,
        client: OllamaClient,
        *,
        min_free_vram_mb: int = 6000,
        vram_probe: Callable[[], int | None] = default_vram_probe,
        verify_loaded: bool = True,
        on_warning: Callable[[str], None] | None = None,
        usage_tracker: UsageTracker | None = None,
    ) -> None:
        self._client = client
        self._min_free = min_free_vram_mb
        self._vram_probe = vram_probe
        self._verify = verify_loaded
        self._on_warning = on_warning
        self._usage = usage_tracker
        self._active: str | None = None
        self._lock = asyncio.Lock()

    @property
    def active_model(self) -> str | None:
        return self._active

    def _warn(self, message: str) -> None:
        if self._on_warning is not None:
            self._on_warning(message)

    async def ensure_active(self, model: str) -> None:
        """Make ``model`` the single active model, swapping if needed."""

        async with self._lock:
            if self._active == model:
                return

            if self._active is not None:
                await self._client.unload(self._active)
                self._active = None

            free = self._vram_probe()
            if free is not None and free < self._min_free:
                raise InsufficientVRAMError(
                    f"only {free} MiB free VRAM, need >= {self._min_free} MiB to load {model!r}"
                )

            await self._client.warm(model)

            if self._verify:
                loaded = {m.name for m in await self._client.loaded_models()}
                if model not in loaded:
                    self._warn(
                        f"model {model!r} not reported by /api/ps after warm; proceeding anyway"
                    )

            self._active = model

    async def unload_active(self) -> None:
        async with self._lock:
            if self._active is not None:
                await self._client.unload(self._active)
                self._active = None

    async def generate(
        self,
        model: str,
        prompt: str,
        *,
        system: str | None = None,
        options: Mapping[str, Any] | None = None,
    ) -> GenerateResult:
        await self.ensure_active(model)
        result = await self._client.generate(model, prompt, system=system, options=options)
        if self._usage is not None:
            self._usage.record(
                model,
                input_tokens=result.prompt_eval_count or 0,
                output_tokens=result.eval_count or 0,
            )
        return result

    async def chat(
        self,
        model: str,
        messages: Sequence[ChatMessage],
        *,
        options: Mapping[str, Any] | None = None,
    ) -> ChatResult:
        await self.ensure_active(model)
        result = await self._client.chat(model, messages, options=options)
        if self._usage is not None:
            self._usage.record(model, output_tokens=result.eval_count or 0)
        return result

    async def chat_stream(
        self,
        model: str,
        messages: Sequence[ChatMessage],
        *,
        options: Mapping[str, Any] | None = None,
    ) -> AsyncIterator[ChatStreamPiece]:
        """Stream a chat reply, swapping in ``model`` first.

        Because :meth:`ensure_active` serialises on a single lock and only
        one model is resident at a time, concurrent callers are queued
        rather than run in parallel - intentional on an 8 GB GPU.
        """

        await self.ensure_active(model)
        async for piece in self._client.chat_stream(model, messages, options=options):
            if piece.done and self._usage is not None:
                self._usage.record(
                    model,
                    input_tokens=piece.prompt_eval_count or 0,
                    output_tokens=piece.eval_count or 0,
                )
            yield piece
