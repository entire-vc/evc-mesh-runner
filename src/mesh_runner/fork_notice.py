"""Keep macOS's malloc stack logging notice out of the Runner's log.

The Runner's stderr is launchd's StandardErrorPath, ``~/.mesh-runner/runner.log``. On macOS, the
first critical memory pressure or process memory limit event a process receives makes libmalloc load
MallocStackLogging.framework (``malloc_memory_event_handler`` -> ``malloc_register_stack_logger``).
From then on, the framework's fork-child hook runs in every child the process forks and, because stack
logging was never turned on, prints

    Python(<child pid>) MallocStackLogging: can't turn off malloc stack logging because it was not enabled.

It writes to fd 2 before ``subprocess`` redirects the child's streams, so the line lands in
runner.log whatever ``stderr=`` a call passes: once per git, gate, account or diagnostic child, until
the Runner restarts. No environment variable sets it, and ``MallocDebugReport`` does not reach it.

Turning lite stack logging on and straight off once, at start, leaves the framework in its "was
enabled, now off" state, and in that state the fork-child hook is silent. Later memory events then
find the framework already loaded. Measured on macOS 27: no recording stays on, allocation speed and
footprint are unchanged, two extra malloc zones, nothing written to stderr or $TMPDIR. The behaviour
is the closed-source framework's: if a macOS update changes it, the notice comes back and nothing
breaks.
"""
from __future__ import annotations

import ctypes
import sys

STACK_LOGGING_MODE_LITE = 4      # stack_logging_mode_type in libmalloc's private/stack_logging.h


def quiet_fork_stack_logging_notice() -> str:
    """Pre-arm stack logging off in this process; returns what happened, for one startup log line.

    Call it in the image that forks, before it starts threads or children. Never raises."""
    if sys.platform != "darwin":
        return "skipped: not macOS"
    try:
        libc = ctypes.CDLL(None)
        turn_on = libc.turn_on_stack_logging
        turn_off = libc.turn_off_stack_logging
        turn_on.argtypes = [ctypes.c_int]
        turn_on.restype = ctypes.c_bool
        turn_off.argtypes = []
        turn_off.restype = None
        if not turn_on(STACK_LOGGING_MODE_LITE):
            return "unavailable: turn_on_stack_logging refused"
        turn_off()
    except (OSError, AttributeError) as exc:
        return f"unavailable: {type(exc).__name__}"
    return "armed: malloc stack logging turned on and off"
