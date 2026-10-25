"""Which of the agent's tasks may start now, in which order."""
from __future__ import annotations

import datetime as dt
import threading
import time
import weakref
from dataclasses import dataclass

from .mesh import MeshClient, MeshError

PRIORITY_RANK = {"urgent": 0, "high": 1, "medium": 2, "low": 3, "none": 4}
CLOSED = {"done", "cancelled"}


@dataclass
class Policy:
    skip_labels: tuple[str, ...] = ("no-runner",)
    skip_label_prefixes: tuple[str, ...] = ("park:",)
    require_label: str = ""
    project_ids: tuple[str, ...] = ()
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
    if policy.project_ids and task.get("project_id") not in policy.project_ids:
        return "project not served"
    if policy.require_label and policy.require_label not in labels:
        return f"label {policy.require_label} missing"
    for label in labels:
        if label in policy.skip_labels or label.startswith(policy.skip_label_prefixes):
            return f"label {label}"
    if policy.human_gate and task.get("human_gate"):
        return "waiting for a human decision"
    start_after = parse_time(task.get("start_after"))
    if start_after and start_after > now:
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


def satisfied(cats: StatusCategories, blocker: dict) -> bool:
    """A blocker no longer blocks when it is closed, or when what it delivers has
    shipped (Mesh's is_shipped: merged and deployed) while its card still waits
    for acceptance or a closing comment. Dependants wait for the condition, not for
    the bookkeeping."""
    return blocker.get("is_shipped") is True or cats.category(blocker) in CLOSED


def open_blockers(mesh: MeshClient, cats: StatusCategories, task_id: str) -> list[str]:
    """Ids of open tasks this task formally depends on (``blocks`` edges).
    Raises MeshError when the answer is unknown: unknown is not 'no blockers'."""
    return [b["id"] for b in blocking_tasks(mesh, cats, task_id)]


def blocking_tasks(mesh: MeshClient, cats: StatusCategories, task_id: str) -> list[dict]:
    """The open tasks themselves (each with ``id``), as open_blockers reads them."""
    rows = mesh.dependencies(task_id)
    if isinstance(rows, dict):
        rows = rows["outgoing"] if "outgoing" in rows else rows.get("dependencies") or rows.get("items") or []
    blockers = []
    for dep in rows or []:
        if (dep.get("dependency_type") or "blocks").lower() != "blocks" or not dep.get("depends_on_task_id"):
            continue
        bid = dep["depends_on_task_id"]
        try:
            blocker = mesh.task(bid)
        except MeshError as e:
            if e.status in (404, 410):
                continue          # a deleted blocker cannot hold anything
            blocker = _task_via_others(mesh, bid) if e.status == 403 else None
            if blocker is None:
                raise
        blocker = blocker.get("task", blocker) if isinstance(blocker, dict) else {}
        if not satisfied(cats, blocker):
            blockers.append(dict(blocker, id=bid))
    return blockers


def order(tasks: list[dict]) -> list[dict]:
    return sorted(tasks, key=lambda t: (PRIORITY_RANK.get(t.get("priority") or "none", 4),
                                        t.get("created_at") or ""))
