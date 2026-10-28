"""A lane moved to another Claude account must not resume a conversation that account does not hold
(28.10: six turns died at start with "No conversation found with session ID")."""
import tempfile
import unittest
from pathlib import Path

from fakes import FakeMesh, calls, task, write_replies
from mesh_runner.adapters.claude import ClaudeAdapter
from test_runner import Loop


class CarryFromAccountDirectories(unittest.TestCase):
    def test_a_lane_on_the_default_directory_finds_the_session_of_an_account_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            proj = home / ".claude-accounts" / "riker-team" / "projects" / "-work-agent"
            proj.mkdir(parents=True)
            (proj / "s-1.jsonl").write_text("{}\n")
            (home / ".claude").mkdir()
            env = {"HOME": tmp}                              # no CLAUDE_CONFIG_DIR: the default directory
            a = ClaudeAdapter()
            self.assertFalse(a.session_known("s-1", Path("/work/agent"), env))
            self.assertIn("riker-team", a.prepare_resume("s-1", Path("/work/agent"), env))
            self.assertTrue(a.session_known("s-1", Path("/work/agent"), env))
            self.assertTrue((home / ".claude/projects/-work-agent/s-1.jsonl").is_file())

    def test_a_session_no_directory_holds_is_not_known(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"HOME": tmp}
            a = ClaudeAdapter()
            self.assertEqual(a.prepare_resume("s-none", Path("/w"), env), "")
            self.assertFalse(a.session_known("s-none", Path("/w"), env))


class AccountChange(Loop):
    def wake_after_wait(self, known):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        write_replies(self.replies, {t["id"][:8]: "\u23f3 WAIT decision:@lead"})
        r = self.runner(mesh)
        r.tick()
        self.settle_all(r)
        self.adapter.sessions_known = known                  # the lane is routed to another account now
        mesh.add_comment(t["id"], "answer", author="lead")
        r.tick()
        self.settle_all(r)
        return t, mesh, r

    def test_other_account_starts_a_new_session_in_the_same_turn(self):
        t, mesh, r = self.wake_after_wait(False)
        done = calls(self.calls)
        self.assertEqual([c["resume"] for c in done], [False, False])    # no resume of the old id
        self.assertNotEqual(done[0]["sid"], done[1]["sid"])
        self.assertIn("#" + t["id"][:8], done[1]["prompt"])              # the card context of a fresh start
        a = r.store.by_state(self.agent.name, ("waiting",))[0]
        self.assertEqual(a.no_outcome, 0)                                # not a failed turn
        self.assertEqual(a.session_id, done[1]["sid"])
        self.assertIn("session_missing", [k for _, k, _ in r.store.events(a.id)])

    def test_same_account_resumes_as_before(self):
        t, mesh, r = self.wake_after_wait(True)
        done = calls(self.calls)
        self.assertEqual([c["resume"] for c in done], [False, True])
        self.assertEqual(done[0]["sid"], done[1]["sid"])
        a = r.store.by_state(self.agent.name, ("waiting",))[0]
        self.assertNotIn("session_missing", [k for _, k, _ in r.store.events(a.id)])

    def test_no_conversation_on_resume_still_restarts_without_a_failure(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        write_replies(self.replies, {t["id"][:8]: "\u23f3 WAIT decision:@lead",
                                     t["id"][:8] + ":resume": "ERROR:No conversation found with session ID: x"})
        r = self.runner(mesh)
        r.tick()
        self.settle_all(r)
        mesh.add_comment(t["id"], "answer", author="lead")
        for _ in range(2):
            r.tick()
            self.settle_all(r)
        self.assertEqual([c["resume"] for c in calls(self.calls)], [False, True, False])
        a = r.store.by_state(self.agent.name, ("waiting",))[0]
        self.assertEqual(a.no_outcome, 0)


if __name__ == "__main__":
    unittest.main()
