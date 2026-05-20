"""System metrics via psutil (CPU, RAM, disk I/O)."""

from __future__ import annotations

import psutil
from pydantic import BaseModel, ConfigDict

from crew_os.core.models import utcnow


class SystemMetrics(BaseModel):
    model_config = ConfigDict(frozen=True)

    cpu_percent: float
    ram_percent: float
    ram_used_mb: int
    ram_total_mb: int
    disk_read_bytes: int
    disk_write_bytes: int
    timestamp: str


def sample_system() -> SystemMetrics:
    """Sample current system metrics.

    ``cpu_percent(interval=None)`` is non-blocking and reports usage since
    the previous call, so a long-lived sampler yields meaningful values.
    """

    cpu = psutil.cpu_percent(interval=None)
    vm = psutil.virtual_memory()
    dio = psutil.disk_io_counters()
    return SystemMetrics(
        cpu_percent=float(cpu),
        ram_percent=float(vm.percent),
        ram_used_mb=int(vm.used) // (1024 * 1024),
        ram_total_mb=int(vm.total) // (1024 * 1024),
        disk_read_bytes=int(dio.read_bytes) if dio else 0,
        disk_write_bytes=int(dio.write_bytes) if dio else 0,
        timestamp=utcnow().isoformat(),
    )
