"""Consumer of Mesh's next-action projection (v1): one prep start per generation.

A task whose only open blockers are typed edges (shipped/deployed/accepted) is held by the
selector as a whole. When the projection says the prep stage is ready and every open edge
gates only later stages, the selector may start the task once per projection generation.
Final acceptance is never granted here: the edges stay open and Mesh keeps computing them.
Default off (``next_action_prep``). Reads only; the single write is a local mark that is made
after the attempt has really been dispatched, so a refused or failed start is retried.
"""
from __future__ import annotations

import json

from .selection import TYPED_CONDITIONS

MARK = "next_action_prep"
VERSION = 1


def _rows(projection: dict, key: str) -> list:
    rows = projection.get(key)
    return rows if isinstance(rows, list) else []


def clearance(task_id: str, edges: list[dict], projection: dict) -> tuple[str, str]:
    """(generation, "") when prep may start now, ("", reason) when it must stay held."""
    if (not isinstance(projection, dict) or type(projection.get("version")) is not int
            or projection["version"] != VERSION):
        return "", "next-action projection version unsupported"
    if projection.get("task_id") != task_id:
        return "", "next-action projection is for another task"
    generation = projection.get("generation")
    if not isinstance(generation, str) or not generation:
        return "", "next-action projection has no generation"
    if projection.get("next_action") != "continue_prep":
        return "", f"next action is {projection.get('next_action')!r}, not continue_prep"
    stage_rows = _rows(projection, "stages")
    order = [s.get("stage") if isinstance(s, dict) else None for s in stage_rows]
    if not order or not all(isinstance(n, str) and n for n in order):
        return "", "next-action stages are malformed"
    if len(set(order)) != len(order):
        return "", "next-action stages are ambiguous (duplicate stage)"
    if "prep" not in order:
        return "", "prep stage is missing"
    if stage_rows[order.index("prep")].get("ready") is not True:
        return "", "prep stage is not ready"
    require_rows = _rows(projection, "requires")
    ids = [r.get("edge_id") if isinstance(r, dict) else None for r in require_rows]
    if not all(isinstance(i, str) and i for i in ids):
        return "", "next-action requirements are malformed"
    if len(set(ids)) != len(ids):
        return "", "next-action requirements are ambiguous (duplicate edge_id)"
    requires = dict(zip(ids, require_rows))
    if not edges:
        return "", "no open edges to release"
    open_ids = [e.get("id") if isinstance(e, dict) else None for e in edges]
    if len(set(map(str, open_ids))) != len(open_ids):
        return "", "open edges are ambiguous (duplicate id)"
    later = set(order[order.index("prep") + 1:])
    for edge in edges:
        if not isinstance(edge, dict) or str(edge.get("condition") or "").strip().lower() not in TYPED_CONDITIONS:
            return "", "an open blocker is not a typed condition"
        eid = edge.get("id")
        req = requires.get(eid) if isinstance(eid, str) else None
        gates = req.get("gates") if req else None
        if (req is None or req.get("released") is not False or not isinstance(gates, list)
                or not gates
                or not all(isinstance(g, str) and g in later for g in gates)):
            return "", "an open edge gates prep or is not in the projection"
    return generation, ""


def dispatched(store, agent: str, task_id: str, generation: str) -> bool:
    """True when this generation already had an attempt that really started."""
    mark = store.mark(agent, task_id, MARK)
    if not mark:
        return False
    try:
        rec = json.loads(mark[0])
    except ValueError:
        return False
    return rec.get("generation") == generation


def record(store, agent: str, task_id: str, generation: str, attempt_id: str) -> None:
    store.mark(agent, task_id, MARK, json.dumps({"generation": generation, "attempt": attempt_id}))
