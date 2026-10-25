"""A reused pid must not pin a worktree slot or receive our signals."""
import datetime as dt
import os
import signal
import subprocess
import sys
import time
import tempfile
import threading
from pathlib import Path
import unittest
from unittest.mock import patch

from mesh_runner.adapters.base import (_linux_start, group_alive, legacy_stamp, pid_reused, precise_stamp,
                                       proc_stamp, stamp_differs)
from mesh_runner.workspace import WorkspaceError
from fakes import FakeMesh
from test_runner import Bench, task


class PidReuse(Bench):
    def setUp(self):
        super().setUp()
        # A live process leading its own group stands in for the unrelated owner of a reused pid.
        self.other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                      start_new_session=True)
        self.addCleanup(self._reap)
        self.stranger_stamp = proc_stamp(self.other.pid)
        self.assertTrue(self.stranger_stamp)

    def _reap(self):
        if self.other.poll() is None:
            self.other.kill()
        self.other.wait()

    def waiting(self, n, stamp):
        self.agent.workspace_wait_release_minutes = 30
        t = task(n)
        r = self.runner(FakeMesh([t]))
        a = r.store.create("builder", t["id"], "claude")
        path = r.pool.lease(a.id, "app")
        r.store.update(a, state="waiting", workspaces=[str(path)], pid=self.other.pid, stamp=stamp,
                       wait={"started": (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)).isoformat()})
        return r, r.store.get(a.id), str(path)

    def test_reused_pid_does_not_retain_the_lease(self):
        r, a, path = self.waiting(901, "Thu Jan  1 00:00:00 2015")
        self.assertNotEqual(a.stamp, self.stranger_stamp)
        r._suspend_waiting(a)
        self.assertEqual(r.pool.leased_to(a.id), [])

    def test_same_stamp_live_group_still_refuses(self):
        r, a, path = self.waiting(902, self.stranger_stamp)
        with self.assertRaisesRegex(WorkspaceError, "still alive"):
            r._suspend_waiting(a)
        self.assertEqual(r.pool.leased_to(a.id), [path])

    def test_empty_stamp_stays_conservative(self):
        r, a, path = self.waiting(903, "")
        with self.assertRaisesRegex(WorkspaceError, "still alive"):
            r._suspend_waiting(a)
        self.assertEqual(r.pool.leased_to(a.id), [path])

    def test_kill_with_reused_pid_sends_no_signal(self):
        r, a, _ = self.waiting(904, "Thu Jan  1 00:00:00 2015")
        with patch("mesh_runner.runner.interrupt_group") as intr, patch("os.killpg") as killpg:
            r._kill(a)
        intr.assert_not_called()
        killpg.assert_not_called()
        self.assertIsNone(self.other.poll())

    def test_kill_with_matching_stamp_signals_the_group(self):
        r, a, _ = self.waiting(905, self.stranger_stamp)
        reaper = threading.Thread(target=self.other.wait)     # a zombie group would read as alive
        reaper.start()
        r._kill(a)
        reaper.join(5)
        self.assertEqual(self.other.returncode, -signal.SIGTERM)

    def test_kill_with_empty_stamp_still_signals(self):
        r, a, _ = self.waiting(906, "")
        with patch("mesh_runner.runner.interrupt_group") as intr, \
                patch("mesh_runner.runner.group_alive", return_value=False):
            r._kill(a)
        intr.assert_called_once_with(a.pid, signal.SIGTERM)

    def test_kill_of_owned_popen_ignores_stamp_check(self):
        r, a, _ = self.waiting(907, "Thu Jan  1 00:00:00 2015")
        r.procs[a.id] = self.other
        r._kill(a)
        self.assertEqual(self.other.wait(timeout=5), -signal.SIGTERM)


    def test_kill_with_an_exited_popen_and_a_reused_pid_sends_no_signal(self):
        r, a, _ = self.waiting(908, "Thu Jan  1 00:00:00 2015")
        gone = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
        gone.wait()                                            # ours exited and was reaped
        r.procs[a.id] = gone                                   # the stale Popen object remains
        with patch("mesh_runner.runner.interrupt_group") as intr, patch("os.killpg") as killpg:
            r._kill(a)
        intr.assert_not_called()
        killpg.assert_not_called()
        self.assertIsNone(self.other.poll())

class PreciseStamp(unittest.TestCase):
    def setUp(self):
        self.child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                      start_new_session=True)
        self.addCleanup(lambda: (self.child.kill(), self.child.wait()))

    @unittest.skipUnless(sys.platform == "darwin", "macOS sysctl reader")
    def test_darwin_reader_returns_the_spawn_moment(self):
        from mesh_runner.adapters import base
        before = time.time()
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
        after = time.time()
        sibling = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
        try:
            started = float(base._darwin_start(child.pid)[len(base.STAMP_V2):])
            self.assertGreaterEqual(started, before - 0.05)          # the start time, not another field
            self.assertLessEqual(started, after + 0.05)
            self.assertNotEqual(base._darwin_start(child.pid), base._darwin_start(sibling.pid))
        finally:
            for p in (child, sibling):
                p.kill()
                p.wait()

    def test_v2_stamp_of_a_live_process_is_stable(self):
        first = proc_stamp(self.child.pid)
        self.assertRegex(first, r"^v2:\d+\.\d{6}$")
        for _ in range(3):
            self.assertEqual(proc_stamp(self.child.pid), first)
        self.assertFalse(stamp_differs(self.child.pid, first))

    def test_v2_stamps_differing_by_microseconds_are_reused(self):
        first = proc_stamp(self.child.pid)
        sec, micros = first[3:].split(".")
        other = f"v2:{sec}.{(int(micros) + 1) % 1_000_000:06d}"
        self.assertTrue(stamp_differs(self.child.pid, other))
        self.assertTrue(pid_reused(self.child.pid, other))

    def test_identical_v2_stamp_is_the_same_process(self):
        with patch("mesh_runner.adapters.base.precise_stamp", return_value="v2:1.000002"):
            self.assertFalse(stamp_differs(self.child.pid, "v2:1.000002"))
            self.assertTrue(stamp_differs(self.child.pid, "v2:1.000003"))

    def test_legacy_stamp_equal_to_current_legacy_string_is_not_reused(self):
        legacy = legacy_stamp(self.child.pid)
        self.assertTrue(legacy)
        self.assertFalse(stamp_differs(self.child.pid, legacy))
        self.assertFalse(pid_reused(self.child.pid, legacy))
        self.assertTrue(group_alive(self.child.pid, legacy))

    def test_legacy_stamp_that_differs_is_reused(self):
        self.assertTrue(stamp_differs(self.child.pid, "Thu Jan  1 00:00:00 2015"))
        self.assertFalse(group_alive(self.child.pid, "Thu Jan  1 00:00:00 2015"))

    def test_precise_read_failure_falls_back_to_legacy(self):
        with patch("mesh_runner.adapters.base.precise_stamp", return_value=""):
            self.assertEqual(proc_stamp(self.child.pid), legacy_stamp(self.child.pid))

    def test_v2_stamp_is_conservative_when_precise_read_fails_now(self):
        with patch("mesh_runner.adapters.base.precise_stamp", return_value=""):
            self.assertFalse(stamp_differs(self.child.pid, "v2:1.000002"))

    def test_dead_pid_is_not_reused_and_has_no_stamp(self):
        self.child.kill()
        self.child.wait()
        self.assertFalse(stamp_differs(self.child.pid, "v2:1.000002"))
        self.assertFalse(stamp_differs(self.child.pid, "Thu Jan  1 00:00:00 2015"))

    def test_linux_reader_combines_boot_time_and_ticks(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(f"{root}/42")
            fields = ["S"] + ["0"] * 18 + ["12345"] + ["0"] * 5       # starttime is field 22
            Path(f"{root}/42/stat").write_text("42 (a) b) " + " ".join(fields) + "\n")
            Path(f"{root}/stat").write_text("cpu 1 2 3\nbtime 1000\n")
            with patch("os.sysconf", return_value=100):
                self.assertEqual(_linux_start(42, root), "v2:1123.450000")

    def test_unreadable_precise_source_returns_empty(self):
        with patch("sys.platform", "linux"):
            self.assertEqual(precise_stamp(2**22 + 12345), "")


if __name__ == "__main__":
    unittest.main()
