"""Async Ollama HTTP client.

Wraps the subset of the Ollama REST API the platform needs:

* ``health``        - ``GET /api/version``
* ``list_models``   - ``GET /api/tags``      (installed on disk)
* ``loaded_models`` - ``GET /api/ps``        (currently in VRAM)
* ``generate``      - ``POST /api/generate``
* ``chat``          - ``POST /api/chat``
* ``warm``          - load a model into memory (empty prompt)
* ``unload``        - evict a model now (``keep_alive: 0``)
* ``generate_stream`` - token-by-token streaming generate

All transport/HTTP errors are normalised to :class:`OllamaError`.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping, Sequence
from types import TracebackType
from typing import Any, Literal, cast

import httpx
from pydantic import BaseModel, ConfigDict, Field

from crew_os.core.exceptions import OllamaError

Role = Literal["system", "user", "assistant", "tool"]


class ChatMessage(BaseModel):
    model_config = ConfigDict(frozen=True)

    role: Role
    content: str


class GenerateResult(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True, protected_namespaces=())

    model: str
    response: str
    done: bool = True
    total_duration_ns: int | None = Field(default=None, alias="total_duration")
    prompt_eval_count: int | None = None
    eval_count: int | None = None


class ChatResult(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True, protected_namespaces=())

    model: str
    message: ChatMessage
    done: bool = True
    total_duration_ns: int | None = Field(default=None, alias="total_duration")
    eval_count: int | None = None


class LoadedModel(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True, protected_namespaces=())

    name: str
    size: int = 0
    size_vram: int = 0


class OllamaClient:
    """Thin async client. Owns its httpx client unless one is injected."""

    def __init__(
        self,
        host: str,
        *,
        timeout: float = 120.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._host = host.rstrip("/")
        self._timeout = timeout
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> OllamaClient:
        self._client_or_create()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    def _client_or_create(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self._host, timeout=self._timeout)
            self._owns_client = True
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    # ── low-level helpers ───────────────────────────────────────────

    async def _get_json(self, path: str) -> dict[str, Any]:
        client = self._client_or_create()
        try:
            resp = await client.get(path)
            resp.raise_for_status()
            return cast("dict[str, Any]", resp.json())
        except httpx.HTTPStatusError as exc:
            raise OllamaError(f"GET {path} returned HTTP {exc.response.status_code}") from exc
        except httpx.HTTPError as exc:
            raise OllamaError(f"GET {path} failed: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise OllamaError(f"GET {path} returned invalid JSON: {exc}") from exc

    async def _post_json(self, path: str, body: Mapping[str, Any]) -> dict[str, Any]:
        client = self._client_or_create()
        try:
            resp = await client.post(path, json=dict(body))
            resp.raise_for_status()
            return cast("dict[str, Any]", resp.json())
        except httpx.HTTPStatusError as exc:
            raise OllamaError(f"POST {path} returned HTTP {exc.response.status_code}") from exc
        except httpx.HTTPError as exc:
            raise OllamaError(f"POST {path} failed: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise OllamaError(f"POST {path} returned invalid JSON: {exc}") from exc

    # ── public API ──────────────────────────────────────────────────

    async def health(self) -> str:
        data = await self._get_json("/api/version")
        return str(data.get("version", "unknown"))

    async def list_models(self) -> list[str]:
        data = await self._get_json("/api/tags")
        return [m["name"] for m in data.get("models", []) if "name" in m]

    async def loaded_models(self) -> list[LoadedModel]:
        data = await self._get_json("/api/ps")
        return [LoadedModel(**m) for m in data.get("models", [])]

    async def generate(
        self,
        model: str,
        prompt: str,
        *,
        system: str | None = None,
        options: Mapping[str, Any] | None = None,
        keep_alive: str | int | None = None,
    ) -> GenerateResult:
        body: dict[str, Any] = {"model": model, "prompt": prompt, "stream": False}
        if system is not None:
            body["system"] = system
        if options:
            body["options"] = dict(options)
        if keep_alive is not None:
            body["keep_alive"] = keep_alive
        data = await self._post_json("/api/generate", body)
        return GenerateResult(**data)

    async def chat(
        self,
        model: str,
        messages: Sequence[ChatMessage],
        *,
        options: Mapping[str, Any] | None = None,
        keep_alive: str | int | None = None,
    ) -> ChatResult:
        body: dict[str, Any] = {
            "model": model,
            "messages": [m.model_dump() for m in messages],
            "stream": False,
        }
        if options:
            body["options"] = dict(options)
        if keep_alive is not None:
            body["keep_alive"] = keep_alive
        data = await self._post_json("/api/chat", body)
        return ChatResult(**data)

    async def warm(self, model: str) -> None:
        """Load ``model`` into memory (empty-prompt generate)."""

        await self._post_json("/api/generate", {"model": model})

    async def unload(self, model: str) -> None:
        """Evict ``model`` from memory immediately."""

        await self._post_json("/api/generate", {"model": model, "keep_alive": 0})

    async def generate_stream(
        self,
        model: str,
        prompt: str,
        *,
        system: str | None = None,
        options: Mapping[str, Any] | None = None,
    ) -> AsyncIterator[str]:
        body: dict[str, Any] = {"model": model, "prompt": prompt, "stream": True}
        if system is not None:
            body["system"] = system
        if options:
            body["options"] = dict(options)

        client = self._client_or_create()
        try:
            async with client.stream("POST", "/api/generate", json=body) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    stripped = line.strip()
                    if not stripped:
                        continue
                    chunk = json.loads(stripped)
                    piece = chunk.get("response", "")
                    if piece:
                        yield piece
                    if chunk.get("done"):
                        return
        except httpx.HTTPStatusError as exc:
            raise OllamaError(
                f"stream /api/generate returned HTTP {exc.response.status_code}"
            ) from exc
        except httpx.HTTPError as exc:
            raise OllamaError(f"stream /api/generate failed: {exc}") from exc
