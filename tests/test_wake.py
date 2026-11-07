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

    def task(self, tid):
        raise MeshError(403, "workspace access denied")


class Reader(Denied):
    """An agent of this process whose grant covers that workspace."""
    def __init__(self, status="done"):
        self.status = status

    def task_by_short(self, short):
        return {"id": short + "-full", "project_id": "p-w1", "status_id": "st-w1", "status_category": self.status}

    def task(self, tid):
        if tid.startswith("other-"):
            raise MeshError(404, "not found")
        return {"id": tid, "project_id": "p-w1", "status_id": "st-w1", "status_category": self.status}


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

            def task(self, tid):
                return {"id": tid, "project_id": "p-w2", "status_id": "st-w2"}
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
        self.assertIn("UNKNOWN", check(mine.mesh, mine, None, {}, wait, "me", [], later))

    def test_unreadable_wake_names_the_access_action_and_no_target_content(self):
        wait = self.wait()
        mine = StatusCategories(Denied())
        out = check(mine.mesh, mine, None, {}, wait, "me", [], NOW + dt.timedelta(hours=7))
        self.assertIn("ask the owner of that workspace for access", out)
        self.assertNotIn("done", out.replace("UNKNOWN (unreadable, not done and not missing)", ""))
        self.assertFalse(wait.get("met"))

    def test_a_short_ref_two_workspaces_resolve_differently_is_ambiguous_not_guessed(self):
        class Other(Reader):
            def task_by_short(self, short):
                return {"id": "other-" + short, "project_id": "p-w3", "status_id": "s", "status_category": "done"}

            def task(self, tid):
                if not tid.startswith("other-"):
                    raise MeshError(404, "not found")
                return {"id": tid, "project_id": "p-w3", "status_id": "s", "status_category": "done"}
        wait = self.wait()
        mine = StatusCategories(Denied())
        a, b = StatusCategories(Reader("done")), StatusCategories(Other("done"))
        out = check(mine.mesh, mine, None, {}, wait, "me", [], NOW + dt.timedelta(hours=7))
        self.assertFalse(wait.get("met"))
        self.assertTrue(wait["ambiguous"])
        self.assertIn("full task id", out)
        del a, b

    def test_a_direct_read_is_checked_against_other_tenants_once(self):
        class Own(Denied):
            def request(self, method, path, body=None, query=None):
                return {"id": "22223333-full", "project_id": "p", "status_id": "s", "status_category": "done"}

        class Other(Reader):
            def task_by_short(self, short):
                return {"id": "other-" + short, "project_id": "p-w3", "status_id": "s", "status_category": "done"}

            def task(self, tid):
                if not tid.startswith("other-"):
                    raise MeshError(404, "not found")
                return {"id": tid, "project_id": "p-w3", "status_id": "s", "status_category": "done"}
        wait = self.wait()
        mine = StatusCategories(Own())
        b = StatusCategories(Other("done"))
        out = check(mine.mesh, mine, None, {}, wait, "me", [], NOW + dt.timedelta(hours=7))
        self.assertFalse(wait.get("met"))
        self.assertTrue(wait["ambiguous"])
        self.assertIn("full task id", out)
        del b

    def test_a_stale_nobody_cache_does_not_skip_the_direct_read_collision_check(self):
        class Own(Denied):
            def request(self, method, path, body=None, query=None):
                return {"id": "22223333-full", "project_id": "p", "status_id": "s", "status_category": "done"}

        class Other(Reader):
            def task_by_short(self, short):
                return {"id": "other-" + short, "project_id": "p-w3", "status_id": "s", "status_category": "done"}

            def task(self, tid):
                if not tid.startswith("other-"):
                    raise MeshError(404, "not found")
                return {"id": tid, "project_id": "p-w3", "status_id": "s", "status_category": "done"}
        wait = self.wait()
        mine = StatusCategories(Own())
        b = StatusCategories(Other("done"))
        selection._NOBODY["s:22223333"] = 10 ** 12          # an earlier "nobody could read it"
        check(mine.mesh, mine, None, {}, wait, "me", [], NOW + dt.timedelta(hours=7))
        self.assertFalse(wait.get("met"))
        self.assertTrue(wait["ambiguous"])
        del b

    def test_a_direct_read_of_another_card_never_replaces_the_pin(self):
        class Own(Denied):
            def request(self, method, path, body=None, query=None):
                return {"id": "someone-else", "project_id": "p", "status_id": "s", "status_category": "done"}
        wait = self.wait(target_id="22223333-full")
        mine = StatusCategories(Own())
        out = check(mine.mesh, mine, None, {}, wait, "me", [], NOW + dt.timedelta(hours=7))
        self.assertFalse(wait.get("met"))
        self.assertEqual(wait["target_id"], "22223333-full")
        self.assertIn("full task id", out)

    def test_own_next_supersedes_a_wait_whose_target_cannot_be_read(self):
        wait = self.wait(target_id="22223333-full")
        mine = StatusCategories(Denied())
        own = {"author_id": "me", "body": "go on\n\n↻ NEXT",
               "created_at": (NOW + dt.timedelta(minutes=1)).isoformat()}
        out = check(mine.mesh, mine, None, {}, wait, "me", [own], NOW + dt.timedelta(minutes=2))
        self.assertIn("superseded", out)
        self.assertTrue(wait["met"])
        foreign = {"author_id": "other", "body": "hello", "author_name": "x",
                   "created_at": (NOW + dt.timedelta(minutes=1)).isoformat()}
        wait2 = self.wait(target_id="22223333-full")
        out2 = check(mine.mesh, mine, None, {}, wait2, "me", [foreign], NOW + dt.timedelta(minutes=2))
        self.assertNotIn("superseded", out2)                 # someone else's NEXT-less comment is no supersede
        self.assertFalse(wait2.get("met"))

    def test_a_pinned_card_is_read_by_its_id_when_the_short_ref_reads_as_another_card(self):
        class Own(Denied):
            def request(self, method, path, body=None, query=None):
                if "by-short-id" in path:
                    return {"id": "someone-else", "project_id": "p", "status_id": "s", "status_category": "review"}
                raise MeshError(403, "workspace access denied")
        wait = self.wait(target_id="22223333-full")
        mine = StatusCategories(Own())
        a = StatusCategories(Reader("done"))                 # A's workspace grant
        self.assertIn("is done", check(mine.mesh, mine, None, {}, wait, "me", [], NOW))
        self.assertEqual(wait["target_id"], "22223333-full")
        del a

    def test_a_masked_404_with_a_pin_is_not_gone_and_a_reader_of_the_pin_delivers(self):
        class Own(Denied):
            def request(self, method, path, body=None, query=None):
                raise MeshError(404, "not found")
        wait = self.wait(target_id="22223333-full")
        mine = StatusCategories(Own())
        out = check(mine.mesh, mine, None, {}, wait, "me", [], NOW + dt.timedelta(hours=7))
        self.assertNotIn("gone", out)
        self.assertIn("UNKNOWN", out)
        a = StatusCategories(Reader("done"))
        self.assertIn("is done", check(mine.mesh, mine, None, {}, wait, "me", [], NOW))
        del a

    def test_a_reader_fault_means_identity_is_not_established_and_nothing_is_pinned(self):
        class Own(Denied):
            def request(self, method, path, body=None, query=None):
                return {"id": "22223333-full", "project_id": "p", "status_id": "s", "status_category": "done"}

        class Faulty(Reader):
            def task_by_short(self, short):
                raise MeshError(500, "boom")
        wait = self.wait()
        mine = StatusCategories(Own())
        f = StatusCategories(Faulty())
        out = check(mine.mesh, mine, None, {}, wait, "me", [], NOW)
        self.assertEqual(out, "")
        self.assertFalse(wait.get("met"))
        self.assertNotIn("target_id", wait)
        self.assertEqual(wait["errors"], 1)
        del f

    def test_an_unpinned_masked_404_is_resolved_by_a_granted_reader_before_it_is_gone(self):
        class Own(Denied):
            def request(self, method, path, body=None, query=None):
                raise MeshError(404, "not found")
        wait = self.wait()
        mine = StatusCategories(Own())
        a = StatusCategories(Reader("done"))
        out = check(mine.mesh, mine, None, {}, wait, "me", [], NOW)
        self.assertIn("is done", out)
        self.assertNotIn("gone", out)
        self.assertEqual(wait["target_id"], "22223333-full")
        del a

    def test_an_unpinned_404_nobody_can_read_is_unknown_not_gone(self):
        class Own(Denied):
            def request(self, method, path, body=None, query=None):
                raise MeshError(404, "not found")
        wait = self.wait()
        mine = StatusCategories(Own())
        self.assertEqual(check(mine.mesh, mine, None, {}, wait, "me", [], NOW), "")   # not gone, not met
        self.assertFalse(wait.get("met"))
        self.assertTrue(wait.get("unreadable"))
        out = check(mine.mesh, mine, None, {}, wait, "me", [], NOW + dt.timedelta(hours=7))
        self.assertIn("UNKNOWN", out)
        self.assertNotIn("is gone", out)
        self.assertFalse(wait.get("met"))

    def test_a_late_colliding_reader_does_not_unpin_a_delivered_card(self):
        class Own(Denied):
            def request(self, method, path, body=None, query=None):
                return {"id": "22223333-full", "project_id": "p", "status_id": "s", "status_category": "done"}

        class Other(Reader):
            def task_by_short(self, short):
                return {"id": "other-" + short, "project_id": "p-w3", "status_id": "s", "status_category": "done"}

            def task(self, tid):
                if not tid.startswith("other-"):
                    raise MeshError(404, "not found")
                return {"id": tid, "project_id": "p-w3", "status_id": "s", "status_category": "done"}
        wait = self.wait()
        mine = StatusCategories(Own())
        self.assertIn("is done", check(mine.mesh, mine, None, {}, wait, "me", [], NOW))     # A resolved and pinned
        self.assertEqual(wait["target_id"], "22223333-full")
        b = StatusCategories(Other("done"))                                                 # B appears later
        wait.pop("met", None)
        self.assertIn("is done", check(mine.mesh, mine, None, {}, wait, "me", [], NOW))     # A still delivered
        self.assertEqual(wait["target_id"], "22223333-full")
        self.assertFalse(wait.get("ambiguous"))
        del b

    def test_a_pinned_card_that_is_no_longer_readable_stays_unknown_even_if_b_is(self):
        class Other(Reader):
            def task_by_short(self, short):
                return {"id": "other-" + short, "project_id": "p-w3", "status_id": "s", "status_category": "done"}

            def task(self, tid):
                if not tid.startswith("other-"):
                    raise MeshError(404, "not found")
                return {"id": tid, "project_id": "p-w3", "status_id": "s", "status_category": "done"}
        wait = self.wait(target_id="22223333-full")
        mine = StatusCategories(Denied())                    # A's grant is gone
        b = StatusCategories(Other("done"))
        out = check(mine.mesh, mine, None, {}, wait, "me", [], NOW + dt.timedelta(hours=7))
        self.assertFalse(wait.get("met"))
        self.assertIn("UNKNOWN", out)
        self.assertEqual(wait["target_id"], "22223333-full")
        del b

    def test_a_direct_read_with_no_colliding_card_is_pinned_and_delivers(self):
        class Own(Denied):
            def request(self, method, path, body=None, query=None):
                return {"id": "22223333-full", "project_id": "p", "status_id": "s", "status_category": "done"}
        wait = self.wait()
        mine = StatusCategories(Own())
        self.assertIn("is done", check(mine.mesh, mine, None, {}, wait, "me", [], NOW))
        self.assertEqual(wait["target_id"], "22223333-full")

    def test_a_resolved_target_is_pinned_to_its_full_id(self):
        class Other(Reader):
            def task_by_short(self, short):
                return {"id": "other-" + short, "project_id": "p-w3", "status_id": "s", "status_category": "done"}

            def task(self, tid):
                if not tid.startswith("other-"):
                    raise MeshError(404, "not found")
                return {"id": tid, "project_id": "p-w3", "status_id": "s", "status_category": "done"}
        wait = self.wait(target_id="22223333-full")
        mine = StatusCategories(Denied())
        a, b = StatusCategories(Reader("review")), StatusCategories(Other("done"))
        self.assertEqual(check(mine.mesh, mine, None, {}, wait, "me", [], NOW), "")   # not the other tenant's done
        self.assertFalse(wait.get("met"))
        del a, b

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
