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


if __name__ == "__main__":
    unittest.main()
