"""A NEXT after which the leased worktree changed did work: it is not charged to daily_next_budget."""
import subprocess
import tempfile
import unittest
from pathlib import Path

from fakes import AGENT_ID, FakeMesh, task, write_replies
from test_requeue_on_cap import CapBench


class NextWorkBudget(CapBench):
    def spend(self, changing):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        self.cfg.daily_next_budget = 2
        self.agent.lead = "lead"
        write_replies(self.replies, {t["id"][:8]: "↻ NEXT"})
        r = self.runner(mesh)
        digests = iter(range(1000))
        r._work_digest = (lambda a: str(next(digests))) if changing else (lambda a: "same")
        r.tick()
        for turn in range(4):
            finished = self.finish_turn(r)
            if finished.state != "ready":
                break
            r.tick()
        return r, mesh, t

    def test_nexts_that_changed_the_worktree_do_not_hit_the_turn_cap(self):
        r, mesh, t = self.spend(changing=True)
        self.assertFalse(any("TURN CAP" in b for b in self.bodies(mesh, t)))
        self.assertEqual(r.store.outcomes_today(self.agent.name, t["id"]).get("next"), 0)

    def test_nexts_that_changed_nothing_still_hit_the_turn_cap(self):
        r, mesh, t = self.spend(changing=False)
        self.assertTrue(any("TURN CAP" in b for b in self.bodies(mesh, t)))


class UnknownDigest(NextWorkBudget):
    def test_unreadable_digest_at_the_end_is_still_charged(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        self.cfg.daily_next_budget = 2
        self.agent.lead = "lead"
        write_replies(self.replies, {t["id"][:8]: "↻ NEXT"})
        r = self.runner(mesh)
        calls = iter(["a", ""] * 10)
        r._work_digest = lambda a: next(calls)         # known at start, unreadable at the end
        r.tick()
        for turn in range(4):
            if self.finish_turn(r).state != "ready":
                break
            r.tick()
        self.assertTrue(any("TURN CAP" in b for b in self.bodies(mesh, t)))


class WorkDigest(CapBench):
    def test_digest_moves_with_a_commit_and_with_a_diff(self):
        with tempfile.TemporaryDirectory() as d:
            sh = lambda *c: subprocess.run(["git", "-C", d, *c], check=True, capture_output=True)
            sh("init", "-q")
            sh("config", "user.email", "t@t")
            sh("config", "user.name", "t")
            Path(d, "f").write_text("1")
            sh("add", "f")
            sh("commit", "-qm", "one")
            r = self.runner(FakeMesh([task(1)], max_concurrent=1))
            a = type("A", (), {"workspaces": [d]})()
            first = r._work_digest(a)
            self.assertEqual(first, r._work_digest(a))
            Path(d, "f").write_text("2")
            dirty = r._work_digest(a)
            self.assertNotEqual(first, dirty)
            sh("commit", "-qam", "two")
            clean = r._work_digest(a)
            Path(d, "new").write_text("x")                  # untracked only
            self.assertNotEqual(clean, r._work_digest(a))
            Path(d, "new").unlink()
            self.assertNotEqual(dirty, clean)
            self.assertEqual(r._work_digest(type("A", (), {"workspaces": []})()), "")


if __name__ == "__main__":
    unittest.main()
