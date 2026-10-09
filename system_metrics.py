"""Safe, lightweight host metrics for the Telegram status panel."""

from __future__ import annotations

import os
import platform
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass

try:
    import psutil
except ImportError:  # pragma: no cover - dependency is installed in production
    psutil = None


@dataclass(frozen=True)
class GPUInfo:
    model: str | None = None
    usage_percent: float | None = None
    temperature_c: float | None = None


@dataclass(frozen=True)
class SystemMetrics:
    cpu_model: str | None
    cpu_usage_percent: float | None
    cpu_cores: int | None
    gpu: GPUInfo
    ram_used_gb: float | None
    ram_total_gb: float | None
    ram_usage_percent: float | None
    disk_used_gb: float | None
    disk_total_gb: float | None
    disk_free_gb: float | None
    disk_usage_percent: float | None
    uptime_seconds: float | None
    python_version: str
    platform_name: str
    hostname: str


def _cpu_model() -> str | None:
    try:
        with open("/proc/cpuinfo", encoding="utf-8", errors="replace") as cpuinfo:
            for line in cpuinfo:
                if line.lower().startswith("model name"):
                    _, value = line.split(":", 1)
                    return value.strip() or None
    except (OSError, ValueError):
        pass

    value = platform.processor().strip()
    return value or None


def _number(value: str) -> float | None:
    try:
        return float(value.strip())
    except (TypeError, ValueError):
        return None


def detect_gpu() -> GPUInfo:
    """Read NVIDIA GPU metrics when the host exposes nvidia-smi."""
    executable = shutil.which("nvidia-smi")
    if not executable:
        return GPUInfo()

    try:
        result = subprocess.run(
            [
                executable,
                "--query-gpu=name,utilization.gpu,temperature.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=1,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return GPUInfo()

    if result.returncode != 0 or not result.stdout.strip():
        return GPUInfo()

    values = [value.strip() for value in result.stdout.splitlines()[0].split(",")]
    if not values:
        return GPUInfo()
    values += [""] * (3 - len(values))
    return GPUInfo(
        model=values[0] or None,
        usage_percent=_number(values[1]),
        temperature_c=_number(values[2]),
    )


def _gigabytes(bytes_value: int | float | None) -> float | None:
    if bytes_value is None:
        return None
    return bytes_value / (1024**3)


def collect_system_metrics() -> SystemMetrics:
    """Collect only safe host statistics; individual probes fail soft."""
    cpu_usage: float | None = None
    ram_used = ram_total = ram_percent = None
    disk_used = disk_total = disk_free = disk_percent = None
    uptime: float | None = None

    if psutil is not None:
        try:
            cpu_usage = float(psutil.cpu_percent(interval=0.05))
        except (OSError, RuntimeError):
            pass
        try:
            memory = psutil.virtual_memory()
            ram_used = _gigabytes(memory.used)
            ram_total = _gigabytes(memory.total)
            ram_percent = float(memory.percent)
        except (OSError, RuntimeError):
            pass
        try:
            disk = psutil.disk_usage(os.path.abspath(os.sep))
            disk_used = _gigabytes(disk.used)
            disk_total = _gigabytes(disk.total)
            disk_free = _gigabytes(disk.free)
            disk_percent = float(disk.percent)
        except (OSError, RuntimeError):
            pass
        try:
            uptime = max(0.0, time.time() - psutil.boot_time())
        except (OSError, RuntimeError, ValueError):
            pass

    return SystemMetrics(
        cpu_model=_cpu_model(),
        cpu_usage_percent=cpu_usage,
        cpu_cores=os.cpu_count(),
        gpu=detect_gpu(),
        ram_used_gb=ram_used,
        ram_total_gb=ram_total,
        ram_usage_percent=ram_percent,
        disk_used_gb=disk_used,
        disk_total_gb=disk_total,
        disk_free_gb=disk_free,
        disk_usage_percent=disk_percent,
        uptime_seconds=uptime,
        python_version=platform.python_version(),
        platform_name=f"{platform.system()} {platform.release()}",
        hostname=socket.gethostname(),
    )


def overall_status(metrics: SystemMetrics) -> tuple[str, str]:
    """Return the overall status based on measured resource utilization."""
    values = [
        value
        for value in (
            metrics.cpu_usage_percent,
            metrics.ram_usage_percent,
            metrics.disk_usage_percent,
            metrics.gpu.usage_percent,
        )
        if value is not None
    ]
    peak = max(values, default=0.0)
    if peak >= 85:
        return "🔴", "ʜɪɢʜ ʟᴏᴀᴅ"
    if peak >= 60:
        return "🟡", "ᴍᴏᴅᴇʀᴀᴛᴇ"
    return "🟢", "ᴇxᴄᴇʟʟᴇɴᴛ"


def format_uptime(seconds: float | None) -> str:
    if seconds is None:
        return "N/A"
    total_minutes = max(0, int(seconds)) // 60
    days, remainder = divmod(total_minutes, 24 * 60)
    hours, minutes = divmod(remainder, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"