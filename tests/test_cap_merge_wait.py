"""A declared ``WAIT merge:`` survives a cap: the capped task wakes on the merge request, not on
"new input" that nothing will send (the merge gate runs only from a waiting session)."""
import datetime as dt
import sys

from fakes import AGENT_ID, CAT_TO_STATUS, FakeGitLab, FakeMesh, task
from test_merge_wait import GATE
from test_requeue_on_cap import CapBench

from mesh_runner.runner import HANDBACK_WAIT
from mesh_runner.store import iso

SHA = "a" * 40
OTHER = "b" * 40
TARGET = "g/app!56"
LINE = f"⏳ WAIT merge:{TARGET} after={SHA}"
LINK = [{"url": "https://git.example/g/app/-/merge_requests/56", "status": "open",
         "pipeline_status": "manual", "head_sha": SHA}]


def mr(state="opened", sha=SHA):
    return {"sha": sha, "state": state, "head_pipeline": {"id": 10, "sha": sha, "status": "manual"}}


class MergeCapBench(CapBench):
    def setUp(self):
        super().setUp()
        gate = self.root / "gate.py"
        gate.write_text(GATE)
        self.cfg.merge_command = (sys.executable, str(gate), "{iid}", "{project}", "{sha}")

    def bench(self, cat="in_progress"):
        t = task(1, cat=cat)
        mesh = FakeMesh([t], max_concurrent=1)
        mesh.links = {t["id"]: [dict(x) for x in LINK]}
        gl = FakeGitLab()
        gl.mr_rows[TARGET] = mr()
        r = self.runner(mesh, gl)
        r.refresh_me()
        return r, mesh, gl, t

    def woke(self, r, mesh, t, rec):
        r._input_checked.clear()
        r._cap_wake_reasons.pop(t["id"], None)
        return r._inputs_changed(dict(mesh.tasks[t["id"]]), rec)


class CappedMergeWait(MergeCapBench):
    def capped(self):
        """A task capped right after its session declared ``WAIT merge:``."""
        r, mesh, gl, t = self.bench()
        a = r.store.create("builder", t["id"], "claude")
        r.store.update(a, state="running", wait={"kind": "merge", "target": TARGET, "line": LINE,
                                                 "merge": {"sha": SHA, "runs": 0},
                                                 "since": iso(), "started": iso()})
        r.store.mark("builder", t["id"], "cap_observation", a.id)
        r._hand_up(a, dict(mesh.tasks[t["id"]]), "🔁 TURN CAP", "budget spent", "turn budget")
        rec = r._cap_wait(t["id"])
        self.assertIsNotNone(rec)
        return r, mesh, gl, t, rec

    def test_the_cap_record_keeps_the_declared_merge_wait(self):
        r, mesh, gl, t, rec = self.capped()
        self.assertEqual((rec["wait"]["kind"], rec["wait"]["target"]), ("merge", TARGET))
        self.assertEqual(rec["wait"]["merge"]["sha"], SHA)

    def test_an_open_mr_at_the_reviewed_head_wakes_without_any_link_change(self):
        r, mesh, gl, t, rec = self.capped()
        self.assertTrue(self.woke(r, mesh, t, rec))              # links unchanged: nothing else can move it
        self.assertIn("declared wait satisfied", r._cap_wake_reasons[t["id"]])
        self.assertIn("merge gate", r._cap_wake_reasons[t["id"]])

    def test_merged_closed_and_a_moved_head_wake(self):
        for row, text in ((mr("merged"), "merged"), (mr("closed"), "closed without merge"),
                          (mr(sha=OTHER), "head moved")):
            with self.subTest(text):
                r, mesh, gl, t, rec = self.capped()
                gl.mr_rows[TARGET] = row
                self.assertTrue(self.woke(r, mesh, t, rec))
                self.assertIn(text, r._cap_wake_reasons[t["id"]])
                self.tearDown()
                self.setUp()

    def test_an_unreadable_or_locked_mr_is_not_news(self):
        for row in ({}, mr("locked")):
            with self.subTest(row):
                r, mesh, gl, t, rec = self.capped()
                gl.mr_rows[TARGET] = row
                self.assertFalse(self.woke(r, mesh, t, rec))
                self.tearDown()
                self.setUp()

    def test_without_a_gitlab_client_the_merge_wait_is_not_satisfied(self):
        r, mesh, gl, t, rec = self.capped()
        r.gitlab = None
        self.assertFalse(self.woke(r, mesh, t, rec))
        mesh.add_comment(t["id"], "please look", author="linus")  # other inputs still wake
        self.assertTrue(self.woke(r, mesh, t, rec))


class ReviewHandback(MergeCapBench):
    """A review session moves the card back to todo for itself and ends with ``WAIT merge:``."""
    def handed_back(self, cat="todo", assignee=AGENT_ID, line=LINE):
        r, mesh, gl, t = self.bench(cat="review")
        a = r.store.create("builder", t["id"], "claude", "t", mode="review")
        started = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5)
        r.store.update(a, state="running", turns=1, turn_started_at=iso(started))
        mesh.tasks[t["id"]].update(status_id=CAT_TO_STATUS[cat], assignee_id=assignee)
        mesh.add_comment(t["id"], f"Back in todo: delivery to main is not finished.\n\n{line}")
        r.settle(r.store.get(a.id))
        return r, mesh, gl, t, r.store.get(a.id)

    def test_settle_keeps_the_wait_of_a_review_handed_back_to_itself(self):
        r, mesh, gl, t, a = self.handed_back()
        self.assertEqual(a.state, "completed")                   # the review attempt itself ends
        kept = r._handback_wait(t["id"])
        self.assertEqual((kept["kind"], kept["target"], kept["merge"]["sha"]), ("merge", TARGET, SHA))

    def test_a_start_cap_after_the_handback_waits_on_the_merge_and_wakes(self):
        r, mesh, gl, t, a = self.handed_back()
        self.started(r, t)
        r._start_cap(dict(mesh.tasks[t["id"]]), 2)
        rec = r._cap_wait(t["id"])
        self.assertEqual(rec["wait"]["kind"], "merge")
        self.assertIsNone(r._handback_wait(t["id"]))             # consumed by the cap record
        self.assertTrue(self.woke(r, mesh, t, rec))
        gl.mr_rows[TARGET] = mr("merged")
        self.assertTrue(self.woke(r, mesh, t, rec))
        self.assertIn("merged", r._cap_wake_reasons[t["id"]])

    def test_nothing_is_kept_when_the_task_left_this_agent_or_the_work_statuses(self):
        for cat, assignee in (("todo", "agent-other"), ("done", AGENT_ID), ("review", AGENT_ID)):
            with self.subTest(cat=cat, assignee=assignee):
                r, mesh, gl, t, a = self.handed_back(cat=cat, assignee=assignee)
                self.assertIsNone(r._handback_wait(t["id"]))
                self.tearDown()
                self.setUp()

    def test_the_wait_is_kept_when_the_handback_cleared_the_assignee(self):
        r, mesh, gl, t, a = self.handed_back(assignee=None)      # move_task to todo without assignee_id
        kept = r._handback_wait(t["id"])
        self.assertEqual((kept["kind"], kept["target"], kept["merge"]["sha"]), ("merge", TARGET, SHA))

    def test_a_non_vcs_wait_is_not_kept(self):
        r, mesh, gl, t, a = self.handed_back(line="⏳ WAIT card:#abcdef12")
        self.assertIsNone(r._handback_wait(t["id"]))

    def test_a_later_work_start_supersedes_the_kept_wait(self):
        r, mesh, gl, t, a = self.handed_back()
        self.cfg.daily_start_cap = 0
        r._start_attempt = lambda *args, **kw: True
        self.assertTrue(r._start_one(dict(mesh.tasks[t["id"]])))
        self.assertIsNone(r._handback_wait(t["id"]))
