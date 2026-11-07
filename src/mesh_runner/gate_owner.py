"""Bounded admission of a human-gated card to the owner of the live ask.

A gated card is held by every lane, so an answer from the ask's addressee never reaches
the one agent that can act on it (withdraw its own marker). Mesh now says, on GET task,
``human_gate_info.decision_pending`` + ``decision_comment_id`` + ``owner_agent_id``; this
module turns that into ONE admitted turn per decision comment. It does not clear the gate,
does not look at comment text and does not widen who may clear: ordinary work stays held
until Mesh itself reports ``human_gate=false``.
"""
from __future__ import annotations

MARK = "gate_decision"


def pending_decision(task: dict, agent_id: str) -> str:
    """The decision comment id to act on, or "" - only for the owner of the live ask."""
    if not isinstance(task, dict) or not task.get("human_gate"):
        return ""
    info = task.get("human_gate_info") or {}
    if not isinstance(info, dict) or not info.get("decision_pending"):
        return ""
    if not agent_id or info.get("owner_agent_id") != agent_id:
        return ""
    if (task.get("assignee_id") or (task.get("assignee") or {}).get("id")) != agent_id:
        return ""
    return str(info.get("decision_comment_id") or "")
