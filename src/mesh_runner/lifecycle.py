"""The attempt lifecycle as one table.

An attempt is one execution of one task by one agent (see ``store``). The runner decides when its state
changes in many places, but it writes every change through one object, ``AttemptStateMachine`` (one method
per target state: ``to_running``, ``to_waiting``, ``to_ready`` ...); no other code passes ``state=`` to the
store. This module is also the single statement of which changes exist. ``Store.update`` asks it on every
state write:

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


class AttemptStateMachine:
    """The one writer of an attempt's ``state``: every other module moves an attempt through these methods
    (``tests/test_state_writes.py`` enforces it). Each method is a pass-through to the store and decides nothing:

    * without ``marks`` it is ``store.update(a, state=..., **changes)``;
    * with ``marks`` (a dict, possibly empty) it is ``store.update_with_marks(a, marks, state=..., **changes)``,
      the transaction that commits the state together with its observation receipts.

    ``changes`` carries the store's own keywords unchanged (``expect=``, and with ``marks`` also ``expect_wait=``
    and ``validate=``) next to the columns written with the state. The return value is the store's, as is the
    ``check`` of the change against ``TRANSITIONS``. The attempt is created in ``starting`` by ``Store.create``.
    """

    def __init__(self, store) -> None:
        self.store = store

    def to(self, a, state: str, marks: dict[str, str] | None = None, **changes) -> bool:
        """Move ``a`` to ``state`` (for a caller that holds the target state in a variable, e.g. an end)."""
        if marks is None:
            return self.store.update(a, state=state, **changes)
        return self.store.update_with_marks(a, marks, state=state, **changes)

    def to_starting(self, a, marks: dict[str, str] | None = None, **changes) -> bool:
        """Reserve or re-reserve the start: the prompt intent is recorded before the spawn, or the native binary
        could not be executed after the launch was registered and the start is held again."""
        return self.to(a, STARTING, marks, **changes)

    def to_running(self, a, marks: dict[str, str] | None = None, **changes) -> bool:
        """A turn begins (spawn or resume), or a turn whose delivery was unknown is shown to have begun."""
        return self.to(a, RUNNING, marks, **changes)

    def to_waiting(self, a, marks: dict[str, str] | None = None, **changes) -> bool:
        """Park on an event: the turn ended in a wait, a ready attempt is parked again without a turn, or a native
        delivery is held for repair. The slot is freed; session and workspaces are kept."""
        return self.to(a, WAITING, marks, **changes)

    def to_ready(self, a, marks: dict[str, str] | None = None, **changes) -> bool:
        """Needs a slot to (re)run: the wake condition is met, the turn asked to continue, a turn is sent back
        with guidance, or a start or resume is held locally and retried later."""
        return self.to(a, READY, marks, **changes)

    def to_unknown(self, a, marks: dict[str, str] | None = None, **changes) -> bool:
        """The outcome of a spawn or a delivery cannot be established, or a registration is unresolvable:
        the attempt keeps occupying its slot until it is reconciled or resolved by the owner."""
        return self.to(a, UNKNOWN, marks, **changes)

    def to_completed(self, a, marks: dict[str, str] | None = None, **changes) -> bool:
        """End: the task left the agent's working statuses."""
        return self.to(a, COMPLETED, marks, **changes)

    def to_failed(self, a, marks: dict[str, str] | None = None, **changes) -> bool:
        """End: gave up (no outcome, repeated crashes); the workspace is kept for a human."""
        return self.to(a, FAILED, marks, **changes)

    def to_aborted(self, a, marks: dict[str, str] | None = None, **changes) -> bool:
        """End: the attempt never started (checkout refused, no workspace, preflight refused) or was withdrawn."""
        return self.to(a, ABORTED, marks, **changes)

    def to_aborted_notified(self, a, marks: dict[str, str] | None = None, **changes) -> bool:
        """The bookkeeping mark after an abort: its notice was posted on the task."""
        return self.to(a, ABORTED_NOTIFIED, marks, **changes)
