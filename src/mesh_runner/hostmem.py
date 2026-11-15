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
above 1 can start every session so slowly that the
drivers' start-up and accept waits run out, and each failed start only adds load. It holds starts and
resumes while the load is over the hold threshold and releases below the release threshold. Memory
and load log under their own strings ("host memory: ..." / "host load: ...").

A hold lifts only after its release condition has held for ``settle_seconds`` (a reading back under the
release threshold restarts that clock), and then StartRamp lets sessions back one at a time for a while.
Without them a memory hold could lift for a few minutes and come back, and a load hold could lift into a
burst of starts: the sessions started in those gaps were the ones that never took their prompt.
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


class _Settling:
    """A hold that lifts only after its release condition held for ``settle_seconds``.

    ``released_at`` is the clock reading of the last real release (StartRamp counts its ramp from it);
    ``_release_since`` the first reading of the current release streak. The caller holds the gate's lock."""

    settle_seconds: float = 0.0
    released_at: float | None = None
    _release_since: float | None = None
    _what = ""

    def _settle(self, was: bool, want: bool, now: float, seen: bool = True) -> bool:
        """``want`` is the thresholds' next state, ``seen`` whether the host could be read at all; returns
        the gate's next state."""
        if want or not was:
            self._release_since = None          # holding on (or a new hold): the release streak is broken
            return want
        if not seen and self.settle_seconds > 0:
            # The thresholds fail open on a missing reading, but no reading is no sight of the release
            # condition: the hold stays and the streak starts again at the next real reading. A host
            # short of memory is also when the readers fail, so this must not count toward the settle.
            if self._release_since is not None:
                log.warning("host %s: no reading during the release settle; it starts again at the next "
                            "reading", self._what)
            self._release_since = None
            return True
        if self._release_since is None:
            self._release_since = now
            if self.settle_seconds > 0:
                log.warning("host %s: release condition met, new sessions wait %gs of it (%s)",
                            self._what, self.settle_seconds, self.describe())
        if now - self._release_since < self.settle_seconds:
            return True
        self._release_since = None
        self.released_at = now
        return False


class MemoryGate(_Settling):
    """Shared by every agent thread of one runner; reads the host at most every
    `cache_seconds`, logs only on a state change."""

    _what = "memory"

    def __init__(self, t: Thresholds, reader=read_host, cache_seconds: float = 30.0, clock=time.monotonic,
                 settle_seconds: float = 0.0):
        self.t, self._read, self._cache, self._clock = t, reader, cache_seconds, clock
        self.settle_seconds = settle_seconds
        self._lock = threading.Lock()
        self.holding = False
        self.free: float | None = None
        self.swap: float | None = None
        self._at: float | None = None

    def describe(self) -> str:
        f = "?" if self.free is None else f"{self.free:.0f}%"
        s = "?" if self.swap is None else f"{self.swap:.0f}MB"
        return f"free {f}, swap free {s}"

    def check(self, fresh: bool = False) -> bool:
        """True = hold new sessions now. ``fresh`` reads the host now instead of the cached reading:
        the last look right before a session starts (a 30 s old reading could let starts through into an
        exhausted swap)."""
        if self.t.hold_free_pct <= 0:
            return False
        with self._lock:
            now = self._clock()
            if fresh or self._at is None or now - self._at >= self._cache:
                self._at = now
                try:
                    self.free, self.swap = self._read()
                except Exception:
                    self.free = self.swap = None
                was = self.holding
                self.holding = self._settle(was, decide(was, self.free, self.swap, self.t), now,
                                            seen=self.free is not None or self.swap is not None)
                if self.holding != was:
                    log.warning("host memory: %s new sessions (%s)",
                                "HOLD" if self.holding else "resume", self.describe())
            return self.holding


def short_now(hold_free_pct: float, hold_swap_mb: float, reader=read_host) -> str:
    """The reading that is under a hold threshold right now ("" when none is, or nothing can be read).
    Stateless: the interactive driver asks it under the host start lock, right before it creates a window."""
    if hold_free_pct <= 0:
        return ""
    try:
        free, swap = reader()
    except Exception:
        return ""
    if not decide(False, free, swap, Thresholds(hold_free_pct, 0.0, hold_swap_mb, 0.0)):
        return ""
    f = "?" if free is None else f"{free:.0f}%"
    s = "?" if swap is None else f"{swap:.0f}MB"
    return f"free {f}, swap free {s}"


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


class LoadGate(_Settling):
    """The CPU twin of MemoryGate: shared by every agent thread of one runner, reads the load at most
    every `cache_seconds`, logs only on a state change."""

    _what = "load"

    def __init__(self, t: LoadThresholds, reader=read_load, cache_seconds: float = 30.0, clock=time.monotonic,
                 settle_seconds: float = 0.0):
        self.t, self._read, self._cache, self._clock = t, reader, cache_seconds, clock
        self.settle_seconds = settle_seconds
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

    def check(self, fresh: bool = False) -> bool:
        """True = hold new sessions now. ``fresh`` reads the load now, as MemoryGate.check does right
        before a session starts: resumes went through a cached open gate."""
        if self.t.hold_per_cpu <= 0:
            return False
        with self._lock:
            now = self._clock()
            if fresh or self._at is None or now - self._at >= self._cache:
                self._at = now
                try:
                    self.load, self.cpus = self._read()
                except Exception:
                    self.load = None
                was = self.holding
                self.holding = self._settle(was, decide_load(was, self.load, self.cpus, self.t), now,
                                            seen=self.load is not None)
                if self.holding != was:
                    log.warning("host load: %s new sessions (%s)",
                                "HOLD" if self.holding else "resume", self.describe())
            return self.holding


class StartRamp:
    """After a host hold lifts, sessions come back one at a time: for ``ramp_seconds`` after the latest
    release of any of ``gates`` at most one start per ``interval_seconds`` on the whole host. Shared by
    every agent thread of one runner, like the gates. 0 for either value = no ramp.

    The slot goes to the heaviest wait root first, across lanes. While the host holds or
    ramps, every lane ``claim``s the wait roots it would start now, each with the number of waiting
    cards behind it. A start lighter than the heaviest standing claim is refused, so a root with six
    cards behind it is no longer passed by cards nobody waits on just because their lane asked first.
    A claim lapses when its lane stops renewing it (``claim_seconds``), when the root starts, or when it
    keeps an open slot for ``claim_seconds`` without taking it: a root its lane cannot start does not
    stop the host's other starts for more than that, once per ramp."""

    def __init__(self, interval_seconds: float, ramp_seconds: float, gates=(), clock=time.monotonic,
                 claim_seconds: float | None = None):
        self.interval, self.ramp, self.gates, self._clock = interval_seconds, ramp_seconds, tuple(gates), clock
        self.claim_seconds = 3 * interval_seconds if claim_seconds is None else claim_seconds
        self._lock = threading.Lock()
        self._last: float | None = None
        self._claims: dict[str, tuple[dict[str, int], float]] = {}   # lane -> ({task: weight}, renewed at)
        self._epoch: float | None = None          # the lift the per-ramp state below belongs to
        self._taken: set[str] = set()             # roots started in this ramp
        self._passed: set[str] = set()            # roots that kept an open slot too long in this ramp
        self._kept: tuple[str, float] | None = None   # (root, since): the root an open slot is kept for

    def _lifted(self) -> float | None:
        stamps = [g.released_at for g in self.gates if getattr(g, "released_at", None) is not None]
        return max(stamps) if stamps else None

    def _window(self, now: float) -> float | None:
        """The lift whose ramp is running now, else None."""
        lifted = self._lifted()
        if self.interval <= 0 or self.ramp <= 0 or lifted is None or now - lifted >= self.ramp:
            return None
        return lifted

    def _open(self, now: float) -> bool:
        lifted = self._window(now)
        if lifted is None:
            return True
        return self._last is None or self._last < lifted or now - self._last >= self.interval

    def _head(self, now: float) -> tuple[str, int, str] | None:
        """The heaviest standing claim: (task, weight, lane)."""
        best = None
        for lane, (claims, at) in self._claims.items():
            if now - at >= self.claim_seconds:
                continue
            for task, weight in claims.items():
                if task in self._taken or task in self._passed:
                    continue
                if best is None or weight > best[1]:
                    best = (task, weight, lane)
        return best

    def _first(self, now: float, key: str, weight: int) -> bool:
        """Inside an open ramp slot: may a start of ``weight`` take it, or is it kept for a heavier root.
        The keep is timed from the first start it refused while the slot was free and ends when the
        slot is used or nothing claims it; a root that keeps one slot ``claim_seconds`` is passed."""
        lifted = self._window(now)
        if lifted is None:
            return True
        if self._epoch != lifted:
            self._epoch, self._kept = lifted, None
            self._taken.clear()
            self._passed.clear()
        while True:
            head = self._head(now)
            if head is None:
                self._kept = None                 # nothing claims the slot: whatever kept it is gone
                return True
            if weight >= head[1] or (key and key == head[0]):
                return True
            if self._kept is None or self._kept[0] != head[0]:
                self._kept = (head[0], now)
                log.info("host ramp: next start kept for wait root #%s (%d waiting behind it, lane %s)",
                         head[0][:8], head[1], head[2])
            if now - self._kept[1] < self.claim_seconds:
                return False
            log.info("host ramp: wait root #%s (lane %s) did not start within %gs; the slot goes on",
                     head[0][:8], head[2], self.claim_seconds)
            self._passed.add(head[0])
            self._kept = None

    def claiming(self) -> bool:
        """The host holds or ramps up: lanes should tell which roots they would start."""
        with self._lock:
            return (any(getattr(g, "holding", False) for g in self.gates)
                    or self._window(self._clock()) is not None)

    def claim(self, lane: str, claims: dict[str, int]) -> None:
        """Replace ``lane``'s claims: task -> waiting cards behind it, for the starts it would make now."""
        claims = {t: int(w) for t, w in claims.items() if t and w and w > 0}
        with self._lock:
            if claims:
                self._claims[lane] = (claims, self._clock())
            else:
                self._claims.pop(lane, None)

    def open(self) -> bool:
        """Is a ramp slot free now, for anyone (nothing is taken, claims are not asked)."""
        with self._lock:
            return self._open(self._clock())

    def ready(self, key: str = "", weight: int = 0) -> bool:
        """Would this start be let through now (nothing is taken)."""
        with self._lock:
            now = self._clock()
            return self._open(now) and self._first(now, key, weight)

    def admit(self, key: str = "", weight: int = 0) -> bool:
        """Take the next start, or False while the ramp spaces starts or keeps the slot for a heavier root."""
        with self._lock:
            now = self._clock()
            if not (self._open(now) and self._first(now, key, weight)):
                return False
            self._last = now
            self._kept = None                     # the slot is used: the next one is kept afresh
            if self._window(now) is not None and key:
                self._taken.add(key)
                if weight > 0:
                    log.info("host ramp: start #%s goes first: a wait root with %d waiting behind it",
                             key[:8], weight)
            return True

    def describe(self) -> str:
        out = "one start per %gs for %gs after a host hold" % (self.interval, self.ramp)
        kept = self._kept
        if kept is not None:
            out += "; next start kept for wait root #%s" % kept[0][:8]
        return out
