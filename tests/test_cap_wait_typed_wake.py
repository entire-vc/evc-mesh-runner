"""A capped task that had declared a pipeline / merge request wait wakes on that wait, not on any
change of the linked merge request or pipeline."""
import datetime as dt

from fakes import FakeGitLab, FakeMesh, calls, task, write_replies, FUTURE_AT
from test_requeue_on_cap import CapBench

from mesh_runner.outcome import parse_line
from mesh_runner.store import iso

SHA = "a" * 40
NEXT_SHA = "b" * 40
NOW = dt.datetime.now(dt.timezone.utc)


def link(pipeline="running", status="open", head=SHA):
    return {"url": "https://git.example/g/app/-/merge_requests/9", "status": status,
            "pipeline_status": pipeline, "head_sha": head}


def row(pid=7, status="running", sha=SHA, state="opened", **extra):
    return {"sha": sha, "state": state, "head_pipeline": {"sha": sha, "id": pid, "status": status}, **extra}


class TypedCapWake(CapBench):
    def capped(self, wait_line=None, wait=None, links=None):
        """A task whose turn budget ran out right after the session declared ``wait_line``."""
        t = task(1, cat="in_progress")
        mesh = FakeMesh([t], max_concurrent=1)
        mesh.links = {t["id"]: links if links is not None else [link()]}
        gl = FakeGitLab()
        gl.pipelines["g/app#18827"] = "running"
        gl.pipeline_rows["g/app#18827"] = {"sha": SHA, "id": 18827, "status": "running"}
        gl.mr_head["g/app!9"] = "7:running"
        gl.mr_rows["g/app!9"] = row()
        r = self.runner(mesh, gl)
        r.refresh_me()
        a = r.store.create("builder", t["id"], "claude")
        if wait_line:
            o = parse_line(wait_line, NOW)
            wait = {"kind": o.kind, "target": o.target, "handle": o.handle, "line": o.line, **o.extra,
                    "since": iso(), "started": iso()}
            if not o.extra:
                wait.update(gl.seed(o.kind, o.target, NOW))
        if wait:
            r.store.update(a, state="running", wait=wait)
            r.store.mark("builder", t["id"], "cap_observation", a.id)
        r._hand_up(a, dict(mesh.tasks[t["id"]]), "🔁 TURN CAP", "budget spent", "turn budget")
        rec = r._cap_wait(t["id"])
        self.assertIsNotNone(rec)
        return r, mesh, gl, t, rec

    def woke(self, r, mesh, t, rec):
        r._input_checked.clear()
        return r._inputs_changed(dict(mesh.tasks[t["id"]]), rec)

    # --- plain pipeline wait: wait for the pipeline to finish
    def test_intermediate_pipeline_change_does_not_wake_a_pipeline_wait(self):
        r, mesh, gl, t, rec = self.capped("⏳ WAIT pipeline:g/app#18827")
        self.assertEqual(rec["wait"]["kind"], "pipeline")
        self.assertFalse(self.woke(r, mesh, t, rec))
        mesh.links[t["id"]] = [link(pipeline="pending")]       # a job started: a link change, no result
        self.assertFalse(self.woke(r, mesh, t, rec))
        self.assertNotIn(t["id"], r._cap_wake_reasons)

    def test_terminal_pipeline_wakes_a_pipeline_wait(self):
        r, mesh, gl, t, rec = self.capped("⏳ WAIT pipeline:g/app#18827")
        gl.pipelines["g/app#18827"] = "success"
        mesh.links[t["id"]] = [link(pipeline="success")]
        self.assertTrue(self.woke(r, mesh, t, rec))
        self.assertIn("declared wait satisfied", r._cap_wake_reasons[t["id"]])

    # --- typed pipeline wait: until=change
    def test_queue_hop_does_not_wake_a_typed_pipeline_change_wait(self):
        r, mesh, gl, t, rec = self.capped(f"⏳ WAIT pipeline:g/app#18827 until=change after={SHA}:18827:pending")
        gl.pipeline_rows["g/app#18827"] = {"sha": SHA, "id": 18827, "status": "running"}
        mesh.links[t["id"]] = [link(pipeline="running")]
        self.assertFalse(self.woke(r, mesh, t, rec))
        gl.pipeline_rows["g/app#18827"] = {"sha": SHA, "id": 18827, "status": "failed"}
        mesh.links[t["id"]] = [link(pipeline="failed")]
        self.assertTrue(self.woke(r, mesh, t, rec))

    # --- typed MR wait: until=merged
    def test_post_review_pipeline_restart_does_not_wake_an_until_merged_wait(self):
        r, mesh, gl, t, rec = self.capped(f"⏳ WAIT mr:g/app!9 until=merged after={SHA}:opened",
                                          links=[link(pipeline="success")])
        self.assertEqual(rec["wait"]["mr_state"]["predicate"], "merged")
        gl.mr_rows["g/app!9"] = row(pid=8, status="running")    # the post-review stage started
        mesh.links[t["id"]] = [link(pipeline="running")]
        self.assertFalse(self.woke(r, mesh, t, rec))
        gl.mr_rows["g/app!9"] = row(pid=8, status="success")
        mesh.links[t["id"]] = [link(pipeline="success")]
        self.assertFalse(self.woke(r, mesh, t, rec))

    def test_merge_wakes_an_until_merged_wait(self):
        r, mesh, gl, t, rec = self.capped(f"⏳ WAIT mr:g/app!9 until=merged after={SHA}:opened",
                                          links=[link(pipeline="success")])
        gl.mr_rows["g/app!9"] = row(state="merged", merged_at="2026-10-25T12:00:00Z")
        mesh.links[t["id"]] = [link(pipeline="success", status="merged")]
        self.assertTrue(self.woke(r, mesh, t, rec))
        self.assertIn("merged", r._cap_wake_reasons[t["id"]])

    # --- the other inputs still wake as before
    def test_foreign_comment_wakes_whatever_the_declared_wait(self):
        r, mesh, gl, t, rec = self.capped(f"⏳ WAIT mr:g/app!9 until=merged after={SHA}:opened")
        self.assertFalse(self.woke(r, mesh, t, rec))
        mesh.add_comment(t["id"], "please look", author="linus")
        self.assertTrue(self.woke(r, mesh, t, rec))
        self.assertIn("comment from someone else", r._cap_wake_reasons[t["id"]])

    def test_new_assignee_wakes_whatever_the_declared_wait(self):
        r, mesh, gl, t, rec = self.capped("⏳ WAIT pipeline:g/app#18827")
        mesh.tasks[t["id"]].update(assignee_id="agent-reviewer")
        self.assertTrue(self.woke(r, mesh, t, rec))
        self.assertEqual(r._cap_wake_reasons[t["id"]], "new assignee")

    def test_closed_dependency_wakes_whatever_the_declared_wait(self):
        t = task(1, cat="in_progress")
        dep = dict(task(2), id=t["id"] + "-dep")
        mesh = FakeMesh([t, dep], max_concurrent=1)
        mesh.links = {t["id"]: [link()]}
        mesh.deps = {t["id"]: [{"depends_on_task_id": dep["id"], "dependency_type": "blocks"}]}
        gl = FakeGitLab()
        r = self.runner(mesh, gl)
        r.refresh_me()
        a = r.store.create("builder", t["id"], "claude")
        r.store.update(a, state="running", wait={"kind": "pipeline", "target": "g/app#18827",
                                                 "seen": {"pipeline": "running"}})
        r.store.mark("builder", t["id"], "cap_observation", a.id)
        r._hand_up(a, dict(mesh.tasks[t["id"]]), "TURN CAP", "x", "turn budget")
        rec = r._cap_wait(t["id"])
        self.assertFalse(self.woke(r, mesh, t, rec))
        mesh.tasks[dep["id"]].update(status_id="s-done")
        self.assertTrue(self.woke(r, mesh, t, rec))
        self.assertEqual(r._cap_wake_reasons[t["id"]], "a blocking dependency closed")

    # --- no typed declared wait: unchanged behaviour
    def test_without_a_declared_vcs_wait_any_link_change_wakes(self):
        for label, wait in (("none", None),
                            ("time wait", {"kind": "time", "until": iso(), "since": iso(), "started": iso()})):
            with self.subTest(label):
                r, mesh, gl, t, rec = self.capped(wait=wait)
                self.assertNotIn("wait", rec)
                self.assertFalse(self.woke(r, mesh, t, rec))
                mesh.links[t["id"]] = [link(pipeline="pending")]
                self.assertTrue(self.woke(r, mesh, t, rec))
                self.assertEqual(r._cap_wake_reasons[t["id"]], "linked merge request or pipeline changed")
                self.tearDown()
                self.setUp()

    def test_a_wait_of_an_older_attempt_is_not_the_declared_wait(self):
        t = task(1, cat="in_progress")
        mesh = FakeMesh([t], max_concurrent=1)
        mesh.links = {t["id"]: [link()]}
        r = self.runner(mesh, FakeGitLab())
        r.refresh_me()
        a = r.store.create("builder", t["id"], "claude")
        r.store.update(a, state="running", wait={"kind": "pipeline", "target": "g/app#1"})   # not marked
        r._hand_up(a, dict(mesh.tasks[t["id"]]), "TURN CAP", "x", "turn budget")
        self.assertNotIn("wait", r._cap_wait(t["id"]))

    def test_an_unreadable_declared_wait_is_not_news(self):
        r, mesh, gl, t, rec = self.capped(f"⏳ WAIT mr:g/app!9 until=merged after={SHA}:opened")
        gl.mr_rows["g/app!9"] = {}                                # the provider answers nothing usable
        mesh.links[t["id"]] = [link(pipeline="pending")]
        self.assertFalse(self.woke(r, mesh, t, rec))

    def test_without_a_gitlab_client_a_declared_wait_is_not_satisfied(self):
        r, mesh, gl, t, rec = self.capped("⏳ WAIT pipeline:g/app#18827")
        r.gitlab = None                                           # nothing can evaluate the declared wait
        mesh.links[t["id"]] = [link(pipeline="pending")]
        self.assertFalse(self.woke(r, mesh, t, rec))
        mesh.add_comment(t["id"], "please look", author="linus")  # other inputs still wake
        self.assertTrue(self.woke(r, mesh, t, rec))

    def test_a_provider_failure_is_not_a_satisfied_wait(self):
        r, mesh, gl, t, rec = self.capped(f"⏳ WAIT pipeline:g/app#18827 until=change after={SHA}:18827:pending")
        rec["wait"]["errors"] = 4                                 # an old error count must not carry over
        mesh.links[t["id"]] = [link(pipeline="pending")]

        def broken(target):
            raise OSError("provider down")
        gl.get_pipeline = broken
        self.assertFalse(self.woke(r, mesh, t, rec))
        self.assertNotIn(t["id"], r._cap_wake_reasons)

    # --- the wake is logged with its reason
    def test_the_start_after_a_cap_logs_what_woke_it(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        write_replies(self.replies, {t["id"][:8]: f"⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 soak"})
        r = self.runner(mesh)
        r.refresh_me()
        self.started(r, t)
        r.tick()
        mesh.add_comment(t["id"], "please look again", author="someone")
        with self.assertLogs("mesh_runner", level="INFO") as logs:
            self.settle_all(r)
        self.assertEqual(len(calls(self.calls)), 1)
        lines = [m for m in logs.output if "woke after start cap" in m]
        self.assertEqual(len(lines), 1)
        self.assertIn("comment from someone else", lines[0])
