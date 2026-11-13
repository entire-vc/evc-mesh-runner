"""Which of the agent's tasks may start now, in which order."""
from __future__ import annotations

import datetime as dt
import threading
import time
import weakref
from dataclasses import dataclass

from . import observation_budget as budget
from .mesh import MeshClient, MeshError

PRIORITY_RANK = {"urgent": 0, "high": 1, "medium": 2, "low": 3, "none": 4}
CLOSED = {"done", "cancelled"}


@dataclass
class Policy:
    skip_labels: tuple[str, ...] = ("no-runner",)
    skip_label_prefixes: tuple[str, ...] = ("park:",)
    require_label: str = ""
    # False: the server human_gate flag alone does not hold the task — an operator's
    # start gate decides with richer data (the flag never clears itself after an answer)
    human_gate: bool = True


def parse_time(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        t = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def static_skip_reason(task: dict, policy: Policy, now: dt.datetime) -> str:
    """Reasons that need no extra API call. Empty string means eligible so far."""
    labels = [str(x) for x in task.get("labels") or []]
    if policy.require_label and policy.require_label not in labels:
        return f"label {policy.require_label} missing"
    for label in labels:
        if label in policy.skip_labels or label.startswith(policy.skip_label_prefixes):
            return f"label {label}"
    if policy.human_gate and task.get("human_gate") and not task.get("_gate_decision"):
        # `_gate_decision` is set only by the runner after Mesh itself reported that the
        # ask's addressee answered and THIS agent owns the live ask (gate_owner.py); it
        # admits one bounded turn and never lifts the gate.
        return "waiting for a human decision"
    start_after = parse_time(task.get("start_after"))
    # The same admitted gate-owner answer also passes a future start_after: Mesh reports
    # that delivery for a scheduled card (comment_delivery_outcome), so the runner must not
    # hold it by the schedule. The marker is only ever set together with human_gate, so
    # ordinary work keeps both holds; the schedule itself is left untouched.
    owner_turn = bool(task.get("human_gate") and task.get("_gate_decision"))
    if start_after and start_after > now and not owner_turn:
        return f"start_after {start_after.isoformat(timespec='minutes')}"
    if (task.get("delegation_level") or "") == "supervised":
        return "supervised task"
    expires = parse_time(task.get("checkout_expires") or task.get("checkout_expires_at"))
    if task.get("checked_out_by") and (expires is None or expires > now):
        return "checked out by another writer"
    return ""


# Status ids are unique across a Mesh instance, so what one agent read about a
# project's statuses holds for every agent of this process. An agent outside a
# project can read a task there (a blocker) but not the project's statuses (403);
# without this it could never tell whether that blocker is closed.
_SHARED: dict[str, str] = {}
_SHARED_LOCK = threading.Lock()
# Every agent client of this process: a project one agent may not read (403) is often
# readable by another agent of the same runner.
_READERS: "weakref.WeakSet[StatusCategories]" = weakref.WeakSet()
# Asking every other agent costs one request each: who could read a project or task is
# asked first next time, and what nobody could read is not asked again for a while.
_READ_BY: "weakref.WeakValueDictionary[str, StatusCategories]" = weakref.WeakValueDictionary()
_NOBODY: dict[str, float] = {}           # "p:<project>" / "t:<task>" -> monotonic until
NOBODY_SECONDS = 600
# The cross-tenant uniqueness sweep asks every other reader once, one GET each. At fleet size it can
# outlast one wake's read budget (measured: 65 readers took 76.7 s against 45 s), and a sweep that
# restarts from zero on every attempt never finishes, so the wait is never pinned. What each reader
# answered (the id it read, "" for a denial or a missing card) is kept per short ref for SWEPT_SECONDS:
# the next attempt asks only the readers that have not answered yet. Faults are not remembered.
_SWEPT: "dict[str, tuple[float, weakref.WeakKeyDictionary]]" = {}
SWEPT_SECONDS = NOBODY_SECONDS
# One sweep per short ref at a time: a concurrent attempt waits (within its own read budget) and then
# asks only the readers the first one left unanswered, so no reader is asked twice while answers are fresh.
_SWEEP_LOCKS: dict[str, threading.RLock] = {}


def _read_elsewhere(key: str, me, read, strict: bool = False):
    """``read(reader)`` through another agent of this process: the one that could last
    time first, then the rest; None when nobody can (remembered NOBODY_SECONDS).
    ``strict``: when nobody could and some reader failed with something other than a
    denial or a missing card (a 5xx, a timeout), that error is raised instead: it is
    a fault, not an answer, and is not remembered."""
    with _SHARED_LOCK:
        if time.monotonic() < _NOBODY.get(key, 0):
            return None
        first = _READ_BY.get(key)
        others = [r for r in _READERS if r.mesh is not me]
    order = ([first] if first is not None and first in others else []) + [r for r in others if r is not first]
    fault = None
    for reader in order:
        try:
            got = read(reader)
        except MeshError as e:
            if e.status not in (403, 404, 410):
                fault = fault or e
            continue
        with _SHARED_LOCK:
            _READ_BY[key] = reader
        return got
    if strict and fault is not None:
        raise fault
    with _SHARED_LOCK:
        _NOBODY[key] = time.monotonic() + NOBODY_SECONDS
    return None


AMBIGUOUS = object()     # a short ref that more than one reader resolves to different cards


def _read_unique_elsewhere(key: str, me, read, pinned: str = "", known_id: str = ""):
    """See ``_sweep_unique``; serialized per key (``_SWEEP_LOCKS``). A wait for a running sweep that
    outlasts this caller's read budget fails as a spent budget does: a timeout, never an answer."""
    with _SHARED_LOCK:
        lock = _SWEEP_LOCKS.setdefault(key, threading.RLock())
    obs = budget.current()
    left = -1 if obs is None else max(0.0, obs.deadline - time.monotonic())
    if not lock.acquire(timeout=left):
        raise MeshError(0, "observation budget spent waiting for a sweep in progress", kind="timeout",
                        stage=obs.stage if obs is not None else "")
    try:
        return _sweep_unique(key, me, read, pinned=pinned, known_id=known_id)
    finally:
        lock.release()


def _sweep_unique(key: str, me, read, pinned: str = "", known_id: str = ""):
    """Like ``_read_elsewhere`` for a short card ref, but never guesses across tenants.
    With ``pinned`` (the full UUID resolved earlier) only a card of that id is accepted.
    Unpinned, every other reader is asked once: two different full ids for the same short
    ref return AMBIGUOUS (nothing is read as the target); one id is returned and the caller
    pins it. None when nobody can read it (remembered like ``_read_elsewhere``); a fault of
    a reader that nobody else answered is raised.
    ``known_id``: the id this agent's own grant already read; other readers only look for a
    DIFFERENT card under the same short ref (AMBIGUOUS) and the result is ``{"id": known_id}``."""
    with _SHARED_LOCK:
        if not known_id and time.monotonic() < _NOBODY.get(key, 0):
            return None                    # (a known id still compares: access may have changed, and it pins once)
        others = [r for r in _READERS if r.mesh is not me]
        until, answers = _SWEPT.get(key, (0.0, None))
        if answers is None or time.monotonic() >= until:
            answers = weakref.WeakKeyDictionary()
            _SWEPT[key] = (time.monotonic() + SWEPT_SECONDS, answers)
        remembered = {r: answers[r] for r in others if r in answers}
    found: dict[str, tuple] = {known_id: (None, {"id": known_id})} if known_id else {}
    fault = None
    for reader in others:
        if reader in remembered:
            tid = remembered[reader]           # answered in an earlier attempt: not asked again
            if not tid or (pinned and tid != pinned):
                continue
            found.setdefault(tid, (reader, None))   # the card itself is read fresh below if it is the result
            if pinned:
                break
            continue
        try:
            got = read(reader)
        except MeshError as e:
            if e.status not in (403, 404, 410):
                fault = fault or e
            else:
                with _SHARED_LOCK:
                    answers[reader] = ""
            continue
        got = got.get("task", got) if isinstance(got, dict) else got
        tid = str((got or {}).get("id") or "")
        with _SHARED_LOCK:
            answers[reader] = tid
        if pinned and tid != pinned:
            continue                           # another tenant's card with the same short ref
        found.setdefault(tid, (reader, got))
        if pinned:
            break
    if len(found) > 1:
        return AMBIGUOUS
    if fault is not None and not pinned:
        raise fault                        # a reader that failed could hold the colliding card: identity not established
    if known_id:
        return {"id": known_id}            # every other reader answered and none sees another card
    if found:
        expected, (reader, got) = next(iter(found.items()))
        if got is None:
            # the answer was remembered, the card was not: a stale copy must not decide a wake
            try:
                got = read(reader)
            except MeshError as e:
                if e.status not in (403, 404, 410):
                    raise
                got = None
            got = got.get("task", got) if isinstance(got, dict) else got
            if str((got or {}).get("id") or "") != expected:
                # its access changed, or the ref now reads as another card: never return a card
                # other than the one remembered (and pinned); ask that reader again next time
                with _SHARED_LOCK:
                    answers.pop(reader, None)
                return None
        if reader is not None:
            with _SHARED_LOCK:
                _READ_BY[key] = reader
        return got
    if fault is not None:
        raise fault
    with _SHARED_LOCK:
        _NOBODY[key] = time.monotonic() + NOBODY_SECONDS
    return None


class StatusCategories:
    """status_id -> category, cached per project."""

    def __init__(self, mesh: MeshClient):
        self.mesh, self._maps = mesh, {}
        with _SHARED_LOCK:
            _READERS.add(self)
            _NOBODY.clear()                  # a new agent may read what nobody could

    def _via_others(self, pid: str) -> list[dict] | None:
        """The project's statuses read by another agent of this process, or None."""
        return _read_elsewhere(f"p:{pid}", self.mesh, lambda r: r.mesh.statuses(pid))

    def category(self, task: dict) -> str:
        if task.get("status_category"):
            return task["status_category"]
        pid = task.get("project_id") or ""
        if pid not in self._maps:
            try:
                statuses = self.mesh.statuses(pid)
            except MeshError as e:
                with _SHARED_LOCK:
                    known = _SHARED.get(task.get("status_id") or "")
                if e.status != 403:
                    raise
                if known is not None:
                    return known            # not a member of that project: another agent's reading
                statuses = self._via_others(pid)   # or ask another agent of this process now
                if statuses is None:
                    raise
                with _SHARED_LOCK:
                    _SHARED.update({st.get("id"): st.get("category", "") for st in statuses if st.get("id")})
                return next((st.get("category", "") for st in statuses if st.get("id") == task.get("status_id")), "")
            self._maps[pid] = {st.get("id"): st.get("category", "") for st in statuses}
            with _SHARED_LOCK:
                _SHARED.update({k: v for k, v in self._maps[pid].items() if k})
        return self._maps[pid].get(task.get("status_id"), "")


def _task_via_others(mesh: MeshClient, task_id: str) -> dict | None:
    """A task this agent may not read (403), read by another agent of this process."""
    return _read_elsewhere(f"t:{task_id}", mesh, lambda r: r.mesh.task(task_id))


# Edge conditions whose False verdict is authoritative (see satisfied).
TYPED_CONDITIONS = frozenset({"shipped", "deployed", "accepted"})


def _released_by_state(condition: str, blocker: dict, cats: StatusCategories) -> bool:
    """The server's release table (Mesh ``TaskDependency.Released``) applied to the blocker's
    own state, for an edge whose verdict the server did not give.
    is_shipped releases only the ``shipped`` / ``deployed`` conditions; a ``task_done`` edge
    (and the evidence-only conditions) wait for done or cancelled. A recorded deploy is not
    readable here, so ``deployed`` is released by is_shipped alone: unknown stays blocked."""
    condition = condition.strip().lower()
    shipped = blocker.get("is_shipped") is True
    category = cats.category(blocker)
    if condition == "shipped":
        return shipped or category == "done"
    if condition == "deployed":
        return shipped
    if condition == "accepted":
        return category == "done"
    return category in CLOSED


def satisfied(cats: StatusCategories, blocker: dict, edge: dict | None = None) -> bool:
    """A blocker no longer blocks when the server's edge verdict says so.

    ``condition_satisfied`` on the dependency row is Mesh's authoritative verdict: True
    releases. False stays False for the typed conditions (shipped, deployed, accepted) and
    is never turned into True by ``is_shipped``. For ``task_done``/untyped edges an older
    server computes False from evidence only, so a closed blocker still releases them. With
    no verdict the blocker's state is read through the server's own table (_released_by_state);
    unknown is not fulfilled. Without an edge the condition is ``task_done``."""
    verdict = edge.get("condition_satisfied") if isinstance(edge, dict) else None
    if verdict is True:
        return True
    condition = str((edge or {}).get("condition") or "")
    if verdict is False and condition.strip().lower() in TYPED_CONDITIONS:
        return False
    if verdict is False:
        return cats.category(blocker) in CLOSED
    return _released_by_state(condition, blocker, cats)


def open_blockers(mesh: MeshClient, cats: StatusCategories, task_id: str) -> list[str]:
    """Ids of open tasks this task formally depends on (``blocks`` edges).
    Raises MeshError when the answer is unknown: unknown is not 'no blockers'."""
    return [b["id"] for b in blocking_tasks(mesh, cats, task_id)]


def blocking_tasks(mesh: MeshClient, cats: StatusCategories, task_id: str) -> list[dict]:
    """The open tasks themselves (each with ``id``), as open_blockers reads them."""
    return [blocker for blocker, _edge in blocking_edges(mesh, cats, task_id)]


def blocking_edges(mesh: MeshClient, cats: StatusCategories, task_id: str) -> list[tuple[dict, dict]]:
    """(open blocker with ``id``, its dependency edge row) for every blocking edge."""
    rows = mesh.dependencies(task_id)
    if isinstance(rows, dict):
        rows = rows["outgoing"] if "outgoing" in rows else rows.get("dependencies") or rows.get("items") or []
    blockers = []
    for dep in rows or []:
        if (dep.get("dependency_type") or "blocks").lower() != "blocks" or not dep.get("depends_on_task_id"):
            continue
        bid = dep["depends_on_task_id"]
        if dep.get("condition_satisfied") is True:
            continue                  # released by the server: evidence or the blocker's state, no read needed
        try:
            blocker = mesh.task(bid)
        except MeshError as e:
            if e.status in (404, 410):
                # The edge is still listed and not released: a deleted or inaccessible blocker is
                # a tombstone, not a removed edge. Held and named with the reason; only a released
                # verdict or an edge no longer listed frees the dependant.
                blockers.append(({"id": bid, "unavailable": e.status}, dep))
                continue
            blocker = _task_via_others(mesh, bid) if e.status == 403 else None
            if blocker is None:
                raise
        blocker = blocker.get("task", blocker) if isinstance(blocker, dict) else {}
        if not satisfied(cats, blocker, dep):
            blockers.append((dict(blocker, id=bid), dep))
    return blockers


def order(tasks: list[dict]) -> list[dict]:
    return sorted(tasks, key=lambda t: (PRIORITY_RANK.get(t.get("priority") or "none", 4),
                                        t.get("created_at") or ""))
