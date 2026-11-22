"""A usage-limit notice left on the pane is not a running turn.

Claude Code keeps "Usage limit reached · continuing automatically at 12pm · esc to cancel" on screen after the
stated time. BUSY_RE read its "esc to cancel" as a running turn, so every later turn on that window waited
busy_wait, failed with "the session stayed busy" and the runner paused new starts for 30 minutes, again and again.
"""
import datetime as dt
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

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
MSK = ZoneInfo("Europe/Moscow")
NOTICE_SHOWN = dt.datetime(2026, 11, 21, 11, 32, tzinfo=MSK)   # "done 11:32 AM": the limit is live until 12pm
NOTICE_PASSED = dt.datetime(2026, 11, 21, 14, 0, tzinfo=MSK)   # the same screen after its 12pm reset


def host_clock_at(when):
    """Patch the driver module's datetime so datetime.now() reads ``when``: the host's clock, not the fixture's."""
    class Fixed(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return when.astimezone(tz) if tz else when.astimezone().replace(tzinfo=None)
    return mock.patch("mesh_runner.tui_driver.datetime", Fixed)


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

    def driver(self, screen, wall=NOTICE_SHOWN):
        """The wall clock is the notice's own moment: the screen says "done 11:32 AM" and "resets 12pm", so the
        limit is live. Read from the host, the same fixture turned into a past notice after noon (red on 0.2.120)."""
        tmux = ScriptedTmux(screen)
        out = io.StringIO()
        d = Driver(harness="claude", agent="builder", state=self.root / "state", tmux=tmux, max_sessions=10,
                   cwd=self.root, session_id="", mode="resume", key="att-1", argv=["claude"], out=out,
                   env={"HOME": str(self.root)}, clock=self.clock.time, sleep=self.clock.sleep,
                   timeouts={"beat": 3600, "paste": 5})
        d.wall = lambda: wall
        d.private_dir()
        d.ensure_session = lambda: None
        d.marks = {}
        return d, tmux, out

    def test_the_limit_is_reported_at_once_and_the_window_is_closed(self):
        self.assert_reported_and_closed()

    def test_the_outcome_does_not_depend_on_the_host_clock(self):
        for i, host in enumerate((NOTICE_SHOWN.replace(hour=11, minute=0), NOTICE_PASSED)):
            with self.subTest(host=host.isoformat()), host_clock_at(host):
                self.root, self.clock = Path(self.tmp.name) / str(i), FakeClock()
                self.assert_reported_and_closed()

    def test_the_same_notice_after_its_reset_is_no_limit(self):
        """Past 12pm the notice has outlived its window (StaleNoticeIsNoLimit in test_limit_hold): the offer is
        dismissed once and the turn goes on to the paste instead of closing the window as a limit."""
        d, tmux, _ = self.driver(NOTICE, wall=NOTICE_PASSED)
        self.assertEqual(d.live_limit_notice(), "")
        self.assertEqual([c[-1] for c in tmux.calls if c[0] == "send-keys"], ["Escape"])

        d, tmux, _ = self.driver(NOTICE, wall=NOTICE_PASSED)

        class Pasting(Exception):
            pass
        with mock.patch.object(d, "wait_background", return_value=True), \
                mock.patch.object(d, "wait_idle", side_effect=Pasting):
            with self.assertRaises(Pasting):
                d.turn(PROMPT)
        self.assertFalse(tmux.killed())

    def assert_reported_and_closed(self):
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
