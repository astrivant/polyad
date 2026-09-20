"""
Read live Linux cgroup resource assignments and usage without Kubernetes API access.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ContainerMetrics:
    """
    A point-in-time cgroup v2 resource sample for the current container.

    Attributes:
        cpu_usage_usec (int | None): Cumulative cgroup CPU use in microseconds.
        cpu_limit_millicores (int | None): Current CPU quota in millicores, or None when unbounded.
        memory_usage_bytes (int | None): Current cgroup memory use in bytes.
        memory_limit_bytes (int | None): Current memory limit in bytes, or None when unbounded.
    """

    cpu_usage_usec: int | None = None
    cpu_limit_millicores: int | None = None
    memory_usage_bytes: int | None = None
    memory_limit_bytes: int | None = None

    @property
    def memory_available_bytes(self) -> int | None:
        """
        Return limit minus current use, or None when memory is unbounded/unavailable.

        Returns:
            int | None: Nonnegative memory headroom when both observations are available.
        """

        # Missing or unlimited capacity is unknown, not zero available memory.
        if self.memory_limit_bytes is None or self.memory_usage_bytes is None:
            return None
        return max(0, self.memory_limit_bytes - self.memory_usage_bytes)


def container_metrics(root: str | Path = "/sys/fs/cgroup") -> ContainerMetrics:
    """
    Sample the current process's cgroup v2 usage and live in-place-resized limits.

    Args:
        root (str | Path): Current container's cgroup v2 directory.

    Returns:
        ContainerMetrics: Best-effort usage and limit sample; missing observations are None.
    """
    path = Path(root)

    def read(name: str) -> str | None:
        try:
            return (path / name).read_text(encoding="ascii").strip()
        except (FileNotFoundError, OSError, UnicodeError):
            return None

    # Read on every call: VPA can resize the live cgroup without replacing the
    # process, while projected environment variables remain startup snapshots.
    memory_current = read("memory.current")
    memory_max = read("memory.max")
    cpu_max = read("cpu.max")
    cpu_stat = read("cpu.stat")
    usage = None

    # CPU usage is a cumulative counter. A utilization rate requires two samples
    # and elapsed time; this helper deliberately returns the underlying reading.
    if cpu_stat:
        values = dict(line.split(maxsplit=1) for line in cpu_stat.splitlines() if len(line.split(maxsplit=1)) == 2)
        usage = int(values["usage_usec"]) if values.get("usage_usec", "").isdecimal() else None
    cpu_limit = None

    # cpu.max contains quota and period in matching units. Their ratio is CPU
    # cores; multiply by 1000 for the millicore units used by projected VPA bounds.
    if cpu_max:
        quota, _, period = cpu_max.partition(" ")
        if quota.isdecimal() and period.isdecimal() and int(period) > 0:
            cpu_limit = int(quota) * 1000 // int(period)

    # The kernel's "max" sentinel is not numeric, so an unbounded limit remains
    # None instead of looking like a real zero-sized allocation.
    return ContainerMetrics(
        cpu_usage_usec=usage,
        cpu_limit_millicores=cpu_limit,
        memory_usage_bytes=int(memory_current) if memory_current and memory_current.isdecimal() else None,
        memory_limit_bytes=int(memory_max) if memory_max and memory_max.isdecimal() else None,
    )
