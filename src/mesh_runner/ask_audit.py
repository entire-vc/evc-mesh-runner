"""Read-only review of the human gates that are open now, with the hard/soft verdict of asks.py.

Meant for the owner, before ``soft_person_asks_to_lead`` is switched on: it shows which of the
questions waiting on a person the classifier would have kept as a gate (hard) and which it would
have put to the project lead (soft), and the rule that decided each. Only GET requests are made
(task lists and comments); nothing is written, moved or commented.
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
            handle, question = asks.blocking_question(mesh.comments(tid))
            read = True
        except MeshError as e:
            handle, question, read = "", "", False
            errors.append(f"#{tid[:8]} comments: HTTP {e.status}")
        verdict = (asks.classify(question, t.get("labels"), cfg.hard_ask_patterns, cfg.hard_ask_labels)
                   if read else asks.Verdict(True, "unsure: comments unreadable"))
        rows.append({"task": tid[:8], "task_id": tid, "agent": item["agent"], "title": (t.get("title") or "")[:90],
                     "labels": list(t.get("labels") or []), "asked": handle, "verdict": verdict.kind,
                     "rule": verdict.rule, "question": question[:QUESTION_CHARS]})
    if errors:
        rows.append({"errors": errors})
    return rows


def summary(rows: list[dict]) -> dict:
    gates = [r for r in rows if "verdict" in r]
    return {"gates": len(gates), "hard": sum(r["verdict"] == "hard" for r in gates),
            "soft": sum(r["verdict"] == "soft" for r in gates)}
