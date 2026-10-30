import datetime as dt
import json
import unittest

from mesh_runner import selection
from mesh_runner.mesh import MeshError
from mesh_runner.selection import StatusCategories
from mesh_runner.wake import SYSTEM_AUTHOR, GitLab, changed_since, check, foreign_comment, gitlab_mr_targets, vcs_event

NOW = dt.datetime(2026, 10, 16, 20, 0, tzinfo=dt.timezone.utc)


class StubGitLab(GitLab):
    def __init__(self, mr=None, pipeline="running"):
        super().__init__("https://git.example", "t")
        self.mr, self.pipeline = mr or {}, pipeline

    def get(self, path):
        return self.mr if "merge_requests" in path else {"status": self.pipeline}


def mr(sha="abc", head_sha="abc", status="running", state="opened", pid=7):
    return {"state": state, "sha": sha,
            "head_pipeline": {"id": pid, "sha": head_sha, "status": status} if head_sha else None}


class Seed(unittest.TestCase):
    def test_final_pipeline_settles_at_once(self):
        for status in ("failed", "success", "canceled", "manual"):
            out = StubGitLab(pipeline=status).seed("pipeline", "g/a#5", NOW)
            self.assertIn("settled", out, status)
        out = StubGitLab(pipeline="running").seed("pipeline", "g/a#5", NOW)
        self.assertNotIn("settled", out)
        self.assertEqual(out["seen"], {"pipeline": "running"})

    def test_mr_head_pipeline_already_final_settles(self):
        out = StubGitLab(mr(status="success")).seed("mr", "g/a!7", NOW)
        self.assertIn("already finished (success)", out["settled"])
        self.assertIn("already merged", StubGitLab(mr(state="merged")).seed("mr", "g/a!7", NOW)["settled"])
        self.assertNotIn("settled", StubGitLab(mr(status="running")).seed("mr", "g/a!7", NOW))

    def test_a_result_on_an_older_commit_is_not_the_head_result(self):
        out = StubGitLab(mr(sha="new", head_sha="old", status="failed")).seed("mr", "g/a!7", NOW)
        self.assertNotIn("settled", out)
        self.assertIn("no_pipeline_since", out["seen"])


class MrWithoutPipeline(unittest.TestCase):
    def test_head_without_pipeline_wakes_after_the_grace_once_per_commit(self):
        gl = StubGitLab(mr(sha="new", head_sha="old", status="failed"))
        last = gl.seed("mr", "g/a!7", NOW)["seen"]
        self.assertEqual(gl.mr_event("g/a!7", last, NOW + dt.timedelta(minutes=5)), "")
        event = gl.mr_event("g/a!7", last, NOW + dt.timedelta(minutes=16))
        self.assertIn("has no pipeline", event)
        self.assertEqual(gl.mr_event("g/a!7", last, NOW + dt.timedelta(minutes=40)), "")   # told once
        gl.mr = mr(sha="newer", head_sha="old", status="failed")                            # another push
        self.assertEqual(gl.mr_event("g/a!7", last, NOW + dt.timedelta(minutes=41)), "")   # its own grace
        self.assertIn("has no pipeline", gl.mr_event("g/a!7", last, NOW + dt.timedelta(minutes=57)))

    def test_a_pipeline_that_appears_on_the_head_clears_the_clock(self):
        gl = StubGitLab(mr(sha="new", head_sha="old", status="failed", pid=7))
        last = gl.seed("mr", "g/a!7", NOW)["seen"]
        gl.mr = mr(sha="new", head_sha="new", status="running", pid=8)
        self.assertEqual(gl.mr_event("g/a!7", last, NOW + dt.timedelta(minutes=20)), "")
        self.assertNotIn("no_pipeline_since", last)
        gl.mr = mr(sha="new", head_sha="new", status="success", pid=8)
        self.assertIn("pipeline 8 success", gl.mr_event("g/a!7", last, NOW + dt.timedelta(minutes=30)))


class MergedOrClosed(unittest.TestCase):
    def test_redeclared_wait_on_a_merged_mr_wakes_once(self):
        for state in ("merged", "closed"):
            gl = StubGitLab(mr(state=state))
            seeded = gl.seed("mr", "g/a!7", NOW)
            self.assertIn("settled", seeded)
            last = seeded["seen"]
            self.assertEqual(gl.mr_event("g/a!7", last, NOW), "")       # told by the seed already
            self.assertEqual(gl.mr_event("g/a!7", last, NOW), "")

    def test_an_mr_merged_during_the_wait_wakes_once(self):
        gl = StubGitLab(mr(status="running"))
        last = gl.seed("mr", "g/a!7", NOW)["seen"]
        gl.mr = mr(state="merged")
        self.assertIn("is merged", gl.mr_event("g/a!7", last, NOW))
        self.assertEqual(gl.mr_event("g/a!7", last, NOW), "")


class MergedResults(unittest.TestCase):
    def test_a_merge_ref_pipeline_is_the_head_pipeline(self):
        m = mr(sha="head", head_sha="mergecommit", status="success")
        m["head_pipeline"]["ref"] = "refs/merge-requests/7/merge"
        self.assertIn("settled", StubGitLab(m).seed("mr", "g/a!7", NOW))


class ForeignComments(unittest.TestCase):
    def test_mesh_automation_does_not_count(self):
        at = (NOW + dt.timedelta(minutes=1)).isoformat()
        base = {"created_at": at, "body": "x"}
        since = NOW
        self.assertIsNone(foreign_comment([dict(base, author_id=SYSTEM_AUTHOR, author_type="system",
                                                body="⏸ Gate armed")], "me", since))
        self.assertIsNone(foreign_comment([dict(base, author_id="other", auto_summary=True)], "me", since))
        self.assertIsNone(foreign_comment([dict(base, author_id="other", body="[runner] note")], "me", since))
        self.assertIsNone(foreign_comment([dict(base, author_id="me")], "me", since))
        self.assertIsNotNone(foreign_comment([dict(base, author_id="person", author_type="user")], "me", since))



def at(minutes):
    return (NOW + dt.timedelta(minutes=minutes)).isoformat()


class ChangesSince(unittest.TestCase):
    """Events after the agent's last turn, read from timestamps: no baseline needed."""
    ECHO = dt.timedelta(minutes=2)

    def test_merge_request_events_by_gitlab_time(self):
        gl = StubGitLab(dict(mr(status="running"), merged_at=at(-5), state="merged"))
        self.assertEqual(changed_since(gl, "mr", "g/a!7", NOW, self.ECHO), "")         # merged before: seen
        gl.mr["merged_at"] = at(1)
        self.assertEqual(changed_since(gl, "mr", "g/a!7", NOW, self.ECHO), "")         # its own merge's echo
        gl.mr["merged_at"] = at(5)
        self.assertEqual(changed_since(gl, "mr", "g/a!7", NOW, self.ECHO), "MR g/a!7 was merged")
        gl.mr = mr(status="success")
        gl.mr["head_pipeline"]["finished_at"] = at(1)
        self.assertEqual(changed_since(gl, "mr", "g/a!7", NOW, self.ECHO), "MR g/a!7: pipeline 7 success on abc")
        gl.mr = mr(status="running")
        gl.mr["head_pipeline"]["created_at"] = at(1)                                      # its own push's echo
        self.assertEqual(changed_since(gl, "mr", "g/a!7", NOW, self.ECHO), "")
        gl.mr["head_pipeline"]["created_at"] = at(10)
        self.assertIn("new head", changed_since(gl, "mr", "g/a!7", NOW, self.ECHO))

    def test_pipeline_finished_after(self):
        gl = StubGitLab()
        gl.get = lambda path: {"status": "failed", "finished_at": at(3)}
        self.assertEqual(changed_since(gl, "pipeline", "g/a#5", NOW), "pipeline g/a#5 finished: failed")
        gl.get = lambda path: {"status": "running", "updated_at": at(3)}
        self.assertEqual(changed_since(gl, "pipeline", "g/a#5", NOW), "")

    def test_mesh_link_stamps(self):
        old = "2026-01-01T00:00:00Z"
        link = {"link_type": "pr", "title": "!7", "status": "open", "created_at": old}
        self.assertEqual(vcs_event([link], NOW, self.ECHO), "")
        self.assertEqual(vcs_event([dict(link, pipeline_status="failed", pipeline_updated_at=at(1))], NOW, self.ECHO),
                         "")                                                             # its own push's echo
        self.assertEqual(vcs_event([dict(link, pipeline_status="failed", pipeline_updated_at=at(5))], NOW, self.ECHO),
                         "MR !7: pipeline failed")
        self.assertEqual(vcs_event([dict(link, status="closed", status_synced_at=at(1))], NOW, self.ECHO), "")
        self.assertEqual(vcs_event([dict(link, status="closed", status_synced_at=at(5))], NOW, self.ECHO),
                         "MR !7 was closed")
        self.assertEqual(vcs_event([{"link_type": "commit", "title": "fix", "created_at": at(5)}], NOW, self.ECHO),
                         "commit linked: fix")

    def test_mr_targets_only_on_this_gitlab(self):
        links = [{"url": "https://git.example/g/sub/app/-/merge_requests/12"},
                 {"url": "https://git.example/g/sub/app/-/merge_requests/12#note_1"},
                 {"url": "https://github.com/o/r/pull/3"}, {"url": "https://other.example/g/x/-/merge_requests/1"}]
        self.assertEqual(gitlab_mr_targets(links, "https://git.example"), ["g/sub/app!12"])


class Denied:
    """An agent of another workspace: the awaited card is not its to read."""
    def request(self, method, path, body=None, query=None):
        raise MeshError(403, "workspace access denied [GET %s]" % path)

    def statuses(self, pid):
        raise MeshError(403, "agent is not a member of this project")


class Reader(Denied):
    """An agent of this process whose grant covers that workspace."""
    def __init__(self, status="done"):
        self.status = status

    def task_by_short(self, short):
        return {"id": short + "-full", "project_id": "p-w1", "status_id": "st-w1", "status_category": self.status}


class CrossWorkspaceCardWait(unittest.TestCase):
    def setUp(self):
        self._readers = list(selection._READERS)
        selection._READERS.clear()
        selection._NOBODY.clear()
        selection._READ_BY.clear()

    def tearDown(self):
        for r in self._readers:
            selection._READERS.add(r)

    def _check(self, wait):
        mine = StatusCategories(Denied())
        return mine, check(mine.mesh, mine, None, {}, wait, "me", [], NOW)

    def wait(self, **kw):
        return dict({"kind": "card", "target": "22223333", "since": NOW.isoformat(), "started": NOW.isoformat()}, **kw)

    def test_a_denial_nobody_can_read_around_does_not_wake_the_session(self):
        wait = self.wait()
        mine = StatusCategories(Denied())
        for _ in range(20):                      # far past max_errors
            self.assertEqual(check(mine.mesh, mine, None, {}, wait, "me", [], NOW), "")
        self.assertFalse(wait.get("met"))
        self.assertFalse(wait.get("errors"))
        self.assertTrue(wait["unreadable"])

    def test_another_agent_of_the_process_delivers_the_result(self):
        wait = self.wait()
        mine = StatusCategories(Denied())
        reader = StatusCategories(Reader("done"))
        self.assertIn("is done", check(mine.mesh, mine, None, {}, wait, "me", [], NOW))
        self.assertTrue(wait["met"])
        del reader

    def test_a_card_read_without_its_category_and_statuses_denied_does_not_wake(self):
        class NoCategory(Reader):
            def task_by_short(self, short):
                return {"id": short + "-full", "project_id": "p-w2", "status_id": "st-w2"}
        wait = self.wait(errors=4)
        mine = StatusCategories(Denied())
        reader = StatusCategories(NoCategory())
        for _ in range(10):
            self.assertEqual(check(mine.mesh, mine, None, {}, wait, "me", [], NOW), "")
        self.assertEqual(wait["errors"], 0)
        del reader

    def test_a_denial_plus_a_fault_of_the_other_reader_still_counts_toward_a_wake(self):
        class Faulty(Reader):
            def task_by_short(self, short):
                raise MeshError(500, "boom")
        wait = self.wait()
        mine = StatusCategories(Denied())
        reader = StatusCategories(Faulty())
        out = [check(mine.mesh, mine, None, {}, wait, "me", [], NOW) for _ in range(5)]
        self.assertEqual(out[:4], [""] * 4)
        self.assertEqual(wait["errors"], 5)
        self.assertIn("cannot check the card wait", out[-1])
        self.assertFalse(wait.get("unreadable"))
        del reader

    def test_unreadable_wait_still_has_bounded_visibility(self):
        wait = self.wait()
        mine = StatusCategories(Denied())
        later = NOW + dt.timedelta(hours=7)
        self.assertIn("still unreadable", check(mine.mesh, mine, None, {}, wait, "me", [], later))

    def test_a_non_denial_error_still_counts_toward_a_wake(self):
        class Broken(Denied):
            def request(self, method, path, body=None, query=None):
                raise MeshError(500, "boom")
        wait = self.wait()
        mine = StatusCategories(Broken())
        out = [check(mine.mesh, mine, None, {}, wait, "me", [], NOW) for _ in range(5)]
        self.assertIn("cannot check the card wait", out[-1])

    def test_old_transient_pipeline_errors_remain_visible_at_the_error_limit(self):
        class BrokenGitLab:
            def pipeline_event(self, target, seen):
                raise RuntimeError("observer unavailable")

        wait = {"kind": "pipeline", "target": "g/a#5", "since": NOW.isoformat(),
                "started": (NOW - dt.timedelta(hours=8)).isoformat()}
        out = [check(None, None, BrokenGitLab(), {}, wait, "me", [], NOW) for _ in range(5)]
        self.assertEqual(out[:4], [""] * 4)
        self.assertIn("cannot check the pipeline wait", out[-1])

    def test_old_healthy_unchanged_pipeline_stays_asleep(self):
        class HealthyGitLab:
            def pipeline_event(self, target, seen):
                return ""

        wait = {"kind": "pipeline", "target": "g/a#5", "since": NOW.isoformat(),
                "started": (NOW - dt.timedelta(hours=8)).isoformat()}
        self.assertEqual(check(None, None, HealthyGitLab(), {}, wait, "me", [], NOW), "")
        self.assertNotIn("recheck_due", wait)

    def test_persisted_pipeline_without_observer_stays_visible_without_recheck(self):
        # Simulates a pre-restart WAIT whose GitLab observer was absent on recovery.
        original = {"kind": "pipeline", "target": "g/a#5", "since": NOW.isoformat(),
                    "started": (NOW - dt.timedelta(hours=3)).isoformat(), "seen": {"pipeline": "running"}}
        wait = json.loads(json.dumps(original))
        event = check(None, None, None, {}, wait, "me", [], NOW,
                      max_wait=dt.timedelta(hours=6), first_recheck=dt.timedelta(hours=2))
        self.assertEqual(event, "")
        self.assertEqual(wait["integration_blocked"], "GitLab observer unavailable")
        self.assertFalse(wait.get("recheck_due"))
        self.assertFalse(wait.get("met"))
        self.assertEqual(wait["kind"], "pipeline")
        self.assertEqual(wait["target"], original["target"])
        self.assertEqual(wait["seen"], original["seen"])

    def test_repeated_missing_observer_remains_unobserved_and_visible(self):
        wait = {"kind": "mr", "target": "g/a!7", "since": NOW.isoformat(),
                "started": (NOW - dt.timedelta(hours=7)).isoformat(), "rechecks": 1}
        first = check(None, None, None, {}, wait, "me", [], NOW)
        self.assertEqual(first, "")
        self.assertIn("observer unavailable", wait["integration_blocked"])
        self.assertFalse(wait.get("recheck_due"))
        self.assertFalse(wait.get("met"))
        # Persist the marker and check again: absence must never be mistaken for a
        # healthy unchanged MR or for a completed condition.
        wait = json.loads(json.dumps(wait))
        again = check(None, None, None, {}, wait, "me", [], NOW)
        self.assertEqual(again, "")
        self.assertIn("observer unavailable", wait["integration_blocked"])
        self.assertFalse(wait.get("recheck_due"))
        self.assertFalse(wait.get("met"))


if __name__ == "__main__":
    unittest.main()
