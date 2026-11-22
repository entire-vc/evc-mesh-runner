"""A busy hint quoted in the conversation is not a running turn.

A finished Claude Code turn quoted a limit notice, and the 220-column pane wrapped the quote so that a line read
"automatically at 12am · esc to cancel". is_busy scanned the whole viewport, matched it above an empty ❯ box, and
every later prompt on that window failed after busy_wait with "the session stayed busy"; the lane sat BROKEN.
Only Claude Code's live UI counts: the status line right above the input box, the box and the footer below it.
"""
import io
import subprocess
import tempfile
import unittest
from pathlib import Path

from mesh_runner.tui_driver import Driver, is_busy, limit_notice

RULE = "─" * 60
QUOTE = ('⏺ The runner log for the failed attempt reads, verbatim: "Usage limit reached · continuing\n'
         '  automatically at 12am · esc to cancel". Series of resumes after that all failed the same way.\n')
QUOTE_WRAPPED = ('⏺ Quoted from the pane: "⏺ Usage limit reached · continuing\n'
                 '8 automatically at 12am · esc to cancel". Series of turns stayed busy afterwards.\n')
FOOTER_IDLE = "  ⏵⏵ bypass permissions on (shift+tab to cycle)\n"
BOX = RULE + "\n❯ \n" + RULE + "\n"
# the screen of the incident: an old quote high up, a finished turn, an empty box
INCIDENT = ("❯ Continue the card.\n\n" + QUOTE_WRAPPED + "\n" + "⏺ Bash(git status)\n  ⎿  clean\n\n"
            + "⏺ Done, waiting for review.\n\n✻ Worked for 3m 12s\n\n" + BOX + FOOTER_IDLE + "\n\n\n")
# the live layout as captured (blank lines kept) from a running Claude Code pane
RUNNING = ("⏺ Bash(pytest -q)\n  ⎿  Running…\n\n"
           "✽ Warping… (7m 30s · ↓ 21.0k tokens · thought for 15s)\n"
           "  ⎿  Tip: Use /btw to ask a quick side question without interrupting Claude's current work\n\n"
           + BOX + "  ⏵⏵ bypass permissions on · 1 shell · esc to interrupt · ↓ to manage\n\n")

class LiveRegion(unittest.TestCase):
    def test_wrapped_quote_above_an_idle_box_is_not_busy(self):
        self.assertFalse(is_busy(INCIDENT))

    def test_quote_in_the_last_reply_right_above_the_box_is_not_busy(self):
        for quote in (QUOTE, QUOTE_WRAPPED):
            with self.subTest(quote=quote[:30]):
                self.assertFalse(is_busy(quote + BOX + FOOTER_IDLE))
                self.assertFalse(is_busy(quote + "\n✻ Crunched for 4s\n" + BOX + FOOTER_IDLE))

    def test_a_wrap_that_starts_with_punctuation_is_not_a_status_line(self):
        for mark in ("·", "-", "*", "|", "•"):
            wrapped = '⏺ The notice read "Usage limit reached · continuing automatically at 12am\n' + mark + ' esc to cancel". Series…\n'
            with self.subTest(mark=mark):
                self.assertFalse(is_busy(wrapped + BOX + FOOTER_IDLE))

    def test_a_quoted_notice_near_the_bottom_is_no_usage_limit(self):
        for screen in (INCIDENT, QUOTE + BOX + FOOTER_IDLE, QUOTE_WRAPPED + "\n✻ Crunched for 4s\n" + BOX + FOOTER_IDLE):
            with self.subTest(screen=screen[:30]):
                self.assertEqual(limit_notice(screen), "")
        native = "⏺ Usage limit reached · continuing automatically at 12pm · esc to cancel\n✻ Crunched for 1s\n" + BOX + FOOTER_IDLE
        self.assertIn("Usage limit reached", limit_notice(native))
        self.assertIn("weekly limit", limit_notice("  ⎿  You’ve hit your weekly limit · resets 12pm\n" + BOX + FOOTER_IDLE))

    def test_tool_output_quoting_a_hint_right_above_the_box_is_not_busy(self):
        screen = "⏺ Bash(grep cancel log)\n  ⎿  Working (3s • esc to interrupt)\n     esc to cancel\n" + BOX + FOOTER_IDLE
        self.assertFalse(is_busy(screen))

    def test_live_running_layout_is_busy(self):
        self.assertTrue(is_busy(RUNNING))
        self.assertTrue(is_busy(INCIDENT + RUNNING))

    def test_each_live_marker_is_busy_with_an_old_quote_above(self):
        live = {"spinner": QUOTE + "✽ Forging… (1m 2s · ↓ 3.1k tokens)\n" + BOX + FOOTER_IDLE,
                "spinner hint": QUOTE + "✽ Forging… (esc to interrupt)\n  ⎿  Tip: x\n" + BOX + FOOTER_IDLE,
                "footer interrupt": QUOTE + BOX + "  esc to interrupt\n",
                "todo list under spinner": QUOTE + "✶ Running the tests… (2m 4s · ↓ 1k tokens)\n  ⎿  ☒ Read the card\n"
                                           "     ☐ Run the tests\n     ☐ Push\n\n" + BOX + FOOTER_IDLE,
                "wrapped tip under spinner": QUOTE + "· Thinking… (12s)\n  ⎿  Tip: a long tip that the pane wraps onto\n"
                                             "     a second line\n" + BOX + FOOTER_IDLE,
                "spinner without timer": QUOTE + "✳ Running the migration…\n" + BOX + FOOTER_IDLE + "  esc to interrupt\n",
                "footer background": QUOTE + BOX + "  esc to interrupt · ctrl+b to run in background\n"}
        for name, screen in live.items():
            with self.subTest(name=name):
                self.assertTrue(is_busy(screen))
                self.assertEqual(limit_notice(screen), "")

    def test_screens_without_the_box_are_read_whole(self):
        dialog = QUOTE + "Do you want to proceed?\n❯ 1. Yes\n  2. No\nEsc to cancel\n"
        codex = "• Working (3s • esc to interrupt)\n› Ask Codex to do anything\n  ? for shortcuts\n"
        for screen in (dialog, codex):
            with self.subTest(screen=screen[-40:]):
                self.assertTrue(is_busy(screen))


class ScriptedTmux:
    """External process boundary: a captured pane, no provider."""
    def __init__(self, screen):
        self.screen, self.calls = screen, []

    def run(self, *args, **kw):
        self.calls.append(args)
        out = self.screen if args[0] == "capture-pane" else ("0 \n" if args[0] == "list-panes" else "")
        return subprocess.CompletedProcess(args, 0, out, "")


class Clock:
    def __init__(self):
        self.now = 1000.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class DriverWaitsOnlyForALiveTurn(unittest.TestCase):
    def wait(self, screen):
        clock = Clock()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            d = Driver(harness="claude", agent="builder", state=root / "state", tmux=ScriptedTmux(screen),
                       max_sessions=10, cwd=root, session_id="", mode="resume", key="att-1", argv=["claude"],
                       out=io.StringIO(), env={"HOME": str(root)}, clock=clock.time, sleep=clock.sleep,
                       timeouts={"beat": 3600})
            d.private_dir()
            return d.wait_idle(600)

    def test_the_incident_screen_is_idle_for_the_driver(self):
        self.assertTrue(self.wait(INCIDENT))

    def test_a_running_turn_still_holds_the_prompt(self):
        self.assertFalse(self.wait(INCIDENT + RUNNING))

    def test_the_turn_pastes_the_prompt_into_the_incident_window(self):
        clock = Clock()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tmux, out = ScriptedTmux(INCIDENT), io.StringIO()
            d = Driver(harness="claude", agent="builder", state=root / "state", tmux=tmux, max_sessions=10,
                       cwd=root, session_id="", mode="resume", key="att-1", argv=["claude"], out=out,
                       env={"HOME": str(root)}, clock=clock.time, sleep=clock.sleep,
                       timeouts={"beat": 3600, "paste": 5})
            d.private_dir()
            d.ensure_session = lambda: None
            d.marks = {}
            d.run("Continue the card and report back.")
            self.assertTrue([c for c in tmux.calls if c[0] == "paste-buffer"], out.getvalue()[-400:])
            self.assertNotIn("stayed busy", out.getvalue())
            self.assertNotIn("usage limit reached", out.getvalue().lower())


if __name__ == "__main__":
    unittest.main()
