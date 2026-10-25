"""_guard keeps failure counts per step and backs off persistent failures."""
from unittest.mock import patch

from mesh_runner.workspace import WorkspaceError
import test_workspace_requests as requests_fixture
from test_runner import Bench


class GuardBackoff(Bench):
    parked = requests_fixture.WorkspaceRequests.parked

    def setUp(self):
        super().setUp()
        self.r, (self.a,) = self.parked()

    def boom(self, a):
        raise WorkspaceError("checkpoint: insufficient disk budget; lease retained")

    def test_successful_wake_does_not_reset_suspend_failures(self):
        calls = []
        clock = [1000.0]
        with patch("mesh_runner.runner.time.monotonic", lambda: clock[0]):
            for _ in range(6):
                self.r._guard(self.a, "wake", lambda x: None)
                self.r._guard(self.a, "workspace-suspend", lambda x: (calls.append(1), self.boom(x)))
                clock[0] += 120
        self.assertEqual(len(calls), 3)                    # 4th tick is quarantined

    def test_backoff_grows_and_success_clears_it(self):
        calls = []
        clock = [1000.0]
        step = lambda x: (calls.append(clock[0]), self.boom(x))
        with patch("mesh_runner.runner.time.monotonic", lambda: clock[0]):
            for _ in range(3):
                self.r._guard(self.a, "workspace-suspend", step)
            key = (self.a.id, "workspace-suspend")
            self.assertEqual(self.r._quarantine[key] - clock[0], 900)
            clock[0] += 901
            for _ in range(3):
                self.r._guard(self.a, "workspace-suspend", step)
            self.assertEqual(self.r._quarantine[key] - clock[0], 1800)
            clock[0] += 1801
            self.r._guard(self.a, "workspace-suspend", lambda x: None)
            self.assertNotIn(key, self.r._strikes)
        self.assertEqual(len(calls), 6)
