import subprocess
import threading
import unittest
from unittest import mock

from mesh_runner import checkpoint
from mesh_runner.workspace import TimedLock, WorkspaceError


class TimedLockTests(unittest.TestCase):
    def test_with_form_gives_up_and_names_the_holder(self):
        lock = TimedLock("t", timeout=0.2)
        held, done = threading.Event(), threading.Event()

        def hold():
            with lock:
                held.set()
                done.wait(5)

        t = threading.Thread(target=hold, name="stuck-slot")
        t.start()
        held.wait(5)
        try:
            with self.assertRaisesRegex(WorkspaceError, "stuck-slot"):
                with lock:
                    pass
        finally:
            done.set()
            t.join()

    def test_reentrant_and_acquire_release_api(self):
        lock = TimedLock("t", timeout=0.2)
        with lock:
            self.assertTrue(lock._is_owned())
            with lock:
                pass
        self.assertFalse(lock._is_owned())
        self.assertTrue(lock.acquire(blocking=False))
        lock.release()

    def test_checkpoint_run_times_out_into_workspace_error(self):
        with self.assertRaisesRegex(WorkspaceError, "timed out"):
            checkpoint._run(["sleep", "30"], timeout=0.3)

    def test_checkpoint_run_returns_when_the_child_cannot_be_reaped(self):
        proc = mock.Mock()
        proc.communicate.side_effect = subprocess.TimeoutExpired("x", 1)
        proc.wait.side_effect = subprocess.TimeoutExpired("x", 5)
        with mock.patch.object(checkpoint.subprocess, "Popen", return_value=proc):
            with self.assertRaisesRegex(WorkspaceError, "timed out"):
                checkpoint._run(["du"], timeout=1)
        proc.kill.assert_called_once()

    def test_checkpoint_run_keeps_run_semantics(self):
        r = checkpoint._run(["cat"], input=b"hi", capture_output=True)
        self.assertEqual((r.returncode, r.stdout), (0, b"hi"))
        with self.assertRaises(subprocess.CalledProcessError):
            checkpoint._run(["false"], check=True)


if __name__ == "__main__":
    unittest.main()
