"""Read-only review of the human gates that are open now, with the hard/soft verdict of asks.py.

Meant for the owner, to check the classifier: for each question waiting on a person it shows the
class the rule gives it (``asks.gate_class``, the same rule Mesh arms the gate with), the rule that
decided it and the class Mesh actually holds. ``auto`` is a gate Mesh holds soft: its stated
default applies at the deadline, so it is not a gate on the person. ``soft`` is a gate the rule
calls soft but Mesh still holds hard; with the rule in place that count is zero. Only GET requests
are made (task lists and comments); nothing is written, moved or commented.
"""
from __future__ import annotations

import logging

from . import asks
from .mesh import MeshClient, MeshError

log = logging.getLogger("mesh_runner")

CATEGORIES = ("todo", "in_progress", "review", "backlog")
QUESTION_CHARS = 300


def open_gates(cfg, client_for=None) -> list[dict]:
    """One row per task with an open human gate among the tasks of the configured agents
    (each task once, however many agents list it). ``client_for(agent)`` returns a Mesh
    client; the default is the agent's own."""
    client_for = client_for or (lambda ag: MeshClient(cfg.mesh_url, ag.key, basic_auth=cfg.mesh_basic_auth,
                                                  bearer=cfg.extension_token))
    seen: dict[str, dict] = {}
    errors: list[str] = []
    for ag in cfg.agents:
        mesh = client_for(ag)
        for cat in CATEGORIES:
            try:
                tasks = mesh.my_tasks(cat)
            except MeshError as e:
                errors.append(f"{ag.name}/{cat}: HTTP {e.status}")
                continue
            for t in tasks:
                if t.get("human_gate") and t.get("id") and t["id"] not in seen:
                    seen[t["id"]] = {"agent": ag.name, "task": t, "mesh": mesh}
    rows = []
    for tid, item in sorted(seen.items(), key=lambda kv: kv[1]["task"].get("created_at") or ""):
        t, mesh = item["task"], item["mesh"]
        try:
            comments = mesh.comments(tid)
            handle, question = asks.blocking_question(comments)
            body = asks.blocking_ask(comments)[1]
            read = True
        except MeshError as e:
            handle, question, body, read = "", "", "", False
            errors.append(f"#{tid[:8]} comments: HTTP {e.status}")
        default = t.get("recommended_default") or ""
        if not read:
            verdict = asks.Verdict(True, "unsure: comments unreadable")
        elif handle:
            verdict = asks.marker_gate_class(body, handle, t.get("labels"), cfg.hard_ask_patterns,
                                             cfg.hard_ask_labels)
        else:
            verdict = asks.gate_class(question, t.get("labels"), default, cfg.hard_ask_patterns, cfg.hard_ask_labels)
        held = str(t.get("human_gate_class") or "hard")
        kind = "auto" if held == "soft" else verdict.kind
        rows.append({"task": tid[:8], "task_id": tid, "agent": item["agent"], "title": (t.get("title") or "")[:90],
                     "labels": list(t.get("labels") or []), "asked": handle, "verdict": kind,
                     "rule": verdict.rule, "held": held, "default": default[:QUESTION_CHARS],
                     "deadline": t.get("gate_deadline") or "", "question": question[:QUESTION_CHARS]})
    if errors:
        rows.append({"errors": errors})
    return rows


def summary(rows: list[dict]) -> dict:
    gates = [r for r in rows if "verdict" in r]
    return {"gates": len(gates), "hard": sum(r["verdict"] == "hard" for r in gates),
            "soft": sum(r["verdict"] == "soft" for r in gates),
            "auto": sum(r["verdict"] == "auto" for r in gates)}
