"""Tests for ``crew_os.llm.circuit_breaker`` with an injected clock."""

from __future__ import annotations

import pytest

from crew_os.core.exceptions import CircuitOpenError
from crew_os.llm.circuit_breaker import CircuitBreaker, CircuitState


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


async def _ok() -> str:
    return "ok"


async def _boom() -> str:
    raise RuntimeError("boom")


@pytest.mark.parametrize(("threshold", "recovery"), [(0, 1.0), (1, 0.0), (1, -1.0)])
def test_rejects_bad_construction(threshold: int, recovery: float) -> None:
    with pytest.raises(ValueError, match="must be"):
        CircuitBreaker(failure_threshold=threshold, recovery_timeout=recovery)


async def test_starts_closed() -> None:
    cb = CircuitBreaker()
    assert cb.state == CircuitState.CLOSED


async def test_success_passes_through() -> None:
    cb = CircuitBreaker()
    assert await cb.call(_ok) == "ok"
    assert cb.state == CircuitState.CLOSED


async def test_opens_after_threshold_failures() -> None:
    cb = CircuitBreaker(failure_threshold=3)
    for _ in range(3):
        with pytest.raises(RuntimeError):
            await cb.call(_boom)
    assert cb.state == CircuitState.OPEN


async def test_open_short_circuits_without_calling() -> None:
    cb = CircuitBreaker(failure_threshold=1)
    with pytest.raises(RuntimeError):
        await cb.call(_boom)
    assert cb.state == CircuitState.OPEN

    called = False

    async def tracked() -> str:
        nonlocal called
        called = True
        return "x"

    with pytest.raises(CircuitOpenError):
        await cb.call(tracked)
    assert called is False


async def test_half_open_after_recovery_then_close_on_success() -> None:
    clock = _Clock()
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout=10.0, time_fn=clock)
    with pytest.raises(RuntimeError):
        await cb.call(_boom)
    assert cb.state == CircuitState.OPEN

    clock.advance(11.0)
    assert await cb.call(_ok) == "ok"
    assert cb.state == CircuitState.CLOSED


async def test_half_open_failure_reopens() -> None:
    clock = _Clock()
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout=10.0, time_fn=clock)
    with pytest.raises(RuntimeError):
        await cb.call(_boom)
    clock.advance(11.0)
    # Trial call fails → straight back to OPEN.
    with pytest.raises(RuntimeError):
        await cb.call(_boom)
    assert cb.state == CircuitState.OPEN
    # And it short-circuits again immediately (timeout not elapsed).
    with pytest.raises(CircuitOpenError):
        await cb.call(_ok)


async def test_success_resets_failure_count() -> None:
    cb = CircuitBreaker(failure_threshold=3)
    with pytest.raises(RuntimeError):
        await cb.call(_boom)
    with pytest.raises(RuntimeError):
        await cb.call(_boom)
    assert cb.failure_count == 2
    await cb.call(_ok)
    assert cb.failure_count == 0
    assert cb.state == CircuitState.CLOSED


async def test_reset_forces_closed() -> None:
    cb = CircuitBreaker(failure_threshold=1)
    with pytest.raises(RuntimeError):
        await cb.call(_boom)
    assert cb.state == CircuitState.OPEN
    await cb.reset()
    assert cb.state == CircuitState.CLOSED
    assert cb.failure_count == 0
