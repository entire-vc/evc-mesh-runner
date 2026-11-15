"""Consumer of Mesh's next-action projection (v1): one prep start per generation.

A task whose only open blockers are typed edges (shipped/deployed/accepted) is held by the
selector as a whole. When the projection says the prep stage is ready and every open edge
gates only later stages, the selector may start the task once per projection generation.
Final acceptance is never granted here: the edges stay open and Mesh keeps computing them.
Default off (``next_action_prep``). Reads only; the only write is a local mark, made after the
attempt has really been dispatched (so a refused or failed start is retried) or moved as below.

The generation is Mesh's dedupe key, but it hashes more than this consumer acts on: every edge id,
the blocker's HEAD and its review verdict. A card wait re-declared with the same target and
condition (a new owned edge after the old one was removed at the wake) made a new generation every
1-2 min; every push to the blocker's MR and every SHIP on it made one more empty
wake. The mark therefore keeps the projection's content (stages, requirements and what they
gate and release, next action, without edge ids) and the start or wake is spent on a new content,
once. A generation with the recorded content only moves the mark to itself. A blocker push or
verdict that releases or holds an edge changes ``released`` or ``reason``, so it still counts; one
that changes nothing the consumer acts on does not. A card that must follow every push of its
blocker (a rebase onto a stacked branch) waits on the blocker's merge request, ``wait_for on=mr
until=changed``, not on this wake.
"""
from __future__ import annotations

import hashlib
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


# The fields this consumer acts on. A whitelist: a field Mesh adds later (perhaps another id) never
# turns a pure edge churn back into a wake.
_REQUIRE_FIELDS = ("from_task", "condition", "gates", "released", "reason", "owner")
_PRODUCE_FIELDS = ("to_task", "condition")
_TOP_FIELDS = ("version", "task_id", "next_action", "next_actor")


def _canon_rows(rows: list, fields: tuple[str, ...]) -> list[str]:
    """Rows without their edge ids, as a sorted multiset (Mesh orders them by edge id)."""
    return sorted(json.dumps({f: r.get(f) for f in fields} if isinstance(r, dict) else r,
                             sort_keys=True, default=str) for r in rows)


def fingerprint(projection: dict) -> dict:
    """``content``: a hash of the projection without its generation and edge ids; ``edges``: the
    sorted edge ids of its requires and produces rows."""
    if not isinstance(projection, dict):
        return {"content": "", "edges": []}
    stages = [{"stage": s.get("stage"), "ready": s.get("ready"),
               "blocked_by": _canon_rows(_rows(s, "blocked_by"), _REQUIRE_FIELDS)}
              if isinstance(s, dict) else s for s in _rows(projection, "stages")]
    body = {f: projection.get(f) for f in _TOP_FIELDS}
    body.update(stages=stages, requires=_canon_rows(_rows(projection, "requires"), _REQUIRE_FIELDS),
                produces=_canon_rows(_rows(projection, "produces"), _PRODUCE_FIELDS))
    content = hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()[:16]
    edges = sorted({str(r["edge_id"]) for key in ("requires", "produces") for r in _rows(projection, key)
                    if isinstance(r, dict) and r.get("edge_id")})
    return {"content": content, "edges": edges}


def mark_value(generation: str, attempt_id: str, projection: dict | None = None) -> str:
    rec = {"generation": generation, "attempt": attempt_id}
    if isinstance(projection, dict):
        rec.update(fingerprint(projection))
    return json.dumps(rec)


def _recorded(store, agent: str, task_id: str) -> dict | None:
    mark = store.mark(agent, task_id, MARK)
    if not mark:
        return None
    try:
        rec = json.loads(mark[0])
    except ValueError:
        return None
    return rec if isinstance(rec, dict) else None


def dispatched(store, agent: str, task_id: str, generation: str, projection: dict | None = None) -> bool:
    """True when this generation needs no prep start or wake: it already had one, or (given the
    projection) its content is the recorded one, so only edge ids, the blocker's HEAD or its verdict
    moved. The latter moves the mark to this generation, keeping its attempt and content. A mark
    without content (written by an older runner) compares generations only."""
    rec = _recorded(store, agent, task_id)
    if rec is None:
        return False
    if rec.get("generation") == generation:
        return True
    if projection is None or not isinstance(rec.get("content"), str) or not rec["content"]:
        return False
    now = fingerprint(projection)
    if now["content"] != rec["content"]:
        return False
    store.mark(agent, task_id, MARK, json.dumps(dict(rec, generation=generation, edges=now["edges"])))
    return True


def record(store, agent: str, task_id: str, generation: str, attempt_id: str,
           projection: dict | None = None) -> None:
    store.mark(agent, task_id, MARK, mark_value(generation, attempt_id, projection))
