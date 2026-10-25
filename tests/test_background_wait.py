"""A prompt is held while the interactive session's background agents run.

While Claude Code runs background agents its prompt box is free, so a pasted prompt goes in
at once and only gets a "still waiting" turn; when an agent finishes, Claude Code resumes the
main thread by itself. The driver detects the agents from the rows Claude Code lists under the
prompt box and holds the prompt until they are gone and the session settled, at most
``background_wait_minutes``.
"""
import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_interactive as ti

from mesh_runner import interactive
from mesh_runner import tui_driver
from mesh_runner.config import ConfigError, load
from mesh_runner.adapters.claude import ClaudeAdapter
from mesh_runner.tui_driver import Driver, background_agents
from mesh_runner.watchdog import Signal, judge

RULE = "─" * 60

# the footer of a live session while one background agent runs (captured from Claude Code)
RUNNING = f"""
⏺ Two reviewers are running in the background; I will continue when they report.

{RULE}
❯
{RULE}
  ⏵⏵ bypass permissions on (shift+tab to cycle) · esc to interrupt · ↓ to manage
  ⏺ main
  ◯ reviewer-a  Running keylevel reconcile mutations                       8m 48
"""

# the same while the main thread itself is idle: no interrupt hint, the prompt box takes input
RUNNING_IDLE_MAIN = f"""
⏺ Two reviewers are running in the background; I will continue when they report.

{RULE}
❯ 
{RULE}
  ⏵⏵ bypass permissions on (shift+tab to cycle) · ↓ to manage
  ⏺ main
  ◯ reviewer-a  Running keylevel reconcile mutations                       8m 48
"""

# no agent listed: only the hint line under the prompt box
NO_ROWS = f"""
⏺ Done.

{RULE}
❯
{RULE}
  ⏵⏵ bypass permissions on (shift+tab to cycle)
"""

MAIN_ONLY = f"""
{RULE}
❯
{RULE}
  ⏵⏵ bypass permissions on (shift+tab to cycle) · ↓ to manage
  ⏺ main
"""

IDLE_ROWS = f"""
{RULE}
❯
{RULE}
  ⏵⏵ bypass permissions on (shift+tab to cycle) · ↓ to manage
  ⏺ main
  ◯ reviewer-a  Reviewed the change                                         idle
  ◯ reviewer-b  Waiting for a message                                    waiting
  ◯ 3 idle agents
"""

SEVERAL = f"""
{RULE}
>
{RULE}
  ⏵⏵ bypass permissions on (shift+tab to cycle) · ↓ to manage
  ◯ main                                                              ↑ 1 more
❯ ⏺ reviewer-a  Reading the diff                                     2m 3s · ↓ 1.2k tokens
  └─ ◯ helper  Running the tests                                           41s
  ● reviewer-b  Checking the migration                                      12s
  ↓ 2 more
"""

# the conversation above the prompt box may hold the same glyphs and words
TRANSCRIPT_ONLY = f"""
⏺ main
  ◯ reviewer-a  Running keylevel reconcile mutations                       8m 48
⏺ Bash(git checkout main)
{RULE}
❯
{RULE}
  ⏵⏵ bypass permissions on (shift+tab to cycle)
"""

CODEX_SCREENS = [
    """
• Running the test suite

  └ PYTHONPATH=src:tests python3 -m unittest

◦ Working (1m 02s • esc to interrupt)

› Ask Codex to do anything

  ? for shortcuts                                                        78% context left
""",
    f"""
─ Worked for 2m 10s {RULE}

• Background terminal finished: tests (main)

› Ask Codex to do anything
  ⏎ send   ⌃J newline   ⌃T transcript   ⌃C quit
""",
    f"""
{RULE}
• main
◦ reviewer  waiting for review
› Implement the check
""",
]


class Detector(unittest.TestCase):
    def test_a_running_background_agent_is_found_in_the_footer(self):
        self.assertEqual(background_agents(RUNNING), ["reviewer-a"])
        self.assertEqual(background_agents(RUNNING_IDLE_MAIN), ["reviewer-a"])

    def test_no_agent_rows_means_none(self):
        self.assertEqual(background_agents(NO_ROWS), [])
        self.assertEqual(background_agents(""), [])

    def test_the_main_row_alone_is_not_an_agent(self):
        self.assertEqual(background_agents(MAIN_ONLY), [])

    def test_idle_and_waiting_agents_do_not_count(self):
        self.assertEqual(background_agents(IDLE_ROWS), [])

    def test_selected_nested_and_viewed_rows_count_and_the_more_lines_do_not(self):
        self.assertEqual(background_agents(SEVERAL), ["reviewer-a", "helper", "reviewer-b"])

    def test_the_conversation_above_the_prompt_box_never_counts(self):
        self.assertEqual(background_agents(TRANSCRIPT_ONLY), [])

    def test_codex_screens_never_match(self):
        for screen in CODEX_SCREENS:
            self.assertEqual(background_agents(screen), [], screen)

    def test_a_codex_driver_never_reports_agents_even_on_such_a_screen(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Driver(harness="codex", agent="b", state=Path(tmp), tmux=interactive.Tmux("true"), max_sessions=1,
                       cwd=Path(tmp), session_id="", mode="resume", key="k", argv=["codex"], out=io.StringIO())
            d.screen = lambda: RUNNING
            self.assertEqual(d.background(), [])
            self.assertTrue(d.wait_background())                     # nothing to hold for
            self.assertFalse(d.progress["held"])


@unittest.skipUnless(ti.TMUX, "tmux not installed")
class HeldPrompt(unittest.TestCase):
    """The first turn runs normally; the second goes into the same live session while the
    screen shows a background agent for its first checks. The fake harness and private tmux
    server are those of test_interactive."""
    setUp, tearDown = ti.DriverTurns.setUp, ti.DriverTurns.tearDown
    driver, events, starts, phases = (ti.DriverTurns.driver, ti.DriverTurns.events, ti.DriverTurns.starts,
                                      ti.DriverTurns.phases)

    def resumed(self, polls, **timeouts):
        d, _ = self.driver("claude", sid="s-1")
        self.assertEqual(d.run("start the reviewers in the background"), 0)
        d2, out = self.driver("claude", sid="s-1", mode="resume")
        d2.t.update({"beat": 0, "background_poll": 0.05, "background_settle": 0.3, **timeouts})
        seen = {"screens": 0, "paste_at": None, "held_before_paste": []}
        real_screen, real_paste = d2.screen, d2.paste

        def screen():
            seen["screens"] += 1
            text = real_screen()
            return text + RUNNING_IDLE_MAIN if polls is None or seen["screens"] <= polls else text

        def paste(prompt):
            seen["paste_at"] = seen["screens"]
            real_paste(prompt)
        d2.screen, d2.paste = screen, paste
        reports = self.phases(d2)
        return d2, out, seen, reports

    def test_the_prompt_waits_until_the_background_agent_is_gone(self):
        d2, out, seen, reports = self.resumed(polls=30)
        self.assertEqual(d2.run("continue with the verdicts"), 0, out.getvalue()[-300:])
        self.assertGreater(seen["paste_at"], 30)                       # pasted only after the rows cleared
        held = [p for p in reports if p.get("held")]
        self.assertTrue(held)
        self.assertTrue(all(p["external_wait"] and p["phase"] == "ready" for p in held))
        # nothing was sent while held: every held report precedes the first "sent"
        first_sent = next(i for i, p in enumerate(reports) if p["phase"] == "sent")
        self.assertLess(max(i for i, p in enumerate(reports) if p.get("held")), first_sent)
        final = interactive.read_progress(self.state, "att-1")
        self.assertFalse(final["held"])
        self.assertGreater(final["released_at"], 0)
        self.assertTrue(all(p["phase"] in interactive.PHASES for p in reports))
        released = next(p for p in reports if p["released_at"])         # the release is progress
        self.assertEqual(released["phase"], "ready")
        self.assertGreaterEqual(released["progress_at"], released["released_at"])
        self.assertIn("continue with the verdicts", self.events(out)[-1]["result"])
        self.assertEqual(len(self.starts()), 1)                        # the same live session

    def test_after_the_limit_the_prompt_goes_in_anyway_with_one_log_line(self):
        d2, out, seen, reports = self.resumed(polls=None, background_wait=0.5)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(d2.run("continue anyway"), 0, out.getvalue()[-300:])
        self.assertIsNotNone(seen["paste_at"])
        lines = [x for x in err.getvalue().splitlines() if "goes in anyway" in x]
        self.assertEqual(len(lines), 1, err.getvalue())
        self.assertIn("continue anyway", self.events(out)[-1]["result"])
        self.assertTrue(any(p.get("held") for p in reports))

    def test_zero_keeps_the_old_behaviour(self):
        d2, out, seen, reports = self.resumed(polls=None, background_wait=0)
        self.assertEqual(d2.run("continue now"), 0, out.getvalue()[-300:])
        self.assertFalse(any(p.get("held") for p in reports))
        self.assertEqual(interactive.read_progress(self.state, "att-1")["released_at"], 0)


class Watchdog(unittest.TestCase):
    NOW = 100_000.0

    def test_first_output_is_not_judged_while_the_prompt_is_held(self):
        held = Signal("driver", "ready", self.NOW - 3000, True, self.NOW, held=True)
        self.assertEqual(judge(held, started=self.NOW - 3000, now=self.NOW, first_output_minutes=40,
                               idle_minutes=45), "")

    def test_first_output_counts_from_the_end_of_the_hold(self):
        released = Signal("driver", "ready", self.NOW - 60, False, self.NOW, released_at=self.NOW - 60)
        self.assertEqual(judge(released, started=self.NOW - 4000, now=self.NOW, first_output_minutes=40), "")
        late = Signal("driver", "sent", self.NOW - 60, False, self.NOW, released_at=self.NOW - 2500)
        why = judge(late, started=self.NOW - 4000, now=self.NOW, first_output_minutes=40)
        self.assertIn("no first output 41 min", why)
        self.assertIn("background agents", why)

    def test_a_busy_session_that_is_not_held_is_still_judged(self):
        busy = Signal("driver", "ready", self.NOW - 3000, True, self.NOW)
        self.assertIn("no first output", judge(busy, started=self.NOW - 3000, now=self.NOW, first_output_minutes=40))


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

    def test_default_and_values(self):
        self.assertEqual(self.load().background_wait_minutes, 45.0)
        self.assertEqual(self.load("background_wait_minutes = 0\n").background_wait_minutes, 0.0)
        self.assertEqual(self.load("background_wait_minutes = 12.5\n").background_wait_minutes, 12.5)
        for bad in ("background_wait_minutes = -1\n", 'background_wait_minutes = "45"\n',
                    "background_wait_minutes = true\n"):
            with self.assertRaises(ConfigError):
                self.load(bad)

    def test_the_value_reaches_the_driver(self):
        w = interactive.wrap(ClaudeAdapter(), "builder", "interactive", Path("/state"), "tmux", 20,
                             background_wait_minutes=12.5)
        argv = w.resume_argv("s-1", Path("/w"), [])
        head = argv[:argv.index("--")]
        self.assertEqual(head[head.index("--background-wait-minutes") + 1], "12.5")
        default = interactive.wrap(ClaudeAdapter(), "builder", "interactive", Path("/state"), "tmux", 20)
        argv = default.resume_argv("s-1", Path("/w"), [])
        self.assertEqual(argv[argv.index("--background-wait-minutes") + 1], "45")
        seen = {}

        def run(self, prompt):
            seen["wait"] = self.t["background_wait"]
            return 0
        with patch.object(tui_driver.Driver, "run", run), patch.object(sys, "stdin", io.StringIO("p")), \
                patch.object(tui_driver.signal, "signal"):
            tui_driver.main(["--harness", "claude", "--agent", "a", "--state", os.devnull, "--cwd", "/w",
                             "--key", "k", "--background-wait-minutes", "12.5", "--", "claude"])
            self.assertEqual(seen["wait"], 750.0)
            tui_driver.main(["--harness", "claude", "--agent", "a", "--state", os.devnull, "--cwd", "/w",
                             "--key", "k", "--", "claude"])
            self.assertEqual(seen["wait"], 45 * 60)


if __name__ == "__main__":
    unittest.main()
