"""Async circuit breaker.

States:

* ``CLOSED``    - calls flow through. Consecutive failures are counted;
  reaching ``failure_threshold`` trips to ``OPEN``.
* ``OPEN``      - calls fail fast with :class:`CircuitOpenError` until
  ``recovery_timeout`` elapses, then the next call is allowed through as
  a trial (``HALF_OPEN``).
* ``HALF_OPEN`` - a single trial call is permitted. Success closes the
  circuit; failure re-opens it and restarts the timeout.

The clock is injectable (``time_fn``) so tests need no real sleeping.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import TypeVar

from crew_os.core.exceptions import CircuitOpenError

T = TypeVar("T")


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        name: str = "circuit",
        time_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        if recovery_timeout <= 0:
            raise ValueError("recovery_timeout must be > 0")
        self._threshold = failure_threshold
        self._recovery = recovery_timeout
        self._name = name
        self._now = time_fn
        self._state = CircuitState.CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._lock = asyncio.Lock()

    @property
    def name(self) -> str:
        return self._name

    @property
    def state(self) -> CircuitState:
        return self._state

    @property
    def failure_count(self) -> int:
        return self._failures

    async def _before_call(self) -> None:
        async with self._lock:
            if self._state == CircuitState.OPEN:
                if self._now() - self._opened_at >= self._recovery:
                    self._state = CircuitState.HALF_OPEN
                else:
                    raise CircuitOpenError(
                        f"circuit {self._name!r} is open; "
                        f"retry in "
                        f"{self._recovery - (self._now() - self._opened_at):.1f}s"
                    )

    async def _on_success(self) -> None:
        async with self._lock:
            self._failures = 0
            self._state = CircuitState.CLOSED

    async def _on_failure(self) -> None:
        async with self._lock:
            self._failures += 1
            if self._state == CircuitState.HALF_OPEN or self._failures >= self._threshold:
                self._state = CircuitState.OPEN
                self._opened_at = self._now()

    async def call(self, fn: Callable[[], Awaitable[T]]) -> T:
        """Run ``fn`` through the breaker.

        Raises:
            CircuitOpenError: if the circuit is open and not yet recovered.
            Exception: re-raises whatever ``fn`` raised, after recording it.
        """

        await self._before_call()
        try:
            result = await fn()
        except Exception:
            await self._on_failure()
            raise
        await self._on_success()
        return result

    async def reset(self) -> None:
        async with self._lock:
            self._state = CircuitState.CLOSED
            self._failures = 0
            self._opened_at = 0.0
