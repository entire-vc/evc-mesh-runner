"""Test doubles: an in-memory Mesh and a fake harness process."""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

from mesh_runner import lifecycle
from mesh_runner.adapters.claude import ClaudeAdapter
from mesh_runner.adapters.codex import CodexAdapter
from mesh_runner.mesh import Checkout, MeshError

# Every test that drives a runner through these doubles also checks the lifecycle table: a state change
# the table does not name raises instead of being recorded (see mesh_runner.lifecycle).
lifecycle.STRICT = True

# A moment ahead of the real clock for time-WAIT fixtures: the parser refuses a past or bare
# timestamp, so a fixed calendar date would turn these tests red once it passes.
FUTURE_AT = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=60)).strftime("%Y-%m-%dT%H:%MZ")


AGENT_ID = "agent-1"

# Where the fake agent records the Mesh turn commands it "called" (set by the Bench fixture per test);
# FakeMesh serves them back as turn-outcome rows and the active wait, the way the live MCP does.
OUTCOME_SINK: Path | None = None


def mesh_turn(command: str, text: str = "", **fields) -> dict:
    """A fake agent's reply that ends the turn with a Mesh turn command, as a live lane does:
    ``text`` is the turn's final prose (no marker line), the command is recorded in Mesh.

    command: ``next`` (continue_next, ``note``), ``wait`` (wait_for: ``on`` plus that kind's fields),
    ``ask_agent`` (``to``, ``question``), ``ask_human`` (``to``, ``question``), ``finish``
    (finish_task, ``to``=review|done) or ``handoff`` (hand_off, ``to``=agent id)."""
    return {"mesh_turn": command, "text": text, **fields}


def slow(prefix: str, turn: dict) -> dict:
    """The same turn command behind a harness prefix of the fake agent (``SLEEP:<s>:``, ``BG:<d>:``,
    ``GATE:<path>:``, ``COMPACT:<n>:``): the command is recorded when the turn ends."""
    return dict(turn, pre=turn.get("pre", "") + prefix)


def next_turn(note: str = "", text: str = "") -> dict:
    return mesh_turn("next", text, note=note)


def wait_card(target: str, note: str = "", condition: str = "", text: str = "") -> dict:
    """wait_for on=card; ``target`` a full card id (a short one is padded, as the MCP resolves it)."""
    fields = {"target_task_id": target if len(target) == 36 else target[:8] + "-0000-4000-8000-000000000000"}
    if condition:
        fields["target_condition"] = condition
    return mesh_turn("wait", text, on="card", note=note, **fields)


def wait_time(until_at: str = "", why: str = "soak window", event_ref: str = "fixture-window",
              source: str = "https://status.example.com/fixture-window", text: str = "") -> dict:
    """wait_for on=time with its published source."""
    return mesh_turn("wait", text, on="time", until_at=until_at or FUTURE_AT, event_ref=event_ref,
                     source=source, why=why, note=why)


def wait_pipeline(project: str, pipeline_id: int, until: str = "finished", jobs=(), note: str = "",
                  text: str = "", **baseline) -> dict:
    fields = dict(project_path=project, pipeline_id=int(pipeline_id), until=until, note=note, **baseline)
    if jobs:
        fields["required_jobs"] = list(jobs)
    return mesh_turn("wait", text, on="pipeline", **fields)


def wait_mr(project: str, iid: int, until: str = "", provider: str = "gitlab", jobs=(), note: str = "",
            text: str = "", **baseline) -> dict:
    """wait_for on=mr; no ``until`` is the plain form (merged, or the head pipeline's result)."""
    fields = dict(project_path=project, mr_iid=int(iid), provider=provider, note=note, **baseline)
    if until:
        fields["until"] = until
    if jobs:
        fields["required_jobs"] = list(jobs)
    return mesh_turn("wait", text, on="mr", **fields)


def wait_merge(project: str, iid: int, sha: str, note: str = "", text: str = "") -> dict:
    return mesh_turn("wait", text, on="merge", project_path=project, mr_iid=int(iid), after_sha=sha, note=note)


def wait_registered(registration: str, ref: str, note: str = "", text: str = "") -> dict:
    return mesh_turn("wait", text, on="registered", registration=registration, registration_ref=ref, note=note)


def wait_manual(note: str, text: str = "") -> dict:
    """wait_for on=manual: Mesh records it on the card itself (no target, no timer), only under ``wake:manual``."""
    return mesh_turn("wait", text, on="manual", note=note)


def ask_agent(to: str, question: str, text: str = "") -> dict:
    return mesh_turn("ask_agent", text, to=to, question=question)


def ask_human(to: str, question: str, text: str = "") -> dict:
    return mesh_turn("ask_human", text, to=to, question=question)


def finish(to: str = "done", evidence: str = "criteria met", text: str = "") -> dict:
    return mesh_turn("finish", text, to=to, evidence=evidence)


def hand_off(to: str, why: str = "", text: str = "") -> dict:
    return mesh_turn("handoff", text, to=to, why=why)
STATUSES = [
    {"id": "s-todo", "category": "todo"}, {"id": "s-prog", "category": "in_progress"},
    {"id": "s-rev", "category": "review"}, {"id": "s-done", "category": "done"},
    {"id": "s-back", "category": "backlog"},
]
CAT_TO_STATUS = {s["category"]: s["id"] for s in STATUSES}


def task(n: int, priority: str = "medium", cat: str = "todo", **extra) -> dict:
    tid = f"{n:08x}-0000-4000-8000-{n:012x}"
    return {"id": tid, "title": f"task {n}", "priority": priority, "project_id": "p1",
            "status_id": CAT_TO_STATUS[cat], "assignee_id": AGENT_ID, "labels": [], "description": "",
            "created_at": f"2026-10-01T00:00:{n:02d}Z", **extra}


class FakeMesh:
    def __init__(self, tasks: list[dict], max_concurrent: int = 2):
        self.tasks = {t["id"]: t for t in tasks}
        self.comment_log: dict[str, list[dict]] = {}
        self.deps: dict[str, list[dict]] = {}
        self.checkouts: dict[str, dict] = {}
        self.max_concurrent = max_concurrent
        self.refuse_checkout: set[str] = set()
        self.broken_tasks: set[str] = set()       # GET /tasks/{id} answers 500
        self.runtime: dict[str, dict] = {}
        self.edges: dict[str, list[dict]] = {}
        self.created: list[dict] = []
        self.fail_create = 0
        self.lose_create_response = 0
        self.fail_find = 0
        self.finds = 0
        self.reports: list[dict] = []
        self.fail_delete = 0
        self.agents = [{"id": AGENT_ID, "name": "builder"}, {"id": "agent-lead", "name": "lead"},
                       {"id": "agent-reviewer", "name": "reviewer"}]
        self.refuse_patch = False
        self.heartbeats = 0
        self.base = "https://mesh.test/api/v1"       # what MeshClient.base is: the evidence source is built from it
        # Mesh's start_after contract (docs/api/typed-outcomes.md): False keeps the old permissive
        # model, True refuses an agent's null / evidence-less date the way the server does
        self.strict_start_after = False
        self.start_after_calls: list[tuple[str, str, dict]] = []   # ("patch" | "compare-clear", task, body)
        self.clear_receipts: dict[tuple[str, str], dict] = {}

    # MeshClient surface used by the runner
    def me(self):
        return {"id": AGENT_ID, "name": "builder", "workspace_id": "ws1", "max_concurrent_tasks": self.max_concurrent}

    def put_runtime_state(self, tid, state):
        known = getattr(self, "known_codes", None)       # None: every reason code is accepted
        if known is not None and state.get("reason_code") and state["reason_code"] not in known:
            self.refused_puts = getattr(self, "refused_puts", 0) + 1
            raise MeshError(422, "invalid runtime_state: reason_code is not a known reason code",
                            {"field": "reason_code"})
        self.runtime[tid] = dict(state)

    def delete_runtime_state(self, tid):
        if self.fail_delete:
            self.fail_delete -= 1
            raise MeshError(503, "unavailable")
        self.runtime.pop(tid, None)

    def add_dependency(self, tid, dep, condition=None):
        e = {"id": f"edge-{len(self.edges) + 1}", "task_id": tid, "depends_on_task_id": dep,
             "dependency_type": "blocks", "condition": condition or "task_done", "condition_satisfied": False}
        self.edges.setdefault(tid, []).append(e)
        return e

    def delete_dependency(self, tid, edge_id):
        self.edges[tid] = [e for e in self.edges.get(tid, []) if e["id"] != edge_id]

    def task_by_short(self, short):
        for t in self.tasks.values():
            if t["id"].startswith(short):
                return dict(t)
        raise MeshError(404, "not found")

    def session_report(self, task_id, tokens_in, tokens_out, model, cost):
        self.reports.append({"task": task_id, "in": tokens_in, "out": tokens_out, "model": model, "cost": cost})

    def mentions(self, workspace_id):
        return [m for m in getattr(self, "mention_rows", []) if not m.get("seen")]

    def mention_seen(self, comment_id):
        for m in getattr(self, "mention_rows", []):
            if m["comment_id"] == comment_id:
                m["seen"] = True

    def create_task(self, pid, fields):
        if self.fail_create:
            self.fail_create -= 1
            raise MeshError(503, "unavailable")
        n = 100 + len(self.created)
        t = task(n, cat="todo", **{k: v for k, v in fields.items() if k not in ("status_id",)})
        self.tasks[t["id"]] = t
        self.created.append(t)
        if self.lose_create_response:                  # committed, but the caller sees a failure
            self.lose_create_response -= 1
            raise MeshError(503, "response lost")
        return dict(t)

    def find_tasks(self, project_id, search):
        if self.fail_find:
            self.fail_find -= 1
            raise MeshError(503, "unavailable")
        self.finds += 1
        return [dict(t) for t in self.tasks.values() if t.get("project_id") == project_id
                and (search in (t.get("title") or "") or search in (t.get("description") or ""))]

    def find_tasks_all(self, project_id, search):
        return self.find_tasks(project_id, search)

    def assign(self, tid, agent_id):
        if self.tasks[tid].get("pinned"):
            raise MeshError(422, "assignment_pinned")
        self.tasks[tid]["assignee_id"] = agent_id

    def workspace_recipients(self, ws, handle):
        rows = getattr(self, "recipients", None)
        if rows is not None:
            return list(rows)
        return ([{"id": a["id"], "kind": "agent", "slug": a["name"],
                  "display_name": a["name"]} for a in self.agents]
                + [{"id": name, "kind": "user", "slug": name, "display_name": name}
                   for name in ("owner", "owner2", "owner3", "person")])

    def workspace_agents(self, ws):
        return list(self.agents)

    def subtasks(self, tid):
        return [dict(t) for t in self.tasks.values() if t.get("parent_task_id") == tid]

    def my_tasks(self, cat):
        self.my_tasks_calls = getattr(self, "my_tasks_calls", []) + [cat]
        self._sync_turns()
        cats = cat.split(",")
        return [dict(t) for t in self.tasks.values()
                if t.get("assignee_id") == AGENT_ID and self.category(t) in cats]

    def category(self, t):
        return next(s["category"] for s in STATUSES if s["id"] == t["status_id"])

    def task(self, tid):
        if tid in self.broken_tasks:
            raise MeshError(500, "internal error")
        if tid in getattr(self, "forbidden_tasks", ()):
            raise MeshError(403, "agent is not a member of this project")
        if tid not in self.tasks:
            raise MeshError(404, "not found")
        self._sync_turns()
        out = dict(self.tasks[tid])
        out.setdefault("version", 1)
        if getattr(self, "shows_runtime", False):     # a Mesh that returns the card's runtime state
            out["runtime_state"] = dict(self.runtime[tid], source="explicit") if tid in self.runtime else None
        return out

    def request(self, method, path, body=None, query=None):
        if path.startswith("/tasks/by-short-id/"):
            short = path.rsplit("/", 1)[1]
            for t in self.tasks.values():
                if t["id"].startswith(short):
                    return dict(t)
            raise MeshError(404, "not found")
        raise NotImplementedError(path)

    def comments(self, tid, limit=20):
        self.recorded_turns(tid)
        return list(self.comment_log.get(tid, []))[-limit:]

    def comments_all(self, tid):
        self.recorded_turns(tid)
        return list(self.comment_log.get(tid, []))

    def add_comment(self, tid, body, author=AGENT_ID, at=None):
        c = {"id": str(uuid.uuid4()), "author_id": author, "author_name": author, "body": body,
             "created_at": (at or dt.datetime.now(dt.timezone.utc)).isoformat()}
        self.comment_log.setdefault(tid, []).append(c)
        return c

    def recorded_turns(self, tid):
        """(row, wait) pairs the fake agent's turn commands recorded for ``tid`` (see ``mesh_turn``), oldest
        first: what Mesh's turn-outcomes table holds after a live lane called wait_for / continue_next /
        ask_agent / ask_human. finish_task and hand_off change the card itself and are applied when read."""
        out = []
        for rec in self._turn_records():
            if not tid.startswith(rec["task8"]) or tid not in self.tasks:
                continue
            row, wait = rec["row"], rec.get("wait")
            # Mesh records the command under the calling agent: the identity this runner works as.
            try:
                caller = (self.me() or {}).get("id")
            except Exception:
                caller = None
            row = dict(row, task_id=tid, agent_id=caller or row.get("agent_id"))
            if wait is not None:
                wait = dict(wait, task_id=tid)
            out.append((row, wait))
            self._apply_turn(tid, rec)
        return out

    def _turn_records(self):
        sink = getattr(self, "outcome_sink", None) or OUTCOME_SINK
        if sink is None or not Path(sink).exists():
            return []
        return [json.loads(line) for line in Path(sink).read_text().splitlines()]

    def _sync_turns(self):
        """Apply every recorded turn command to the cards (status, assignee, gate, comment), as Mesh has
        by the time anyone reads a card after the command returned."""
        for rec in self._turn_records():
            tid = next((t for t in self.tasks if t.startswith(rec["task8"])), None)
            if tid is not None:
                self._apply_turn(tid, rec)

    def _apply_turn(self, tid, rec):
        """What Mesh did to the card when the command ran, once per recorded call: the command's comment,
        ask_human's gate, finish_task's status, hand_off's assignee."""
        done = self.__dict__.setdefault("_applied_turns", set())
        if rec["row"]["id"] in done:
            return
        done.add(rec["row"]["id"])
        # Mesh writes the command's own comment, authored by the agent, in the same transaction
        # (typed_outcome_command_service.go): it moves the lane's comment cursor like any comment.
        row, wait = rec["row"], rec.get("wait") or {}
        note = row.get("note") or ""
        body = {"next": "Continuing: " + note if note else "Continuing with the next step.",
                "wait": (f"❓ Blocking @{wait.get('addressee')}: {wait.get('question')}" if wait.get("kind") == "human"
                         else f"❔ **Question to {(wait.get('note') or '').removeprefix('asked ')}** · request "
                              f"`{wait.get('decision_request_id')}`\n\n{wait.get('question')}"
                         if wait.get("kind") == "decision"
                         else f"Waiting on {wait.get('kind', 'an event')}." + ("\n\n" + note if note else "")),
                "done": "Finished.\n\n" + note, "review": "Handed to review.\n\n" + note,
                "handoff": f"Handed over to {rec.get('to')}.\n\n" + note}.get(row["kind"], note)
        c = {"id": str(uuid.uuid4()), "author_id": AGENT_ID, "author_name": AGENT_ID, "body": body,
             "created_at": row["created_at"]}
        log = self.comment_log.setdefault(tid, [])
        at = next((i for i, o in enumerate(log) if (o.get("created_at") or "") > c["created_at"]), len(log))
        log.insert(at, c)
        t = self.tasks[tid]
        if wait.get("kind") == "human":                # ask_human raises the person's gate on the card
            t["human_gate"] = True
        if rec["row"]["kind"] in ("done", "review"):
            t["status_id"] = "s-rev" if rec["row"]["kind"] == "review" else "s-done"
        elif rec["row"]["kind"] == "handoff":
            t["assignee_id"], t["status_id"] = rec["to"], "s-todo"
        if rec["row"]["kind"] in ("done", "review", "handoff"):
            # finish_task and hand_off release the caller's checkout in the same step (Mesh MCP
            # turn_outcomes.go): a runner that sees the card closed mid-turn finds no lease left.
            self.checkouts.pop(tid, None)

    def turn_outcomes(self, tid, limit=5):
        """Typed turn outcomes (Mesh turn commands), newest first: the fake agent's recorded commands
        (``mesh_turn``) and ``mesh.outcomes[tid]`` a test sets directly.
        ``mesh.typed_error`` (a MeshError or OSError) makes the read fail; ``typed_reads`` counts reads."""
        self.typed_reads = getattr(self, "typed_reads", 0) + 1
        if getattr(self, "typed_error", None) is not None:
            raise self.typed_error
        rows = [row for row, _ in self.recorded_turns(tid)]
        rows += list(getattr(self, "outcomes", {}).get(tid, []))
        rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
        return rows[:limit]

    def active_wait(self, tid, agent_id):
        """The agent's active wait on ``tid``: one a test set in ``mesh.waits``, else the wait of the newest
        recorded turn command (a later continue_next / finish_task / hand_off ends it, as in Mesh)."""
        self.typed_reads = getattr(self, "typed_reads", 0) + 1
        if getattr(self, "wait_error", None) is not None:
            raise self.wait_error
        if tid in getattr(self, "waits", {}):
            return self.waits[tid]
        recorded = self.recorded_turns(tid)
        if not recorded or recorded[-1][1] is None:
            return None
        wait = recorded[-1][1]
        if wait["id"] in {w for _, w, _, _ in getattr(self, "resolved", [])}:
            return None
        return wait

    def resolve_wait(self, tid, wait_id, resolution, cause):
        """POST /tasks/:id/waits/:wait_id/resolve: recorded in ``mesh.resolved``; ``mesh.resolve_errors``
        (a list of MeshError/OSError, consumed in order) makes calls fail."""
        self.resolved = getattr(self, "resolved", [])
        self.resolved.append((tid, wait_id, resolution, cause))
        errors = getattr(self, "resolve_errors", [])
        if errors:
            raise errors.pop(0)
        return {"wait": {"id": wait_id, "resolution": resolution}, "resolved": True}

    def open_decision(self, rid, tid, question, to="agent-lead", sender=(AGENT_ID, "builder")):
        """A decision request ``ask_agent`` opened (the server side of it): listed for ``to`` until answered."""
        t = self.tasks[tid]
        self.decision_rows = getattr(self, "decision_rows", [])
        self.decision_rows.append({"id": rid, "task_id": tid, "question": question, "state": "open",
                                   "created_at": dt.datetime.now(dt.timezone.utc).isoformat(), "to_agent_id": to,
                                   "from_agent": {"id": sender[0], "name": sender[1], "slug": sender[1]},
                                   "task": {"id": tid, "title": t.get("title"), "project_id": t.get("project_id"),
                                            "workspace_id": "ws1", "status_category": self.category(t)}})

    def decision_requests(self, state="open", limit=100, to=None):
        """GET /agents/me/decision-requests: the requests addressed to the caller (``to``, default ``me``),
        oldest first. ``decision_error`` (a MeshError) makes the read fail; ``decision_reads`` counts reads."""
        self.decision_reads = getattr(self, "decision_reads", 0) + 1
        if getattr(self, "decision_error", None) is not None:
            raise self.decision_error
        to = to or self.me()["id"]
        return [{k: v for k, v in r.items() if k != "to_agent_id"} for r in getattr(self, "decision_rows", [])
                if r["to_agent_id"] == to and r["state"] == state][:limit]

    def answer_decision(self, rid, answer, author="agent-lead"):
        """POST /decision-requests/:id/answer: the answer is a comment on the asking card."""
        row = next((r for r in getattr(self, "decision_rows", []) if r["id"] == rid), None)
        if row is None:
            raise MeshError(404, "not found")
        if row["to_agent_id"] != author:
            raise MeshError(403, "forbidden")
        if row["state"] != "open":
            raise MeshError(409, "conflict")
        row["state"] = "answered"
        c = self.add_comment(row["task_id"], f"💬 **Answer to question** `{rid}`\n\n{answer}", author=author)
        c["metadata"] = {"answers": rid}
        return {"decision_request": dict(row), "comment_id": c["id"]}

    def vcs_links(self, tid):
        return [dict(link) for link in getattr(self, "links", {}).get(tid, [])]

    def dependencies(self, tid):
        # edges the runner added through add_dependency are real rows in Mesh too
        known = {e.get("id") for e in self.deps.get(tid, [])}
        extra = [e for e in self.edges.get(tid, []) if e["id"] not in known]
        return {"outgoing": list(self.deps.get(tid, [])) + extra, "incoming": []}

    def patch_task(self, tid, fields):
        if self.refuse_patch:
            raise MeshError(403, "forbidden")
        fields = dict(fields)
        evidence = fields.pop("start_after_evidence", None)
        if "start_after" in fields:
            self.start_after_calls.append(("patch", tid, {"start_after": fields["start_after"],
                                                          "start_after_evidence": evidence}))
            if self.strict_start_after:
                fields = self._strict_start_after(tid, fields, evidence)
        self.tasks[tid].update(fields)
        return dict(self.tasks[tid])

    def _refuse(self, status, message, code, **extra):
        raise MeshError(status, message, {"message": message, "code": code, "error_code": code, **extra},
                        endpoint="PATCH /tasks/<uuid>")

    def _strict_start_after(self, tid, fields, evidence):
        """The server's check of an agent's start_after write (domain.ValidateStartAfterEvidence)."""
        from mesh_runner.effects import GENERIC_RETRY_WORDS, same_instant
        t = self.tasks[tid]
        if fields["start_after"] is None:
            if t.get("start_after"):
                self._refuse(422, "an agent clears start_after only through POST /tasks/:id/start-after/compare-clear",
                             "clear_requires_compare")
            return fields
        if same_instant(fields["start_after"], t.get("start_after")):
            return fields
        ev = evidence if isinstance(evidence, dict) else {}
        if not all(isinstance(ev.get(k), str) and ev[k].strip() for k in ("event_ref", "source", "why")):
            self._refuse(422, "start_after from an agent needs start_after_evidence: event_ref, source and why",
                         "missing_evidence")
        if any(len(ev[k]) > 500 for k in ("event_ref", "source", "why")) or len(ev["why"].strip()) < 10 \
                or not ev["source"].startswith("https://"):
            self._refuse(422, "start_after_evidence does not qualify", "missing_evidence")
        if set(re.split(r"[^a-z0-9]+", ev["event_ref"].lower())) & GENERIC_RETRY_WORDS:
            self._refuse(422, "event_ref names a retry or poll, not an external event", "generic_retry")
        return dict(fields, start_after_origin="agent_evidence", start_after_receipt_id=str(uuid.uuid4()),
                    version=t.get("version", 1) + 1)

    def compare_clear_start_after(self, tid, *, expected_start_after, expected_version, idempotency_key,
                                  expected_origin=None, schedule_receipt_id=None, reason=None):
        """POST /tasks/:id/start-after/compare-clear (internal/repository/postgres/start_after_repo.go)."""
        from mesh_runner.effects import same_instant
        body = {"expected_start_after": expected_start_after, "expected_version": expected_version,
                "expected_origin": expected_origin, "schedule_receipt_id": schedule_receipt_id,
                "idempotency_key": idempotency_key, "reason": reason}
        self.start_after_calls.append(("compare-clear", tid, body))
        if tid not in self.tasks:
            raise MeshError(404, "not found")
        t = self.tasks[tid]
        prior = self.clear_receipts.get((tid, idempotency_key))
        if prior:
            return {"receipt": prior, "replayed": True, "version": t.get("version", 1)}
        same = (t.get("start_after") and t.get("start_after_origin")
                and same_instant(t["start_after"], expected_start_after)
                and t.get("version", 1) == expected_version
                and (expected_origin is None or expected_origin == t["start_after_origin"])
                and (schedule_receipt_id is None or schedule_receipt_id == t.get("start_after_receipt_id")))
        if not same:
            msg = "cas_conflict: start_after changed since it was read"
            raise MeshError(409, msg, {"code": "cas_conflict", "error_code": "cas_conflict", "message": msg,
                                       "current_version": t.get("version", 1),
                                       "current_start_after": t.get("start_after"),
                                       "current_origin": t.get("start_after_origin"),
                                       "current_receipt": t.get("start_after_receipt_id")},
                            endpoint=f"POST /tasks/{tid}/start-after/compare-clear")
        if t["start_after_origin"] == "human_explicit":
            raise MeshError(403, "start_after was set by a person; an agent cannot clear it",
                            {"message": "start_after was set by a person; an agent cannot clear it"})
        receipt = {"id": str(uuid.uuid4()), "op": "clear", "cleared_receipt_id": t.get("start_after_receipt_id"),
                   "idempotency_key": idempotency_key}
        self.clear_receipts[(tid, idempotency_key)] = receipt
        t.update(start_after=None, start_after_origin=None, start_after_receipt_id=None,
                 version=t.get("version", 1) + 1)
        return {"receipt": receipt, "replayed": False, "version": t["version"]}

    def statuses(self, pid):
        return list(STATUSES)

    def move(self, tid, status_id, expected_version=None, expected_status_id=None):
        cur = self.tasks[tid]
        if (expected_version is not None and cur.get("version", 1) != expected_version) or \
                (expected_status_id and cur["status_id"] != expected_status_id):
            raise MeshError(409, "cas_conflict")
        self.tasks[tid]["status_id"] = status_id
        return dict(self.tasks[tid])

    def set_category(self, tid, cat):
        self.tasks[tid]["status_id"] = CAT_TO_STATUS[cat]

    def checkout(self, tid, session_id, request_id, ttl_minutes=120):
        if tid in self.refuse_checkout or tid in self.checkouts:
            raise MeshError(409, "checked out")
        self.checkouts[tid] = {"session": session_id}
        return Checkout(tid, "tok-" + tid[:4], 1, "", session_id, request_id)

    def heartbeat(self, co, ttl_minutes=120):
        self.heartbeats += 1
        if co.task_id not in self.checkouts:
            raise MeshError(409, "lost")

    def release(self, co):
        self.checkouts.pop(co.task_id, None)


class AgentView:
    """The same fake Mesh seen through another agent's key (its ``me`` and its decision-request list)."""

    def __init__(self, mesh: FakeMesh, agent_id: str, name: str, workspace_id: str = "ws1"):
        self.mesh, self.agent_id, self.name, self.workspace_id = mesh, agent_id, name, workspace_id

    def __getattr__(self, name):
        return getattr(self.mesh, name)

    def me(self):
        return {"id": self.agent_id, "name": self.name, "workspace_id": self.workspace_id}

    def decision_requests(self, state="open", limit=100):
        return self.mesh.decision_requests(state, limit, to=self.agent_id)


class HarnessGate:
    """Keep a real fake harness alive until the test explicitly releases it."""

    def __init__(self, path: Path):
        self.path = path
        self.started = path.with_suffix(".started")
        self.released = path.with_suffix(".released")

    def reply(self, text):
        if isinstance(text, dict):                     # a typed turn: gate the text, keep the command
            return slow(f"GATE:{self.path}:", text)
        return f"GATE:{self.path}:{text}"

    def release(self):
        self.released.touch()

    @contextlib.contextmanager
    def hold(self, proc):
        try:
            deadline = time.monotonic() + 10
            while not self.started.exists():
                if proc.poll() is not None:
                    raise AssertionError("fake harness exited before reaching its gate")
                if time.monotonic() >= deadline:
                    raise AssertionError("fake harness did not reach its gate")
                time.sleep(0.01)
            yield
        finally:
            self.release()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
                raise


FAKE_AGENT = r'''
import json, os, re, subprocess, sys
args = sys.argv[1:]
sid = args[args.index("--session-id") + 1] if "--session-id" in args else args[args.index("--resume") + 1]
prompt = sys.stdin.read()
# Emit the selected native protocol at the external CLI boundary.
if "--fixture-codex" in args:
    original_print = print
    def print(line):
        event = json.loads(line)
        if event.get("type") == "system":
            pass
        elif event.get("type") == "result":
            if event.get("is_error"):
                original_print(json.dumps({"type": "turn.failed", "error": {"message": event["result"]}}))
            else:
                original_print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": event["result"]}}))
                original_print(json.dumps({"type": "turn.completed", "usage": event.get("usage", {})}))
replies = json.load(open(os.environ["FAKE_REPLIES"]))
m = re.search(r"#([0-9a-f]{8})", prompt)
key = m.group(1) if m else ""
reply = replies.get(key + (":resume" if "--resume" in args else ""), replies.get(key, ""))
turn_command = None
if isinstance(reply, dict):                      # the turn ends with a Mesh turn command (mesh_turn)
    turn_command, reply = reply, reply.get("pre", "") + reply.get("text", "")
if not reply.startswith("DIE:"):
    if "--settings" in args:
        settings = json.load(open(args[args.index("--settings") + 1]))
        for group in settings.get("hooks", {}).get("UserPromptSubmit", []):
            for hook in group["hooks"]:
                code = subprocess.run(hook["command"], shell=True, input=json.dumps({
                    "hook_event_name": "UserPromptSubmit", "session_id": sid, "prompt": prompt}), text=True).returncode
                if code == 2:
                    sys.exit(1)
    if "--fixture-codex" in args:
        original_print(json.dumps({"type": "thread.started", "thread_id": sid}), flush=True)
        original_print(json.dumps({"type": "turn.started"}), flush=True)
if reply.startswith("GATE:"):
    import time
    from pathlib import Path
    _, gate, reply = reply.split(":", 2)
    Path(gate).with_suffix(".started").touch()
    while not Path(gate).with_suffix(".released").exists():
        time.sleep(0.01)
if reply.startswith("SLEEP:"):
    import time
    time.sleep(float(reply.split(":")[1]))
    reply = reply.split(":", 2)[2]
if reply.startswith("DIE:"):                     # the harness refuses before its stream starts
    sys.stderr.write(reply[4:] + "\n")
    sys.exit(1)
if reply.startswith("ERROR:"):
    print(json.dumps({"type": "system", "subtype": "init", "session_id": sid}))
    print(json.dumps({"type": "result", "subtype": "error_during_execution", "is_error": True,
                      "result": reply[6:], "errors": [reply[6:]], "session_id": sid}))
    open(os.environ["FAKE_CALLS"], "a").write(json.dumps({"task": key, "sid": sid, "resume": "--resume" in args}) + "\n")
    sys.exit(1)
compactions = 0
if reply.startswith("COMPACT:"):                 # the harness compacted the context N times
    compactions = int(reply.split(":")[1])
    reply = reply.split(":", 2)[2]
if reply.startswith("BG:"):
    duration = reply.split(":")[1]
    if duration == "GATE":
        child = ("import os,time; from pathlib import Path; "
                 "p=Path(os.environ['FAKE_BG_GATE']); p.with_suffix('.started').touch(); "
                 "exec(\"while not p.with_suffix('.released').exists(): time.sleep(0.01)\")")
    else:
        child = "import time; time.sleep(%s)" % duration
    subprocess.Popen([sys.executable, "-c", child])
    reply = reply.split(":", 2)[2]
open(os.environ["FAKE_CALLS"], "a").write(json.dumps({"task": key, "sid": sid, "resume": "--resume" in args,
                                                      "prompt": prompt,
                                                      "probe": os.environ.get("PROBE", ""),
                                                      "profile": args[args.index("--profile") + 1]
                                                      if "--profile" in args else "",
                                                      "profile_env": os.environ.get("MESH_RUNNER_PROFILE", ""),
                                                      "model": args[args.index("--model") + 1]
                                                      if "--model" in args else "",
                                                      "effort": args[args.index("--effort") + 1]
                                                      if "--effort" in args else ""}) + "\n")
if turn_command is not None and os.environ.get("FAKE_OUTCOMES"):
    import datetime, uuid
    cmd = dict(turn_command)
    command = cmd.pop("mesh_turn"); cmd.pop("text", None); cmd.pop("pre", None)
    at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    oid, wid = str(uuid.uuid4()), str(uuid.uuid4())
    row = {"id": oid, "agent_id": "agent-1", "created_at": at, "note": cmd.get("note", "")}
    wait = None
    if command == "next":
        row["kind"] = "next"
    elif command == "wait":
        row["kind"] = "wait"
        wait = dict({k: v for k, v in cmd.items() if k != "on"}, kind=cmd["on"], id=wid)
    elif command == "ask_agent":
        row.update(kind="wait", note="asked " + cmd["to"])
        wait = {"id": wid, "kind": "decision", "decision_request_id": str(uuid.uuid4()),
                "note": "asked " + cmd["to"], "question": cmd["question"]}
    elif command == "ask_human":
        row.update(kind="wait", note=cmd["question"])
        wait = {"id": wid, "kind": "human", "addressee": cmd["to"], "question": cmd["question"],
                "human_gate_ref": "gate-" + oid[:8]}
    elif command == "finish":                    # Mesh records finish_task as done / review
        row.update(kind="review" if cmd.get("to") == "review" else "done", note=cmd.get("evidence", ""))
    else:                                        # handoff
        row.update(kind="handoff", note=cmd.get("why", ""))
    if wait is not None:
        row["wait_id"] = wid
    rec = {"task8": key, "row": row, "wait": wait, "to": cmd.get("to", "")}
    open(os.environ["FAKE_OUTCOMES"], "a").write(json.dumps(rec) + "\n")
print(json.dumps({"type": "system", "subtype": "init", "session_id": sid, "model": "fake-model"}))
for _ in range(compactions):
    print(json.dumps({"type": "system", "subtype": "compact_boundary", "session_id": sid}))
print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": reply, "session_id": sid,
                  "total_cost_usd": 0.01, "usage": {"input_tokens": 10, "cache_read_input_tokens": 90,
                                                    "output_tokens": 5}}))
'''


class FakeProfileAdapter(ClaudeAdapter):
    """Codex-shaped: carries a harness profile on the command line."""

    def __init__(self, script: Path, profile: str, *, native="claude"):
        super().__init__(binary=sys.executable)
        self.script, self.profile, self.native = script, profile, native

    def parse(self, log_path):
        return CodexAdapter().parse(log_path) if self.native == "codex" else super().parse(log_path)

    def _native_args(self):
        return ["--fixture-codex"] if self.native == "codex" else []

    def start_argv(self, session_id, cwd, add_dirs):
        return [sys.executable, str(self.script), "--session-id", session_id, "--profile", self.profile, *self._native_args()]

    def resume_argv(self, session_id, cwd, add_dirs):
        return [sys.executable, str(self.script), "--resume", session_id, "--profile", self.profile, *self._native_args()]


class FakeAdapter(ClaudeAdapter):
    """Claude-shaped adapter that runs FAKE_AGENT instead of claude."""

    sessions_known = True      # a test sets False: the routed account holds no copy of the session

    def __init__(self, script: Path):
        super().__init__(binary=sys.executable)
        self.script = script

    def session_known(self, session_id, cwd, env):
        return self.sessions_known

    def start_argv(self, session_id, cwd, add_dirs):
        return [sys.executable, str(self.script), "--session-id", session_id]

    def resume_argv(self, session_id, cwd, add_dirs):
        return [sys.executable, str(self.script), "--resume", session_id]


def write_replies(path: Path, replies: dict) -> None:
    path.write_text(json.dumps(replies))


def calls(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []


class FakeGitLab:
    """Pipeline/MR states keyed by target."""
    def __init__(self):
        self.base_url = "https://git.example"
        self.mr_head = {}        # target -> "id:status"
        self.mr_state = {}
        self.pipelines = {}
        self.mr_rows = {}        # target -> merge request JSON (timestamps)
        self.pipeline_rows = {}  # target -> pipeline JSON
        self.active_jobs = set()  # targets with a queued or running job
        self.newer = {}          # target -> pipeline row of the same ref with a higher id
        self.pipeline_errors = {}  # target -> exception get_pipeline raises (404, a timeout)
        self.jobs = {}           # pipeline target -> GitLab job rows of that pipeline

    def get_mr(self, target):
        return dict(self.mr_rows.get(target, {}))

    def get_pipeline(self, target):
        if target in self.pipeline_errors:
            raise self.pipeline_errors[target]
        return dict(self.pipeline_rows.get(target, {}))

    def pipeline_has_active_jobs(self, target):
        return target in self.active_jobs

    def pipeline_jobs(self, target):
        return [dict(j) for j in self.jobs.get(target, [])]

    def newer_pipeline(self, target, row=None):
        return self.newer.get(target)

    def mr_head_key(self, target):
        return self.mr_head.get(target, "1:running")

    def seed(self, kind, target, now):
        if kind == "pipeline":
            st = self.pipeline_status(target)
            out = {"seen": {"pipeline": st}}
            if st in ("success", "failed", "manual"):
                out["settled"] = f"pipeline {target} had already finished ({st})"
            return out
        key = self.mr_head.get(target, "1:running")
        out = {"seen": {"mr_pipeline": key}}
        if self.mr_state.get(target) in ("merged", "closed"):
            out["settled"] = f"MR {target} was already {self.mr_state[target]}"
        elif key.split(":")[1] in ("success", "failed", "manual"):
            out["settled"] = f"MR {target}: pipeline {key} had already finished"
        return out

    def mr_event(self, target, last, now=None):
        if self.mr_state.get(target) in ("merged", "closed"):
            return f"MR {target} is {self.mr_state[target]}"
        key = self.mr_head.get(target, "1:running")
        if key.split(":")[1] in ("success", "failed") and key != last.get("mr_pipeline"):
            last["mr_pipeline"] = key
            return f"MR {target}: pipeline {key}"
        return ""

    def pipeline_status(self, target):
        return self.pipelines.get(target, "running")

    def pipeline_event(self, target, last):
        st = self.pipelines.get(target, "running")
        if st in ("success", "failed", "manual") and st != last.get("pipeline"):
            last["pipeline"] = st
            return f"pipeline {target} finished: {st}"
        if st not in ("success", "failed", "manual"):
            last["pipeline"] = st
        return ""
