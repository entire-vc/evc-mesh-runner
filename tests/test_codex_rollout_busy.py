"""Codex: a turn is busy by its rollout, not by the screen; the thread and the rollout are ours.

wait_idle used to read ``transcript_size()`` (0 for Codex, so "settled" at once) and the screen's
spinner, so a Codex task between tool calls got a prompt pasted into it. The rollout's open
task_started, the thread named by the rollout file (not by a title side task's notify) and a
fresh_rollout bound to our prompt close those three gaps.
"""
import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from mesh_runner import tui_driver
from mesh_runner.tui_driver import Driver

THREAD = "0199aaaa-bbbb-cccc-dddd-eeeeffff0001"
OTHER = "0199aaaa-bbbb-cccc-dddd-eeeeffff0002"


class Tmux:
    def __init__(self, screen=""):
        self.screen = screen

    def run(self, *args, **kw):
        return SimpleNamespace(returncode=0, stdout=self.screen, stderr="")


def rec(kind, **payload):
    return json.dumps({"type": kind, "payload": payload}) + "\n"


def started(turn="t-1"):
    return rec("event_msg", type="task_started", turn_id=turn)


def complete(msg="done"):
    return rec("event_msg", type="task_complete", last_agent_message=msg)


def aborted():
    # what Codex writes after Escape: no task_complete, only this developer message
    return rec("response_item", type="message", role="developer",
               content=[{"type": "input_text", "text": "<turn_aborted>\nThe previous turn was interrupted on purpose.\n</turn_aborted>"}])


def user(text):
    return rec("response_item", type="message", role="user", content=[{"type": "input_text", "text": text}])


def meta(cwd):
    return rec("session_meta", id=THREAD, cwd=str(cwd))


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def sleep(self, s):
        self.now += s


class RolloutCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cwd = self.root / "wt"
        self.cwd.mkdir()
        self.home = self.root / "home"
        self.day = self.home / "sessions" / "2026" / "10" / "26"
        self.day.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def driver(self, screen="", sid="", clock=None, key="k"):
        clock = clock or Clock()
        d = Driver(harness="codex", agent="builder", state=self.root / "state", tmux=Tmux(screen), max_sessions=5,
                   cwd=self.cwd, session_id=sid, mode="start", key=key, argv=["codex"], out=io.StringIO(),
                   env={"CODEX_HOME": str(self.home), "HOME": str(self.root)}, clock=clock, sleep=clock.sleep,
                   timeouts={"settle": 1, "poll": 0.1})
        d.dir.mkdir(parents=True, exist_ok=True)
        return d

    def rollout(self, name, *lines, mtime=None):
        path = self.day / f"rollout-2026-10-26T00-00-00-{name}.jsonl"
        path.write_text("".join(lines))
        return path


class CodexRollout(RolloutCase):
    # (1) a running task is busy although the screen shows nothing
    def test_open_task_in_the_rollout_is_not_idle(self):
        d = self.driver(sid=THREAD)
        d.rollout = str(self.rollout(THREAD, meta(self.cwd), user("go"), started()))
        self.assertFalse(d.wait_idle(5))

    def test_ended_task_in_the_rollout_is_idle(self):
        d = self.driver(sid=THREAD)
        d.rollout = str(self.rollout(THREAD, meta(self.cwd), user("go"), started(), complete()))
        self.assertTrue(d.wait_idle(5))

    def test_interrupted_task_without_a_task_complete_is_idle(self):
        d = self.driver(sid=THREAD)
        d.rollout = str(self.rollout(THREAD, meta(self.cwd), user("go"), started(), aborted()))
        self.assertTrue(d.wait_idle(5))

    def test_next_task_after_an_interrupt_is_open_again(self):
        d = self.driver(sid=THREAD)
        d.rollout = str(self.rollout(THREAD, meta(self.cwd), user("go"), started("t-1"), aborted(),
                                     user("next"), started("t-2")))
        self.assertFalse(d.wait_idle(5))

    def test_abort_event_msg_ends_the_task_too(self):
        d = self.driver(sid=THREAD)
        d.rollout = str(self.rollout(THREAD, meta(self.cwd), user("go"), started(), rec("event_msg", type="turn_aborted")))
        self.assertTrue(d.wait_idle(5))

    def test_first_idle_resample_is_short_for_a_chat_window_and_a_full_settle_for_a_card(self):
        for key, first in (("chat-abc", 0.3), ("k", 1)):
            clock = Clock()
            d = self.driver(sid=THREAD, clock=clock, key=key)
            d.rollout = str(self.rollout(THREAD, meta(self.cwd), user("go"), started(), complete()))
            naps = []
            d.sleep = lambda n, naps=naps, c=clock: (naps.append(n), c.sleep(n))
            self.assertTrue(d.wait_idle(5))
            self.assertEqual(naps[0], first)

    def test_paste_check_looks_after_a_short_nap_then_every_half_second(self):
        clock = Clock()
        d = self.driver(screen="nothing here", clock=clock)
        naps = []
        d.sleep = lambda n: (naps.append(n), clock.sleep(n))
        self.assertFalse(d.pasted("hello world, a prompt"))
        self.assertEqual(naps[:3], [0.15, 0.5, 0.5])
        d = self.driver(screen="[Pasted text #1]", clock=Clock())
        naps = []
        d.sleep = lambda n: naps.append(n)
        self.assertTrue(d.pasted("hello"))
        self.assertEqual(naps, [0.15])

    def test_a_growing_rollout_is_not_idle_even_with_no_open_task(self):
        d = self.driver(sid=THREAD)
        path = self.rollout(THREAD, meta(self.cwd), started(), complete())
        d.rollout = str(path)
        sizes = iter(range(100, 10000, 100))
        d.progress_size = lambda: next(sizes)
        self.assertFalse(d.wait_idle(5))

    def test_idle_finds_the_rollout_of_a_known_thread(self):
        d = self.driver(sid=THREAD)
        self.rollout(THREAD, meta(self.cwd), user("go"), started())
        self.assertFalse(d.wait_idle(5))             # the driver locates the file itself

    # (2) the thread is the rollout's, never a title task's
    def test_thread_comes_from_the_rollout_not_the_notify(self):
        d = self.driver()
        d.rollout = str(self.rollout(THREAD, meta(self.cwd)))
        d.meta = {}
        d.emit_codex({"event": {"type": "agent-turn-complete", "thread-id": "title-thread",
                                "last-assistant-message": "x"}, "totals": {}})
        self.assertEqual(d.meta["session_id"], THREAD)
        self.assertIn(THREAD, d.out.getvalue())
        self.assertNotIn("title-thread", d.out.getvalue())

    # (3) with several cards in one cwd the rollout is the one that opens with our prompt
    def test_fresh_rollout_picks_the_one_with_our_prompt(self):
        d = self.driver()
        d.prompt = "Task A: fix the lint"
        self.rollout(OTHER, meta(self.cwd), user("Task B: something else"), started())
        mine = self.rollout(THREAD, meta(self.cwd), user("<environment_context>x</environment_context>"),
                            user("Task A: fix the lint"), started())
        self.assertEqual(d.fresh_rollout(), str(mine))

    def test_fresh_rollout_never_takes_another_cards_session(self):
        d = self.driver()
        d.prompt = "Task A: fix the lint"
        self.rollout(OTHER, meta(self.cwd), user("Task B: something else"), started())
        self.assertEqual(d.fresh_rollout(), "")

    def test_fresh_rollout_never_takes_a_blank_session_once_the_prompt_is_known(self):
        d = self.driver()
        d.prompt = "Task A: fix the lint"
        self.rollout(THREAD, meta(self.cwd))             # maybe another card's, just created
        self.assertEqual(d.fresh_rollout(), "")

    def test_fresh_rollout_never_takes_a_foreign_child_thread_with_an_empty_prompt(self):
        d = self.driver()                                # pre-prompt: nothing to tell the cards apart by
        child = self.rollout(OTHER, rec("session_meta", id=OTHER, cwd=str(self.cwd),
                                        source={"subagent": {"thread_spawn": {"parent_thread_id": THREAD}}}),
                             started())
        self.assertEqual(d.fresh_rollout(), "")
        self.assertTrue(child.exists())

    def test_run_binds_the_prompt_before_the_first_rollout_discovery(self):
        d = self.driver()
        seen = []
        d.beat = lambda *a, **k: seen.append(d.prompt)
        d.ensure_session = lambda: (_ for _ in ()).throw(tui_driver.DriverError("stop", close=True))
        d.run("Task A: fix the lint")
        self.assertEqual(seen[0], "Task A: fix the lint")
        self.rollout(OTHER, meta(self.cwd))              # a foreign blank session, same cwd, new mtime
        self.assertEqual(d.fresh_rollout(), "")

    def test_open_task_is_found_behind_more_than_one_tail_block(self):
        d = self.driver(sid=THREAD)
        filler = rec("response_item", type="function_call_output", output="x" * 4000)
        d.rollout = str(self.rollout(THREAD, meta(self.cwd), started(), *[filler] * 60))
        self.assertGreater(Path(d.rollout).stat().st_size, 3 * tui_driver.ROLLOUT_TAIL)
        self.assertTrue(d.rollout_open())
        Path(d.rollout).write_text(Path(d.rollout).read_text() + complete())
        self.assertFalse(d.rollout_open())


PANE_PID = 4242
PROCESS_START = datetime(2026, 10, 26, 7, 16, 13, tzinfo=timezone.utc).timestamp()


class PaneTmux(Tmux):
    """capture-pane gives the screen, display-message gives the pane's pid."""

    def __init__(self, screen="", pid=str(PANE_PID)):
        super().__init__(screen)
        self.pid = pid

    def run(self, *args, **kw):
        if args and args[0] == "display-message":
            return SimpleNamespace(returncode=0 if self.pid else 1, stdout=self.pid + "\n", stderr="")
        return super().run(*args, **kw)


def stamped(kind, at, **payload):
    return json.dumps({"timestamp": at, "type": kind, "payload": payload}) + "\n"


class StaleOpenTask(RolloutCase):
    """A task_started left by a Codex process that died mid-turn is not a running turn."""

    def idle_driver(self, started_at, process_start=PROCESS_START, pid=str(PANE_PID)):
        d = self.driver(sid=THREAD)
        d.tmux = PaneTmux("\u203a ", pid)           # the idle prompt, no spinner
        d.rollout = str(self.rollout(THREAD, meta(self.cwd), user("go"),
                                     stamped("event_msg", started_at, type="task_started", turn_id="t-1")))
        stamp = f"v2:{process_start:.6f}" if process_start else ""
        patch = mock.patch.object(tui_driver, "precise_stamp", lambda pid: stamp if pid == PANE_PID else "")
        patch.start()
        self.addCleanup(patch.stop)
        return d

    # (1) opened before the pane's process started: dead, the idle screen settles at once
    def test_task_older_than_the_pane_process_does_not_keep_the_session_busy(self):
        d = self.idle_driver("2026-10-26T05:29:46.123Z")
        self.assertFalse(d.rollout_open())
        self.assertTrue(d.wait_idle(5))

    # (2) opened after the process started: a running turn, as before
    def test_task_newer_than_the_pane_process_is_still_busy(self):
        d = self.idle_driver("2026-10-26T07:20:00.000Z")
        self.assertTrue(d.rollout_open())
        self.assertFalse(d.wait_idle(5))

    def test_task_within_the_slack_of_the_process_start_is_still_busy(self):
        d = self.idle_driver("2026-10-26T07:16:10.000Z")          # 3 s before: a wrapper started first
        self.assertTrue(d.rollout_open())

    # (3) the process start cannot be read: fail-safe, the open task counts
    def test_unknown_process_start_keeps_the_open_task_busy(self):
        for kw in ({"process_start": 0}, {"pid": ""}, {"pid": "not-a-pid"}):
            d = self.idle_driver("2026-10-26T05:29:46.123Z", **kw)
            self.assertTrue(d.rollout_open(), kw)
            self.assertFalse(d.wait_idle(5), kw)

    def test_open_task_without_a_timestamp_is_still_busy(self):
        d = self.idle_driver("2026-10-26T05:29:46.123Z")
        Path(d.rollout).write_text(Path(d.rollout).read_text().replace('"timestamp": "2026-10-26T05:29:46.123Z", ', ""))
        self.assertTrue(d.rollout_open())

    def test_a_spinner_on_the_screen_still_means_busy_for_a_stale_task(self):
        d = self.idle_driver("2026-10-26T05:29:46.123Z")
        d.tmux = PaneTmux("Working (12s \u2022 esc to interrupt)")
        self.assertFalse(d.wait_idle(5))


if __name__ == "__main__":
    unittest.main()
