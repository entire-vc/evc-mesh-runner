"""A time WAIT on a card whose merge request is still open is a timer standing in for the MR/CI event.

The core rules forbid a timer instead of an MR or CI wait, yet the runner used to accept
``wait_for on=time`` on such a card: on 2026-11-18 eleven cards slept to an invented moment, nine
of them on red mutation pipelines, instead of waking on the CI result. The runner now refuses
it and names the MR/pipeline wait to use. It confirms the merge request is open with the forge
itself and accepts the timer (and logs it) when it cannot tell.
"""
import datetime as dt
import logging
import urllib.error

from fakes import FakeGitLab, FakeMesh, calls, task, write_replies, wait_time, wait_mr, FUTURE_AT
from test_runner import Bench

from mesh_runner.mesh import MeshError
from mesh_runner.store import iso

SHA = "c" * 40
MR_URL = "https://git.example/g/app/-/merge_requests/7"


def link(status="open", pipeline="running", url=MR_URL):
    return {"link_type": "pr", "provider": "gitlab", "url": url, "external_id": "7", "status": status,
            "pipeline_status": pipeline, "metadata": {"head_sha": SHA}}


def gitlab(state="opened", pipeline_status="running", pid=41):
    gl = FakeGitLab()
    gl.mr_head["g/app!7"] = f"{pid}:{pipeline_status}"
    gl.mr_state["g/app!7"] = state
    gl.mr_rows["g/app!7"] = {"state": state, "sha": SHA, "iid": 7, "labels": [],
                             "head_pipeline": {"id": pid, "status": pipeline_status, "sha": SHA}}
    return gl


class _GitLabDown(FakeGitLab):
    def get_mr(self, target):
        raise urllib.error.URLError("connection refused")


class TimeWaitOnOpenMergeRequest(Bench):
    def setUp(self):
        super().setUp()
        self.cfg.mesh_effects = True       # an accepted time wait schedules start_after in Mesh

    def run_card(self, links, gl, replies):
        t = task(1, cat="in_progress")
        mesh = FakeMesh([t], max_concurrent=1)
        if links is not None:
            mesh.links = {t["id"]: links}
        t8 = t["id"][:8]
        write_replies(self.replies, {(t8 + k): v for k, v in replies.items()})
        r = self.runner(mesh, gl)
        r.tick()
        self.settle_all(r)
        return r, mesh, t

    def test_time_wait_on_card_with_open_mr_is_refused_with_the_mr_and_pipeline_wait(self):
        hinted = wait_mr("g/app", 7, until="green", after_sha=SHA, after_pipeline_id=41, after_status="running")
        r, mesh, t = self.run_card([link()], gitlab(), {"": wait_time(), ":resume": hinted})
        sent = calls(self.calls)
        self.assertEqual(len(sent), 2, "the time wait was accepted: the card sleeps to an invented moment")
        correction = sent[1]["prompt"]
        self.assertIn("open merge request", correction)
        self.assertIn("wait_for on=mr project_path=g/app mr_iid=7 until=green", correction)
        self.assertIn(f"after_sha={SHA} after_pipeline_id=41 after_status=running", correction)
        self.assertIn("wait_for on=pipeline project_path=g/app pipeline_id=41 until=finished", correction)
        # the hinted command is one the runner accepts: the card now waits on the MR, not on a clock
        waiting = r.store.by_state(self.agent.name, ("waiting",))
        self.assertEqual([a.wait["kind"] for a in waiting], ["mr"])
        self.assertFalse(mesh.tasks[t["id"]].get("start_after"))
        self.assertEqual(r.store.events_on_task(t["id"], "open_mr_time_wait_refused"), 1)

    def test_time_wait_on_red_head_is_refused_with_a_wait_the_runner_accepts(self):
        # the main case of 2026-11-18: a red pipeline slept on a timer. The red-head hint settles as a wait
        hinted = wait_mr("g/app", 7, until="green", after_sha=SHA, after_pipeline_id=41, after_status="failed")
        r, mesh, t = self.run_card([link(pipeline="failed")], gitlab(pipeline_status="failed"),
                                   {"": wait_time(), ":resume": hinted})
        sent = calls(self.calls)
        self.assertGreaterEqual(len(sent), 2, "the time wait on a red head was accepted")
        self.assertIn("head pipeline 41 failed", sent[1]["prompt"])
        self.assertIn(f"until=green after_sha={SHA} after_pipeline_id=41 after_status=failed", sent[1]["prompt"])
        # the hinted wait settled as an MR wait and woke on the red result itself (the owner notice), not
        # on a refusal; the fake then re-declares it for ever, which the existing send-back bound ends
        self.assertIn("pipeline 41 finished: failed", sent[2]["prompt"])
        self.assertNotIn("cannot read", sent[2]["prompt"])
        self.assertIn("Waiting on mr.", [c["body"] for c in mesh.comment_log.get(t["id"], [])])
        self.assertEqual(r.store.events_on_task(t["id"], "open_mr_time_wait_refused"), 1)
        self.assertFalse(mesh.tasks[t["id"]].get("start_after"))

    def test_time_wait_without_merge_request_is_accepted(self):
        # positive control: an external schedule on a card with no merge request stays a valid wait
        r, mesh, t = self.run_card(None, gitlab(), {"": wait_time()})
        self.assertEqual(len(calls(self.calls)), 1)
        waiting = r.store.by_state(self.agent.name, ("waiting",))
        self.assertEqual([a.wait["kind"] for a in waiting], ["time"])
        self.assertTrue(mesh.tasks[t["id"]].get("start_after"))

    def test_merged_or_closed_merge_request_does_not_refuse_a_time_wait(self):
        for status, pipeline in (("merged", "failed"), ("closed", "running")):
            with self.subTest(status=status):
                self.tearDown(); self.setUp()
                r, mesh, t = self.run_card([link(status=status, pipeline=pipeline)],
                                           gitlab(state=status, pipeline_status=pipeline), {"": wait_time()})
                self.assertEqual([a.wait["kind"] for a in r.store.by_state(self.agent.name, ("waiting",))], ["time"])

    def test_stale_open_link_is_checked_with_gitlab(self):
        # Mesh's link still says open (its sync is hours old) but GitLab merged it: not an open MR
        r, mesh, t = self.run_card([link()], gitlab(state="merged", pipeline_status="success"), {"": wait_time()})
        self.assertEqual([a.wait["kind"] for a in r.store.by_state(self.agent.name, ("waiting",))], ["time"])

    def test_unreadable_provider_or_links_accept_the_timer_and_log_it(self):
        with self.assertLogs("mesh_runner", level="INFO") as logs:
            r, mesh, t = self.run_card([link()], _GitLabDown(), {"": wait_time()})
        self.assertEqual([a.wait["kind"] for a in r.store.by_state(self.agent.name, ("waiting",))], ["time"])
        self.assertTrue(any("time WAIT accepted" in line for line in logs.output), logs.output)

        self.tearDown(); self.setUp()
        t = task(1, cat="in_progress")
        mesh = FakeMesh([t], max_concurrent=1)
        def broken(tid):
            raise MeshError(503, "unavailable")
        mesh.vcs_links = broken
        write_replies(self.replies, {t["id"][:8]: wait_time()})
        r = self.runner(mesh, gitlab())
        with self.assertLogs("mesh_runner", level="INFO") as logs:
            r.tick()
            self.settle_all(r)
        self.assertEqual([a.wait["kind"] for a in r.store.by_state(self.agent.name, ("waiting",))], ["time"])
        self.assertTrue(any("time WAIT accepted" in line for line in logs.output), logs.output)

    def test_no_gitlab_observer_accepts_the_timer(self):
        r, mesh, t = self.run_card([link()], None, {"": wait_time()})
        self.assertEqual([a.wait["kind"] for a in r.store.by_state(self.agent.name, ("waiting",))], ["time"])

    def test_insisting_on_the_timer_ends_bounded_not_in_a_loop(self):
        r, mesh, t = self.run_card([link()], gitlab(), {"": wait_time(), ":resume": wait_time()})
        self.assertEqual(len(calls(self.calls)), 2)
        self.assertFalse(r.store.by_state(self.agent.name, ("waiting", "ready", "starting", "running")))
        self.assertFalse(mesh.tasks[t["id"]].get("start_after"))
        self.assertTrue(any("WAIT CORRECTION REQUIRED" in c["body"] for c in mesh.comment_log.get(t["id"], [])))


class PersistedTimeWaitOnOpenMergeRequest(Bench):
    """A time wait declared before the rule, or before the merge request was opened."""

    def waiting(self, links, gl):
        self.cfg.mesh_effects = True
        t = task(1, cat="in_progress")
        mesh = FakeMesh([t], max_concurrent=1)
        mesh.links = {t["id"]: links}
        reads = []
        original = mesh.vcs_links
        mesh.vcs_links = lambda tid: reads.append(tid) or original(tid)
        r = self.runner(mesh, gl)
        r.refresh_me()
        a = r.store.create(self.agent.name, t["id"], "claude")
        mesh.tasks[t["id"]]["start_after"] = FUTURE_AT
        wait = {"kind": "time", "until": FUTURE_AT, "since": iso(), "started": iso(), "scheduled": FUTURE_AT,
                "line": f"⏳ WAIT {FUTURE_AT}: external=fixture-window",
                "external_time": {"event": "fixture-window", "source": "https://status.example.com/w",
                                  "why": "soak window", "at": FUTURE_AT}}
        r.store.update(a, state="waiting", session_id="same-session", wait=wait)
        return r, mesh, t, a, reads

    def test_persisted_time_wait_on_open_mr_is_woken_with_the_mr_wait(self):
        r, mesh, t, a, reads = self.waiting([link(pipeline="failed")], gitlab(pipeline_status="failed"))
        r._wake_one(a)
        current = r.store.get(a.id)
        self.assertEqual((current.state, current.session_id), ("ready", "same-session"))
        self.assertIn("open merge request", current.wake_text)
        self.assertIn("pipeline 41 failed", current.wake_text)
        self.assertIn("wait_for on=mr project_path=g/app mr_iid=7 until=green", current.wake_text)
        self.assertFalse(mesh.tasks[t["id"]].get("start_after"))     # only the runner's own schedule goes

    def test_persisted_time_wait_without_open_mr_keeps_sleeping_and_reads_are_bounded(self):
        r, mesh, t, a, reads = self.waiting([link(status="merged", pipeline="success")],
                                            gitlab(state="merged", pipeline_status="success"))
        r._wake_one(a)
        r._wake_one(a)
        self.assertEqual(r.store.get(a.id).state, "waiting")
        self.assertEqual(mesh.tasks[t["id"]]["start_after"], FUTURE_AT)
        self.assertEqual(len(reads), 1, "the links are read once per recheck interval, not every poll")


class Guidance(Bench):
    """The refusal names a command the runner accepts for the state it read."""

    def test_hint_follows_the_head_pipeline_state(self):
        from mesh_runner.outcome import open_mr_time_wait_guidance
        mr = {"target": "g/app!7", "sha": SHA, "pipeline_id": 41}
        running = open_mr_time_wait_guidance([dict(mr, pipeline_status="running")])
        self.assertIn(f"wait_for on=mr project_path=g/app mr_iid=7 until=green after_sha={SHA} "
                      "after_pipeline_id=41 after_status=running", running)
        self.assertIn("wait_for on=pipeline project_path=g/app pipeline_id=41 until=finished", running)
        failed = open_mr_time_wait_guidance([dict(mr, pipeline_status="failed")])
        self.assertIn("pipeline 41 failed", failed)
        self.assertIn("push", failed)
        self.assertNotIn("until=changed", failed)          # a final baseline is sent back on until=changed
        # a red job the lane does not own: the new head its owner pushes, or the merge of the card's MR
        self.assertIn(f"wait_for on=mr project_path=g/app mr_iid=7 until=green after_sha={SHA} "
                      "after_pipeline_id=41 after_status=failed", failed)
        self.assertIn("until=merged", failed)
        self.assertIn("wait_for on=card target_task_id=<card> target_condition=artifact_published", failed)
        passed = open_mr_time_wait_guidance([dict(mr, pipeline_status="success")])
        self.assertIn(f"wait_for on=mr project_path=g/app mr_iid=7 until=merged after_sha={SHA} "
                      "after_status=opened", passed)
        self.assertIn(f"wait_for on=merge project_path=g/app mr_iid=7 after_sha={SHA}", passed)
        manual = open_mr_time_wait_guidance([dict(mr, pipeline_status="manual")])
        self.assertIn("manual", manual)
        self.assertNotIn("until=changed", manual)
        github = open_mr_time_wait_guidance([{"target": "github:o/r!3", "sha": SHA}])
        self.assertIn(f"wait_for on=mr provider=github project_path=o/r mr_iid=3 until=merged after_sha={SHA} "
                      "after_status=opened", github)
