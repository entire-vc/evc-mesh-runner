"""Host gates: hold NEW sessions while the machine is short of memory, or overloaded.

Parallel sessions multiply memory use; on a small host the next start can push
it into swap thrash, which slows every running session. The gate holds starts
and resumes (running turns are never touched) while free memory is under the
hold threshold, and releases only above the release threshold (hysteresis).
Swap counts only while free memory is already low. A reading that cannot be
taken imposes nothing: no readings at all = open (fail open).

macOS: ``sysctl kern.memorystatus_level`` (free %), ``sysctl vm.swapusage``.
Linux: ``/proc/meminfo`` (MemAvailable / MemTotal, SwapFree).

The load gate (LoadGate) is the same idea for CPU: a host whose 5-minute load average per CPU is far
above 1 (26.10: load ~300 on 10 CPUs from runaway renderers) starts every session so slowly that the
drivers' start-up and accept waits run out, and each failed start only adds load. It holds starts and
resumes while the load is over the hold threshold and releases below the release threshold. Memory
and load log under their own strings ("host memory: ..." / "host load: ...").
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass

log = logging.getLogger("mesh_runner")

_SWAP_FIELD = re.compile(r"(total|used|free)\s*=\s*([0-9]+(?:[.,][0-9]+)?)\s*([KMGT]?)", re.I)
_UNIT_MB = {"": 1.0 / (1024 * 1024), "K": 1.0 / 1024, "M": 1.0, "G": 1024.0, "T": 1024.0 * 1024}


def parse_swapusage(text: str) -> float | None:
    """`sysctl -n vm.swapusage` -> free swap MB, or None. macOS grows swap on
    demand and reports total=0 while none is used: that is 'no constraint'."""
    vals = {}
    for k, num, unit in _SWAP_FIELD.findall(text or ""):
        vals[k.lower()] = float(num.replace(",", ".")) * _UNIT_MB[unit.upper()]
    if vals.get("total", 0) <= 0 or "used" not in vals:
        return None
    return max(0.0, vals["total"] - vals["used"])


def parse_meminfo(text: str) -> tuple[float | None, float | None]:
    """/proc/meminfo -> (free %, free swap MB)."""
    kb = {}
    for line in (text or "").splitlines():
        m = re.match(r"(\w+):\s+(\d+)", line)
        if m:
            kb[m.group(1)] = int(m.group(2))
    free = None
    if kb.get("MemTotal"):
        free = 100.0 * kb.get("MemAvailable", kb.get("MemFree", 0)) / kb["MemTotal"]
    swap = kb["SwapFree"] / 1024 if kb.get("SwapTotal") else None
    return free, swap


def _run(argv: list[str]) -> str:
    return subprocess.run(argv, capture_output=True, text=True, timeout=5, check=True).stdout


def read_host() -> tuple[float | None, float | None]:
    try:
        with open("/proc/meminfo") as fh:
            return parse_meminfo(fh.read())
    except OSError:
        pass
    free = swap = None
    try:
        v = float(_run(["/usr/sbin/sysctl", "-n", "kern.memorystatus_level"]).strip())
        free = v if 0 <= v <= 100 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    try:
        swap = parse_swapusage(_run(["/usr/sbin/sysctl", "-n", "vm.swapusage"]))
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return free, swap


@dataclass
class Thresholds:
    hold_free_pct: float = 0.0          # 0 = gate off
    release_free_pct: float = 0.0
    hold_swap_mb: float = 0.0
    release_swap_mb: float = 0.0
    swap_relevant_below_pct: float = 30.0


def decide(holding: bool, free: float | None, swap: float | None, t: Thresholds) -> bool:
    """Next state, True = hold new sessions. Pure."""
    if t.hold_free_pct <= 0 or (free is None and swap is None):
        return False
    if free is not None and free >= t.swap_relevant_below_pct:
        swap = None
    if not holding:
        return ((free is not None and free < t.hold_free_pct)
                or (swap is not None and t.hold_swap_mb > 0 and swap < t.hold_swap_mb))
    released = ((free is None or free >= max(t.release_free_pct, t.hold_free_pct))
                and (swap is None or swap >= max(t.release_swap_mb, t.hold_swap_mb)))
    return not released


class MemoryGate:
    """Shared by every agent thread of one runner; reads the host at most every
    `cache_seconds`, logs only on a state change."""

    def __init__(self, t: Thresholds, reader=read_host, cache_seconds: float = 30.0, clock=time.monotonic):
        self.t, self._read, self._cache, self._clock = t, reader, cache_seconds, clock
        self._lock = threading.Lock()
        self.holding = False
        self.free: float | None = None
        self.swap: float | None = None
        self._at: float | None = None

    def describe(self) -> str:
        f = "?" if self.free is None else f"{self.free:.0f}%"
        s = "?" if self.swap is None else f"{self.swap:.0f}MB"
        return f"free {f}, swap free {s}"

    def check(self) -> bool:
        """True = hold new sessions now."""
        if self.t.hold_free_pct <= 0:
            return False
        with self._lock:
            now = self._clock()
            if self._at is None or now - self._at >= self._cache:
                self._at = now
                try:
                    self.free, self.swap = self._read()
                except Exception:
                    self.free = self.swap = None
                was = self.holding
                self.holding = decide(was, self.free, self.swap, self.t)
                if self.holding != was:
                    log.warning("host memory: %s new sessions (%s)",
                                "HOLD" if self.holding else "resume", self.describe())
            return self.holding


@dataclass
class LoadThresholds:
    hold_per_cpu: float = 0.0           # 5-minute load average per CPU; 0 = gate off
    release_per_cpu: float = 0.0        # 0 = three quarters of the hold threshold


def read_load() -> tuple[float | None, int]:
    """(5-minute load average, CPU count); (None, n) when the load cannot be read."""
    cpus = os.cpu_count() or 1
    try:
        return os.getloadavg()[1], cpus
    except (OSError, AttributeError):
        return None, cpus


def decide_load(holding: bool, load: float | None, cpus: int, t: LoadThresholds) -> bool:
    """Next state, True = hold new sessions. Pure. No reading = open (fail open)."""
    if t.hold_per_cpu <= 0 or load is None:
        return False
    per_cpu = load / max(1, cpus)
    if not holding:
        return per_cpu > t.hold_per_cpu
    release = t.release_per_cpu if 0 < t.release_per_cpu <= t.hold_per_cpu else 0.75 * t.hold_per_cpu
    return per_cpu >= release


class LoadGate:
    """The CPU twin of MemoryGate: shared by every agent thread of one runner, reads the load at most
    every `cache_seconds`, logs only on a state change."""

    def __init__(self, t: LoadThresholds, reader=read_load, cache_seconds: float = 30.0, clock=time.monotonic):
        self.t, self._read, self._cache, self._clock = t, reader, cache_seconds, clock
        self._lock = threading.Lock()
        self.holding = False
        self.load: float | None = None
        self.cpus = os.cpu_count() or 1
        self._at: float | None = None

    def describe(self) -> str:
        if self.load is None:
            return "load average ?"
        return "load5 %.1f on %d CPUs = %.1f per CPU, hold above %g" % (
            self.load, self.cpus, self.load / max(1, self.cpus), self.t.hold_per_cpu)

    def check(self) -> bool:
        """True = hold new sessions now."""
        if self.t.hold_per_cpu <= 0:
            return False
        with self._lock:
            now = self._clock()
            if self._at is None or now - self._at >= self._cache:
                self._at = now
                try:
                    self.load, self.cpus = self._read()
                except Exception:
                    self.load = None
                was = self.holding
                self.holding = decide_load(was, self.load, self.cpus, self.t)
                if self.holding != was:
                    log.warning("host load: %s new sessions (%s)",
                                "HOLD" if self.holding else "resume", self.describe())
            return self.holding
