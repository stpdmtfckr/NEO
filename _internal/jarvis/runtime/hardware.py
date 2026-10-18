# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import time
from dataclasses import dataclass, asdict
from typing import Any

import psutil


@dataclass
class HardwareSnapshot:
    timestamp: float
    cpu_percent: float
    cpu_freq_mhz: float
    cpu_temp_c: float | None
    cpu_power_w: float | None
    ram_percent: float
    ram_used_gb: float
    ram_total_gb: float
    gpu_name: str = ""
    gpu_load_percent: float | None = None
    gpu_temp_c: float | None = None
    gpu_power_w: float | None = None
    gpu_clock_mhz: float | None = None
    vram_used_mb: float | None = None
    vram_total_mb: float | None = None
    fps: float | None = None
    providers: tuple[str, ...] = ()


def _nvidia_smi() -> dict[str, Any]:
    from jarvis.integrations import system_monitor as monitor
    rows = monitor._PROBES.read("nvidia", monitor._read_nvidia_gpus)
    if not rows:
        return {}
    row = rows[0]
    return {target: row.get(source) for target, source in (
        ("gpu_name", "name"), ("gpu_load_percent", "utilization_percent"),
        ("gpu_temp_c", "temperature_c"), ("gpu_power_w", "power_w"),
        ("gpu_clock_mhz", "clock_mhz"), ("vram_used_mb", "memory_used_mb"),
        ("vram_total_mb", "memory_total_mb"))}


def _cpu_sensors():
    from jarvis.integrations import system_monitor as monitor
    return monitor._cpu_telemetry(monitor._lhm_sensors())


def _windows_temperature() -> float | None:
    return _cpu_sensors()[0]


def _windows_cpu_power() -> float | None:
    return _cpu_sensors()[1]


def snapshot() -> HardwareSnapshot:
    providers = ["psutil"]
    vm = psutil.virtual_memory()
    freq = psutil.cpu_freq()
    cpu_temp, cpu_power = _cpu_sensors()
    gpu = _nvidia_smi()
    if gpu:
        providers.append("nvidia-smi")
    return HardwareSnapshot(
        timestamp=time.time(), cpu_percent=float(psutil.cpu_percent(interval=0.22)),
        cpu_freq_mhz=float(freq.current if freq else 0.0), cpu_temp_c=cpu_temp, cpu_power_w=cpu_power,
        ram_percent=float(vm.percent), ram_used_gb=round(vm.used / 1024**3, 2), ram_total_gb=round(vm.total / 1024**3, 2),
        providers=tuple(providers), **gpu,
    )


def average(samples: int = 3, interval: float = 0.25) -> dict[str, Any]:
    count=max(1,min(8,int(samples))); rows=[]
    for i in range(count):
        rows.append(snapshot())
        if i + 1 < count:
            time.sleep(max(0.0,min(1.0,float(interval))))
    data = asdict(rows[-1])
    numeric = [
        "cpu_percent", "cpu_freq_mhz", "cpu_temp_c", "cpu_power_w", "ram_percent",
        "gpu_load_percent", "gpu_temp_c", "gpu_power_w", "gpu_clock_mhz", "fps",
    ]
    # HIST-029: GPU values are averaged only over samples of the same device as
    # the shown name; samples of another GPU are not blended under that name.
    identity = getattr(rows[-1], "gpu_name", None)
    same_gpu = [x for x in rows if getattr(x, "gpu_name", None) == identity]
    if len(same_gpu) != len(rows):
        data["avg_gpu_samples"] = len(same_gpu)
        data["avg_gpu_device_changed"] = True
    for key in numeric:
        source = same_gpu if key.startswith("gpu_") or key == "fps" else rows
        vals = [getattr(x, key) for x in source if getattr(x, key) is not None]
        if vals:
            data[f"avg_{key}"] = round(sum(float(v) for v in vals) / len(vals), 2)
    return data
