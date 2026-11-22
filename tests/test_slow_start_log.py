"""A slow start names its steps. The slow-tick line said only ``start 249s`` for a start whose
reserve and admit were four minutes apart, and nothing showed which step took them."""
import unittest
from unittest import mock

from fakes import FakeMesh, task
from test_runner import Bench
from mesh_runner import runner as runner_mod


class SlowStartLog(Bench):
    def test_a_slow_start_is_logged_with_each_of_its_steps(self):
        r = self.runner(FakeMesh([task(1)], max_concurrent=1))
        with mock.patch.object(runner_mod, "SLOW_START_SECONDS", 0.0), \
                self.assertLogs("mesh_runner", "WARNING") as logs:
            r.tick()
        line = next(m for m in logs.output if "start took" in m)
        steps = line.split("(", 1)[1]
        for step in ("reserve", "env", "preflight", "checkout", "task", "workspaces", "admit", "end"):
            self.assertIn(f"{step} ", steps)
        self.assertLess(steps.index("env "), steps.index("workspaces "))

    def test_a_quick_start_is_not_logged(self):
        r = self.runner(FakeMesh([task(1)], max_concurrent=1))
        with self.assertNoLogs("mesh_runner", "WARNING") if hasattr(self, "assertNoLogs") else mock.MagicMock():
            r.tick()
        self.assertEqual(len(r.store.by_state(r.agent.name, ("running",))), 1)

    def test_the_breakdown_of_one_start_never_leaks_into_the_next(self):
        r = self.runner(FakeMesh([task(1)], max_concurrent=1))
        r.tick()
        self.assertIsNone(r._start_marks)
        r._start_mark("env")                                       # outside a start: nothing recorded
        self.assertIsNone(r._start_marks)


if __name__ == "__main__":
    unittest.main()
