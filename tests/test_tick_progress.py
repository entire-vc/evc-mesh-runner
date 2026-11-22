"""A lane's status row stays fresh while a long tick works (progress beats), and a wedged one
is still reported."""
import contextlib
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from mesh_runner import checkpoint, progress, workspace
from mesh_runner.runner import AgentRunner, Runner
import fakes
from test_runner import Bench

FRESH = 300          # the age past which status readers take a lane row for an unknown state
PULSE_BOUND = 120    # a working lane republishes its row at least this often
REAL_MONOTONIC = time.monotonic


class Clock:
    """Fake wall and monotonic time that only moves when the test says so."""
    def __init__(self):
        self.t = 0.0

    def wall(self):
        return 1_800_000_000.0 + self.t

    def mono(self):
        return self.t


class ScaledClock(Clock):
    """Fake time that runs ``scale`` times faster than real time, so that a real git command of two
    seconds is a 400-second one for the lane."""
    def __init__(self, scale):
        super().__init__()
        self.scale, self.origin = scale, REAL_MONOTONIC()

    @property
    def t(self):
        return (REAL_MONOTONIC() - self.origin) * self.scale

    @t.setter
    def t(self, value):
        pass


class Lane:
    """A lane whose tick is a sequence of work units of ``unit`` fake seconds each. ``work`` is
    one unit: a real git command (as in a checkpoint of a worktree) or None for a unit that gives
    no sign of life (one call blocked for the whole unit)."""
    def __init__(self, runner, clock, *, seconds, unit=10.0, work=None):
        self.agent = SimpleNamespace(name="lane")
        self.runner, self.clock, self.seconds, self.unit, self.work = runner, clock, seconds, unit, work
        self.ages = []
        self.raise_next = False

    def status(self):
        return {"agent": "lane", "cap": 2, "paused": False, "attempts": []}

    def tick(self):
        if self.raise_next:
            self.raise_next = False
            raise RuntimeError("mesh down")
        done = 0.0
        while done < self.seconds:
            self.clock.t += self.unit
            done += self.unit
            if self.work is not None:
                self.work()
            row = {r["agent"]: r for r in self.runner.snapshot(now_unix=self.clock.wall())}["lane"]
            self.ages.append(self.clock.wall() - row["tick_at"])
        return self.status()


class LongTickKeepsRowFresh(Bench):
    def setUp(self):
        super().setUp()
        self.clock = Clock()
        self.repo = Path(self.tmp.name) / "unit-repo"
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        self.runner = Runner(self.cfg)
        for target, fake in (("time.time", self.clock.wall), ("time.monotonic", self.clock.mono),
                             ("mesh_runner.progress._clock", self.clock.mono)):
            patcher = mock.patch(target, fake)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.runner._started = self.clock.wall()      # the lane's row is fresh when the tick begins

    def git_unit(self):
        workspace.git(self.repo, "rev-parse", "--git-dir")

    def run_tick(self, lane):
        self.runner.agents = [lane]
        self.runner._tick_agent(lane)
        return lane

    def test_a_400s_tick_of_git_work_keeps_tick_at_fresh(self):
        lane = self.run_tick(Lane(self.runner, self.clock, seconds=400, work=self.git_unit))
        self.assertEqual(len(lane.ages), 40)
        self.assertLessEqual(max(lane.ages), PULSE_BOUND, lane.ages)
        row = self.runner.snapshot(now_unix=self.clock.wall())[0]
        self.assertNotIn("error", row)
        self.assertEqual(row["tick_done_at"], round(self.clock.wall()))

    def test_positive_control_a_400s_tick_without_signs_of_life_goes_stale(self):
        # the same cycle with units that never beat: the measurement above is not vacuous
        lane = self.run_tick(Lane(self.runner, self.clock, seconds=400, work=None))
        self.assertGreater(max(lane.ages), FRESH, lane.ages)

    def test_a_row_published_mid_tick_names_the_tick_and_its_phase(self):
        seen = []

        def unit():
            progress.step("wakes")
            self.git_unit()
            seen.append(dict(self.runner.snapshot(now_unix=self.clock.wall())[0]))

        started = round(self.clock.wall())
        self.run_tick(Lane(self.runner, self.clock, seconds=100, work=unit))
        mid = [r for r in seen if "tick_running_since" in r]
        self.assertTrue(mid)
        self.assertEqual({r["tick_running_since"] for r in mid}, {started})
        self.assertEqual({r["tick_step"] for r in mid}, {"wakes"})
        final = self.runner.snapshot(now_unix=self.clock.wall())[0]
        self.assertNotIn("tick_running_since", final)        # a finished tick's own row

    def test_a_wedged_tick_is_still_reported(self):
        # one call blocked for 1000 s gives no beat: the stall error must still show
        lane = Lane(self.runner, self.clock, seconds=1000, unit=1000, work=None)
        self.runner.agents = [lane]
        rows = []
        lane.tick = lambda: (setattr(self.clock, "t", self.clock.t + 1000),
                             rows.append(self.runner.snapshot(now_unix=self.clock.wall())[0]),
                             lane.status())[-1]
        self.runner._tick_agent(lane)
        self.assertIn("no tick finished", rows[0]["error"])

    def test_the_error_of_a_failed_tick_stays_on_rows_published_by_the_next_one(self):
        lane = Lane(self.runner, self.clock, seconds=100, work=self.git_unit)
        lane.raise_next = True
        self.run_tick(lane)
        self.assertEqual(self.runner.snapshot(now_unix=self.clock.wall())[0]["error"], "mesh down")
        mid = []
        lane.work = lambda: (self.git_unit(), mid.append(self.runner.snapshot(now_unix=self.clock.wall())[0]))
        self.run_tick(lane)
        self.assertTrue(any("tick_running_since" in r for r in mid))
        self.assertTrue(all(r.get("error") == "mesh down" for r in mid))
        self.assertNotIn("error", self.runner.snapshot(now_unix=self.clock.wall())[0])

    def published_gaps(self, lane):
        """Fake seconds between the tick's start, each row it published, and its end."""
        marks = [self.clock.t]
        pulse = self.runner._pulse

        def recorded(*a):
            marks.append(self.clock.t)
            return pulse(*a)

        with mock.patch.object(self.runner, "_pulse", recorded):
            self.run_tick(lane)
        marks.append(self.clock.t)
        return [b - a for a, b in zip(marks, marks[1:])]

    def slow_git_tick(self, slice_seconds):
        """One tick that is a single real git command taking 400 fake seconds (2 s real)."""
        self.clock = ScaledClock(200.0)
        for target, fake in (("time.time", self.clock.wall), ("time.monotonic", self.clock.mono),
                             ("mesh_runner.progress._clock", self.clock.mono),
                             ("mesh_runner.progress.SLICE_SECONDS", slice_seconds)):
            patcher = mock.patch(target, fake)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.runner._started = self.clock.wall()
        slow = lambda: workspace.git(self.repo, "-c", "alias.slow=!sleep 2", "slow")
        return self.published_gaps(Lane(self.runner, self.clock, seconds=1, unit=1, work=slow))

    def test_a_single_slow_git_command_keeps_tick_at_fresh(self):
        gaps = self.slow_git_tick(0.05)
        self.assertGreaterEqual(sum(gaps), 400, gaps)
        self.assertLessEqual(max(gaps), PULSE_BOUND, gaps)

    def test_positive_control_the_same_command_without_slices_goes_stale(self):
        gaps = self.slow_git_tick(60.0)
        self.assertGreater(max(gaps), FRESH, gaps)

    def test_a_slow_tick_is_logged_with_its_phases(self):
        def unit():
            progress.step("resume" if self.clock.t > 200 else "wakes")

        with self.assertLogs("mesh_runner", "WARNING") as logs:
            self.run_tick(Lane(self.runner, self.clock, seconds=400, work=unit))
        line = next(m for m in logs.output if "tick took" in m)
        self.assertIn("lane: tick took 400s", line)
        self.assertIn("wakes 200s", line)
        self.assertIn("resume 190s", line)


LANE_STATES = {"starting", "running", "unknown", "waiting", "ready"}


class FirstRowAfterStart(Bench):
    """Right after a runner starts, every lane's row must already carry what status readers require
    (cap, paused, attempts): a placeholder makes every reader that checks a lane (its pause, its free
    capacity) see an unknown state."""

    def assert_lane_contract(self, row, now):
        # the contract of a status reader that checks one lane: a row without these fields is
        # "stale or malformed" and the lane's state unknown
        self.assertLessEqual(now - row["tick_at"], FRESH, row)
        self.assertIs(type(row.get("paused")), bool, row)
        self.assertIs(type(row.get("cap")), int, row)
        self.assertGreaterEqual(row["cap"], 1, row)
        self.assertIsInstance(row.get("attempts"), list, row)
        for a in row["attempts"]:
            self.assertIn(a.get("state"), LANE_STATES, row)

    def lane(self, max_concurrent=3):
        ar = self.runner(fakes.FakeMesh([], max_concurrent=max_concurrent))
        runner = Runner(self.cfg, lock=False)
        runner.agents = [ar]
        return runner, ar

    def mid_first_tick(self, *, early=True):
        """The lane's row as a reader sees it in the middle of the lane's first tick (``early=False``:
        with the lane's early publication switched off)."""
        runner, ar = self.lane()
        runner._started = time.time()
        seen = []
        settle = ar.settle_finished
        ar.settle_finished = lambda: (seen.append(runner.snapshot()[0]), settle())[-1]
        with mock.patch("mesh_runner.runner.progress.ready", lambda: None, create=True) if not early \
                else contextlib.nullcontext():
            runner._tick_agent(ar)
        return seen[0]

    def test_a_row_published_in_the_first_tick_before_it_ends_meets_the_reader_contract(self):
        row = self.mid_first_tick()
        self.assertIn("tick_running_since", row)            # published by the running tick, not at its end
        self.assert_lane_contract(row, time.time())
        self.assertEqual(row["cap"], 3)                     # read from Mesh, not the default of an unread lane
        self.assertFalse(row["paused"])

    def test_positive_control_without_the_early_publication_the_first_row_is_a_placeholder(self):
        row = self.mid_first_tick(early=False)
        self.assertNotIn("attempts", row)
        with self.assertRaises(AssertionError):
            self.assert_lane_contract(row, time.time())

    def test_a_lane_with_a_row_of_its_own_does_not_republish_at_every_tick_start(self):
        runner, ar = self.lane()
        runner._tick_agent(ar)
        published = []
        with mock.patch.object(runner, "_pulse", lambda *a: published.append(a)):
            runner._tick_agent(ar)
        self.assertEqual(published, [])

    def test_the_first_status_file_of_a_process_carries_every_lanes_own_row(self):
        runner, ar = self.lane()
        self.cfg.poll_seconds = 0.2
        released, stop, first = threading.Event(), threading.Event(), []
        settle = ar.settle_finished
        ar.settle_finished = lambda: (released.wait(10), settle())[-1]   # the first tick is still running
        write = runner.write_status

        def recorded(out):
            if not first:
                first.append([dict(r) for r in out])
                released.set()
                stop.set()
            write(out)

        with mock.patch.object(runner, "write_status", side_effect=recorded):
            loop = threading.Thread(target=runner.run_forever, args=(stop,))
            loop.start()
            loop.join(timeout=30)
        self.assertFalse(loop.is_alive())
        row = first[0][0]
        self.assertIn("tick_running_since", row)            # the lane's early row, not its finished tick
        self.assert_lane_contract(row, time.time())

    def test_a_silent_lane_holds_the_first_status_only_for_its_budget(self):
        runner, ar = self.lane()
        started = time.monotonic()
        with self.assertLogs("mesh_runner", level="WARNING") as logs:
            runner._await_first_rows(threading.Event(), budget=0.3)
        self.assertLess(time.monotonic() - started, 5)
        self.assertIn("builder", logs.output[0])
        stop = threading.Event()
        stop.set()
        started = time.monotonic()
        runner._await_first_rows(stop, budget=30)           # a stop never waits for the budget
        self.assertLess(time.monotonic() - started, 5)


class Beats(unittest.TestCase):
    def setUp(self):
        self.calls = []
        progress.bind(lambda: self.calls.append(threading.current_thread().name), 0.0)
        self.addCleanup(progress.unbind)

    def test_beats_are_rate_limited(self):
        progress.unbind()
        clock = Clock()
        with mock.patch("mesh_runner.progress._clock", clock.mono):
            progress.bind(lambda: self.calls.append(clock.t), 30.0)
            for _ in range(10):
                clock.t += 10
                progress.beat()
        self.assertEqual(self.calls, [30.0, 60.0, 90.0])

    def test_a_beat_on_another_thread_publishes_nothing(self):
        t = threading.Thread(target=progress.beat, name="cleanup")
        t.start()
        t.join()
        progress.beat()
        self.assertEqual(self.calls, [threading.current_thread().name])

    def test_a_failing_callback_never_breaks_the_work_and_never_recurses(self):
        progress.unbind()

        def callback():
            self.calls.append("in")
            progress.beat()                 # the callback's own reads beat: no recursion
            raise OSError("disk")

        progress.bind(callback, 0.0)
        with self.assertLogs("mesh_runner", "WARNING") as logs:
            progress.beat()
            progress.beat()
        self.assertEqual(len(logs.output), 2)
        self.assertEqual(self.calls, ["in", "in"])

    def test_unbound_thread_does_nothing(self):
        progress.unbind()
        progress.beat()
        progress.step("wakes")
        self.assertEqual(progress.unbind(), {})
        self.assertEqual(self.calls, [])

    def test_a_lock_held_by_another_lane_is_waited_for_with_beats(self):
        lock = workspace.TimedLock("base:test", timeout=5)
        held, release = threading.Event(), threading.Event()

        def holder():
            with lock:
                held.set()
                release.wait(5)

        t = threading.Thread(target=holder)
        t.start()
        held.wait(5)
        threading.Timer(0.5, release.set).start()
        with mock.patch.object(workspace, "LOCK_WAIT_SLICE", 0.05):
            with lock:
                pass
        t.join()
        self.assertGreaterEqual(len(self.calls), 3)

    def test_a_lock_wait_keeps_its_total_budget(self):
        lock = workspace.TimedLock("base:test", timeout=0.3)
        held, release = threading.Event(), threading.Event()

        def holder():
            with lock:
                held.set()
                release.wait(5)

        t = threading.Thread(target=holder)
        t.start()
        held.wait(5)
        started = time.monotonic()
        with mock.patch.object(workspace, "LOCK_WAIT_SLICE", 0.05):
            with self.assertRaises(workspace.WorkspaceError):
                with lock:
                    pass
        release.set()
        t.join()
        self.assertLess(time.monotonic() - started, 2)

    def test_a_slow_start_gate_hook_beats_and_still_times_out(self):
        with mock.patch.object(progress, "SLICE_SECONDS", 0.05):
            rc, out, _ = AgentRunner._run_hook(None, ["sh", "-c", "sleep 0.4; echo ok"], {}, 5)
            self.assertEqual((rc, out.strip()), (0, "ok"))
            self.assertGreaterEqual(len(self.calls), 3)
            started = time.monotonic()
            rc, _, err = AgentRunner._run_hook(None, ["sleep", "5"], {}, 0.4)
        self.assertEqual((rc, err), (-1, "timed out"))
        self.assertLess(time.monotonic() - started, 3)

    def test_a_git_subprocess_beats_and_still_times_out(self):
        with mock.patch.object(progress, "SLICE_SECONDS", 0.05):
            r = workspace._run_git(["sh", "-c", "sleep 0.3; echo ok; echo warn >&2"], env=dict(os.environ), timeout=5)
            self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "ok\n", "warn\n"))
            self.assertGreaterEqual(len(self.calls), 3)
            started = time.monotonic()
            with self.assertRaises(subprocess.TimeoutExpired):
                workspace._run_git(["sleep", "5"], env=dict(os.environ), timeout=0.3)
        self.assertLess(time.monotonic() - started, 3)

    def test_a_command_run_by_progress_beats_keeps_input_and_output_and_times_out_like_subprocess_run(self):
        with mock.patch.object(progress, "SLICE_SECONDS", 0.05):
            r = progress.run(["sh", "-c", "sleep 0.3; cat; echo e >&2; exit 3"], input=b"payload", timeout=5)
            self.assertEqual((r.returncode, r.stdout, r.stderr), (3, b"payload", b"e\n"))
            self.assertGreaterEqual(len(self.calls), 3)
            started = time.monotonic()
            with self.assertRaises(subprocess.TimeoutExpired):
                progress.run(["sleep", "5"], timeout=0.3)
        self.assertLess(time.monotonic() - started, 3)
        with self.assertRaises(FileNotFoundError):
            progress.run(["/nonexistent/command"], timeout=1)

    def test_a_large_file_copied_without_cloning_beats_between_chunks(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, dst, small = Path(tmp, "big.bin"), Path(tmp, "copy.bin"), Path(tmp, "small.bin")
            data = os.urandom(5000)
            src.write_bytes(data)
            os.chmod(src, 0o640)
            os.utime(src, (1_700_000_000, 1_700_000_000))
            small.write_bytes(b"x")
            with mock.patch.object(checkpoint, "_clonefile", return_value=None), \
                    mock.patch.object(checkpoint, "COPY_CHUNK", 1024):
                self.assertEqual(checkpoint._copy_file(src, dst), dst)
                beats = len(self.calls)
                checkpoint._copy_file(small, Path(tmp, "small-copy.bin"))
            self.assertEqual(dst.read_bytes(), data)
            self.assertEqual(oct(dst.stat().st_mode & 0o777), "0o640")
            self.assertEqual(int(dst.stat().st_mtime), 1_700_000_000)
            self.assertGreaterEqual(beats, 5)             # one before the file, one after each chunk
            self.assertEqual(Path(tmp, "small-copy.bin").read_bytes(), b"x")

    def test_a_tree_is_removed_with_a_beat_per_directory_and_no_symlink_followed(self):
        with tempfile.TemporaryDirectory() as tmp:
            outside = Path(tmp, "outside")
            outside.mkdir()
            (outside / "keep").write_text("k")
            tree = Path(tmp, "tree")
            for d in ("a/b/c", "d/e", "f"):
                (tree / d).mkdir(parents=True)
                (tree / d / "file").write_text("x")
            os.chmod(tree / "f" / "file", 0o400)
            (tree / "a" / "to-dir").symlink_to(outside)
            (tree / "d" / "to-file").symlink_to(outside / "keep")
            control = Path(tmp, "control")
            subprocess.run(["cp", "-R", str(tree), str(control)], check=True)
            shutil.rmtree(control)
            self.assertEqual(self.calls, [])          # positive control: shutil.rmtree alone never beats
            checkpoint._rmtree(tree)
            self.assertFalse(tree.exists())
            self.assertGreaterEqual(len(self.calls), 7)   # tree, a, a/b, a/b/c, d, d/e, f
            self.assertEqual((outside / "keep").read_text(), "k")
            link = Path(tmp, "link-root")
            link.symlink_to(outside)
            with self.assertRaises(OSError):
                checkpoint._rmtree(link)               # as shutil.rmtree: a symlink root is refused
            self.assertEqual((outside / "keep").read_text(), "k")
            checkpoint._rmtree(Path(tmp, "missing"), ignore_errors=True)
            with self.assertRaises(FileNotFoundError):
                checkpoint._rmtree(Path(tmp, "missing"))

    def test_copying_and_hashing_a_file_beat_per_chunk(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp, "big.bin")
            src.write_bytes(os.urandom(3 << 20))
            dest = os.open(tmp, os.O_RDONLY | os.O_DIRECTORY)
            try:
                workspace._copy_file_fd(str(src), dest, "copy.bin")
            finally:
                os.close(dest)
            self.assertEqual(Path(tmp, "copy.bin").read_bytes(), src.read_bytes())
            self.assertGreaterEqual(len(self.calls), 3)
            del self.calls[:]
            workspace._entry_digest(str(src))
            self.assertGreaterEqual(len(self.calls), 3)

    def test_a_checkpoint_subprocess_beats_and_keeps_its_output_and_input(self):
        with mock.patch.object(progress, "SLICE_SECONDS", 0.05):
            r = checkpoint._run(["sh", "-c", "sleep 0.3; cat"], input=b"payload", capture_output=True)
            self.assertEqual(r.stdout, b"payload")
            self.assertGreaterEqual(len(self.calls), 3)
            with self.assertRaises(workspace.WorkspaceError):
                checkpoint._run(["sleep", "5"], timeout=0.3)


if __name__ == "__main__":
    unittest.main()
