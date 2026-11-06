"""Conservative owner intent reconciliation; no remote effects or wake decisions."""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

from .outcome import declaration_lines, parse_line
from .selection import parse_time


_DEPENDENCY_CONDITIONS = ("task_done", "access_ready", "contract_ready",
                          "artifact_published", "shipped", "deployed", "accepted")


@dataclass(frozen=True)
class Intent:
    target: str
    comment_id: str
    created_at: str
    line: str
    note: str


def latest_intent(wait: dict, comments: list[dict], agent_id: str,
                  task_id: str, now: dt.datetime) -> tuple[bool, Intent | None]:
    """(handled, intent). A newer ambiguous card declaration holds the old wait.

    Only the newest own comment can supersede a wait. Historical examples,
    edited old comments, multiple declarations and fenced snippets are not intent.
    A bounded newest-comments window need not contain every historical comment.
    """
    own = [c for c in comments if (c.get('author_id') or (c.get('author') or {}).get('id')) == agent_id]
    if not own:
        return False, None
    times = [parse_time(c.get('created_at')) for c in own]
    if any(t is None for t in times):
        return True, None
    latest = max(own, key=lambda c: parse_time(c['created_at']))
    since, created = parse_time(wait.get('since')), parse_time(latest.get('created_at'))
    newest = [c for c, timestamp in zip(own, times) if timestamp == created]
    # A prose comment must not hide a simultaneous newer card declaration.
    # Tied prose alone leaves the ordinary observer unchanged.
    if (len(newest) > 1 and (since is None or created > since)
            and any(re.match(r'^\s*⏳\s*WAIT\s+card:', line)
                    for c in newest for line in declaration_lines(c.get('body') or ''))):
        return True, None
    body = latest.get('body') or ''
    lines = declaration_lines(body)
    if not any(re.match(r'^\s*⏳\s*WAIT\s+card:', line) for line in lines):
        return False, None
    if since is None or created is None:
        return True, None
    if created <= since:
        return False, None
    if (created > now or not latest.get('id') or len(lines) != 1 or '```' in body or '~~~' in body
            or len([t for t in times if t == created]) != 1
            or body.rstrip().splitlines()[-1].strip() != lines[0].strip()):
        return True, None
    outcome = parse_line(lines[0], now, task_id[:8])
    if outcome.kind != 'card':
        return True, None
    if outcome.target == wait.get('target'):
        return False, None
    return True, Intent(outcome.target, latest['id'], latest['created_at'], outcome.line, outcome.note)


def sole_dependency(rows: dict, task_id: str, target: str, *, allow_satisfied: bool = False) -> dict | None:
    """Require one fully known outgoing edge; do not infer default conditions."""
    if not isinstance(rows, dict) or not isinstance(rows.get('outgoing'), list):
        return None
    edges = rows['outgoing']
    if len(edges) != 1 or not isinstance(edges[0], dict):
        return None
    edge = edges[0]
    destination = edge.get('depends_on_task_id')
    if (not edge.get('id') or edge.get('task_id') != task_id
            or edge.get('dependency_type') != 'blocks' or edge.get('condition') != 'task_done'
            or type(edge.get('condition_satisfied')) is not bool
            or (not allow_satisfied and edge['condition_satisfied'])
            or not isinstance(destination, str)
            or not re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', destination)
            or destination == task_id or destination[:8] != target
            or rows.get('has_more') or rows.get('next')):
        return None
    return edge


def blocks_stale_observation(rows: dict, task_id: str, target: str) -> bool:
    """An absent newer declaration grants no authority to ignore formal edges.

    Do not pick an older marker. Valid informational links do not constrain
    observation. A known empty or fully satisfied blocking set may use the
    ordinary observer, as may one fully known block to the current target.
    Unsupported or ambiguous edges preserve WAIT.
    """
    if (not isinstance(rows, dict) or not isinstance(rows.get('outgoing'), list)
            or rows.get('has_more') or rows.get('next')):
        return True
    # Validate even informational links before excluding them: malformed,
    # foreign, unknown or duplicate rows must not weaken the guard.
    edges = []
    seen = set()
    seen_semantic = set()
    for edge in rows['outgoing']:
        destination = edge.get("depends_on_task_id") if isinstance(edge, dict) else None
        if (not isinstance(destination, str)
                or not re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', destination)
                or destination == task_id or edge.get("task_id") != task_id
                or not isinstance(edge.get("id"), str) or not edge["id"].strip()
                or edge.get("dependency_type") not in ("blocks", "relates_to")
                or edge.get("condition") not in _DEPENDENCY_CONDITIONS
                or (edge.get("dependency_type") == "relates_to"
                    and edge.get("condition") != "task_done")
                or type(edge.get("condition_satisfied")) is not bool
                or edge["id"] in seen):
            return True
        semantic = (edge["task_id"], destination, edge["dependency_type"], edge["condition"])
        if semantic in seen_semantic:
            return True
        seen.add(edge["id"])
        seen_semantic.add(semantic)
        if edge["dependency_type"] == "blocks":
            edges.append(edge)
    # Mesh's explicit verdict releases known conditions, including deployment
    # and acceptance. This only admits the ordinary observer, not a wake or
    # retarget; unknown/false verdicts still require the task_done-only path.
    if all(edge["condition_satisfied"] for edge in edges):
        return False
    if len(edges) != 1:
        return True
    # The guard accepts a satisfied edge to the current target too; the normal
    # observer still decides whether that awaited event has actually occurred.
    return sole_dependency({"outgoing": edges}, task_id, target, allow_satisfied=True) is None


def removed_wait_edge(rows: dict, wait: dict, task_id: str) -> str | None:
    """The blocking edge this exact WAIT created, if Mesh now provably lacks it.

    Fails closed (None) on an unreadable/paginated edge set, on any row that is
    not fully well-formed for this task, and while any other blocking edge is
    not known satisfied: a deleted link releases only its own wait, never a
    real remaining blocker. Deletion is not approval and not completion of the
    target; the owner re-decides the next step.
    """
    edge_id = wait.get("edge")
    if not isinstance(edge_id, str) or not edge_id:
        return None
    if (not isinstance(rows, dict) or not isinstance(rows.get("outgoing"), list)
            or rows.get("has_more") or rows.get("next")):
        return None
    seen = set()
    for edge in rows["outgoing"]:
        if not isinstance(edge, dict):
            return None
        destination = edge.get("depends_on_task_id")
        if (not isinstance(edge.get("id"), str) or not edge["id"].strip() or edge["id"] in seen
                or edge.get("task_id") != task_id
                or not isinstance(destination, str)
                or not re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', destination)
                or destination == task_id
                or edge.get("dependency_type") not in ("blocks", "relates_to")
                or edge.get("condition") not in _DEPENDENCY_CONDITIONS
                or type(edge.get("condition_satisfied")) is not bool):
            return None
        seen.add(edge["id"])
        if edge["id"] == edge_id:
            return None
        if edge["dependency_type"] == "blocks" and not edge["condition_satisfied"]:
            return None
    return edge_id
