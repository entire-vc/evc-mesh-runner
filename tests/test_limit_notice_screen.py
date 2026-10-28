"""A usage-limit notice left on the pane is not a running turn (live 28.10).

Claude Code keeps "Usage limit reached · continuing automatically at 12pm · esc to cancel" on screen after the
stated time. BUSY_RE read its "esc to cancel" as a running turn, so every later turn on that window waited
busy_wait, failed with "the session stayed busy" and the runner paused new starts for 30 minutes, again and again.
"""
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from mesh_runner.tui_driver import Driver, DriverError, is_busy, limit_notice

PROMPT = "Continue the card and report back."
NOTICE = ("  ⎿  You've hit your weekly limit · resets 12pm (Europe/Moscow)\n"
          "     /usage-credits to finish what you’re working on.\n"
          "⏺ Usage limit reached · continuing automatically at 12pm · esc to cancel\n"
          "✻ Crunched for 1s · done 11:32 AM\n"
          "─" * 1 + "─" * 40 + "\n"
          "❯\n"
          + "─" * 41 + "\n"
          "  ⏵⏵ bypass permissions on (shift+tab to cycle)\n")
SPINNER = ("⏺ Reading the card.\n✽ Forging… (1m 2s · ↓ 3.1k tokens)\n"
           + "─" * 41 + "\n❯\n" + "─" * 41 + "\n  esc to interrupt\n")
INTERRUPT = "⏺ Running tests\n" + "─" * 41 + "\n❯\n" + "─" * 41 + "\n  esc to interrupt · ctrl+b to run in background\n"
WORKING = "• Working (3s • esc to interrupt)\n› Ask Codex\n"
IDLE = "⏺ Done.\n✻ Crunched for 12s\n" + "─" * 41 + "\n❯\n" + "─" * 41 + "\n  ⏵⏵ bypass permissions on\n"


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class ScriptedTmux:
    def __init__(self, screen):
        self.screen, self.calls = screen, []

    def run(self, *args, **kw):
        self.calls.append(args)
        out = self.screen if args[0] == "capture-pane" else ("0 \n" if args[0] == "list-panes" else "")
        return subprocess.CompletedProcess(args, 0, out, "")

    def killed(self):
        return [c for c in self.calls if c[0] == "kill-window"]

    def pasted(self):
        return [c for c in self.calls if c[0] == "paste-buffer"]


class ScreenReading(unittest.TestCase):
    def test_a_stale_limit_notice_with_an_idle_prompt_is_not_busy(self):
        self.assertFalse(is_busy(NOTICE))

    def test_real_busy_markers_stay_busy(self):
        for screen in (SPINNER, INTERRUPT, WORKING):
            self.assertTrue(is_busy(screen), screen)

    def test_a_running_turn_below_an_old_notice_is_busy(self):
        self.assertTrue(is_busy(NOTICE + SPINNER))
        self.assertEqual(limit_notice(NOTICE + SPINNER), "")

    def test_notice_is_found_only_on_an_idle_screen_and_only_in_the_last_lines(self):
        self.assertIn("Usage limit reached", limit_notice(NOTICE))
        self.assertEqual(limit_notice(IDLE), "")
        self.assertEqual(limit_notice(NOTICE + "\n".join("line %d" % i for i in range(12)) + "\n"), "")


class TurnOnALimitScreen(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.clock = FakeClock()

    def tearDown(self):
        self.tmp.cleanup()

    def driver(self, screen):
        tmux = ScriptedTmux(screen)
        out = io.StringIO()
        d = Driver(harness="claude", agent="builder", state=self.root / "state", tmux=tmux, max_sessions=10,
                   cwd=self.root, session_id="", mode="resume", key="att-1", argv=["claude"], out=out,
                   env={"HOME": str(self.root)}, clock=self.clock.time, sleep=self.clock.sleep,
                   timeouts={"beat": 3600, "paste": 5})
        d.private_dir()
        d.ensure_session = lambda: None
        d.marks = {}
        return d, tmux, out

    def test_the_limit_is_reported_at_once_and_the_window_is_closed(self):
        d, tmux, out = self.driver(NOTICE)
        start = self.clock.now
        self.assertEqual(d.run(PROMPT), 1)
        self.assertLess(self.clock.now - start, 60)         # no busy_wait ("stayed busy") first
        self.assertEqual(tmux.pasted(), [])                  # the prompt never went into the pane
        self.assertTrue(tmux.killed())                       # close=True: the next turn starts a fresh window
        events = [json.loads(x) for x in out.getvalue().splitlines()]
        self.assertEqual((events[-1]["type"], events[-1]["is_error"]), ("result", True))
        message = events[-1]["result"]
        self.assertIn("usage limit reached", message.lower())
        self.assertNotIn("stayed busy", message)

    def test_the_error_is_a_closing_driver_error(self):
        d, _, _ = self.driver(NOTICE)
        with self.assertRaises(DriverError) as ctx:
            d.turn(PROMPT)
        self.assertTrue(ctx.exception.close)


if __name__ == "__main__":
    unittest.main()
