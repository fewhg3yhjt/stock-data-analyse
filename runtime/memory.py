"""Low-overhead process and container memory diagnostics."""

from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import contextmanager
from typing import Iterator

logger = logging.getLogger(__name__)


def _read_int(path: str) -> int | None:
    try:
        with open(path, encoding="ascii") as stream:
            return int(stream.read().strip())
    except (OSError, ValueError):
        return None


def _proc_rss_bytes() -> int | None:
    try:
        with open("/proc/self/status", encoding="ascii") as stream:
            for line in stream:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError):
        return None
    return None


def _cgroup_memory() -> dict[str, int | None]:
    current = _read_int("/sys/fs/cgroup/memory.current")
    maximum = _read_int("/sys/fs/cgroup/memory.max")
    if maximum is None:
        maximum = _read_int("/sys/fs/cgroup/memory/memory.limit_in_bytes")
    return {"current_bytes": current, "limit_bytes": maximum}


def memory_snapshot() -> dict[str, int | float | None]:
    """Return process RSS plus cgroup memory, without third-party dependencies."""
    rss = _proc_rss_bytes()
    cgroup = _cgroup_memory()
    limit = cgroup["limit_bytes"]
    return {
        "rss_bytes": rss,
        "rss_mb": round(rss / 1024 / 1024, 1) if rss is not None else None,
        "container_bytes": cgroup["current_bytes"],
        "container_mb": (round(cgroup["current_bytes"] / 1024 / 1024, 1)
                          if cgroup["current_bytes"] is not None else None),
        "container_limit_bytes": limit,
        "container_limit_mb": (round(limit / 1024 / 1024, 1)
                                if limit is not None else None),
    }


@contextmanager
def monitor_memory(label: str, interval: float = 5.0) -> Iterator[dict]:
    """Sample a task's memory and log start/peak/end measurements."""
    start = memory_snapshot()
    state = {"peak": start, "samples": 1}
    stop = threading.Event()

    def sample() -> None:
        while not stop.wait(interval):
            current = memory_snapshot()
            state["samples"] += 1
            peak = state["peak"]
            if (current.get("rss_bytes") or 0) > (peak.get("rss_bytes") or 0):
                state["peak"] = current
            logger.info("memory task=%s rss=%sMiB container=%sMiB",
                        label, current.get("rss_mb"), current.get("container_mb"))

    thread = threading.Thread(target=sample, daemon=True, name=f"memory-{label}")
    thread.start()
    try:
        yield {"start": start, "state": state}
    finally:
        stop.set()
        thread.join(timeout=max(1.0, interval))
        end = memory_snapshot()
        peak = state["peak"]
        if (end.get("rss_bytes") or 0) > (peak.get("rss_bytes") or 0):
            peak = end
        state["peak"] = peak
        result = {"start": start, "peak": peak, "end": end,
                  "samples": state["samples"],
                  "rss_delta_mb": round((end.get("rss_bytes") or 0) / 1024 / 1024
                                         - (start.get("rss_bytes") or 0) / 1024 / 1024, 1)}
        logger.info("memory task=%s start=%sMiB peak=%sMiB end=%sMiB delta=%sMiB",
                    label, start.get("rss_mb"), peak.get("rss_mb"),
                    end.get("rss_mb"), result["rss_delta_mb"])
        state["result"] = result


def memory_status() -> dict:
    """API-friendly status, including the configured process identity."""
    return {"pid": os.getpid(), "timestamp": time.time(), **memory_snapshot()}
