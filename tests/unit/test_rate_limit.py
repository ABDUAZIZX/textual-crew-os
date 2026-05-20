"""Tests for ``crew_os.security.rate_limit``."""

from __future__ import annotations

import threading
import time

import pytest

from crew_os.core.exceptions import RateLimitExceeded
from crew_os.security.rate_limit import RateLimiter, TokenBucket

# ─────────────────────────────── token bucket ──────────────────────────


def test_starts_full() -> None:
    b = TokenBucket(capacity=5, refill_per_sec=1)
    assert b.available() == pytest.approx(5.0)


def test_try_consume_drains_until_empty() -> None:
    b = TokenBucket(capacity=3, refill_per_sec=0.001)
    assert b.try_consume() is True
    assert b.try_consume() is True
    assert b.try_consume() is True
    assert b.try_consume() is False


def test_refills_linearly_over_time() -> None:
    b = TokenBucket(capacity=10, refill_per_sec=100)  # ~0.01s per token
    for _ in range(10):
        b.try_consume()
    assert b.try_consume() is False
    time.sleep(0.05)
    # After 50ms at 100/s we should have ~5 tokens.
    assert b.available() == pytest.approx(5.0, abs=1.5)


def test_does_not_exceed_capacity() -> None:
    b = TokenBucket(capacity=2, refill_per_sec=1000)
    time.sleep(0.05)
    assert b.available() <= 2.0 + 1e-9


def test_time_until_zero_when_available() -> None:
    b = TokenBucket(capacity=1, refill_per_sec=10)
    assert b.time_until(1) == 0.0


def test_time_until_positive_after_drain() -> None:
    b = TokenBucket(capacity=1, refill_per_sec=10)
    b.try_consume()
    wait = b.time_until(1)
    assert 0 < wait <= 0.2


@pytest.mark.parametrize(
    ("capacity", "refill"),
    [(0, 1), (-1, 1), (1, 0), (1, -2)],
)
def test_rejects_invalid_construction(capacity: float, refill: float) -> None:
    with pytest.raises(ValueError, match="must be > 0"):
        TokenBucket(capacity=capacity, refill_per_sec=refill)


def test_rejects_non_positive_consume() -> None:
    b = TokenBucket(capacity=1, refill_per_sec=1)
    with pytest.raises(ValueError, match="tokens must be > 0"):
        b.try_consume(0)


def test_concurrent_consumes_never_exceed_capacity() -> None:
    b = TokenBucket(capacity=50, refill_per_sec=0.0001)
    successes: list[bool] = []
    lock = threading.Lock()

    def worker() -> None:
        ok = b.try_consume()
        with lock:
            successes.append(ok)

    threads = [threading.Thread(target=worker) for _ in range(200)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(1 for s in successes if s) == 50


# ─────────────────────────────── limiter ───────────────────────────────


def test_unconfigured_key_is_unlimited() -> None:
    rl = RateLimiter()
    for _ in range(1000):
        assert rl.check("ghost") is True


def test_configured_key_obeys_capacity() -> None:
    rl = RateLimiter()
    rl.configure("coder", capacity=2, refill_per_sec=0.0001)
    assert rl.check("coder") is True
    assert rl.check("coder") is True
    assert rl.check("coder") is False


def test_require_raises_on_overflow() -> None:
    rl = RateLimiter()
    rl.configure("k", capacity=1, refill_per_sec=0.0001)
    rl.require("k")
    with pytest.raises(RateLimitExceeded):
        rl.require("k")


def test_available_returns_none_for_unconfigured() -> None:
    rl = RateLimiter()
    assert rl.available("ghost") is None
    rl.configure("ghost", capacity=3, refill_per_sec=1)
    assert rl.available("ghost") == pytest.approx(3.0)
