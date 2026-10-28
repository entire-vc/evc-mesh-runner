"""Audited operator permission for one more start after a start cap.

This changes only the local input hold. Every normal admission check still runs;
no credential, checkout, WAIT, turn budget or dependency is cleared.
"""
from __future__ import annotations

import json
from uuid import uuid4

from .store import OPEN, iso


def _cap(store, agent, task):
    row = store.mark(agent, task, "cap_wait")
    if not row or not row[0]:
        raise ValueError("no pending cap")
    try:
        rec = json.loads(row[0])
    except (ValueError, TypeError):
        raise ValueError("unreadable cap") from None
    if not isinstance(rec, dict) or rec.get("kind") != "start cap" or not rec.get("at"):
        raise ValueError("only a start cap can be rearmed")
    if rec.get("wait") or (store.mark(agent, task, "cap_observation") or ("",))[0]:
        raise ValueError("declared WAIT must resolve normally")
    return row[0]


def pending(store, agent, task):
    """Return a grant only while its exact cap record is still current."""
    row = store.mark(agent, task, "cap_rearm")
    if not row or not row[0]:
        return None
    try:
        grant = json.loads(row[0])
        if (isinstance(grant.get("id"), str) and grant["id"]
                and grant.get("cap") == _cap(store, agent, task)):
            return grant
    except (ValueError, TypeError, AttributeError):
        pass
    return None


def request(store, agent, task, reason, *, apply=False):
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
        raise ValueError("a nonempty reason of at most 1000 characters is required")
    # Serialize with attempt creation and competing operators. No network call in
    # this transaction: Mesh gates are checked by the runner at the actual start.
    store.db.execute("BEGIN IMMEDIATE" if apply else "BEGIN")
    try:
        cap = _cap(store, agent, task)
        if store.db.execute("SELECT 1 FROM attempts WHERE task_id=? AND state IN (" +
                            ",".join("?" for _ in OPEN) + ") LIMIT 1", (task, *OPEN)).fetchone():
            raise ValueError("task has an open attempt")
        if pending(store, agent, task):
            raise ValueError("cap already has a pending rearm")
        # Never issue a second grant for the same cap, even if the first spawn
        # failed before it could clear that cap. Recovery then needs a new event.
        for row in store.db.execute("SELECT value FROM marks WHERE agent=? AND task_id=? "
                                    "AND key LIKE 'cap_rearm_audit:%'", (agent, task)):
            if json.loads(row[0]).get("cap") == cap:
                raise ValueError("this cap has already been rearmed")
        result = {"eligible": True, "applied": apply, "agent": agent, "task": task,
                  "reason": reason.strip(), "normal_admission_required": True}
        if apply:
            grant = {"id": str(uuid4()), "cap": cap, "reason": reason.strip(), "at": iso()}
            store.mark(agent, task, "cap_rearm", json.dumps(grant))
            store.mark(agent, task, "cap_rearm_audit:" + grant["id"], json.dumps(grant))
            result["grant"] = grant["id"]
        store.db.execute("COMMIT")
        return result
    except BaseException:
        store.db.execute("ROLLBACK")
        raise
