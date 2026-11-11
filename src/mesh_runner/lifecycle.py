"""The attempt lifecycle as one table.

An attempt is one execution of one task by one agent (see ``store``). Its state changes in many places
of the runner; this module is the single statement of which changes exist. ``Store.update`` asks it on
every state write:

    starting --> running --> waiting --> ready --> running ...
        |           |           |          |
        v           v           v          v
     aborted    completed   completed   completed          (the task left the lane, or the work ended)
                 failed                   failed           (gave up)
                 unknown                  unknown          (the process outcome cannot be established)

Three verdicts:

``ok``        the change is in the table.
``refused``   the attempt has ended (completed, failed, aborted, aborted-notified). An ended attempt never
              runs again: a new execution of the task is a new attempt. The write is not applied.
``unlisted``  a change between live states that the table does not name. In production it is applied and
              recorded (an ``unlisted_transition`` event and a warning), because wedging a live attempt on a
              rule this table forgot would cost more than the oddity. Under ``STRICT`` (the test suite) it
              raises, which is how the table is kept complete.

The table was drawn from the code and checked against a production attempt registry: every change observed there is
listed.
"""
from __future__ import annotations

STARTING, RUNNING, WAITING, READY, UNKNOWN = "starting", "running", "waiting", "ready", "unknown"
COMPLETED, FAILED, ABORTED, ABORTED_NOTIFIED = "completed", "failed", "aborted", "aborted-notified"

LIVE = (STARTING, RUNNING, WAITING, READY, UNKNOWN)
ENDED = (COMPLETED, FAILED, ABORTED, ABORTED_NOTIFIED)
STATES = LIVE + ENDED

# from -> the states it may change to. A state may always be written again unchanged (a refresh of the row).
TRANSITIONS: dict[str, frozenset[str]] = {
    # reserved, process spawned or about to be: the turn begins, the start is held and retried later,
    # the spawn outcome is unknown, the task is gone before the first turn, or the start is refused
    STARTING: frozenset({RUNNING, READY, UNKNOWN, COMPLETED, FAILED, ABORTED}),
    # a turn runs: it ends in a wait, in "continue" (ready), with the task gone, in a give-up,
    # or with a delivery whose outcome cannot be established; or the native binary could not be
    # executed after the launch was registered, so the turn never began and the start is held again
    RUNNING: frozenset({WAITING, READY, UNKNOWN, COMPLETED, FAILED, STARTING}),
    # parked on an event: the event arrives, the task leaves the lane, the wait is given up, the attempt
    # is withdrawn before it ever resumes, or a registration turns out unresolvable
    WAITING: frozenset({READY, RUNNING, UNKNOWN, COMPLETED, FAILED, ABORTED}),
    # wake condition met: resumed (directly, or through a fresh spawn), parked again without a turn,
    # gone, given up, or a resume delivery left unknown
    READY: frozenset({RUNNING, STARTING, WAITING, UNKNOWN, COMPLETED, FAILED, ABORTED}),
    # unknown delivery: reconciled to ready (retry) or to a turn that did begin, or ended
    UNKNOWN: frozenset({READY, RUNNING, WAITING, COMPLETED, FAILED, ABORTED}),
    # ended. The one move after an end is the bookkeeping mark that its abort notice was posted.
    ABORTED: frozenset({ABORTED_NOTIFIED}),
    ABORTED_NOTIFIED: frozenset(),
    COMPLETED: frozenset(),
    FAILED: frozenset(),
}

OK, REFUSED, UNLISTED = "ok", "refused", "unlisted"

# The test suite sets this (tests/fakes.py): an unlisted change raises instead of being recorded.
STRICT = False


class UnlistedTransition(AssertionError):
    pass


def seed(attempt) -> bool:
    """Under ``STRICT`` only: a test builds its scenario by putting a just-created attempt, which never had a
    process, a turn or a session, straight into the state the scenario starts from. That is a fixture, not
    a change the runner makes; production records the same write as unlisted."""
    return (attempt.state == STARTING and not attempt.pid and not attempt.turns and not attempt.session_id)


def check(before: str, after: str) -> str:
    """``ok``, ``refused`` or ``unlisted`` for a state write ``before -> after``."""
    if after == before:
        return OK
    if after in TRANSITIONS.get(before, frozenset()):
        return OK
    if before in ENDED:
        return REFUSED
    return UNLISTED
