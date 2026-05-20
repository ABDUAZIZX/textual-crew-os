"""GPU metrics via nvidia-ml-py (pynvml).

All probing is best-effort: if pynvml or the GPU is unavailable, a
``GpuMetrics(available=False)`` is returned rather than raising.
"""

from __future__ import annotations

import contextlib

from pydantic import BaseModel, ConfigDict


class GpuMetrics(BaseModel):
    model_config = ConfigDict(frozen=True)

    available: bool
    name: str = ""
    utilization_percent: int = 0
    vram_used_mb: int = 0
    vram_total_mb: int = 0
    vram_free_mb: int = 0
    temperature_c: int = 0


def sample_gpu() -> GpuMetrics:
    try:
        import pynvml  # noqa: PLC0415 - optional dependency, imported lazily
    except ImportError:
        return GpuMetrics(available=False)

    try:
        pynvml.nvmlInit()
    except Exception:  # no driver / no GPU present
        return GpuMetrics(available=False)

    try:
        handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        name = pynvml.nvmlDeviceGetName(handle)
        if isinstance(name, bytes):
            name = name.decode("utf-8", errors="replace")
        util = pynvml.nvmlDeviceGetUtilizationRates(handle)
        mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
        temp = pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU)
        return GpuMetrics(
            available=True,
            name=str(name),
            utilization_percent=int(util.gpu),
            vram_used_mb=int(mem.used) // (1024 * 1024),
            vram_total_mb=int(mem.total) // (1024 * 1024),
            vram_free_mb=int(mem.free) // (1024 * 1024),
            temperature_c=int(temp),
        )
    except Exception:  # any NVML error degrades to "unavailable"
        return GpuMetrics(available=False)
    finally:
        with contextlib.suppress(Exception):
            pynvml.nvmlShutdown()
