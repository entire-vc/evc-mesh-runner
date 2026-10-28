"""Test doubles: an in-memory Mesh and a fake harness process."""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

from mesh_runner.adapters.claude import ClaudeAdapter
from mesh_runner.mesh import Checkout, MeshError

AGENT_ID = "agent-1"
STATUSES = [
    {"id": "s-todo", "category": "todo"}, {"id": "s-prog", "category": "in_progress"},
    {"id": "s-rev", "category": "review"}, {"id": "s-done", "category": "done"},
    {"id": "s-back", "category": "backlog"},
]
CAT_TO_STATUS = {s["category"]: s["id"] for s in STATUSES}


def task(n: int, priority: str = "medium", cat: str = "todo", **extra) -> dict:
    tid = f"{n:08x}-0000-4000-8000-{n:012x}"
    return {"id": tid, "title": f"task {n}", "priority": priority, "project_id": "p1",
            "status_id": CAT_TO_STATUS[cat], "assignee_id": AGENT_ID, "labels": [],
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

    def add_dependency(self, tid, dep):
        e = {"id": f"edge-{len(self.edges) + 1}", "depends_on_task_id": dep, "dependency_type": "blocks"}
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
        return [dict(t) for t in self.tasks.values()
                if t.get("assignee_id") == AGENT_ID and self.category(t) == cat]

    def category(self, t):
        return next(s["category"] for s in STATUSES if s["id"] == t["status_id"])

    def task(self, tid):
        if tid in self.broken_tasks:
            raise MeshError(500, "internal error")
        if tid in getattr(self, "forbidden_tasks", ()):
            raise MeshError(403, "agent is not a member of this project")
        if tid not in self.tasks:
            raise MeshError(404, "not found")
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
        return list(self.comment_log.get(tid, []))[-limit:]

    def add_comment(self, tid, body, author=AGENT_ID, at=None):
        c = {"id": str(uuid.uuid4()), "author_id": author, "author_name": author, "body": body,
             "created_at": (at or dt.datetime.now(dt.timezone.utc)).isoformat()}
        self.comment_log.setdefault(tid, []).append(c)
        return c

    def vcs_links(self, tid):
        return [dict(link) for link in getattr(self, "links", {}).get(tid, [])]

    def dependencies(self, tid):
        return {"outgoing": self.deps.get(tid, []), "incoming": []}

    def patch_task(self, tid, fields):
        if self.refuse_patch:
            raise MeshError(403, "forbidden")
        self.tasks[tid].update(fields)
        return dict(self.tasks[tid])

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


class HarnessGate:
    """Keep a real fake harness alive until the test explicitly releases it."""

    def __init__(self, path: Path):
        self.path = path
        self.started = path.with_suffix(".started")
        self.released = path.with_suffix(".released")

    def reply(self, text: str) -> str:
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
replies = json.load(open(os.environ["FAKE_REPLIES"]))
m = re.search(r"#([0-9a-f]{8})", prompt)
key = m.group(1) if m else ""
reply = replies.get(key + (":resume" if "--resume" in args else ""), replies.get(key, ""))
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
print(json.dumps({"type": "system", "subtype": "init", "session_id": sid, "model": "fake-model"}))
for _ in range(compactions):
    print(json.dumps({"type": "system", "subtype": "compact_boundary", "session_id": sid}))
print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": reply, "session_id": sid,
                  "total_cost_usd": 0.01, "usage": {"input_tokens": 10, "cache_read_input_tokens": 90,
                                                    "output_tokens": 5}}))
'''


class FakeProfileAdapter(ClaudeAdapter):
    """Codex-shaped: carries a harness profile on the command line."""

    def __init__(self, script: Path, profile: str):
        super().__init__(binary=sys.executable)
        self.script, self.profile = script, profile

    def start_argv(self, session_id, cwd, add_dirs):
        return [sys.executable, str(self.script), "--session-id", session_id, "--profile", self.profile]

    def resume_argv(self, session_id, cwd, add_dirs):
        return [sys.executable, str(self.script), "--resume", session_id, "--profile", self.profile]


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

    def get_mr(self, target):
        return dict(self.mr_rows.get(target, {}))

    def get_pipeline(self, target):
        return dict(self.pipeline_rows.get(target, {}))

    def pipeline_has_active_jobs(self, target):
        return target in self.active_jobs

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
