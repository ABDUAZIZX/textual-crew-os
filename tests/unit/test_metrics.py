"""Tests for the metrics modules and the background sampler."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from crew_os.metrics.gpu import GpuMetrics, sample_gpu
from crew_os.metrics.power import gpu_power_watts, read_rapl_energy_uj
from crew_os.metrics.sampler import MetricsSampler
from crew_os.metrics.system import SystemMetrics, sample_system

# ─────────────────────────────── real probes ───────────────────────────


def test_sample_system_returns_sane_values() -> None:
    m = sample_system()
    assert m.cpu_percent >= 0.0
    assert 0.0 <= m.ram_percent <= 100.0
    assert m.ram_total_mb > 0
    assert m.ram_used_mb <= m.ram_total_mb
    assert m.timestamp


def test_sample_gpu_returns_metrics() -> None:
    m = sample_gpu()
    assert isinstance(m, GpuMetrics)
    if m.available:
        assert m.vram_total_mb > 0
        assert m.vram_used_mb + m.vram_free_mb <= m.vram_total_mb + 1


def test_sample_gpu_unavailable_without_pynvml(monkeypatch: pytest.MonkeyPatch) -> None:
    # Make `import pynvml` raise ImportError inside sample_gpu.
    monkeypatch.setitem(sys.modules, "pynvml", None)
    m = sample_gpu()
    assert m.available is False


def test_gpu_power_none_without_pynvml(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "pynvml", None)
    assert gpu_power_watts() is None


def test_gpu_power_watts_type() -> None:
    val = gpu_power_watts()
    assert val is None or (isinstance(val, float) and val >= 0)


def test_read_rapl_from_tmp(tmp_path: Path) -> None:
    f = tmp_path / "energy_uj"
    f.write_text("123456\n")
    assert read_rapl_energy_uj(f) == 123456


def test_read_rapl_missing_returns_none(tmp_path: Path) -> None:
    assert read_rapl_energy_uj(tmp_path / "nope") is None


def test_read_rapl_garbage_returns_none(tmp_path: Path) -> None:
    f = tmp_path / "energy_uj"
    f.write_text("not-a-number")
    assert read_rapl_energy_uj(f) is None


# ─────────────────────────────── sampler ───────────────────────────────


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _fake_system() -> SystemMetrics:
    return SystemMetrics(
        cpu_percent=10.0,
        ram_percent=50.0,
        ram_used_mb=32000,
        ram_total_mb=64000,
        disk_read_bytes=0,
        disk_write_bytes=0,
        timestamp="t",
    )


def _fake_gpu() -> GpuMetrics:
    return GpuMetrics(available=True, name="RTX 2060 SUPER", vram_total_mb=8192)


def test_sampler_rejects_bad_interval() -> None:
    with pytest.raises(ValueError, match="interval"):
        MetricsSampler(interval=0)


async def test_sample_once_populates_latest() -> None:
    s = MetricsSampler(
        system_fn=_fake_system,
        gpu_fn=_fake_gpu,
        gpu_power_fn=lambda: 120.0,
        rapl_fn=lambda: 1000,
    )
    assert s.latest() is None
    snap = await s.sample_once()
    assert s.latest() is snap
    assert snap.system.cpu_percent == 10.0
    assert snap.gpu.name == "RTX 2060 SUPER"
    assert snap.gpu_watts == 120.0


async def test_cpu_watts_none_on_first_then_computed() -> None:
    clock = _Clock()
    rapl = {"v": 1_000_000}  # 1 joule
    s = MetricsSampler(
        system_fn=_fake_system,
        gpu_fn=_fake_gpu,
        gpu_power_fn=lambda: None,
        rapl_fn=lambda: rapl["v"],
        time_fn=clock,
    )
    first = await s.sample_once()
    assert first.cpu_watts is None  # primes the delta

    clock.advance(2.0)
    rapl["v"] = 1_000_000 + 30_000_000  # +30 J over 2 s -> 15 W
    second = await s.sample_once()
    assert second.cpu_watts == pytest.approx(15.0)


async def test_cpu_watts_none_when_rapl_unavailable() -> None:
    s = MetricsSampler(
        system_fn=_fake_system,
        gpu_fn=_fake_gpu,
        gpu_power_fn=lambda: None,
        rapl_fn=lambda: None,
    )
    snap = await s.sample_once()
    assert snap.cpu_watts is None


async def test_start_primes_and_stop() -> None:
    s = MetricsSampler(
        interval=0.01,
        system_fn=_fake_system,
        gpu_fn=_fake_gpu,
        gpu_power_fn=lambda: 1.0,
        rapl_fn=lambda: 1,
    )
    await s.start()
    assert s.running is True
    assert s.latest() is not None
    await s.stop()
    assert s.running is False
