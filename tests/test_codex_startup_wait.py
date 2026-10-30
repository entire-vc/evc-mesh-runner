"""Codex start-up on a loaded host: the driver waits for it instead of failing the turn.

On a saturated host, a resumed Codex can show its prompt box at once ("Resuming session…"); a
prompt entered then is queued under "Waiting for startup · esc cancel", and start-up can take 30-130 s. The
driver ran on a scripted tmux and a fake clock, so minutes of waiting cost no time.
"""
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from mesh_runner.hostmem import LoadGate, LoadThresholds, decide_load
from mesh_runner.tui_driver import DriverError, Driver, CODEX_STARTUP_RE

PROMPT = "Continue the card and report back."
IDLE = "› Ask Codex to do anything\n\n  gpt · ~/work\n  ? for shortcuts\n"
RESUMING = ("Resuming session…\n› [Pasted Content 34 chars]\n\n  gpt · ~/work\n"
            "  Waiting for startup · esc cancel\n")
RESUMING_EMPTY = "Resuming session…\n› Ask Codex to do anything\n\n  gpt · ~/work\n  ? for shortcuts\n"
WORKING = "• Working (3s • esc to interrupt)\n› [Pasted Content 34 chars]\n"


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class ScriptedTmux:
    """Answers the few tmux calls a Codex turn makes. `screen(elapsed_since_paste)` is the pane."""

    def __init__(self, clock, screen):
        self.clock, self.screen = clock, screen
        self.pasted_at = None
        self.calls = []

    def run(self, *args, **kw):
        self.calls.append(args)
        cmd = args[0]
        out = ""
        if cmd == "capture-pane":
            out = self.screen(None if self.pasted_at is None else self.clock.now - self.pasted_at)
        elif cmd == "list-panes":
            out = "0 \n"
        elif cmd == "paste-buffer":
            self.pasted_at = self.clock.now
        return subprocess.CompletedProcess(args, 0, out, "")

    def keys(self, key):
        return [c for c in self.calls if c[0] == "send-keys" and key in c]

    def killed(self):
        return [c for c in self.calls if c[0] == "kill-window"]


class StartupWait(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.clock = FakeClock()

    def tearDown(self):
        self.tmp.cleanup()

    def driver(self, screen, **timeouts):
        tmux = ScriptedTmux(self.clock, screen)
        out = io.StringIO()
        d = Driver(harness="codex", agent="builder", state=self.root / "state", tmux=tmux, max_sessions=10,
                   cwd=self.root, session_id="", mode="resume", key="att-1", argv=["codex"], out=out,
                   env={"HOME": str(self.root), "CODEX_HOME": str(self.root / "codex")},
                   clock=self.clock.time, sleep=self.clock.sleep,
                   timeouts={"beat": 3600, "paste": 5, **timeouts})
        d.private_dir()
        d.ensure_session = lambda: None
        d.marks = {}
        return d, tmux, out

    def test_defaults(self):
        d, _, _ = self.driver(lambda e: IDLE)
        self.assertEqual((d.t["ready"], d.t["startup_wait"]), (180, 300))

    def test_marker_shown_for_two_minutes_then_cleared_takes_the_prompt(self):
        d = None

        def screen(elapsed):
            if elapsed is None:
                return IDLE
            if elapsed < 120:
                return RESUMING
            if d.events.exists() is False and elapsed >= 124:
                d.events.write_text(json.dumps({"type": "agent-turn-complete", "thread-id": "t", "turn-id": "x",
                                                "input-messages": [PROMPT], "last-assistant-message": "ok"}) + "\n")
            return WORKING if elapsed < 125 else IDLE
        d, tmux, _ = self.driver(screen)
        done = d.turn(PROMPT)
        self.assertEqual(done["event"]["last-assistant-message"], "ok")
        self.assertTrue(d.prompt_taken)
        self.assertEqual(len(tmux.keys("Enter")), 1)        # the paste's own Enter: no retry, no failure
        self.assertGreaterEqual(self.clock.now - tmux.pasted_at, 120)

    def test_marker_past_the_cap_fails_retryably_and_keeps_the_window(self):
        d, tmux, out = self.driver(lambda e: IDLE if e is None else RESUMING)
        start = self.clock.now
        self.assertEqual(d.run(PROMPT), 1)
        waited = self.clock.now - start
        self.assertTrue(300 <= waited < 330, waited)
        self.assertFalse(d.prompt_taken)
        self.assertEqual(tmux.killed(), [])                  # close=False: the window stays
        events = [json.loads(x) for x in out.getvalue().splitlines()]
        message = events[-1]["error"]["message"]
        self.assertEqual(events[-1]["type"], "turn.failed")
        self.assertIn("start-up stalled", message)
        self.assertIn("host load average", message)

    def test_stall_is_a_driver_error_without_close(self):
        d, _, _ = self.driver(lambda e: IDLE if e is None else RESUMING, startup_wait=20)
        with self.assertRaises(DriverError) as raised:
            d.turn(PROMPT)
        self.assertFalse(raised.exception.close)
        self.assertFalse(d.prompt_taken)

    def test_a_queued_prompt_is_not_taken_just_because_the_marker_is_there(self):
        d, _, _ = self.driver(lambda e: IDLE if e is None else RESUMING, startup_wait=20)
        self.assertFalse(d.accepted(0, False))
        self.assertFalse(d.busy())                           # the marker is not a running turn

    def test_wait_ready_does_not_return_while_resuming_is_shown(self):
        d, _, _ = self.driver(lambda e: RESUMING_EMPTY, ready=40)
        with self.assertRaises(DriverError) as raised:
            d.wait_ready()
        self.assertIn("did not become ready", str(raised.exception))
        self.assertGreaterEqual(self.clock.now - 1000.0, 40)

    def test_wait_ready_returns_once_resuming_is_gone(self):
        start = self.clock.now
        d, _, _ = self.driver(lambda e: RESUMING_EMPTY if self.clock.now - start < 70 else IDLE)
        d.wait_ready()
        self.assertGreaterEqual(self.clock.now - start, 70)
        self.assertLess(self.clock.now - start, 75)

    def test_markers_count_in_the_last_lines_only(self):
        d, _, _ = self.driver(lambda e: IDLE)
        old = "Resuming session…\nWaiting for startup\n" + "history line\n" * 12 + IDLE
        self.assertFalse(d.starting(old))
        self.assertTrue(d.starting("history\n" * 12 + RESUMING))
        self.assertTrue(CODEX_STARTUP_RE.search("Waiting for startup · esc cancel"))
        self.assertTrue(CODEX_STARTUP_RE.search("waiting for startup"))

    def test_claude_is_never_starting(self):
        d, _, _ = self.driver(lambda e: RESUMING)
        d.harness = "claude"
        self.assertFalse(d.starting(RESUMING))


class LoadGateTests(unittest.TestCase):
    T = LoadThresholds(hold_per_cpu=4.0)

    def test_decide_hysteresis_and_off(self):
        self.assertTrue(decide_load(False, 50.0, 10, self.T))        # 5.0 per CPU > 4.0
        self.assertFalse(decide_load(False, 30.0, 10, self.T))       # 3.0 per CPU
        self.assertFalse(decide_load(False, 40.0, 10, self.T))       # exactly the threshold is not over it
        self.assertTrue(decide_load(True, 35.0, 10, self.T))         # still above 0.75 * 4 = 3.0
        self.assertFalse(decide_load(True, 25.0, 10, self.T))
        self.assertTrue(decide_load(True, 37.0, 10, LoadThresholds(4.0, 3.6)))   # explicit release value
        self.assertFalse(decide_load(False, 500.0, 10, LoadThresholds()))        # 0 = off
        self.assertFalse(decide_load(True, None, 10, self.T))                    # no reading: open

    def test_gate_holds_above_the_threshold_and_not_below(self):
        reading = [(30.0, 10)]
        now = [0.0]
        gate = LoadGate(self.T, reader=lambda: reading[0], clock=lambda: now[0])
        with self.assertLogs("mesh_runner", "WARNING") as logs:
            self.assertFalse(gate.check())                            # 3.0 per CPU
            reading[0], now[0] = (300.0, 10), 31.0
            self.assertTrue(gate.check())                             # 30 per CPU
            now[0] = 40.0
            self.assertTrue(gate.check())                             # cached for 30 s, one log line
            reading[0], now[0] = (10.0, 10), 80.0
            self.assertFalse(gate.check())
        self.assertEqual(len(logs.records), 2)
        self.assertIn("host load: HOLD new sessions", logs.output[0])
        self.assertIn("host load: resume new sessions", logs.output[1])
        self.assertNotIn("host memory", "".join(logs.output))

    def test_unreadable_load_and_zero_threshold_do_not_hold(self):
        def broken():
            raise OSError("no load")
        self.assertFalse(LoadGate(self.T, reader=broken).check())
        self.assertFalse(LoadGate(LoadThresholds(), reader=lambda: (900.0, 1)).check())


if __name__ == "__main__":
    unittest.main()
