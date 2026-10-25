"""Turn progress watchdog: decides whether a running turn has stopped making progress.

The runner knows a turn only by its process. A process that is alive says nothing about
the harness behind it: an interactive session can sit on a dialog nobody answers, a
headless harness can hang before its first line, a window can lose its end-of-turn hook.
Until ``max_turn_minutes`` (hours) such a turn counts as running, holds the card's
checkout and a worktree slot, and nobody is told.

What a turn reports (one ``Signal``):

- phase     launcher (the process runs, the harness is not ready), ready (it takes input),
            sent (it accepted the prompt, no reply yet), output (it replied: first output
            seen), done (the turn is over);
- progress  the moment the phase last changed or the harness's own file last grew
            (interactive: the session transcript, via the driver's progress file; headless:
            the turn log);
- external_wait  the harness is busy with a tool, a background task or a spinner, i.e. the
            silence is the agent waiting on something it started.
- held      (interactive) the driver holds the prompt while the session's background agents
            run; ``released_at`` is when it stopped holding. The turn has not asked for output
            during the hold, so first output is not judged then, and counts from the release.

All three limits are minutes, 0 = off, so a runner that sets nothing behaves as before.
"""
from __future__ import annotations

from dataclasses import dataclass

# A reporter that wrote nothing for this long is wedged itself (the interactive driver
# beats every few seconds). Only judged while some limit is on.
REPORTER_SILENT_SECONDS = 600


@dataclass
class Signal:
    source: str                  # "driver" | "log"
    phase: str                   # see the module docstring
    progress_at: float           # epoch seconds
    external_wait: bool = False
    updated: float = 0.0         # when the reporter last wrote (driver only; 0 = not applicable)
    held: bool = False           # the driver holds the prompt for the session's background agents
    released_at: float = 0.0     # when it stopped holding (0 = never held)


def _minutes(seconds: float) -> str:
    return f"{max(0, int(seconds // 60))} min"


def judge(signal: Signal | None, *, started: float, now: float, first_output_minutes: float = 0.0,
          idle_minutes: float = 0.0, external_wait_minutes: float = 0.0) -> str:
    """Why the turn should be ended ("" while it may go on). ``started`` is when the turn began."""
    if signal is None or signal.phase == "done":
        return ""
    if not (first_output_minutes > 0 or idle_minutes > 0 or external_wait_minutes > 0):
        return ""
    where = f"phase {signal.phase}"
    if signal.source == "driver" and signal.updated and now - signal.updated > REPORTER_SILENT_SECONDS:
        return f"the turn driver reported nothing for {_minutes(now - signal.updated)} ({where})"
    # First output is judged only on the driver's report: a headless log carries startup records
    # before the model answers, so its size cannot tell "replied" from "started" (judge it by idle).
    asked = max(started, signal.released_at)          # the prompt could go in from here
    if (first_output_minutes > 0 and signal.source == "driver" and signal.phase != "output" and not signal.held
            and now - asked > first_output_minutes * 60):
        since = "the turn started" if asked == started else "the session's background agents were waited for"
        return f"no first output {_minutes(now - asked)} after {since} ({where})"
    limit = external_wait_minutes if signal.external_wait else idle_minutes
    quiet = now - max(signal.progress_at, started)
    if limit > 0 and quiet > limit * 60:
        what = "the harness is running a tool or waiting on a task" if signal.external_wait else "nothing is running"
        return f"no progress for {_minutes(quiet)} ({where}; {what})"
    return ""
