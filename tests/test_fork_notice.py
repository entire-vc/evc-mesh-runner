"""After a memory pressure event, every child the Runner forked printed a MallocStackLogging notice
into runner.log (97% of the file). The start-up pre-arm keeps fork children silent.

Each case runs in a fresh interpreter whose stderr is a file, like launchd's StandardErrorPath: it
delivers the event libmalloc gets on critical pressure (``malloc_memory_event_handler(0x4)``), then
spawns children through ``subprocess``. The control without the pre-arm must show the notice, or the
green case proves nothing."""
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

NOTICE = "can't turn off malloc stack logging"
SRC = str(Path(__file__).resolve().parents[1] / "src")

CHILD = textwrap.dedent("""
    import ctypes, subprocess, sys
    if sys.argv[1] == "prearm":
        from mesh_runner.fork_notice import quiet_fork_stack_logging_notice
        print("result:", quiet_fork_stack_logging_notice(), flush=True)
    handler = ctypes.CDLL(None).malloc_memory_event_handler
    handler.argtypes = [ctypes.c_ulong]
    for event in (0x4, 0x10, 0x20):          # critical pressure, process limit warn / critical
        handler(event)
        subprocess.run(["/usr/bin/true"], check=True)
        subprocess.run(["/usr/bin/true"], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
""")


def run_child(mode: str) -> tuple[str, str]:
    with tempfile.TemporaryDirectory() as tmp:
        err = Path(tmp) / "stderr.log"
        with open(err, "wb") as fh:
            out = subprocess.run([sys.executable, "-c", CHILD, mode], stdout=subprocess.PIPE,
                                 stderr=fh, env={**os.environ, "PYTHONPATH": SRC}, timeout=60,
                                 text=True, check=True).stdout
        return out, err.read_text(errors="replace")


@unittest.skipUnless(sys.platform == "darwin", "macOS libmalloc behaviour")
class ForkNoticeTest(unittest.TestCase):
    def test_control_without_prearm_prints_the_notice(self):
        _, err = run_child("plain")
        self.assertGreaterEqual(err.count(NOTICE), 6, err[-500:])

    def test_prearm_keeps_fork_children_silent(self):
        _, control = run_child("plain")
        if NOTICE not in control:
            self.fail("control no longer reproduces the notice on this macOS: re-check the cause "
                      "before trusting the pre-arm")
        out, err = run_child("prearm")
        self.assertIn("result: armed", out)
        self.assertEqual(err.count(NOTICE), 0, err[-500:])
        self.assertEqual(err, "")


class ForkNoticeElsewhereTest(unittest.TestCase):
    @unittest.skipIf(sys.platform == "darwin", "non-macOS path")
    def test_other_platforms_skip(self):
        from mesh_runner.fork_notice import quiet_fork_stack_logging_notice
        self.assertEqual(quiet_fork_stack_logging_notice(), "skipped: not macOS")


if __name__ == "__main__":
    unittest.main()
