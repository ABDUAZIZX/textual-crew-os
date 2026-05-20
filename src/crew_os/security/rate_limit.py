"""Token-bucket rate limiter, keyed per agent / per tool.

Thread-safe via :class:`threading.Lock`. Buckets are configured ahead of
use; unconfigured keys are treated as unlimited (it is the caller's job
to configure every key it cares about - the platform wires this up in
the agent framework).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from crew_os.core.exceptions import RateLimitExceeded


@dataclass
class TokenBucket:
    capacity: float
    refill_per_sec: float
    _tokens: float = field(init=False)
    _last_refill: float = field(init=False)
    _lock: threading.Lock = field(init=False, default_factory=threading.Lock)

    def __post_init__(self) -> None:
        if self.capacity <= 0:
            raise ValueError(f"capacity must be > 0, got {self.capacity}")
        if self.refill_per_sec <= 0:
            raise ValueError(f"refill_per_sec must be > 0, got {self.refill_per_sec}")
        self._tokens = float(self.capacity)
        self._last_refill = time.monotonic()

    def _refill_locked(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        if elapsed > 0:
            self._tokens = min(self.capacity, self._tokens + elapsed * self.refill_per_sec)
            self._last_refill = now

    def try_consume(self, tokens: float = 1.0) -> bool:
        if tokens <= 0:
            raise ValueError(f"tokens must be > 0, got {tokens}")
        with self._lock:
            self._refill_locked()
            if self._tokens >= tokens:
                self._tokens -= tokens
                return True
            return False

    def available(self) -> float:
        with self._lock:
            self._refill_locked()
            return self._tokens

    def time_until(self, tokens: float = 1.0) -> float:
        if tokens <= 0:
            raise ValueError(f"tokens must be > 0, got {tokens}")
        with self._lock:
            self._refill_locked()
            if self._tokens >= tokens:
                return 0.0
            return (tokens - self._tokens) / self.refill_per_sec


class RateLimiter:
    """Registry of named buckets. Unknown keys pass through unrestricted."""

    def __init__(self) -> None:
        self._buckets: dict[str, TokenBucket] = {}
        self._lock = threading.Lock()

    def configure(self, key: str, *, capacity: float, refill_per_sec: float) -> None:
        with self._lock:
            self._buckets[key] = TokenBucket(capacity=capacity, refill_per_sec=refill_per_sec)

    def has(self, key: str) -> bool:
        with self._lock:
            return key in self._buckets

    def check(self, key: str, tokens: float = 1.0) -> bool:
        with self._lock:
            bucket = self._buckets.get(key)
        if bucket is None:
            return True
        return bucket.try_consume(tokens)

    def require(self, key: str, tokens: float = 1.0) -> None:
        if not self.check(key, tokens):
            raise RateLimitExceeded(f"rate limit exceeded for key {key!r}")

    def available(self, key: str) -> float | None:
        with self._lock:
            bucket = self._buckets.get(key)
        return None if bucket is None else bucket.available()
