"""The count of review attempts without a verdict, its hand-up and its reset.

A review whose attempt ended without a verdict is fed again, at most ``daily_start_cap`` attempts
that held a round (``Store.review_starts_today``). Past that the card is handed up (the runner's
``_review_cap_hold``) instead of being held silently until UTC midnight. The count starts again
from a reset: the lead's word on the card after the hand-up, or an operator's ``rearm-review``.
Every reset is written as a mark with its author and reason (``review_cap_reset``) and kept in an
audit mark (``review_cap_reset_audit:<id>``).

Only the local count changes here. No checkout, WAIT, credential, dependency or gate is cleared: the
next review start still goes through every ordinary admission check.
"""
from __future__ import annotations

import datetime as dt
import json
from uuid import uuid4

from .selection import parse_time
from .store import OPEN, iso, now

HANDUP = "review_cap_handup"            # the latest hand-up: JSON (id, at, streak, level, to, ...)
RESET = "review_cap_reset"              # the latest reset: JSON (id, at, by, reason, source)
AUDIT = "review_cap_reset_audit:"       # one per reset, never overwritten
HANDUPS_PER_LEVEL = 2                   # hand-ups in a row without a verdict before the next one up


def _json(store, agent: str, task: str, key: str) -> dict | None:
    row = store.mark(agent, task, key)
    if not row or not row[0]:
        return None
    try:
        rec = json.loads(row[0])
    except (TypeError, ValueError):
        return None
    return rec if isinstance(rec, dict) else None


def handup(store, agent: str, task: str) -> dict | None:
    """The latest hand-up record of this agent's review of the task (None: none, or a verdict since)."""
    return _json(store, agent, task, HANDUP)


def reset(store, agent: str, task: str) -> dict | None:
    return _json(store, agent, task, RESET)


def reset_at(store, agent: str, task: str) -> dt.datetime | None:
    rec = reset(store, agent, task)
    return parse_time(rec.get("at")) if rec else None


def floor(store, agent: str, task: str) -> dt.datetime:
    """Where the count starts: UTC midnight, or the last reset when that is later."""
    midnight = now().replace(hour=0, minute=0, second=0, microsecond=0)
    at = reset_at(store, agent, task)
    return at if at is not None and at > midnight else midnight


def pending(store, agent: str, task: str) -> dict | None:
    """The hand-up made for the current count (after its floor): it waits for the lead's word. A
    hand-up from an earlier count (before a reset or before UTC midnight) is history: it only
    carries the streak."""
    rec = handup(store, agent, task)
    at = parse_time(rec.get("at")) if rec else None
    if at is None or at < now().replace(hour=0, minute=0, second=0, microsecond=0):
        return None
    last = reset(store, agent, task)
    if last and (last.get("handup") == rec.get("id") or (parse_time(last.get("at")) or at) > at):
        return None                     # answered: the reset names this hand-up (or came after it)
    return rec


def record_handup(store, agent: str, task: str, rec: dict) -> None:
    store.mark(agent, task, HANDUP, json.dumps(rec, sort_keys=True))


def clear_streak(store, agent: str, task: str) -> bool:
    """A verdict ends the run of hand-ups: the next hand-up goes to the nearest lead again."""
    row = store.mark(agent, task, HANDUP)
    if not row or not row[0]:
        return False
    store.mark(agent, task, HANDUP, "")
    return True


def record_reset(store, agent: str, task: str, *, by: str, reason: str, source: str,
                 at: dt.datetime | None = None) -> dict:
    """Start the count again from now: the current mark and its audit copy."""
    rec = {"id": str(uuid4()), "at": iso(at), "by": str(by)[:200], "reason": str(reason)[:1000],
           "source": source}
    prior = handup(store, agent, task)
    if prior:
        rec["handup"] = prior.get("id") or ""
    body = json.dumps(rec, sort_keys=True)
    store.mark(agent, task, RESET, body)
    store.mark(agent, task, AUDIT + rec["id"], body)
    return rec


def request(store, agent: str, task: str, reason: str, *, by: str, apply: bool = False) -> dict:
    """The operator's ``rearm-review``: start the count of review attempts without a verdict again.

    Eligible only when there is something to reset (a counted attempt since the floor, or a
    hand-up waiting for an answer) and no review attempt of the task is open. ``apply`` False is a
    preview: nothing is written."""
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
        raise ValueError("a nonempty reason of at most 1000 characters is required")
    store.db.execute("BEGIN IMMEDIATE" if apply else "BEGIN")
    try:
        if store.db.execute("SELECT 1 FROM attempts WHERE agent=? AND task_id=? AND state IN (" +
                            ",".join("?" for _ in OPEN) + ") LIMIT 1", (agent, task, *OPEN)).fetchone():
            raise ValueError("task has an open attempt of this agent")
        since = floor(store, agent, task)
        counted = store.review_starts_today(agent, task, since=since)
        waiting = pending(store, agent, task)
        if not counted and not waiting:
            raise ValueError("no review attempts to reset since " + iso(since))
        result = {"eligible": True, "applied": apply, "agent": agent, "task": task,
                  "reason": reason.strip(), "counted_attempts": counted, "since": iso(since),
                  "handed_to": (waiting or {}).get("to") or "", "normal_admission_required": True}
        if apply:
            rec = record_reset(store, agent, task, by=by, reason=reason.strip(), source="rearm-review")
            result["reset"] = rec["id"]
        store.db.execute("COMMIT")
        return result
    except BaseException:
        store.db.execute("ROLLBACK")
        raise
