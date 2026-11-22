"""The cost of one turn out of the harness's session-wide counter.

Claude CLI restores its cost counter on ``--resume``: ``result.total_cost_usd`` of a resumed turn is the
running total of the whole session, not the turn's own cost. A turn's cost is that total minus the total
the previous settled turn of the same session reported. The last total per task is kept in a store mark,
so a runner restart between turns keeps the base, and a turn settled again (retry, restart) gets the
same base it got the first time.

A first turn, a new session, a missing base or a total below the base (a counter that started over)
counts the whole total: an unknown base never makes a turn cheaper than the harness reported."""
from __future__ import annotations

import json

MARK = "cost_base"


def _amount(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value >= 0 else None


def session_base(store, agent: str, task_id: str, turn: str, session_id: str, total) -> float | None:
    """The session total before ``turn``; None when the turn reported no total.

    Records ``total`` as the base of the next turn of ``session_id``. Idempotent per ``turn``."""
    total = _amount(total)
    if total is None:
        return None
    got = store.mark(agent, task_id, MARK)
    try:
        rec = json.loads(got[0]) if got and got[0] else {}
    except ValueError:
        rec = {}
    if not isinstance(rec, dict):
        rec = {}
    if rec.get("turn") == turn and rec.get("session") == session_id and _amount(rec.get("before")) is not None:
        return _amount(rec.get("before"))       # this turn was settled before: the same base
    before = 0.0
    prev = _amount(rec.get("total"))
    if session_id and rec.get("session") == session_id and prev is not None and prev <= total:
        before = prev
    store.mark(agent, task_id, MARK, json.dumps(
        {"session": session_id, "turn": turn, "before": before, "total": total}))
    return before
