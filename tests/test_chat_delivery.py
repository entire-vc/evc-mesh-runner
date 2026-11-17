import json
import sys
import tempfile
import unittest
from pathlib import Path

from mesh_runner.chat_delivery import DriverDelivery, translate, window_key

CLAUDE = [{"type": "system", "subtype": "init", "session_id": "s1"},
          {"type": "assistant", "message": {"content": [{"type": "text", "text": "Hi"},
                                                         {"type": "tool_use", "id": "a", "name": "Read",
                                                          "input": {"file_path": "/secret"}}]}},
          {"type": "result", "is_error": False, "usage": {"input_tokens": 3, "cache_read_input_tokens": 7,
                                                          "output_tokens": 2}}]


class Translate(unittest.TestCase):
    def test_claude_turn(self):
        evs = list(translate(CLAUDE, "claude"))
        self.assertEqual([e["type"] for e in evs],
                         ["session", "text_delta", "tool_status", "tool_status", "usage", "final"])
        self.assertEqual(evs[4]["tokens_in"], 10)
        self.assertNotIn("secret", json.dumps(evs))          # tool input never leaves

    def test_codex_turn_and_failure(self):
        ok = [{"type": "thread.started", "thread_id": "t"}, {"type": "item.completed", "item": {
            "type": "agent_message", "text": "yo"}}, {"type": "turn.completed", "usage": {"output_tokens": 4}}]
        self.assertEqual(list(translate(ok, "codex"))[-1]["type"], "final")
        bad = list(translate([{"type": "turn.failed", "error": {"message": "limit"}}], "codex"))
        self.assertEqual(bad[-1]["code"], "harness_error")

    def test_no_result_is_an_error(self):
        self.assertEqual(list(translate([], "claude"))[-1]["code"], "no_result")

    def test_error_result(self):
        evs = list(translate([{"type": "result", "is_error": True, "result": "boom", "usage": {}}], "claude"))
        self.assertEqual(evs[-1]["type"], "error")

    def test_window_key_is_per_chat(self):
        self.assertNotEqual(window_key("aaaa-1"), window_key("bbbb-2"))
        self.assertTrue(window_key("x/../y").startswith("chat-"))


class Driver(unittest.TestCase):
    def delivery(self, script):
        tmp = Path(tempfile.mkdtemp())
        return DriverDelivery("claude", lambda t: ([sys.executable, "-c", script], tmp, {}, "prompt"),
                              tmp / "logs", poll=0.02, term_wait=2)

    def test_runs_process_and_translates(self):
        script = "import sys,json\nsys.stdin.read()\n" + "\n".join(
            f"print({json.dumps(json.dumps(e))})" for e in CLAUDE)
        evs = list(self.delivery(script).run({"turn_id": "turn-12345678"}, lambda: False))
        self.assertEqual(evs[-1], {"type": "final", "stop_reason": "end_turn"})

    def test_failed_turn_logs_its_reason(self):
        failed = {"type": "turn.failed", "error": {"message": "codex exited during start-up 3 times: Error: bootstrap"}}
        d = self.delivery("import sys\nsys.stdin.read()\nprint(%r)" % json.dumps(failed))
        d.harness = "codex"
        with self.assertLogs("mesh_runner.chat_delivery", level="WARNING") as cm:
            evs = list(d.run({"turn_id": "turn-12345678"}, lambda: False))
        self.assertEqual(evs[-1]["code"], "harness_error")
        self.assertIn("exited during start-up 3 times: Error: bootstrap", cm.output[0])

    def test_failed_turn_log_does_not_quote_other_driver_errors(self):
        failed = {"type": "turn.failed", "error": {"message": "prompt not taken; screen: SECRET-SCREEN"}}
        d = self.delivery("import sys\nsys.stdin.read()\nprint(%r)" % json.dumps(failed))
        with self.assertLogs("mesh_runner.chat_delivery", level="WARNING") as cm:
            list(d.run({"turn_id": "turn-12345678"}, lambda: False))
        self.assertNotIn("SECRET-SCREEN", cm.output[0])

    def test_expired_login_has_a_safe_actionable_reason(self):
        for message, expected in [
            ("Login expired · Please run /login", "authentication_failed: Login expired; run /login"),
            ("Login expired · Please run /login SECRET-SCREEN", "kind=unclassified"),
        ]:
            with self.subTest(message=message):
                failed = {"type": "result", "is_error": True, "result": message}
                d = self.delivery("import sys\nsys.stdin.read()\nprint(%r)" % json.dumps(failed))
                with self.assertLogs("mesh_runner.chat_delivery", level="WARNING") as cm:
                    events = list(d.run({"turn_id": "turn-12345678"}, lambda: False))
                self.assertEqual(events[-1]["code"], "harness_error")
                self.assertIn(expected, cm.output[0])
                self.assertNotIn("SECRET-SCREEN", cm.output[0])

    def test_failed_turn_log_names_the_driver_error_kind(self):
        failed = {"type": "turn.failed", "error": {"message": "interactive session exited at start: SECRET-SCREEN"}}
        d = self.delivery("import sys\nsys.stdin.read()\nprint(%r)" % json.dumps(failed))
        with self.assertLogs("mesh_runner.chat_delivery", level="WARNING") as cm:
            list(d.run({"turn_id": "turn-12345678"}, lambda: False))
        self.assertIn("kind=interactive session exited at start", cm.output[0])
        self.assertRegex(cm.output[0], r"sha256=[0-9a-f]{12}")
        self.assertNotIn("SECRET-SCREEN", cm.output[0])

    def test_failed_turn_log_classifies_a_start_up_stall(self):
        failed = {"type": "turn.failed", "error": {"message": "codex start-up stalled for 95s, the prompt was not taken: SECRET-SCREEN"}}
        d = self.delivery("import sys\nsys.stdin.read()\nprint(%r)" % json.dumps(failed))
        with self.assertLogs("mesh_runner.chat_delivery", level="WARNING") as cm:
            list(d.run({"turn_id": "turn-12345678"}, lambda: False))
        self.assertIn("kind=codex start-up stalled", cm.output[0])
        self.assertNotIn("SECRET-SCREEN", cm.output[0])

    def test_answer_text_does_not_stay_on_disk_after_the_turn(self):
        d = self.delivery("import sys\nsys.stdin.read()\nprint('secret answer')")
        list(d.run({"turn_id": "turn-12345678"}, lambda: False))
        self.assertEqual(list(d.log_dir.rglob("*.*")), [])

    def test_cancel_terminates_driver_and_finishes_cancelled(self):
        script = ("import signal,sys,time\nsignal.signal(signal.SIGTERM,lambda *a:sys.exit(0))\n"
                  "print('{\"type\":\"system\",\"subtype\":\"init\",\"session_id\":\"s\"}',flush=True)\ntime.sleep(60)")
        flag = {"n": 0}

        def cancelled():
            flag["n"] += 1
            return flag["n"] > 5
        evs = list(self.delivery(script).run({"turn_id": "turn-12345678"}, cancelled))
        self.assertEqual(evs[-1], {"type": "final", "stop_reason": "cancelled"})


# What tui_driver writes when its window start found the host cap full for its whole wait (start_window): the
# host-wait record, then its ordinary error result. Nothing was started or pasted.
CAP_TIMEOUT = [{"type": "mesh_runner.host_wait", "state": "waiting", "reason": "cap"},
               {"type": "mesh_runner.host_wait", "state": "timeout", "reason": "cap"},
               {"type": "system", "subtype": "init", "session_id": "s1", "model": "", "launch_mode": "interactive"},
               {"type": "result", "subtype": "error_during_execution", "is_error": True,
                "result": "host session cap reached (22 live sessions, all busy)", "session_id": "s1", "usage": {}}]


def busy_then_turn(root: Path, busy_runs: int) -> str:
    """A driver whose first ``busy_runs`` runs find no room on the host; the next one runs the turn. Every run
    is counted in ``runs`` once it has written all it writes; a run that reaches the session records the
    prompt in ``delivered``."""
    lines = "\n".join(f"    print({json.dumps(json.dumps(e))}, flush=True)" for e in CAP_TIMEOUT)
    turn = "\n".join(f"    print({json.dumps(json.dumps(e))}, flush=True)" for e in CLAUDE)
    return (f"import os, sys\nprompt = sys.stdin.read()\nruns = {str(root / 'runs')!r}\n"
            f"n = len(open(runs).read()) if os.path.exists(runs) else 0\n"
            f"if n < {busy_runs}:\n{lines}\n"
            f"else:\n    open({str(root / 'delivered')!r}, 'a').write(prompt + '\\n')\n{turn}\n"
            f"open(runs, 'a').write('x')\n")


class HostCap(unittest.TestCase):
    """a chat turn whose window found the host cap full is not a failed turn: nothing reached the
    session, so it waits for a slot (chat) or comes back unstarted (answer desk), and runs exactly once."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def delivery(self, script, **kw):
        return DriverDelivery("claude", lambda t: ([sys.executable, "-c", script], self.root, {}, "the question"),
                              self.root / "logs", poll=0.02, term_wait=2, **kw)

    def runs(self):
        return len((self.root / "runs").read_text()) if (self.root / "runs").exists() else 0

    def delivered(self):
        p = self.root / "delivered"
        return p.read_text().splitlines() if p.exists() else []

    def test_a_chat_turn_waits_out_a_full_host_and_runs_once_when_a_slot_frees(self):
        turn = {"turn_id": "turn-12345678"}
        with self.assertLogs("mesh_runner.chat_delivery", level="INFO") as cm:
            evs = list(self.delivery(busy_then_turn(self.root, 2)).run(turn, lambda: False))
        self.assertEqual(evs[-1], {"type": "final", "stop_reason": "end_turn"})
        self.assertNotIn("error", [e["type"] for e in evs])            # not a terminal failure
        self.assertEqual([e["type"] for e in evs].count("session"), 1)  # the unstarted runs say nothing
        self.assertEqual(self.runs(), 3)
        self.assertEqual(self.delivered(), ["the question"])            # exactly one turn reached the session
        waiting = [m for m in cm.output if "waits for a free slot" in m]
        self.assertEqual(len(waiting), 1)                               # one line per waiting turn
        self.assertIn("host session cap reached", waiting[0])
        self.assertEqual(list(self.root.joinpath("logs").rglob("*.*")), [])

    def test_without_wait_for_room_the_turn_comes_back_unstarted(self):
        evs = list(self.delivery(busy_then_turn(self.root, 1), wait_for_room=False).run(
            {"turn_id": "turn-12345678"}, lambda: False))
        self.assertEqual([e["type"] for e in evs], ["error"])
        self.assertEqual((evs[0]["code"], evs[0]["retryable"]), ("host_busy", True))
        self.assertEqual((self.runs(), self.delivered()), (1, []))

    def test_stop_while_waiting_for_room_ends_cancelled_with_no_session(self):
        script = ("import signal,sys,time\nsignal.signal(signal.SIGTERM,lambda *a:sys.exit(0))\nsys.stdin.read()\n"
                  "print('{\"type\":\"mesh_runner.host_wait\",\"state\":\"waiting\",\"reason\":\"cap\"}',flush=True)\n"
                  "time.sleep(60)")
        flag = {"n": 0}

        def cancelled():
            flag["n"] += 1
            return flag["n"] > 5
        turn = {"turn_id": "turn-12345678"}
        evs = list(self.delivery(script).run(turn, cancelled))
        self.assertEqual(evs, [{"type": "final", "stop_reason": "cancelled"}])
        self.assertEqual(turn["_host_wait"], "cancelled")

    def test_stop_after_a_run_that_found_no_room_ends_cancelled_with_no_session(self):
        turn = {"turn_id": "turn-12345678"}
        evs = list(self.delivery(busy_then_turn(self.root, 5)).run(turn, lambda: self.runs() >= 1))
        self.assertEqual(evs, [{"type": "final", "stop_reason": "cancelled"}])
        self.assertEqual(turn["_host_wait"], "cancelled")
        self.assertEqual((self.runs(), self.delivered()), (1, []))

    def test_a_turn_that_got_room_after_waiting_is_an_ordinary_turn(self):
        script = "import sys\nsys.stdin.read()\n" + "\n".join(f"print({json.dumps(json.dumps(e))})" for e in (
            [{"type": "mesh_runner.host_wait", "state": "waiting", "reason": "cap"},
             {"type": "mesh_runner.host_wait", "state": "room"}] + CLAUDE))
        turn = {"turn_id": "turn-12345678"}
        evs = list(self.delivery(script).run(turn, lambda: False))
        self.assertEqual(evs[-1], {"type": "final", "stop_reason": "end_turn"})
        self.assertEqual(turn["_host_wait"], "room")


if __name__ == "__main__":
    unittest.main()
