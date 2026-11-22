"""A lane's sign of life while its tick is still running.

A lane's row in status.json used to be computed only when its whole tick returned, so its
``tick_at`` aged with the tick. A lane whose tick checkpoints and restores many worktrees, or
starts several sessions in a row, published a row ten or fifteen minutes old although it was
working all along, and every reader that requires a fresh row took it for an unknown state.

The lane thread binds a callback for the duration of its tick (``bind``/``unbind``). Long
operations call ``beat()`` between their units of work: a git command, a repository, an
attempt, a slice of a subprocess or lock wait. The callback recomputes and republishes the
lane's row at most once per ``interval`` seconds. ``step(name)`` also marks a phase boundary of
the tick, so a slow tick can name the phases that took its time.

Everything here is per thread. A beat on any other thread (cleanup workers, gate workers, the
status writer) is a no-op: the callback reads the lane's own database connection, which belongs
to the lane's thread. A beat never raises: a failing callback is logged and the work it reports
on goes on.
"""
from __future__ import annotations

import logging
import subprocess
import threading
import time
from typing import Callable

log = logging.getLogger("mesh_runner")

_local = threading.local()
_clock = time.monotonic          # replaced in tests
_wait_clock = time.monotonic     # real waits keep real deadlines, whatever a test does to the clock
SLICE_SECONDS = 10.0             # longest single wait of a sliced subprocess wait between two beats


def bind(callback: Callable[[], None] | None, interval: float, *, eager: bool = False) -> None:
    """Start a tick on this thread. ``callback`` republishes the lane's row (None: steps are
    still timed, nothing is published). ``eager``: the lane has no row of its own yet, so
    ``ready()`` publishes one at once instead of after ``interval``."""
    _local.callback = callback
    _local.interval = max(0.0, float(interval))
    _local.last = _local.started = _local.step_at = _clock()
    _local.step = "start"
    _local.steps = {}
    _local.busy = False
    _local.eager = bool(eager)


def ready() -> None:
    """The lane has read what its row needs (its Mesh identity, which carries the cap). A tick
    bound ``eager`` publishes the row now: the first beat would come only ``interval`` seconds
    after the bind, and until then every reader of a just-started runner finds the lane without
    cap, pause state and attempts, an unknown state. Once per bind; a no-op otherwise."""
    if not bound() or not getattr(_local, "eager", False):
        return
    _local.eager = False
    _local.last = float("-inf")
    beat()


def unbind() -> dict[str, float]:
    """End this thread's tick: the seconds spent in each named step (empty when none was bound)."""
    if getattr(_local, "steps", None) is None:
        return {}
    _close_step()
    steps = dict(_local.steps)
    _local.callback = _local.steps = None
    return steps


def bound() -> bool:
    return getattr(_local, "steps", None) is not None


def current_step() -> str:
    return getattr(_local, "step", "") if bound() else ""


def _close_step() -> None:
    now = _clock()
    _local.steps[_local.step] = _local.steps.get(_local.step, 0.0) + (now - _local.step_at)
    _local.step_at = now


def step(name: str) -> None:
    """Mark the start of the tick phase ``name`` and beat."""
    if not bound():
        return
    _close_step()
    _local.step = name
    beat()


def beat() -> None:
    """Republish the lane's row if ``interval`` seconds passed since the last publication."""
    callback = getattr(_local, "callback", None)
    if callback is None or _local.busy:
        return
    now = _clock()
    if now - _local.last < _local.interval:
        return
    _local.last = now           # before the call: a failing callback is not retried in a loop
    _local.busy = True          # the callback's own reads never beat into themselves
    try:
        callback()
    except Exception as e:      # a sign of life must never break the work it reports on
        log.warning("progress beat failed: %s: %s", type(e).__name__, str(e)[:200])
    finally:
        _local.busy = False


def slices(total: float, size: float | None = None):
    """Wait budgets of at most ``size`` (SLICE_SECONDS) seconds that add up to ``total``; beats between them.
    A wait split this way keeps its total deadline and lets a bound lane publish meanwhile."""
    deadline = _wait_clock() + total
    while True:
        remaining = deadline - _wait_clock()
        if remaining <= 0:
            return
        yield min(SLICE_SECONDS if size is None else size, remaining)
        beat()


def communicate(proc: subprocess.Popen, input=None, timeout: float | None = None):
    """``proc.communicate`` cut into ``slices``: the same deadline, a beat between slices. A retried
    communicate loses no output; the input is sent by the first call only. Raises
    ``subprocess.TimeoutExpired`` at the deadline and leaves the process to the caller."""
    if timeout is None:
        return proc.communicate(input)
    for budget in slices(timeout):
        try:
            return proc.communicate(input, timeout=budget)
        except subprocess.TimeoutExpired:
            input = None
    raise subprocess.TimeoutExpired(proc.args, timeout)


def run(argv, *, timeout: float, input=None, **kwargs) -> subprocess.CompletedProcess:
    """``subprocess.run(argv, capture_output=True, timeout=timeout, input=input, **kwargs)`` with the
    wait cut into slices, so a lane whose tick runs a slow command keeps publishing its row. Same
    deadline; on timeout the child is killed and reaped and ``TimeoutExpired`` raised, as
    ``subprocess.run`` does."""
    beat()
    if input is not None:
        kwargs["stdin"] = subprocess.PIPE
    with subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs) as proc:
        try:
            out, err = communicate(proc, input, timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            raise
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)
