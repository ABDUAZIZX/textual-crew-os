"""Tests for ``crew_os.llm.model_manager`` - VRAM-aware explicit swap."""

from __future__ import annotations

import asyncio

import pytest

from crew_os.core.exceptions import InsufficientVRAMError
from crew_os.llm.model_manager import ModelManager, default_vram_probe
from crew_os.llm.ollama_client import (
    ChatMessage,
    ChatResult,
    ChatStreamPiece,
    GenerateResult,
    LoadedModel,
)
from crew_os.metrics.usage import UsageTracker


def test_default_vram_probe_returns_int_or_none() -> None:
    # On a GPU host this returns free MiB; on a probe-less host, None.
    # Either way the function must not raise.
    value = default_vram_probe()
    assert value is None or (isinstance(value, int) and value >= 0)


class FakeClient:
    """Records every call so tests can assert ordering."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._loaded: list[str] = []
        self.warm_barrier: asyncio.Event | None = None

    async def unload(self, model: str) -> None:
        self.calls.append(("unload", model))
        self._loaded = [m for m in self._loaded if m != model]

    async def warm(self, model: str) -> None:
        self.calls.append(("warm", model))
        if self.warm_barrier is not None:
            await self.warm_barrier.wait()
        self._loaded = [model]

    async def loaded_models(self) -> list[LoadedModel]:
        self.calls.append(("ps",))
        return [LoadedModel(name=m) for m in self._loaded]

    async def generate(self, model: str, prompt: str, **_: object) -> GenerateResult:
        self.calls.append(("generate", model, prompt))
        return GenerateResult(model=model, response="ok")

    async def chat(self, model: str, messages: object, **_: object) -> ChatResult:
        self.calls.append(("chat", model))
        return ChatResult(model=model, message=ChatMessage(role="assistant", content="ok"))

    async def chat_stream(self, model: str, messages: object, **_: object):
        self.calls.append(("chat_stream", model))
        yield ChatStreamPiece(content="o")
        yield ChatStreamPiece(content="k")
        yield ChatStreamPiece(content="", done=True, eval_count=5, prompt_eval_count=2)


def _mgr(client: FakeClient, **kw: object) -> ModelManager:
    kw.setdefault("vram_probe", lambda: 8000)
    return ModelManager(client, **kw)  # type: ignore[arg-type]


async def test_chat_stream_swaps_model_and_records_usage() -> None:
    c = FakeClient()
    usage = UsageTracker()
    m = _mgr(c, usage_tracker=usage)
    pieces = [
        p async for p in m.chat_stream("llama3.1:8b", [ChatMessage(role="user", content="hi")])
    ]
    assert m.active_model == "llama3.1:8b"
    assert ("warm", "llama3.1:8b") in c.calls
    assert "".join(p.content for p in pieces) == "ok"
    assert any(p.done for p in pieces)
    report = usage.report()
    # eval_count(5) + prompt_eval_count(2) recorded as output+input tokens.
    assert report.session.total_tokens == 7


async def test_first_activation_warms_without_unload() -> None:
    c = FakeClient()
    m = _mgr(c)
    await m.ensure_active("qwen2.5:3b")
    assert m.active_model == "qwen2.5:3b"
    assert ("warm", "qwen2.5:3b") in c.calls
    assert not any(call[0] == "unload" for call in c.calls)


async def test_same_model_twice_is_noop() -> None:
    c = FakeClient()
    m = _mgr(c)
    await m.ensure_active("qwen2.5:3b")
    warms_before = c.calls.count(("warm", "qwen2.5:3b"))
    await m.ensure_active("qwen2.5:3b")
    assert c.calls.count(("warm", "qwen2.5:3b")) == warms_before


async def test_swap_unloads_old_before_warming_new() -> None:
    c = FakeClient()
    m = _mgr(c)
    await m.ensure_active("model-a")
    await m.ensure_active("model-b")
    # Sequence must show unload(a) strictly before warm(b).
    unload_a = c.calls.index(("unload", "model-a"))
    warm_b = c.calls.index(("warm", "model-b"))
    assert unload_a < warm_b
    assert m.active_model == "model-b"


async def test_insufficient_vram_refuses_and_clears_active() -> None:
    c = FakeClient()
    m = _mgr(c, vram_probe=lambda: 1000, min_free_vram_mb=6000)
    with pytest.raises(InsufficientVRAMError, match="1000 MiB"):
        await m.ensure_active("model-a")
    assert m.active_model is None


async def test_vram_probe_none_proceeds() -> None:
    c = FakeClient()
    m = _mgr(c, vram_probe=lambda: None, min_free_vram_mb=6000)
    await m.ensure_active("model-a")
    assert m.active_model == "model-a"


async def test_verify_loaded_warns_on_missing() -> None:
    class SilentWarmClient(FakeClient):
        async def warm(self, model: str) -> None:
            self.calls.append(("warm", model))
            # Deliberately do NOT mark as loaded.

    c = SilentWarmClient()
    warnings: list[str] = []
    m = _mgr(c, verify_loaded=True, on_warning=warnings.append)
    await m.ensure_active("ghost")
    assert warnings
    assert "ghost" in warnings[0]
    assert m.active_model == "ghost"


async def test_generate_activates_then_delegates() -> None:
    c = FakeClient()
    m = _mgr(c)
    result = await m.generate("model-a", "hello")
    assert result.response == "ok"
    assert ("warm", "model-a") in c.calls
    assert ("generate", "model-a", "hello") in c.calls


async def test_chat_activates_then_delegates() -> None:
    c = FakeClient()
    m = _mgr(c)
    result = await m.chat("model-a", [ChatMessage(role="user", content="hi")])
    assert result.message.content == "ok"
    assert ("warm", "model-a") in c.calls
    assert ("chat", "model-a") in c.calls


async def test_generate_records_usage() -> None:
    class TokenClient(FakeClient):
        async def generate(self, model: str, prompt: str, **_: object) -> GenerateResult:
            self.calls.append(("generate", model, prompt))
            return GenerateResult(model=model, response="ok", prompt_eval_count=120, eval_count=80)

    tracker = UsageTracker()
    c = TokenClient()
    m = ModelManager(c, vram_probe=lambda: 8000, usage_tracker=tracker)  # type: ignore[arg-type]
    await m.generate("qwen2.5:3b", "hi")
    report = tracker.report()
    assert report.session.total_tokens == 200
    assert report.session.per_model[0].model == "qwen2.5:3b"


async def test_unload_active_clears_state() -> None:
    c = FakeClient()
    m = _mgr(c)
    await m.ensure_active("model-a")
    await m.unload_active()
    assert m.active_model is None
    assert ("unload", "model-a") in c.calls


async def test_concurrent_ensure_active_is_serialised() -> None:
    c = FakeClient()
    c.warm_barrier = asyncio.Event()
    m = _mgr(c)

    task_a = asyncio.create_task(m.ensure_active("model-a"))
    await asyncio.sleep(0.01)  # let task_a acquire the lock and block on warm
    task_b = asyncio.create_task(m.ensure_active("model-b"))
    await asyncio.sleep(0.01)

    # task_b must be waiting on the lock; only one warm has started.
    assert c.calls.count(("warm", "model-a")) == 1
    assert ("warm", "model-b") not in c.calls

    c.warm_barrier.set()
    await asyncio.gather(task_a, task_b)
    assert m.active_model == "model-b"
    # The swap to b unloaded a.
    assert ("unload", "model-a") in c.calls
