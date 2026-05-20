"""Tests for ``crew_os.llm.ollama_client`` using respx to mock httpx."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from crew_os.core.exceptions import OllamaError
from crew_os.llm.ollama_client import ChatMessage, OllamaClient

HOST = "http://test-ollama:11434"


@pytest.fixture
async def client() -> OllamaClient:
    c = OllamaClient(HOST, timeout=5.0)
    yield c
    await c.aclose()


@respx.mock
async def test_health(client: OllamaClient) -> None:
    respx.get(f"{HOST}/api/version").mock(
        return_value=httpx.Response(200, json={"version": "0.21.2"})
    )
    assert await client.health() == "0.21.2"


@respx.mock
async def test_list_models(client: OllamaClient) -> None:
    respx.get(f"{HOST}/api/tags").mock(
        return_value=httpx.Response(
            200, json={"models": [{"name": "qwen2.5:3b"}, {"name": "llama3.1:8b"}]}
        )
    )
    assert await client.list_models() == ["qwen2.5:3b", "llama3.1:8b"]


@respx.mock
async def test_loaded_models(client: OllamaClient) -> None:
    respx.get(f"{HOST}/api/ps").mock(
        return_value=httpx.Response(
            200,
            json={"models": [{"name": "qwen2.5:3b", "size": 100, "size_vram": 90}]},
        )
    )
    loaded = await client.loaded_models()
    assert len(loaded) == 1
    assert loaded[0].name == "qwen2.5:3b"
    assert loaded[0].size_vram == 90


@respx.mock
async def test_generate(client: OllamaClient) -> None:
    route = respx.post(f"{HOST}/api/generate").mock(
        return_value=httpx.Response(
            200,
            json={"model": "qwen2.5:3b", "response": "hi there", "done": True},
        )
    )
    result = await client.generate("qwen2.5:3b", "say hi", system="be brief")
    assert result.response == "hi there"
    sent = json.loads(route.calls.last.request.content)
    assert sent["stream"] is False
    assert sent["system"] == "be brief"


@respx.mock
async def test_chat(client: OllamaClient) -> None:
    respx.post(f"{HOST}/api/chat").mock(
        return_value=httpx.Response(
            200,
            json={
                "model": "qwen2.5:3b",
                "message": {"role": "assistant", "content": "pong"},
                "done": True,
            },
        )
    )
    result = await client.chat("qwen2.5:3b", [ChatMessage(role="user", content="ping")])
    assert result.message.content == "pong"
    assert result.message.role == "assistant"


@respx.mock
async def test_warm_posts_model_only(client: OllamaClient) -> None:
    route = respx.post(f"{HOST}/api/generate").mock(
        return_value=httpx.Response(200, json={"model": "m", "response": "", "done": True})
    )
    await client.warm("qwen2.5:3b")
    sent = json.loads(route.calls.last.request.content)
    assert sent == {"model": "qwen2.5:3b"}


@respx.mock
async def test_unload_sets_keep_alive_zero(client: OllamaClient) -> None:
    route = respx.post(f"{HOST}/api/generate").mock(
        return_value=httpx.Response(200, json={"model": "m", "response": "", "done": True})
    )
    await client.unload("qwen2.5:3b")
    sent = json.loads(route.calls.last.request.content)
    assert sent["keep_alive"] == 0
    assert sent["model"] == "qwen2.5:3b"


@respx.mock
async def test_generate_stream_yields_pieces(client: OllamaClient) -> None:
    lines = [
        json.dumps({"response": "Hel", "done": False}),
        json.dumps({"response": "lo", "done": False}),
        json.dumps({"response": "", "done": True}),
    ]
    respx.post(f"{HOST}/api/generate").mock(
        return_value=httpx.Response(200, text="\n".join(lines) + "\n")
    )
    pieces = [p async for p in client.generate_stream("m", "hi")]
    assert pieces == ["Hel", "lo"]


@respx.mock
async def test_generate_with_options_and_keep_alive(client: OllamaClient) -> None:
    route = respx.post(f"{HOST}/api/generate").mock(
        return_value=httpx.Response(200, json={"model": "m", "response": "x", "done": True})
    )
    await client.generate("m", "p", options={"temperature": 0.1}, keep_alive="5m")
    sent = json.loads(route.calls.last.request.content)
    assert sent["options"] == {"temperature": 0.1}
    assert sent["keep_alive"] == "5m"


@respx.mock
async def test_chat_with_options_and_keep_alive(client: OllamaClient) -> None:
    route = respx.post(f"{HOST}/api/chat").mock(
        return_value=httpx.Response(
            200,
            json={
                "model": "m",
                "message": {"role": "assistant", "content": "ok"},
                "done": True,
            },
        )
    )
    await client.chat(
        "m",
        [ChatMessage(role="user", content="hi")],
        options={"top_p": 0.9},
        keep_alive=0,
    )
    sent = json.loads(route.calls.last.request.content)
    assert sent["options"] == {"top_p": 0.9}
    assert sent["keep_alive"] == 0


@respx.mock
async def test_generate_stream_http_error_becomes_ollama_error(
    client: OllamaClient,
) -> None:
    respx.post(f"{HOST}/api/generate").mock(return_value=httpx.Response(500))
    with pytest.raises(OllamaError, match="HTTP 500"):
        async for _ in client.generate_stream("m", "p"):
            pass


@respx.mock
async def test_http_500_becomes_ollama_error(client: OllamaClient) -> None:
    respx.get(f"{HOST}/api/version").mock(return_value=httpx.Response(500))
    with pytest.raises(OllamaError, match="HTTP 500"):
        await client.health()


@respx.mock
async def test_connection_error_becomes_ollama_error(client: OllamaClient) -> None:
    respx.get(f"{HOST}/api/version").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(OllamaError, match="failed"):
        await client.health()


async def test_context_manager_closes() -> None:
    async with OllamaClient(HOST) as c:
        assert c is not None
