"""A hold is logged once per episode: repeated re-checks stay quiet, a new episode logs at once."""
from unittest.mock import patch

from fakes import FakeMesh, task
from test_runner import Bench

from mesh_runner import runner as R


class HoldLogOnce(Bench):
    def lines(self, cm):
        return [m for m in cm.output if "held" in m]

    def test_repeated_hold_with_one_reason_is_one_line(self):
        r = self.runner(FakeMesh([]))
        with self.assertLogs("mesh_runner", "INFO") as cm:
            for _ in range(5):
                r._log_hold("resume", "abc12345", "no free worktree")
            r._log_hold("resume", "abc12345", "runtime capacity unavailable")
        self.assertEqual(len(self.lines(cm)), 2)
        self.assertIn("#abc12345 resume held: no free worktree", self.lines(cm)[0])
        self.assertIn("#abc12345 resume held: runtime capacity unavailable", self.lines(cm)[1])

    def test_return_to_an_earlier_reason_logs_at_once(self):
        r = self.runner(FakeMesh([]))
        with self.assertLogs("mesh_runner", "INFO") as cm:
            r._log_hold("start", "abc12345", "A")
            r._log_hold("start", "abc12345", "B")
            r._log_hold("start", "abc12345", "A")                  # A -> B -> A within the interval
            r._log_hold("start", "abc12345", "A")                  # the same reason again: quiet
        self.assertEqual([m.rsplit(": ", 1)[-1] for m in self.lines(cm)], ["A", "B", "A"])

    def test_relogged_after_the_interval(self):
        r = self.runner(FakeMesh([]))
        now = [1000.0]
        with patch.object(R.time, "monotonic", lambda: now[0]), self.assertLogs("mesh_runner", "INFO") as cm:
            r._log_hold("start", "abc12345", "x")
            now[0] += R.HOLD_RELOG_SECONDS + 1
            r._log_hold("start", "abc12345", "x")
        self.assertEqual(len(self.lines(cm)), 2)

    def test_new_episode_with_the_same_reason_logs_at_once(self):
        r = self.runner(FakeMesh([]))
        with self.assertLogs("mesh_runner", "INFO") as cm:
            r._log_hold("start", "abc12345", "runtime capacity unavailable")
            r._log_hold("start", "abc12345", "runtime capacity unavailable")
            r._clear_hold_log("abc12345")                  # the hold ended
            r._log_hold("start", "abc12345", "runtime capacity unavailable")
            r._log_hold("start", "abc12345", "runtime capacity unavailable")
        self.assertEqual(len(self.lines(cm)), 2)

    def test_distinct_tasks_are_independent(self):
        r = self.runner(FakeMesh([]))
        with self.assertLogs("mesh_runner", "INFO") as cm:
            r._log_hold("start", "aaaaaaaa", "same reason")
            r._log_hold("start", "bbbbbbbb", "same reason")
            r._clear_hold_log("aaaaaaaa")
            r._log_hold("start", "bbbbbbbb", "same reason")       # b's episode goes on: quiet
            r._log_hold("start", "aaaaaaaa", "same reason")       # a's is new: logged
        self.assertEqual(len(self.lines(cm)), 3)
        self.assertIn("#aaaaaaaa", self.lines(cm)[2])

    def test_clearing_one_episode_keeps_the_others_of_the_task(self):
        r = self.runner(FakeMesh([]))
        with self.assertLogs("mesh_runner", "INFO") as cm:
            r._log_hold("resume", "abc12345", "gate refused")
            r._log_hold("resume", "abc12345", "no free worktree", episode="resume workspace")
            r._clear_hold_log("abc12345", "resume")                # the gate passed, the worktree still full
            r._log_hold("resume", "abc12345", "no free worktree", episode="resume workspace")
            r._log_hold("resume", "abc12345", "gate refused")
        self.assertEqual(len(self.lines(cm)), 3)

    def test_the_hold_log_does_not_share_the_gate_reason_table(self):
        r = self.runner(FakeMesh([]))
        t = task(1)
        with self.assertLogs("mesh_runner", "INFO"):
            r.hold_reason(dict(t), "work")                     # fills the per-task reason table
            r._log_hold("start", t["id"][:8], "x")
            r._log_hold("start", t["id"][:8], "y")             # the sweep must not choke on either table

    def test_start_gate_hold_ends_when_the_gate_lets_the_task_through(self):
        t = task(1)
        r = self.runner(FakeMesh([t]))
        self.agent.start_gate_command = ["operator-gate"]
        prefix = t["id"][:8]
        refuse = (R.GATE_HOLD_TASK, "HOLD_REASON_CODE=capacity", "")
        with self.assertLogs("mesh_runner", "INFO") as cm:
            with patch.object(r, "_run_hook", return_value=refuse):
                r._start_one(dict(t))
                r._gate_hold.clear()
                r._start_one(dict(t))                          # same episode: quiet
            with patch.object(r, "_run_hook", return_value=(0, "", "")), \
                    patch.object(r, "_start_attempt", return_value=True):
                r._gate_hold.clear()
                r._start_one(dict(t))                          # let through: the episode ends
            with patch.object(r, "_run_hook", return_value=refuse):
                r._gate_hold.clear()
                r._start_one(dict(t))                          # a new episode, the same reason
        held = [m for m in cm.output if f"#{prefix} start held" in m]
        self.assertEqual(len(held), 2)
