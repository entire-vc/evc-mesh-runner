"""Card-wait edge checks: the formal dependency a card WAIT observes; no remote effects or wake decisions."""
from __future__ import annotations

import re


_DEPENDENCY_CONDITIONS = ("task_done", "access_ready", "contract_ready",
                          "artifact_published", "shipped", "deployed", "accepted")


def owned_decision_wait(edge: dict, receipt: dict, request_id: str, source_id: str,
                        recipient_id: str) -> tuple[str, int, str] | None:
    """Exact compare-delete arguments for a previous request's typed card wait.

    Pair identity alone grants no authority. A manual edge, an operator edit,
    another agent's wait, or a legacy receipt without provenance is preserved.
    The server must still compare both generation and owner at deletion time.
    """
    if not isinstance(edge, dict) or not isinstance(receipt, dict):
        return None
    generation = edge.get("generation")
    edge_id, wait_id = edge.get("id"), edge.get("owner_wait_id")
    if (not isinstance(edge_id, str) or not edge_id
            or not isinstance(wait_id, str) or not wait_id
            or type(generation) is not int or generation < 0
            or edge.get("task_id") != request_id or edge.get("depends_on_task_id") != source_id
            or edge.get("dependency_type") != "blocks"
            or edge.get("condition") not in _DEPENDENCY_CONDITIONS
            or edge.get("owner_agent_id") != recipient_id
            or receipt.get("id") != wait_id or receipt.get("task_id") != request_id
            or receipt.get("agent_id") != recipient_id or receipt.get("kind") != "card"
            or receipt.get("target_task_id") != source_id
            or (receipt.get("target_condition") or "task_done") != edge["condition"]
            or receipt.get("dependency_owned") is not True
            or receipt.get("dependency_id") != edge_id
            or type(receipt.get("dependency_generation")) is not int
            or receipt["dependency_generation"] != generation):
        return None
    return edge_id, generation, wait_id


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
    ordinary observer, as may a set holding one fully known task_done block to
    the current target (other blockers beside it do not retire the target).
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
    # A fully known task_done block to the current target corroborates the declared WAIT,
    # whatever other blockers the card has: the target is not retired, so the ordinary
    # observer may report its own event (it closed). Other blockers stay with the owner, who
    # re-declares on them when woken. Counting siblings here held a WAIT whose target closed
    # until every sibling closed too, with no log line. The guard accepts
    # a satisfied edge to the current target too; the observer still decides the event.
    return not any(sole_dependency({"outgoing": [edge]}, task_id, target, allow_satisfied=True)
                   for edge in edges)


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


_TYPED_CONDITIONS = tuple(c for c in _DEPENDENCY_CONDITIONS if c != "task_done")


def satisfied_wait_edge(rows: dict, wait: dict, task_id: str) -> dict | None:
    """The edge this exact WAIT created, if its owner narrowed it to a typed condition and
    Mesh itself reports exactly that condition satisfied.

    Fails closed (None) on an unreadable/paginated/malformed edge set, on an edge that is not
    ours (id, task, blocks, target prefix), on a non-boolean verdict and on an unsatisfied
    one. The target's own state is not consulted: the typed condition, not task_done,
    is what was asked for."""
    # ``edge``: created by this WAIT; ``observe_edge``: an existing operator-shaped edge it only watches.
    edge_id, target = wait.get("edge") or wait.get("observe_edge"), wait.get("target")
    if not isinstance(edge_id, str) or not edge_id or not isinstance(target, str):
        return None
    if (not isinstance(rows, dict) or not isinstance(rows.get("outgoing"), list)
            or rows.get("has_more") or rows.get("next")):
        return None
    found, seen = None, set()
    for edge in rows["outgoing"]:
        if not isinstance(edge, dict) or not isinstance(edge.get("id"), str) or edge["id"] in seen:
            return None
        seen.add(edge["id"])
        if edge["id"] == edge_id:
            found = edge
    if found is None:
        return None
    destination = found.get("depends_on_task_id")
    if (found.get("task_id") != task_id or found.get("dependency_type") != "blocks"
            or found.get("condition") not in _TYPED_CONDITIONS
            or found.get("condition_satisfied") is not True
            or not isinstance(destination, str)
            or not re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', destination)
            or destination == task_id or destination[:8] != target
            or (wait.get("target_id") and wait["target_id"] != destination)):
        return None
    return found


_UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')


def readable_outgoing(rows: dict, task_id: str) -> list[dict] | None:
    """The complete, fully well-formed outgoing edge set of ``task_id``, else None.

    Used before an edge is created or its record dropped: any unreadable container, pagination,
    malformed/foreign/duplicate row or unknown type/condition means "cannot tell", never "absent"."""
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
                or not isinstance(destination, str) or not _UUID.fullmatch(destination)
                or destination == task_id
                or edge.get("dependency_type") not in ("blocks", "relates_to")
                or edge.get("condition") not in _DEPENDENCY_CONDITIONS
                or type(edge.get("condition_satisfied")) is not bool):
            return None
        seen.add(edge["id"])
    return rows["outgoing"]
