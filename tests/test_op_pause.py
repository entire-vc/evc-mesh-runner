import os
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from mesh_runner import op_pause
from test_runner import Bench
from fakes import FakeMesh, task, write_replies, FUTURE_AT, wait_time

SRC = str(Path(__file__).resolve().parent.parent / "src")


class Tokens(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())

    def test_take_release_touch_no_shared_name(self):
        op_pause.take(self.d, "builder", "op-1")
        self.assertEqual(op_pause.tokens(self.d, "builder"), ["builder/op-1"])
        self.assertFalse((self.d / "pause").exists())
        self.assertTrue(op_pause.release(self.d, "builder", "op-1"))
        self.assertEqual(op_pause.tokens(self.d, "builder"), [])
        self.assertFalse(op_pause.release(self.d, "builder", "op-1"))

    def test_two_tokens_release_one_keeps_pause(self):
        op_pause.take(self.d, "builder", "a")
        op_pause.take(self.d, "_all", "b")
        op_pause.release(self.d, "builder", "a")
        self.assertTrue(op_pause.any_token(self.d, "builder"))
        self.assertTrue(op_pause.any_token(self.d, "other"))      # host-wide token
        op_pause.release(self.d, "_all", "b")
        self.assertFalse(op_pause.any_token(self.d, "builder"))

    def test_other_agent_not_paused_by_agent_token(self):
        op_pause.take(self.d, "builder", "a")
        self.assertFalse(op_pause.any_token(self.d, "other"))

    def test_invalid_names_rejected(self):
        for bad in ("", "..", "a/b", ".hidden", "x" * 200):
            with self.assertRaises(ValueError):
                op_pause.take(self.d, "builder", bad)
        with self.assertRaises(ValueError):
            op_pause.take(self.d, "../pause", "t")

    def test_unreadable_namespace_fails_closed(self):
        op_pause.take(self.d, "builder", "a")
        ns = self.d / "pause.d" / "builder"
        ns.chmod(0)
        try:
            if os.access(ns, os.R_OK):
                self.skipTest("running with privileges that ignore modes")
            self.assertTrue(op_pause.any_token(self.d, "builder"))
        finally:
            ns.chmod(0o755)

    def test_sigkill_during_take_release_never_clears_foreign_ban(self):
        (self.d / "pause").mkdir()
        (self.d / "pause" / "builder").touch()
        code = ("import sys,itertools;sys.path.insert(0,%r);from mesh_runner import op_pause;"
                "from pathlib import Path;d=Path(%r)\n"
                "for i in itertools.count():\n"
                "  op_pause.take(d,'builder','op-%%d'%%(i%%3));op_pause.release(d,'builder','op-%%d'%%(i%%3))\n") % (SRC, str(self.d))
        for delay in (0.0, 0.01, 0.03, 0.05, 0.08):
            p = subprocess.Popen([sys.executable, "-c", code])
            try:
                p.wait(timeout=delay)
            except subprocess.TimeoutExpired:
                pass
            os.kill(p.pid, signal.SIGKILL)
            p.wait()
            self.assertTrue((self.d / "pause" / "builder").exists())     # foreign ban untouched
            # a half-done operation leaves a token (fail closed) or nothing; never a gap in the ban
            for n in op_pause.tokens(self.d, "builder"):
                op_pause.release(self.d, *n.split("/", 1))
            self.assertTrue((self.d / "pause" / "builder").exists())

    def test_sigkill_after_take_leaves_agent_paused(self):
        code = ("import sys,os,signal;sys.path.insert(0,%r);from mesh_runner import op_pause;"
                "from pathlib import Path;op_pause.take(Path(%r),'builder','op-k');os.kill(os.getpid(),signal.SIGKILL)") % (SRC, str(self.d))
        subprocess.run([sys.executable, "-c", code])
        self.assertEqual(op_pause.tokens(self.d, "builder"), ["builder/op-k"])

    def test_concurrent_writers(self):
        (self.d / "pause").mkdir()
        foreign = self.d / "pause" / "builder"
        foreign.touch()
        seen_gap = []
        stop = threading.Event()

        def watcher():
            while not stop.is_set():
                if not (foreign.exists() or op_pause.any_token(self.d, "builder")):
                    seen_gap.append(1)

        def op(tok):
            for _ in range(300):
                op_pause.take(self.d, "builder", tok)
                op_pause.release(self.d, "builder", tok)

        def sharer():
            for _ in range(300):
                foreign.unlink(missing_ok=True)
                foreign.touch()

        op_pause.take(self.d, "builder", "held")                       # a second operation holds throughout
        w = threading.Thread(target=watcher)
        ts = [threading.Thread(target=op, args=(f"t{i}",)) for i in range(4)] + [threading.Thread(target=sharer)]
        w.start()
        [t.start() for t in ts]
        [t.join() for t in ts]
        stop.set()
        w.join()
        self.assertEqual(seen_gap, [])
        self.assertEqual(op_pause.tokens(self.d, "builder"), ["builder/held"])

    def test_concurrent_take_release_on_empty_namespace(self):
        errors = []

        def op(tok):
            try:
                for _ in range(500):
                    op_pause.take(self.d, "builder", tok)
                    self.assertTrue(op_pause.any_token(self.d, "builder"))
                    op_pause.release(self.d, "builder", tok)
            except Exception as e:      # noqa: BLE001
                errors.append(e)

        ts = [threading.Thread(target=op, args=(f"t{i}",)) for i in range(6)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(errors, [])
        self.assertEqual(op_pause.tokens(self.d, "builder"), [])


class RunnerPause(Bench):
    def _start(self, mesh):
        r = self.runner(mesh)
        r.tick()
        return r

    def test_token_with_foreign_ban_release_keeps_paused(self):
        t1 = task(1)
        (self.cfg.state_dir / "pause").mkdir(parents=True, exist_ok=True)
        (self.cfg.state_dir / "pause" / "builder").touch()
        op_pause.migrate_legacy(self.cfg.state_dir, ["builder"])       # what the runner does at start
        op_pause.take(self.cfg.state_dir, "builder", "op-1")
        r = self._start(FakeMesh([t1], max_concurrent=1))
        self.assertTrue(r.paused())
        op_pause.release(self.cfg.state_dir, "builder", "op-1")
        r.tick()
        self.assertTrue(r.paused())
        self.assertEqual(self.states(r), {})

    def test_two_tokens_release_one_keeps_paused(self):
        t1 = task(1)
        op_pause.take(self.cfg.state_dir, "builder", "a")
        op_pause.take(self.cfg.state_dir, "builder", "b")
        r = self._start(FakeMesh([t1], max_concurrent=1))
        op_pause.release(self.cfg.state_dir, "builder", "a")
        r.tick()
        self.assertTrue(r.paused())
        self.assertEqual(self.states(r), {})

    def test_release_last_token_unpauses_next_tick(self):
        t1 = task(1)
        op_pause.take(self.cfg.state_dir, "builder", "a")
        r = self._start(FakeMesh([t1], max_concurrent=1))
        self.assertEqual(self.states(r), {})
        op_pause.release(self.cfg.state_dir, "builder", "a")
        write_replies(self.replies, {t1["id"][:8]: wait_time(why="soak window")})
        r.tick()
        self.assertFalse(r.paused())
        self.assertEqual(len(self.states(r)), 1)

    def test_status_and_metrics_show_tokens(self):
        op_pause.take(self.cfg.state_dir, "builder", "a")
        op_pause.take(self.cfg.state_dir, "_all", "z")
        r = self._start(FakeMesh([], max_concurrent=1))
        st = r.status()
        self.assertTrue(st["paused"])
        self.assertEqual(st["pause_tokens"], ["_all/z", "builder/a"])
        from mesh_runner.runner import Runner as R
        from mesh_runner.store import Store
        self.cfg.metrics_file = self.root / "runner.prom"
        wrapper = R.__new__(R)
        wrapper.cfg, wrapper.store, wrapper._lock = self.cfg, Store(self.root / "state" / "r.sqlite"), None
        wrapper.agents = [r]
        wrapper.write_status([st])
        prom = self.cfg.metrics_file.read_text()
        self.assertIn('mesh_runner_agent_paused{agent="builder"} 1', prom)
        self.assertIn('mesh_runner_agent_pause_tokens{agent="builder"} 2', prom)

    def test_unavailable_peer_check_sees_token(self):
        r = self._start(FakeMesh([], max_concurrent=1))
        self.assertEqual(r._unavailable("builder"), "")
        op_pause.take(self.cfg.state_dir, "builder", "a")
        self.assertEqual(r._unavailable("builder"), "it is paused")


if __name__ == "__main__":
    unittest.main()
