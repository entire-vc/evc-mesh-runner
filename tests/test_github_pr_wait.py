"""mr:/merge: waits on a GitHub pull request: the PR is read as a merge request, so a merged PR
wakes the wait instead of failing on the GitLab API."""
import unittest
from unittest import mock

from mesh_runner.wake import GitHub, GitLab


def pr(**kw):
    row = {"state": "open", "merged": False, "merged_at": None, "merge_commit_sha": None,
           "head": {"sha": "a" * 40}, "base": {"ref": "main"}, "labels": [{"name": "x"}],
           "html_url": "https://github.com/org/repo/pull/68"}
    row.update(kw)
    return row


class GitHubPR(unittest.TestCase):
    def reader(self, row):
        gh = GitHub(orgs=("org",))
        gl = GitLab("https://gl.example", "t", gh)
        return gl, mock.patch.object(GitHub, "get", return_value=row)

    def test_routing_by_org(self):
        gh = GitHub(orgs=("org", "other/one"))
        self.assertTrue(gh.owns("org/repo!1"))
        self.assertTrue(gh.owns("other/one!2"))
        self.assertFalse(gh.owns("other/two!2"))
        self.assertFalse(gh.owns("organic/repo!1"))

    def test_merged_pr_reads_as_merged_mr(self):
        gl, patch = self.reader(pr(state="closed", merged=True, merged_at="2026-10-28T00:40:00Z",
                                   merge_commit_sha="b" * 40))
        with patch, mock.patch.object(GitLab, "get", side_effect=AssertionError("GitLab API used")):
            mr = gl.get_mr("org/repo!68")
        self.assertEqual((mr["state"], mr["merge_commit_sha"], mr["target_branch"]), ("merged", "b" * 40, "main"))

    def test_closed_unmerged_pr_carries_its_timestamps(self):
        gl, patch = self.reader(pr(state="closed", closed_at="2026-10-28T01:00:00Z",
                                   updated_at="2026-10-28T01:00:01Z"))
        with patch:
            mr = gl.get_mr("org/repo!3")
        self.assertEqual((mr["state"], mr["closed_at"], mr["updated_at"]),
                         ("closed", "2026-10-28T01:00:00Z", "2026-10-28T01:00:01Z"))

    def test_open_and_closed_states(self):
        gl, patch = self.reader(pr())
        with patch:
            self.assertEqual(gl.get_mr("org/repo!1")["state"], "opened")
        gl, patch = self.reader(pr(state="closed"))
        with patch:
            self.assertEqual(gl.get_mr("org/repo!1")["state"], "closed")

    def test_gitlab_targets_still_use_gitlab(self):
        gl, _ = self.reader(pr())
        with mock.patch.object(GitLab, "get", return_value={"state": "opened"}) as g:
            self.assertEqual(gl.get_mr("grp/proj!5")["state"], "opened")
        self.assertIn("merge_requests/5", g.call_args[0][0])

    def test_github_only_config_builds_an_observer(self):
        from pathlib import Path
        src = Path(__file__).parent.parent / "src/mesh_runner/runner.py"
        self.assertIn("(cfg.gitlab_url or github)", src.read_text())
        gl = GitLab("", "", GitHub(orgs=("org",)))
        with mock.patch.object(GitHub, "get", return_value=pr(state="closed", merged=True, merged_at="t")):
            self.assertEqual(gl.get_mr("org/repo!1")["state"], "merged")
        with self.assertRaises(ValueError):
            gl.get_mr("grp/proj!1")

    def test_mr_event_wakes_on_merged_github_pr(self):
        gl, patch = self.reader(pr(state="closed", merged=True, merged_at="t"))
        with patch:
            self.assertIn("merged", gl.mr_event("org/repo!68", {}))


if __name__ == "__main__":
    unittest.main()
