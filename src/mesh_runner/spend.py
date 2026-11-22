"""A turn's own spend out of the session totals the harness reports.

Claude restores its cost counter on ``--resume``: the ``result`` event's ``total_cost_usd`` and
``modelUsage`` are cumulative over the whole session, while ``usage`` covers this turn only (and
leaves out subagent calls, which ``modelUsage`` includes). The turn's cost and tokens are therefore
the increment of those totals over the previous turn of the same session; the first turn of a
session is taken as it is. A total that went down is a counter the harness started afresh, so it
is taken as it is too.

The previous model totals live in a store mark per session, so the increment survives a runner restart.
The cost base is ``turn_cost.session_base``, shared with the API ledger. The mark keeps the baseline it
measured a turn against: settling the same turn again (retry, restart before Mesh answered) measures it
against the same baseline and reports the same body.
"""
from __future__ import annotations

import json

from . import turn_cost

# report counter -> Claude ``modelUsage`` counter
COUNTERS = {"input_tokens": "inputTokens", "output_tokens": "outputTokens",
            "cache_read_input_tokens": "cacheReadInputTokens",
            "cache_creation_input_tokens": "cacheCreationInputTokens"}
_PRIMARY = ("input_tokens", "output_tokens")    # missing = unknown; a missing cache bucket is 0
_PLACES = 4                                      # Mesh keeps estimated_cost to 4 places
_BASES = 8                                       # attempts per session whose start totals are kept
_FROM_PREV = object()                            # completion lines chain costs from their own logs


def _num(v) -> float | None:
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 else None


def totals(cost_usd, model_usage) -> dict:
    """The session totals a turn reported: ``{"cost": float|None, "models": {model: {counter: n}}}``."""
    models: dict[str, dict[str, float | None]] = {}
    for name, m in (model_usage or {}).items() if isinstance(model_usage, dict) else ():
        if isinstance(m, dict):
            models[str(name)] = {k: _num(m.get(src)) if k in _PRIMARY else (_num(m.get(src)) or 0)
                                 for k, src in COUNTERS.items()}
    return {"cost": _num(cost_usd), "models": models}


def _restarted(prev: dict, cur: dict) -> bool:
    if cur["cost"] is not None and prev.get("cost") is not None and cur["cost"] < prev["cost"]:
        return True
    for name, m in cur["models"].items():
        before = (prev.get("models") or {}).get(name) or {}
        if any(v is not None and before.get(k) is not None and v < before[k] for k, v in m.items()):
            return True
    return False


def increment(prev: dict | None, cur: dict, usage: dict | None, cost_base=_FROM_PREV) -> tuple[float | None, dict]:
    """This turn's (cost, usage) given the session's previous totals (None: first turn).
    ``cost_base`` is the canonical ``turn_cost`` base; completion lines omit it and chain ``prev``.
    Without ``modelUsage`` the turn's own ``usage`` stands."""
    base = {} if prev is None or _restarted(prev, cur) else prev
    cost = cur["cost"]
    if cost_base is _FROM_PREV:
        cost_base = base.get("cost")
    if cost is not None and cost_base is not None:
        # on Mesh's 4-place grid, so the reported turns add up to the session total exactly
        cost = round(round(cost, _PLACES) - round(cost_base, _PLACES), _PLACES)
    if not cur["models"]:
        return cost, dict(usage or {})
    turn: dict[str, float | None] = {k: 0 for k in COUNTERS}
    for name, m in cur["models"].items():
        before = (base.get("models") or {}).get(name) or {}
        for k, v in m.items():
            if v is None or turn[k] is None:
                turn[k] = None
                continue
            turn[k] += v - (before.get(k) or 0)
    return cost, {k: int(v) if v is not None else None for k, v in turn.items()}


def merged(prev: dict | None, cur: dict) -> dict:
    """Totals to measure the next turn against: what this turn did not report stays as it was."""
    prev = prev or {"cost": None, "models": {}}
    return {"cost": cur["cost"] if cur["cost"] is not None else prev.get("cost"),
            "models": {**(prev.get("models") or {}), **cur["models"]}}


def _state(store, agent: str, task_id: str, session_id: str) -> dict:
    seen = store.mark(agent, task_id, f"spend_total:{session_id}")
    return json.loads(seen[0]) if seen and seen[0] else {}


def turn_spend(store, agent: str, task_id: str, attempt_id: str, session_id: str, turn: str,
               cost_usd, model_usage, usage: dict | None) -> tuple[float | None, dict, float | None]:
    """The turn's own (cost, usage) and the session cost base shared with the API ledger."""
    cur = totals(cost_usd, model_usage)
    base = turn_cost.session_base(store, agent, task_id, turn, session_id, cur["cost"])
    if not session_id or (cur["cost"] is None and not cur["models"]):
        return cost_usd, dict(usage or {}), base
    state = _state(store, agent, task_id, session_id)
    prev = state.get("prev") if state.get("turn") == turn else state.get("totals")
    bases = state.get("bases") or {}
    if attempt_id not in bases:                 # the session's totals before this attempt's first turn
        bases = dict(list(bases.items())[-(_BASES - 1):])
        bases[attempt_id] = prev
    store.mark(agent, task_id, f"spend_total:{session_id}",
               json.dumps({"turn": turn, "prev": prev, "totals": merged(prev, cur), "bases": bases}))
    # ``turn_cost`` is the one cost baseline for both Mesh and the API pool. ``prev`` still carries
    # the raw totals needed to detect a restarted model counter and to rebuild completion lines.
    cost, turn_usage = increment(prev, cur, usage, base)
    return cost, turn_usage, base


def attempt_base(store, agent: str, task_id: str, attempt_id: str, session_id: str) -> dict | None:
    """The session's totals before the attempt's first turn (None: the attempt started it)."""
    return (_state(store, agent, task_id, session_id).get("bases") or {}).get(attempt_id)


def attempt_spend(results, base_for) -> list[tuple[float | None, dict]]:
    """Each turn's own (cost, usage) for an attempt's turn results in order: chained within the
    attempt, the first turn of a session measured against ``base_for(session_id)``, its totals
    before the attempt. Results without session totals stand as they are and read no state."""
    last: dict[str, dict | None] = {}
    out = []
    for r in results:
        cur = totals(r.cost_usd, getattr(r, "model_usage", None))
        sid = r.session_id
        if not sid or (cur["cost"] is None and not cur["models"]):
            out.append((r.cost_usd, dict(r.usage or {})))
            continue
        if sid not in last:
            last[sid] = base_for(sid)
        out.append(increment(last[sid], cur, r.usage))
        last[sid] = merged(last[sid], cur)
    return out
