"""Background metrics sampler - the dashboard's metrics cache.

Periodically samples system, GPU, and power metrics into an in-memory
snapshot the dashboard reads, so per-request handlers never block on
psutil / nvidia-ml-py. CPU watts are derived from RAPL energy deltas
between samples; the clock is injectable for deterministic tests.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable

from pydantic import BaseModel, ConfigDict

from crew_os.core.models import utcnow
from crew_os.metrics.gpu import GpuMetrics, sample_gpu
from crew_os.metrics.power import gpu_power_watts, read_rapl_energy_uj
from crew_os.metrics.system import SystemMetrics, sample_system


class MetricsSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    system: SystemMetrics
    gpu: GpuMetrics
    gpu_watts: float | None = None
    cpu_watts: float | None = None
    timestamp: str


class MetricsSampler:
    def __init__(
        self,
        *,
        interval: float = 0.5,
        system_fn: Callable[[], SystemMetrics] = sample_system,
        gpu_fn: Callable[[], GpuMetrics] = sample_gpu,
        gpu_power_fn: Callable[[], float | None] = gpu_power_watts,
        rapl_fn: Callable[[], int | None] = read_rapl_energy_uj,
        time_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        if interval <= 0:
            raise ValueError("interval must be > 0")
        self._interval = interval
        self._system_fn = system_fn
        self._gpu_fn = gpu_fn
        self._gpu_power_fn = gpu_power_fn
        self._rapl_fn = rapl_fn
        self._now = time_fn
        self._snapshot: MetricsSnapshot | None = None
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._prev_rapl_uj: int | None = None
        self._prev_rapl_t: float | None = None

    def latest(self) -> MetricsSnapshot | None:
        return self._snapshot

    @property
    def running(self) -> bool:
        return self._running

    def _compute_cpu_watts(self, rapl_uj: int | None) -> float | None:
        now = self._now()
        if rapl_uj is None:
            self._prev_rapl_uj = None
            self._prev_rapl_t = None
            return None
        prev_uj, prev_t = self._prev_rapl_uj, self._prev_rapl_t
        self._prev_rapl_uj = rapl_uj
        self._prev_rapl_t = now
        if prev_uj is None or prev_t is None:
            return None
        dt = now - prev_t
        d_uj = rapl_uj - prev_uj
        if dt <= 0 or d_uj < 0:  # no elapsed time, or counter wrapped
            return None
        return (d_uj / 1_000_000.0) / dt

    async def sample_once(self) -> MetricsSnapshot:
        system = await asyncio.to_thread(self._system_fn)
        gpu = await asyncio.to_thread(self._gpu_fn)
        gpu_watts = await asyncio.to_thread(self._gpu_power_fn)
        rapl = await asyncio.to_thread(self._rapl_fn)
        snapshot = MetricsSnapshot(
            system=system,
            gpu=gpu,
            gpu_watts=gpu_watts,
            cpu_watts=self._compute_cpu_watts(rapl),
            timestamp=utcnow().isoformat(),
        )
        self._snapshot = snapshot
        return snapshot

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        await self.sample_once()  # prime so latest() is immediately available
        self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        while self._running:
            await asyncio.sleep(self._interval)
            with contextlib.suppress(Exception):
                await self.sample_once()

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
