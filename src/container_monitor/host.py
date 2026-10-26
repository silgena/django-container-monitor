"""The host's CPU, load, memory, swap, disk and uptime, from /proc.

Inside a container /proc still describes the whole machine, so a worker in
one sees what ``free``, ``uptime`` and ``df`` would show on the server. On
systems without /proc the figures are simply missing.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path


def _cpu_times(proc: Path):
    try:
        values = [
            int(value)
            for value in (proc / "stat").read_text().split("\n")[0].split()[1:]
        ]
    except (OSError, ValueError, IndexError):
        return None
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    total = sum(values[:8])
    return total - idle, total


def _meminfo(proc: Path) -> dict[str, int]:
    info = {}
    try:
        for line in (proc / "meminfo").read_text().splitlines():
            name, _, rest = line.partition(":")
            parts = rest.split()
            if parts:
                info[name] = int(parts[0]) * (1024 if len(parts) > 1 else 1)
    except OSError:
        pass
    return info


def measure(
    proc_root: str = "/proc", disk_paths=("/",), sample_seconds: float = 1.0
) -> dict:
    proc = Path(proc_root)
    metrics: dict = {"cpu_count": os.cpu_count()}
    first = _cpu_times(proc)
    if first is not None and sample_seconds > 0:
        time.sleep(sample_seconds)
        second = _cpu_times(proc)
        busy, total = second[0] - first[0], second[1] - first[1]
        metrics["cpu_percent"] = round(busy / total * 100, 1) if total > 0 else 0.0
    try:
        metrics["load_1"], metrics["load_5"], metrics["load_15"] = (
            round(value, 2) for value in os.getloadavg()
        )
    except (OSError, AttributeError):
        pass
    try:
        metrics["uptime_seconds"] = int(float((proc / "uptime").read_text().split()[0]))
    except (OSError, ValueError, IndexError):
        pass
    memory = _meminfo(proc)
    if total_memory := memory.get("MemTotal"):
        available = memory.get("MemAvailable", memory.get("MemFree", 0))
        metrics.update(
            memory_total=total_memory,
            memory_available=available,
            memory_percent=round((total_memory - available) / total_memory * 100, 1),
        )
    if (swap_total := memory.get("SwapTotal")) is not None:
        swap_used = swap_total - memory.get("SwapFree", swap_total)
        metrics.update(
            swap_total=swap_total,
            swap_used=swap_used,
            swap_percent=round(swap_used / swap_total * 100, 1) if swap_total else 0.0,
        )
    disks = []
    for path in disk_paths:
        try:
            usage = shutil.disk_usage(path)
        except OSError:
            continue
        disks.append(
            {
                "path": str(path),
                "total": usage.total,
                "used": usage.used,
                "percent": round(usage.used / usage.total * 100, 1),
            }
        )
    metrics["disks"] = disks
    return metrics
