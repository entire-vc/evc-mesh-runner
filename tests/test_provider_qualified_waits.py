"""Provider-qualified mr:/pipeline: waits: ``github:`` / ``gitlab:`` select the transport explicitly,
never fall back to the other provider, and keep their own observation namespace."""
import datetime as dt
import unittest
from unittest import mock

from fakes import FakeGitLab, FakeMesh, calls, task, wait_mr, write_replies
from test_runner import Bench

from mesh_runner.outcome import parse_outcome
from mesh_runner.wake import (GitHub, GitLab, ProviderUnavailable, ci_delivery_key, ci_observation,
                              split_provider, target_jobs_state)

NOW = dt.datetime(2026, 11, 3, tzinfo=dt.timezone.utc)
SHA = "a" * 40
PR = {"state": "open", "merged": False, "merged_at": None, "head": {"sha": SHA}, "base": {"ref": "main"},
      "labels": []}
RUN = {"id": 41664214493, "head_sha": SHA, "head_branch": "main", "status": "completed",
       "conclusion": "success", "created_at": "2026-11-03T10:00:00Z", "updated_at": "2026-11-03T10:05:00Z"}
GL_MR = {"state": "opened", "sha": SHA, "head_pipeline": {"id": 7, "status": "success", "sha": SHA}}


def parse(text, merge=False):
    return parse_outcome(text, NOW, merge=merge)


class Parse(unittest.TestCase):
    def test_bare_keeps_semantics(self):
        o = parse("⏳ WAIT mr:entire-vc/evc-mesh!814")
        self.assertEqual((o.kind, o.target), ("mr", "entire-vc/evc-mesh!814"))

    def test_explicit_providers_parse_on_every_predicate(self):
        for prov in ("github", "gitlab"):
            o = parse(f"⏳ WAIT mr:{prov}:entire-vc/evc-mesh!814")
            self.assertEqual((o.kind, o.target), ("mr", f"{prov}:entire-vc/evc-mesh!814"))
            o = parse(f"⏳ WAIT pipeline:{prov}:entire-vc/evc-mesh#5")
            self.assertEqual((o.kind, o.target), ("pipeline", f"{prov}:entire-vc/evc-mesh#5"))
            o = parse(f"⏳ WAIT mr:{prov}:o/r!3 until=merged after={SHA}:opened")
            self.assertEqual(o.extra["mr_state"]["sha"], SHA)
            self.assertEqual(o.target, f"{prov}:o/r!3")
            o = parse(f"⏳ WAIT mr:{prov}:o/r!3 until=merged")
            self.assertEqual(o.extra["mr_default"], {"predicate": "merged"})
            if prov == "gitlab":
                o = parse(f"⏳ WAIT mr:{prov}:o/r!3 until=change after={SHA}:9:running")
                self.assertEqual(o.extra["ci_change"]["pipeline"], 9)
                o = parse(f"⏳ WAIT mr:{prov}:o/r!3 until=success")
                self.assertEqual((o.target, o.extra["ci_default"]["predicate"]), (f"{prov}:o/r!3", "success"))
            o = parse(f"⏳ WAIT pipeline:{prov}:o/r#9 until=change after={SHA}:9:failed jobs=[a,b]")
            self.assertEqual((o.target, o.jobs), (f"{prov}:o/r#9", ("a", "b")))

    def test_github_mr_ci_predicates_are_refused_with_the_run_spelling(self):
        for line in (f"⏳ WAIT mr:github:o/r!3 until=change after={SHA}:9:running", "⏳ WAIT mr:github:o/r!3 until=success",
                     "⏳ WAIT mr:github:o/r!3 until=change jobs=[a]"):
            o = parse(line)
            self.assertEqual(o.kind, "unparsed", line)
            self.assertIn("pipeline:github:", o.note)
        self.assertEqual(parse("⏳ WAIT mr:github:o/r!3").kind, "mr")

    def test_unknown_provider_is_not_a_wait_target(self):
        self.assertEqual(parse("⏳ WAIT mr:bitbucket:o/r!3").kind, "unparsed")

    def test_qualified_merge_is_rejected_not_sent_to_the_gate(self):
        o = parse(f"⏳ WAIT merge:github:o/r!3 after={SHA}", merge=True)
        self.assertEqual(o.kind, "unparsed")
        self.assertIn("no provider-qualified form", o.note)
        self.assertEqual(parse(f"⏳ WAIT merge:o/r!3 after={SHA}", merge=True).kind, "merge")


class Transport(unittest.TestCase):
    def observer(self, orgs):
        return GitLab("https://gl.example", "t", GitHub(orgs=orgs))

    def test_explicit_github_wins_without_allowlist_entry(self):
        gl = self.observer(())
        with mock.patch.object(GitHub, "get", return_value=PR) as gh, \
                mock.patch.object(GitLab, "get", side_effect=AssertionError("GitLab used")):
            self.assertEqual(gl.get_mr("github:entire-vc/evc-mesh!814")["state"], "opened")
        self.assertEqual(gh.call_args[0][0], "repos/entire-vc/evc-mesh/pulls/814")

    def test_explicit_gitlab_ignores_the_github_allowlist(self):
        gl = self.observer(("entire-vc/evc-mesh",))
        with mock.patch.object(GitHub, "get", side_effect=AssertionError("GitHub used")), \
                mock.patch.object(GitLab, "get", return_value=GL_MR) as g:
            self.assertEqual(gl.get_mr("gitlab:entire-vc/evc-mesh!814")["state"], "opened")
        self.assertIn("projects/entire-vc%2Fevc-mesh/merge_requests/814", g.call_args[0][0])

    def test_bare_follows_the_allowlist_as_before(self):
        gl = self.observer(("entire-vc/evc-mesh",))
        with mock.patch.object(GitHub, "get", return_value=PR), \
                mock.patch.object(GitLab, "get", side_effect=AssertionError("GitLab used")):
            gl.get_mr("entire-vc/evc-mesh!814")
        gl = self.observer(())
        with mock.patch.object(GitHub, "get", side_effect=AssertionError("GitHub used")), \
                mock.patch.object(GitLab, "get", return_value=GL_MR):
            gl.get_mr("entire-vc/evc-mesh!814")

    def test_runs_and_pipelines_route_by_selector(self):
        gl = self.observer(("o/r",))                       # allowlisted, still explicit gitlab
        with mock.patch.object(GitHub, "get", side_effect=AssertionError("GitHub used")), \
                mock.patch.object(GitLab, "get", return_value={"id": 9, "status": "running"}) as g:
            gl.get_pipeline("gitlab:o/r#9")
        self.assertIn("projects/o%2Fr/pipelines/9", g.call_args[0][0])
        gl = self.observer(())
        with mock.patch.object(GitHub, "get", return_value=RUN) as gh, \
                mock.patch.object(GitLab, "get", side_effect=AssertionError("GitLab used")):
            row = gl.get_pipeline("github:o/r#41664214493")
        self.assertEqual((row["status"], row["sha"]), ("success", SHA))
        self.assertEqual(gh.call_args[0][0], "repos/o/r/actions/runs/41664214493")

    def test_github_jobs_are_refused_gitlab_jobs_are_read(self):
        gl = self.observer(())
        with self.assertRaises(ValueError), mock.patch.object(GitLab, "get", side_effect=AssertionError):
            gl.pipeline_jobs("github:o/r#5")
        with mock.patch.object(GitLab, "get", return_value=[]) as g:
            gl.pipeline_jobs("gitlab:o/r#5")
        self.assertIn("projects/o%2Fr/pipelines/5/jobs", g.call_args[0][0])

    def test_github_has_no_newer_pipeline_lookup(self):
        gl = self.observer(())
        with mock.patch.object(GitLab, "get", side_effect=AssertionError):
            self.assertIsNone(gl.newer_pipeline("github:o/r#5", dict(RUN, ref="main")))

    def test_missing_github_config_fails_closed_without_gitlab_fallback(self):
        gl = GitLab("https://gl.example", "t", None)
        with mock.patch.object(GitLab, "get", side_effect=AssertionError("fallback to GitLab")):
            with self.assertRaises(ProviderUnavailable):
                gl.get_mr("github:o/r!1")
            with self.assertRaises(ProviderUnavailable):
                gl.get_pipeline("github:o/r#1")
        self.assertIn("not configured", gl.host("github:o/r!1"))

    def test_missing_gitlab_config_fails_closed_without_github_fallback(self):
        gl = GitLab("", "", GitHub(orgs=("o/r",)))
        with mock.patch.object(GitHub, "get", side_effect=AssertionError("fallback to GitHub")):
            with self.assertRaises(ValueError):
                gl.get_mr("gitlab:o/r!1")

    def test_ci_observation_validates_github_run_identity(self):
        gl = self.observer(())
        with mock.patch.object(GitHub, "get", return_value=RUN):
            self.assertEqual(ci_observation(gl, "pipeline", "github:o/r#41664214493")[:3],
                             (SHA, 41664214493, "success"))
            with self.assertRaises(ValueError):
                ci_observation(gl, "pipeline", "github:o/r#1")


class NamedJobs(unittest.TestCase):
    """The integrated jobs= path keeps the selector (same-path GitHub/GitLab repo, allowlisted)."""
    def setUp(self):
        self.gl = GitLab("https://gl.example", "t", GitHub(orgs=("o/r",)))

    def test_github_jobs_wait_is_refused_and_never_reads_gitlab(self):
        for target in ("github:o/r#5", "github:o/r!5"):
            with mock.patch.object(GitLab, "get", side_effect=AssertionError("GitLab read")), \
                    mock.patch.object(GitHub, "get", side_effect=AssertionError("GitHub read")):
                with self.assertRaises(ValueError) as cm:
                    target_jobs_state(self.gl, target, 5, ["a"])
            self.assertIn("GitHub run has none", str(cm.exception))

    def test_explicit_gitlab_jobs_are_read_from_gitlab_despite_allowlist(self):
        rows = [{"id": 1, "name": "a", "status": "success"}]
        with mock.patch.object(GitHub, "get", side_effect=AssertionError("GitHub read")), \
                mock.patch.object(GitLab, "get", return_value=rows) as g:
            self.assertEqual(target_jobs_state(self.gl, "gitlab:o/r#5", 5, ["a"])[0], "success")
        self.assertIn("projects/o%2Fr/pipelines/5/jobs", g.call_args[0][0])


class Namespace(unittest.TestCase):
    def test_observation_keys_never_collide_across_providers(self):
        keys = {ci_delivery_key(t, 7) for t in ("o/r!1", "github:o/r!1", "gitlab:o/r!1")}
        self.assertEqual(len(keys), 3)
        self.assertEqual(ci_delivery_key("github:o/r!1", 7), ci_delivery_key("github:o/r#7", 7))
        self.assertEqual(ci_delivery_key("o/r!1", 7), ci_delivery_key("o/r#7", 7))   # bare unchanged

    def test_split_provider(self):
        self.assertEqual(split_provider("github:o/r!1"), ("github", "o/r!1"))
        self.assertEqual(split_provider("o/r!1"), ("", "o/r!1"))


class TypedGitHubPR(Bench):
    """`wait_for on=mr provider=github until=merged ... after_status=opened`: the route Mesh names for a GitHub PR
    merge (it answers 422 to on=merge provider=github, so no such merge wait reaches the runner). The runner
    waits on the PR and wakes once it is merged."""

    def turn(self, gl, command):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        write_replies(self.replies, {t["id"][:8]: command, t["id"][:8] + ":resume": wait_mr("o/r", 9, note="next")})
        r = self.runner(mesh, gl)
        r.tick()
        self.settle_all(r)
        return r, t

    def test_it_waits_while_the_pr_is_open_and_wakes_when_it_is_merged(self):
        gl = FakeGitLab()
        gl.mr_rows["github:o/r!3"] = {"state": "opened", "sha": SHA}
        r, t = self.turn(gl, wait_mr("o/r", 3, until="merged", provider="github", after_sha=SHA,
                                     after_status="opened", note="PR showcase"))
        a = r.store.by_state(self.agent.name, ("waiting",))[0]
        self.assertEqual((a.wait["kind"], a.wait["target"]), ("mr", "github:o/r!3"))
        self.assertEqual(a.wait["mr_state"], {"predicate": "merged", "sha": SHA, "state": "opened"})
        r.tick()
        self.settle_all(r)
        self.assertEqual(len(calls(self.calls)), 1)            # still open: nothing woke it
        gl.mr_rows["github:o/r!3"] = {"state": "merged", "merged_at": "2026-11-11T19:00:00Z", "sha": SHA}
        r.tick()
        self.settle_all(r)
        c = calls(self.calls)
        self.assertEqual([x["resume"] for x in c], [False, True])
        self.assertIn("github:o/r!3", c[1]["prompt"])


if __name__ == "__main__":
    unittest.main()
