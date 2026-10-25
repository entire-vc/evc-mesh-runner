"""Turn progress watchdog: a turn with no first output / no progress is ended early, safely."""
import os
import signal
import tempfile
import time
import unittest
from unittest.mock import patch
from pathlib import Path

from fakes import FakeMesh, calls, task, write_replies
from test_runner import Bench

from mesh_runner import interactive, watchdog
from mesh_runner.adapters.base import group_alive, interrupt_group
from mesh_runner.config import ConfigError, load
from mesh_runner.watchdog import Signal, judge


def sig(phase="output", ago=0.0, external_wait=False, updated_ago=0.0, source="driver", now=10_000.0):
    return Signal(source, phase, now - ago, external_wait, now - updated_ago if updated_ago is not None else 0.0)


class Judge(unittest.TestCase):
    NOW = 10_000.0

    def judge(self, signal, started_ago=0.0, **limits):
        return judge(signal, started=self.NOW - started_ago, now=self.NOW, **limits)

    def test_everything_off_by_default(self):
        self.assertEqual(self.judge(sig("launcher", ago=99999), started_ago=99999), "")

    def test_no_signal_and_a_finished_turn_are_never_judged(self):
        self.assertEqual(self.judge(None, started_ago=99999, first_output_minutes=1, idle_minutes=1), "")
        self.assertEqual(self.judge(sig("done", ago=99999), started_ago=99999, first_output_minutes=1), "")

    def test_no_first_output_names_the_phase_it_stuck_in(self):
        for phase in ("launcher", "ready", "sent"):
            why = self.judge(sig(phase, ago=1500), started_ago=1500, first_output_minutes=20)
            self.assertIn("no first output 25 min", why)
            self.assertIn(f"phase {phase}", why)

    def test_first_output_in_time_is_not_a_stall(self):
        self.assertEqual(self.judge(sig("sent", ago=100), started_ago=1100, first_output_minutes=20), "")
        self.assertEqual(self.judge(sig("output", ago=100), started_ago=99999, first_output_minutes=20), "")

    def test_idle_limit_counts_from_the_last_progress_not_from_the_start(self):
        self.assertEqual(self.judge(sig("output", ago=60), started_ago=7200, idle_minutes=10), "")
        why = self.judge(sig("output", ago=900), started_ago=7200, idle_minutes=10)
        self.assertIn("no progress for 15 min", why)
        self.assertIn("nothing is running", why)

    def test_a_running_tool_has_its_own_limit_and_off_means_no_early_kill(self):
        busy = sig("output", ago=7200, external_wait=True)
        self.assertEqual(self.judge(busy, started_ago=9000, idle_minutes=10), "")          # idle limit does not apply
        why = self.judge(busy, started_ago=9000, idle_minutes=10, external_wait_minutes=90)
        self.assertIn("no progress for 120 min", why)
        self.assertIn("running a tool", why)
        self.assertEqual(self.judge(sig("output", ago=3000, external_wait=True), started_ago=9000,
                                    idle_minutes=10, external_wait_minutes=90), "")

    def test_progress_from_an_earlier_turn_is_not_this_turns_quiet(self):
        self.assertEqual(self.judge(sig("launcher", ago=99999), started_ago=60, idle_minutes=10), "")

    def test_a_silent_driver_is_a_stall_whatever_it_last_said(self):
        why = self.judge(sig("output", ago=0, updated_ago=700), started_ago=800, idle_minutes=60)
        self.assertIn("driver reported nothing for 11 min", why)
        # a log has no reporter to lose
        self.assertEqual(self.judge(sig("output", ago=0, updated_ago=None, source="log"), started_ago=800,
                                    idle_minutes=60), "")


class Config(unittest.TestCase):
    def load(self, runner=""):
        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "k"
            key.write_text("x")
            key.chmod(0o600)
            path = Path(tmp) / "r.toml"
            path.write_text(f'[runner]\nmesh_url = "http://m"\nstate_dir = "{tmp}"\n{runner}'
                            f'[[agents]]\nname = "a"\nkey_file = "{key}"\nprofile_dir = "{tmp}"\n')
            return load(path)

    def test_defaults_leave_the_watchdog_off(self):
        cfg = self.load()
        self.assertEqual((cfg.turn_first_output_minutes, cfg.turn_idle_minutes, cfg.turn_external_wait_minutes),
                         (0.0, 0.0, 0.0))

    def test_limits_are_read_and_validated(self):
        cfg = self.load("turn_first_output_minutes = 20\nturn_idle_minutes = 45\nturn_external_wait_minutes = 90.5\n")
        self.assertEqual((cfg.turn_first_output_minutes, cfg.turn_idle_minutes, cfg.turn_external_wait_minutes),
                         (20.0, 45.0, 90.5))
        for bad in ("turn_idle_minutes = -1\n", 'turn_idle_minutes = "45"\n', "turn_idle_minutes = true\n"):
            with self.assertRaises(ConfigError):
                self.load(bad)


class Stub:
    """A tmux stand-in that records the windows the runner closes."""
    def __init__(self):
        self.killed = []

    def kill_window(self, session, window):
        self.killed.append((session, window))


class Watched(Bench):
    def start_silent_turn(self, **limits):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        for k, v in limits.items():
            setattr(self.cfg, k, v)
        write_replies(self.replies, {t["id"][:8]: "SLEEP:60:⏳ WAIT 2h: soak window",
                                     t["id"][:8] + ":resume": "⏳ WAIT 2h: soak window"})
        r = self.runner(mesh)
        r.tick()
        a = r.store.by_state("builder", ("running",))[0]
        self.assertTrue(group_alive(a.pid, a.stamp))
        self.addCleanup(self.stop, r, a)                 # never leave the 60 s harness behind
        return r, mesh, t, a

    def kinds(self, r, a):
        return [(kind, detail) for _, kind, detail in r.store.events(a.id) if kind in ("watchdog", "kill")]

    def stop(self, r, a):
        interrupt_group(a.pid, signal.SIGKILL)
        proc = r.procs.get(a.id)
        if proc is not None:
            proc.wait(timeout=10)


class HeadlessTurns(Watched):
    def test_off_by_default_a_silent_turn_is_left_alone(self):
        r, _, _, a = self.start_silent_turn()
        time.sleep(1.2)
        r.tick()
        self.assertEqual(r.store.get(a.id).state, "running")
        self.assertEqual(self.kinds(r, a), [])

    def test_a_silent_headless_turn_is_ended_by_the_idle_limit_and_the_task_goes_on(self):
        r, mesh, t, a = self.start_silent_turn(turn_idle_minutes=0.01)
        time.sleep(1.2)
        started = time.time()
        r.tick()                                                     # the watchdog fires here
        self.assertIn(t["id"], mesh.checkouts)                       # the card stays checked out to the agent
        self.settle_all(r, timeout=20)
        self.assertLess(time.time() - started, 15)                  # not the 60 s the harness would have slept
        kinds = self.kinds(r, a)
        self.assertEqual([k for k, _ in kinds], ["watchdog", "kill"])
        self.assertIn("no progress", kinds[0][1])
        self.assertIn("phase log", kinds[0][1])
        self.assertTrue(kinds[1][1].startswith("turn watchdog: no progress"))
        self.assertFalse(group_alive(a.pid))
        # the existing no-outcome path takes over: the task is resumed in the same session (it
        # is not dropped or started over), then waits as its second turn says
        after = r.store.get(a.id)
        self.assertEqual((after.state, after.turns), ("waiting", 2))
        self.assertEqual([c["resume"] for c in calls(self.calls)], [True])

    def test_a_headless_log_is_not_judged_for_first_output(self):
        r, _, _, a = self.start_silent_turn(turn_first_output_minutes=0.01)
        time.sleep(1.2)
        r.tick()
        self.assertEqual(r.store.get(a.id).state, "running")
        self.assertEqual(self.kinds(r, a), [])

    def test_it_fires_once_per_turn(self):
        r, _, _, a = self.start_silent_turn(turn_idle_minutes=0.01)
        time.sleep(1.2)
        r._watch_progress(a, time.time() - 5)
        r._watch_progress(a, time.time() - 5)
        self.assertEqual([k for k, _ in self.kinds(r, a)].count("watchdog"), 1)

    def test_it_fires_once_per_turn_across_a_runner_restart(self):
        r, _, _, a = self.start_silent_turn(turn_idle_minutes=0.01)
        time.sleep(1.2)
        r._watch_progress(a, time.time() - 5)
        r._watchdog_fired.clear()                          # a restarted runner has no in-memory record
        with patch.object(r, "_kill") as kill:
            r._watch_progress(a, time.time() - 5)
        kill.assert_not_called()
        self.assertEqual([k for k, _ in self.kinds(r, a)].count("watchdog"), 1)

    def test_log_growth_is_progress_and_a_quiet_log_is_not(self):
        r, _, _, a = self.start_silent_turn(turn_idle_minutes=0.02)      # 1.2 s
        log = Path(a.log_path)
        time.sleep(1.5)
        log.write_text('{"type":"system"}\n')                            # first output; the log is fresh
        r.tick()
        self.assertEqual(r.store.get(a.id).state, "running")
        old = time.time() - 120
        os.utime(log, (old, old))                                        # quiet for two minutes
        r.tick()
        why = [d for k, d in self.kinds(r, a) if k == "watchdog"]
        self.assertEqual(len(why), 1)
        self.assertIn("no progress for", why[0])


class InteractiveTurns(Watched):
    """The turn's log stays empty until it ends; the driver's progress file is the signal."""

    def setUp(self):
        super().setUp()
        self.tmux = Stub()

    def watch(self, r, a, **report):
        r._tmux = self.tmux
        r.adapter_for = lambda harness: interactive.InteractiveAdapter(self.adapter, "builder", self.cfg.state_dir)
        d = interactive.agent_dir(self.cfg.state_dir, "builder")
        key = Path(a.log_path).name.split(".")[0]
        now = time.time()
        interactive.write_progress(d, key, {"pid": a.pid, "phase": "output", "progress_at": now, "updated": now,
                                            "external_wait": False, **report})
        return d, key

    def test_a_working_session_with_an_empty_log_is_not_stuck(self):
        r, _, _, a = self.start_silent_turn(turn_first_output_minutes=0.01, turn_idle_minutes=0.5)
        self.assertEqual(Path(a.log_path).stat().st_size, 0)             # the 0-byte log is by design
        time.sleep(1.2)
        self.watch(r, a)
        r.tick()
        self.assertEqual(r.store.get(a.id).state, "running")
        self.assertEqual(self.kinds(r, a), [])

    def test_an_idle_screen_without_progress_closes_the_turn_and_its_window(self):
        r, mesh, t, a = self.start_silent_turn(turn_idle_minutes=0.02)
        time.sleep(1.5)
        old = time.time() - 600
        d, key = self.watch(r, a, progress_at=old)                       # e.g. a dialog nobody answers
        r.tick()
        why = [x for k, x in self.kinds(r, a) if k == "watchdog"]
        self.assertEqual(len(why), 1)
        self.assertIn("no progress for", why[0])
        self.assertIn("nothing is running", why[0])
        self.assertFalse(group_alive(a.pid))
        # one writer: the driver is gone first, then the harness window; the next turn resumes in a new one
        self.assertEqual(self.tmux.killed, [("mr-builder", key)])
        self.assertFalse(interactive.progress_path(d, key).exists())
        self.assertIn(t["id"], mesh.checkouts)

    def test_a_running_tool_is_waited_on_until_its_own_limit(self):
        r, _, _, a = self.start_silent_turn(turn_idle_minutes=0.02)
        time.sleep(1.5)
        self.watch(r, a, progress_at=time.time() - 600, external_wait=True)
        r.tick()
        self.assertEqual(self.kinds(r, a), [])                           # only the idle limit is set
        self.assertEqual(self.tmux.killed, [])
        r.cfg.turn_external_wait_minutes = 0.02
        r.tick()
        self.assertEqual([k for k, _ in self.kinds(r, a)], ["watchdog", "kill"])
        self.assertIn("running a tool", self.kinds(r, a)[0][1])

    def test_a_prompt_held_for_background_agents_is_judged_for_first_output_only_after_the_hold(self):
        r, _, _, a = self.start_silent_turn(turn_first_output_minutes=0.01)
        time.sleep(1.2)
        self.watch(r, a, phase="ready", held=True, external_wait=True)   # the driver holds the prompt
        r.tick()
        self.assertEqual(self.kinds(r, a), [])
        self.watch(r, a, phase="ready", released_at=time.time())          # just released: the clock restarts
        r.tick()
        self.assertEqual(self.kinds(r, a), [])
        self.watch(r, a, phase="sent", released_at=time.time() - 600)
        r.tick()
        why = [x for k, x in self.kinds(r, a) if k == "watchdog"]
        self.assertEqual(len(why), 1)
        self.assertIn("no first output", why[0])

    def test_a_report_from_an_earlier_turn_does_not_count(self):
        r, _, _, a = self.start_silent_turn(turn_first_output_minutes=0.01)
        time.sleep(1.2)
        self.watch(r, a, pid=a.pid + 1)                                  # another driver's file
        r.tick()                                                         # no report for this turn: still "launcher"
        self.assertIn("phase launcher", self.kinds(r, a)[0][1])

    def test_a_driver_of_an_older_runner_reports_nothing_and_is_left_to_the_turn_limit(self):
        r, _, _, a = self.start_silent_turn(turn_first_output_minutes=0.01)
        time.sleep(1.2)
        r._tmux = self.tmux
        r.adapter_for = lambda harness: interactive.InteractiveAdapter(self.adapter, "builder", self.cfg.state_dir)
        r._boot = time.time() + 10                                       # the runner restarted after this turn began
        r.tick()
        self.assertEqual(self.kinds(r, a), [])

    def test_a_driver_that_stopped_reporting_is_ended(self):
        r, _, _, a = self.start_silent_turn(turn_idle_minutes=600)
        self.watch(r, a, updated=time.time() - 2 * watchdog.REPORTER_SILENT_SECONDS)
        r.tick()
        self.assertIn("driver reported nothing", self.kinds(r, a)[0][1])


if __name__ == "__main__":
    unittest.main()
