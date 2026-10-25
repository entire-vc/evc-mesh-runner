"""What a task has received from the outside: the input fingerprint.

A task that hit a daily cap is put back in the queue and does not start again on a
clock. It starts again when something new reached it, and "something new" is the
difference between two fingerprints taken from what Mesh already shows for the task:

- the id of the newest comment by anyone but the agent itself, a runner note or Mesh's
  own automation (the same rule that wakes a waiting task);
- for every linked merge request or commit: its state, its pipeline status and, when
  Mesh records one, its head commit (webhook-fed fields of the VCS link: no GitLab call);
- the open ``blocks`` dependencies (a closed one leaves the list);
- the assignee.

Every read is a GET on the task. Any read that fails raises ``MeshError``: the caller
treats an unreadable fingerprint as "nothing new", never as news.
"""
from __future__ import annotations

from .mesh import MeshClient
from .selection import StatusCategories, blocking_tasks
from .wake import is_foreign

# fields a VCS link may carry for its head commit, on the link or in its metadata
HEAD_FIELDS = ("head_sha", "sha", "commit_sha", "source_sha", "last_commit_sha")


def _head(link: dict) -> str:
    meta = link.get("metadata") if isinstance(link.get("metadata"), dict) else {}
    for source in (link, meta):
        for field in HEAD_FIELDS:
            if source.get(field):
                return str(source[field]).lower()[:40]
    return ""


def _status(link: dict) -> str:
    meta = link.get("metadata") if isinstance(link.get("metadata"), dict) else {}
    return str(link.get("pipeline_status") or meta.get("pipeline_status") or "")


def fingerprint(mesh: MeshClient, cats: StatusCategories, agent_id: str, task: dict) -> dict:
    tid = task["id"]
    last = next((c for c in reversed(mesh.comments(tid)) if is_foreign(c, agent_id)), None)
    links = sorted([str(link.get("url") or link.get("external_id") or link.get("id") or ""),
                    str(link.get("status") or ""), _status(link), _head(link)]
                   for link in mesh.vcs_links(tid))
    blockers = sorted(b["id"] for b in blocking_tasks(mesh, cats, tid))
    return {"comment": (last or {}).get("id") or "", "links": links, "blockers": blockers,
            "assignee": task.get("assignee_id") or (task.get("assignee") or {}).get("id") or ""}
