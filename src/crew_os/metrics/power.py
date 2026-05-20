"""Power metrics: GPU draw via NVML, CPU energy via Intel RAPL.

GPU power is instantaneous (watts). RAPL exposes a cumulative energy
counter (microjoules); converting it to watts needs two readings over
time, which the sampler does. Both probes degrade to ``None`` when the
source is unavailable.
"""

from __future__ import annotations

from pathlib import Path

RAPL_ENERGY_PATH = Path("/sys/class/powercap/intel-rapl:0/energy_uj")


def gpu_power_watts() -> float | None:
    """Return instantaneous GPU power draw in watts, or ``None``."""

    try:
        import pynvml  # noqa: PLC0415 - optional dependency, imported lazily
    except ImportError:
        return None
    try:
        pynvml.nvmlInit()
        handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        milliwatts = pynvml.nvmlDeviceGetPowerUsage(handle)
        pynvml.nvmlShutdown()
    except Exception:  # any NVML error -> unknown
        return None
    return float(milliwatts) / 1000.0


def read_rapl_energy_uj(path: Path = RAPL_ENERGY_PATH) -> int | None:
    """Read the cumulative RAPL energy counter in microjoules, or ``None``."""

    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None
