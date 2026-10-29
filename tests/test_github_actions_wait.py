"""pipeline: waits on a GitHub Actions run, and a pipeline wait whose target does not exist."""
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from fakes import FakeGitLab, FakeMesh, task, write_replies
from test_runner import Bench

from mesh_runner import config as config_mod
from mesh_runner.wake import GitHub, GitLab, ci_observation, pipeline_host

SHA = "c" * 40
REPO = "entire-vc/evc-team-relay-obsidian"
RUN = 40930606626
TARGET = f"{REPO}#{RUN}"


def run(status="completed", conclusion="success", **kw):
    row = {"id": RUN, "head_sha": SHA, "head_branch": "main", "status": status, "conclusion": conclusion,
           "created_at": "2026-10-29T10:00:00Z", "updated_at": "2026-10-29T10:05:00Z",
           "html_url": f"https://github.com/{REPO}/actions/runs/{RUN}", "run_attempt": 1}
    row.update(kw)
    return row


def http_error(code):
    return urllib.error.HTTPError("https://x", code, "boom", {}, None)


def observer(row, orgs=(REPO,)):
    """A GitLab observer over a single GitHub-hosted repo whose run API answers ``row``."""
    gl = GitLab("https://gl.example", "t", GitHub(orgs=tuple(orgs)))
    return gl, mock.patch.object(GitHub, "get", return_value=row)


class Ownership(unittest.TestCase):
    def test_single_repo_owns_its_runs_and_prs_only(self):
        gh = GitHub(orgs=(REPO,))
        self.assertTrue(gh.owns(TARGET))
        self.assertTrue(gh.owns(f"{REPO}!7"))
        self.assertFalse(gh.owns("entire-vc/evc-mesh-runner#12"))
        self.assertFalse(gh.owns("entire-vc/evc-mesh-runner!12"))
        self.assertFalse(gh.owns(f"{REPO}-other#12"))

    def test_org_entry_owns_run_targets_too(self):
        gh = GitHub(orgs=("org",))
        self.assertTrue(gh.owns("org/repo#5"))
        self.assertFalse(gh.owns("organic/repo#5"))

    def test_gitlab_pipeline_of_a_sibling_repo_stays_on_gitlab(self):
        gl, patch = observer(run())
        with patch as gh_get, mock.patch.object(GitLab, "get", return_value={"status": "running"}) as g:
            self.assertEqual(gl.pipeline_status("entire-vc/evc-mesh-runner#12"), "running")
        gh_get.assert_not_called()
        self.assertIn("projects/entire-vc%2Fevc-mesh-runner/pipelines/12", g.call_args[0][0])

    def test_config_declares_a_single_github_repo(self):
        with tempfile.TemporaryDirectory() as d:
            key = Path(d) / "k"
            key.write_text("secret")
            key.chmod(0o600)
            p = Path(d) / "runner.toml"
            p.write_text(f'[runner]\nmesh_url = "http://mesh"\nstate_dir = "{d}/s"\n'
                         f'[github]\nrepos = ["{REPO}"]\norgs = ["other"]\n'
                         f'[[agents]]\nname = "a"\nkey_file = "{key}"\nprofile_dir = "{d}"\n')
            cfg = config_mod.load(p)
        self.assertEqual(sorted(cfg.github_orgs), sorted(["other", REPO]))
        p2 = GitHub(orgs=cfg.github_orgs)
        self.assertTrue(p2.owns(TARGET))
        self.assertFalse(p2.owns("entire-vc/evc-mesh-runner#12"))

    def test_config_rejects_a_repo_entry_that_is_not_org_slash_repo(self):
        with tempfile.TemporaryDirectory() as d:
            key = Path(d) / "k"
            key.write_text("secret")
            key.chmod(0o600)
            p = Path(d) / "runner.toml"
            p.write_text(f'[runner]\nmesh_url = "http://mesh"\nstate_dir = "{d}/s"\n'
                         f'[github]\nrepos = ["entire-vc"]\n'
                         f'[[agents]]\nname = "a"\nkey_file = "{key}"\nprofile_dir = "{d}"\n')
            with self.assertRaises(config_mod.ConfigError):
                config_mod.load(p)


class RunAsPipeline(unittest.TestCase):
    CASES = [
        (("queued", None), "pending"),
        (("requested", None), "pending"),
        (("in_progress", None), "running"),
        (("waiting", None), "waiting_for_resource"),
        (("completed", "success"), "success"),
        (("completed", "neutral"), "success"),
        (("completed", "failure"), "failed"),
        (("completed", "timed_out"), "failed"),
        (("completed", "startup_failure"), "failed"),
        (("completed", "cancelled"), "canceled"),
        (("completed", "skipped"), "skipped"),
        (("completed", "action_required"), "manual"),
    ]

    def test_status_vocabulary(self):
        for (status, conclusion), want in self.CASES:
            with self.subTest(status=status, conclusion=conclusion):
                gl, patch = observer(run(status, conclusion))
                with patch as gh_get:
                    row = gl.get_pipeline(TARGET)
                gh_get.assert_called_once_with(f"repos/{REPO}/actions/runs/{RUN}")
                self.assertEqual(row["status"], want)

    def test_row_has_the_shape_the_ci_checks_read(self):
        gl, patch = observer(run())
        with patch:
            row = gl.get_pipeline(TARGET)
            self.assertEqual(ci_observation(gl, "pipeline", TARGET, row=row), (SHA, RUN, "success"))
        self.assertEqual((row["id"], row["sha"], row["ref"]), (RUN, SHA, "main"))
        self.assertEqual(row["finished_at"], "2026-10-29T10:05:00Z")
        gl, patch = observer(run("in_progress", None))
        with patch:
            self.assertIsNone(gl.get_pipeline(TARGET)["finished_at"])

    def test_unknown_conclusion_is_unreadable_not_guessed(self):
        gl, patch = observer(run("completed", "brand_new"))
        with patch, self.assertRaises(ValueError):
            gl.get_pipeline(TARGET)

    def test_a_run_that_finished_success_before_the_wait_wakes_it(self):
        gl, patch = observer(run())
        with patch:
            seeded = gl.seed("pipeline", TARGET, mock.sentinel.now)
            self.assertIn("had already finished (success)", seeded["settled"])
            self.assertEqual(seeded["seen"], {"pipeline": "success"})
            self.assertEqual(gl.pipeline_event(TARGET, {}), f"pipeline {TARGET} finished: success")

    def test_a_running_run_wakes_when_it_finishes(self):
        last = {}
        gl, patch = observer(run("in_progress", None))
        with patch:
            self.assertEqual(gl.pipeline_event(TARGET, last), "")
        self.assertEqual(last["pipeline"], "running")
        gl, patch = observer(run("completed", "failure"))
        with patch:
            self.assertEqual(gl.pipeline_event(TARGET, last), f"pipeline {TARGET} finished: failed")

    def test_active_jobs_follow_the_run_status(self):
        for row, want in ((run("in_progress", None), True), (run("queued", None), True), (run(), False)):
            gl, patch = observer(row)
            with patch:
                self.assertEqual(gl.pipeline_has_active_jobs(TARGET), want)

    def test_no_newer_run_lookup_on_github(self):
        gl, patch = observer(run("completed", "cancelled"))
        with patch, mock.patch.object(GitLab, "get", side_effect=AssertionError("GitLab API used")):
            self.assertIsNone(gl.newer_pipeline(TARGET))


class Host(unittest.TestCase):
    def test_host_names_the_provider_that_owns_the_target(self):
        gl = GitLab("https://git.entire.host/", "t", GitHub(orgs=(REPO,)))
        self.assertEqual(pipeline_host(gl, TARGET), "github.com")
        self.assertEqual(pipeline_host(gl, "entire-vc/evc-mesh-runner#1"), "git.entire.host")
        self.assertEqual(pipeline_host(FakeGitLab(), "g/a#1"), "git.example")


class MissingPipeline(Bench):
    def flow(self, error):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        gl = FakeGitLab()
        gl.pipeline_errors = {"g/app#999": error}
        r = self.runner(mesh, gl)
        self.addCleanup(r.store.db.close)
        r.refresh_me()
        return r, t

    def turn(self, r, t, line):
        write_replies(self.replies, {t["id"][:8]: line, t["id"][:8] + ":resume": line})
        self.assertEqual(r.start_new(1), 0)
        return self.finish_turn(r)

    def test_404_is_sent_back_with_the_reason(self):
        r, t = self.flow(http_error(404))
        a = self.turn(r, t, "⏳ WAIT pipeline:g/app#999")
        self.assertEqual(a.state, "ready")
        self.assertIn("pipeline g/app#999 not found on git.example", a.wake_text)
        self.assertIn("give a GitLab pipeline id or a GitHub Actions run of a GitHub-hosted repo", a.wake_text)
        self.assertEqual(r.store.outcomes_today(self.agent.name, t["id"]), {"wait": 1})

    def test_typed_wait_on_a_missing_pipeline_is_sent_back_too(self):
        r, t = self.flow(http_error(404))
        a = self.turn(r, t, f"⏳ WAIT pipeline:g/app#999 until=success after={SHA}:999:running")
        self.assertEqual(a.state, "ready")
        self.assertIn("not found on git.example", a.wake_text)

    def test_transient_errors_keep_the_wait(self):
        for error in (http_error(500), http_error(503), urllib.error.URLError("timed out"), TimeoutError("slow")):
            with self.subTest(error=repr(error)):
                self.tearDown(), self.setUp()
                r, t = self.flow(error)
                a = self.turn(r, t, "⏳ WAIT pipeline:g/app#999")
                self.assertEqual(a.state, "waiting")
                self.assertEqual(a.wait["target"], "g/app#999")
                r.store.db.close()

    def test_an_existing_pipeline_is_parked_as_before(self):
        r, t = self.flow(http_error(404))
        r.gitlab.pipeline_rows["g/app#5"] = {"sha": SHA, "id": 5, "status": "running"}
        r.gitlab.pipelines["g/app#5"] = "running"
        a = self.turn(r, t, "⏳ WAIT pipeline:g/app#5")
        self.assertEqual(a.state, "waiting")


if __name__ == "__main__":
    unittest.main()
