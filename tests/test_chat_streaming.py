"""A chat reply reaches Mesh while the turn runs, block by block, and a turn without text is an error.

The driver writes Claude's transcript records out as they land (``stream_claude``) and the chat
delivery reads the driver's output as it is written. Codex's final message comes at the end only;
a Codex turn that ended without one (an API error) must not reach the user as an empty answer.
"""
import io
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

from mesh_runner.chat_delivery import DriverDelivery, translate
from mesh_runner.tui_driver import Driver

PROMPT = "Name the visual code in the picture."


def rec(kind, uid, content=None, **extra):
    body = {"type": kind, "uuid": uid, "sessionId": "s-1", **extra}
    if kind == "user":
        body["message"] = {"role": "user", "content": content}
    else:
        body["message"] = {"role": "assistant", "model": "claude-test", "id": "msg-" + uid, "content": content,
                           "usage": {"output_tokens": 1}}
    return json.dumps(body) + "\n"


def text(t):
    return [{"type": "text", "text": t}]


class ClaudeStreaming(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.transcript = self.root / "t.jsonl"
        self.out = io.StringIO()
        self.d = Driver(harness="claude", agent="builder", state=self.root / "state", tmux=object(), max_sessions=1,
                        cwd=self.root, session_id="s-1", mode="resume", key="chat-abc", argv=["claude"],
                        out=self.out, env={})
        self.d.prompt = PROMPT
        self.d.meta = {"transcript": str(self.transcript)}

    def write(self, *lines):
        with open(self.transcript, "a") as fh:
            fh.write("".join(lines))

    def lines(self):
        return [json.loads(x) for x in self.out.getvalue().splitlines() if x.strip()]

    def test_blocks_go_out_as_written_and_the_end_does_not_repeat_them(self):
        self.write(rec("user", "u0", "an earlier question"), rec("assistant", "a0", text("old answer")))
        self.d.turn_offset = self.transcript.stat().st_size
        self.write(rec("user", "u1", PROMPT))
        self.d.stream_claude()
        self.assertEqual(self.lines(), [], "nothing before the first assistant record (init names its model)")

        cut = rec("assistant", "a1b", text("Reading it now."))
        self.write(rec("assistant", "a1", text("Let me look at the picture.")), cut[:30])
        self.d.stream_claude()
        first = self.lines()
        self.assertEqual([x["type"] for x in first], ["system", "user", "assistant"])
        self.assertEqual((first[0]["session_id"], first[0]["model"]), ("s-1", "claude-test"))

        # the cut line is completed later; the turn goes on with a tool and the answer
        self.write(cut[30:], rec("assistant", "a2", [{"type": "tool_use", "id": "tu", "name": "Read",
                                                             "input": {"file_path": "/p.png"}}]),
                   rec("user", "u2", [{"type": "tool_result", "tool_use_id": "tu", "content": "img"}]))
        self.d.stream_claude()
        self.write(rec("assistant", "a3", text("The code is QX-7341.")))
        self.assertTrue(self.d.emit_claude({"event": {"hook_event_name": "Stop", "session_id": "s-1",
                                                      "transcript_path": str(self.transcript)},
                                            "offset": self.d.turn_offset}))
        out = self.lines()
        self.assertEqual(sum(1 for x in out if x.get("subtype") == "init"), 1)
        said = [b.get("text") or b.get("name") for x in out if x["type"] == "assistant"
                for b in x["message"]["content"]]
        self.assertEqual(said, ["Let me look at the picture.", "Reading it now.", "Read", "The code is QX-7341."])
        self.assertNotIn("old answer", self.out.getvalue())
        self.assertEqual(out[-1]["type"], "result")
        deltas = [e["text"] for e in translate(out, "claude") if e["type"] == "text_delta"]
        self.assertEqual(deltas, ["Let me look at the picture.", "Reading it now.", "The code is QX-7341."])
        self.assertEqual([e["type"] for e in translate(out, "claude")].count("session"), 1)

    def test_cut_off_turn_keeps_what_was_streamed_and_sends_no_second_init(self):
        self.write(rec("user", "u1", PROMPT), rec("assistant", "a1", text("1, 2, 3")))
        self.d.stream_claude()
        self.d.emit_start()
        out = self.lines()
        self.assertEqual([x["type"] for x in out], ["system", "user", "assistant"])


class CodexEmptyTurn(unittest.TestCase):
    def test_codex_turn_without_a_final_message_names_why(self):
        root = Path(tempfile.mkdtemp())
        out = io.StringIO()
        d = Driver(harness="codex", agent="builder", state=root / "state", tmux=object(), max_sessions=1, cwd=root,
                   session_id="th-1", mode="resume", key="chat-abc", argv=["codex"], out=out, env={"HOME": str(root)})
        why = "no model output; other: The 'o3' model is not supported"
        d.emit_codex({"event": {"type": "agent-turn-complete", "thread-id": "th-1",
                                "last-assistant-message": "", "no-reply": why}, "totals": {}})
        lines = [json.loads(x) for x in out.getvalue().splitlines()]
        self.assertEqual(lines[-1]["no_reply"], why)
        evs = list(translate(lines, "codex"))
        self.assertEqual(evs[-1]["type"], "error")
        self.assertEqual(evs[-1]["code"], "empty_reply")
        self.assertIn("'o3' model is not supported", evs[-1]["message"])
        self.assertNotIn("final", [e["type"] for e in evs])

    def test_claude_success_without_text_is_an_empty_reply_too(self):
        evs = list(translate([{"type": "system", "subtype": "init", "session_id": "s"},
                              {"type": "result", "is_error": False, "usage": {}}], "claude"))
        self.assertEqual(evs[-1]["code"], "empty_reply")


def roll(kind, payload):
    return json.dumps({"timestamp": "t", "type": kind, "payload": payload}) + "\n"


class CodexToolCalls(unittest.TestCase):
    """A Codex turn's tool calls reach the chat as tool_status, live, with the tool's name only."""

    SECRET = "printf '%s' tok-123 | sha256sum"

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.rollout = self.root / "rollout-2026-10-26T07-46-47-01a17688-500c-72d2-a1c4-781a6119feed.jsonl"
        self.rollout.write_text("")
        self.out = io.StringIO()
        self.d = Driver(harness="codex", agent="builder", state=self.root / "state", tmux=object(), max_sessions=1,
                        cwd=self.root, session_id="th-1", mode="resume", key="chat-abc", argv=["codex"], out=self.out,
                        env={"HOME": str(self.root)})
        self.d.rollout, self.d.sent_size, self.d.scan_from, self.d.prompt_taken = str(self.rollout), 0, 0, True

    def write(self, *lines):
        with open(self.rollout, "a") as fh:
            fh.write("".join(lines))

    def call(self, kind="custom_tool_call", **extra):
        return roll("response_item", {"type": kind, "status": "completed", "call_id": "call_1", "name": "exec",
                                      "input": self.SECRET, **extra})

    def lines(self):
        return [json.loads(x) for x in self.out.getvalue().splitlines()]

    def test_tool_call_goes_out_while_the_turn_runs(self):
        self.write(roll("event_msg", {"type": "task_started", "turn_id": "turn-1"}), self.call(),
                   roll("response_item", {"type": "custom_tool_call_output", "call_id": "call_1", "output": "abc"}))
        self.assertTrue(self.d.reply_started(self.rollout.stat().st_size))
        self.assertEqual(self.lines(), [{"type": "item.completed",
                                         "item": {"type": "tool_call", "id": "call_1", "name": "exec"}}])
        self.assertNotIn("tok-123", self.out.getvalue())
        evs = list(translate(self.lines(), "codex"))
        self.assertEqual([(e["type"], e["status"], e["label"]) for e in evs[:2]],
                         [("tool_status", "started", "Using exec"), ("tool_status", "finished", "Using exec")])
        self.assertEqual(evs[0]["tool_call_id"], "call_1")

    def test_calls_the_watch_had_not_read_yet_go_out_before_the_reply(self):
        self.write(roll("event_msg", {"type": "task_started", "turn_id": "turn-1"}), self.call(),
                   roll("response_item", {"type": "local_shell_call", "call_id": "call_2", "action": {"command": ["ls"]}}),
                   roll("response_item", {"type": "message", "role": "assistant", "content": []}),
                   roll("event_msg", {"type": "task_complete", "last_agent_message": "## Result"}),
                   self.call(call_id="call_next"))                # the next turn's: never ours
        self.d.emit_codex({"event": {"type": "agent-turn-complete", "thread-id": "th-1",
                                     "last-assistant-message": "## Result"}, "totals": {}})
        out = self.lines()
        self.assertEqual([x["type"] for x in out], ["item.completed", "item.completed", "thread.started",
                                                     "item.completed", "turn.completed"])
        evs = list(translate(out, "codex"))
        tools = [(e["tool_call_id"], e["label"]) for e in evs if e["type"] == "tool_status" and e["status"] == "started"]
        self.assertEqual(tools, [("call_1", "Using exec"), ("call_2", "Using shell")])
        self.assertEqual(evs[-1]["type"], "final")


class LiveDelivery(unittest.TestCase):
    INIT = json.dumps({"type": "system", "subtype": "init", "session_id": "s"})
    TEXT = json.dumps({"type": "assistant", "message": {"content": text("partial")}})
    DONE = json.dumps({"type": "result", "is_error": False, "usage": {}})

    def delivery(self, script):
        tmp = Path(tempfile.mkdtemp())
        self.gate = tmp / "gate"
        script = script.replace("GATE", repr(str(self.gate)))
        return DriverDelivery("claude", lambda t: ([sys.executable, "-c", script], tmp, {}, "prompt"),
                              tmp / "logs", poll=0.02, term_wait=2)

    def test_a_block_written_mid_turn_is_delivered_before_the_turn_ends(self):
        # the driver holds its result until it sees the gate (5 s at most); the test opens the gate
        # only once the first block has been delivered, so a reader that waits for the end is late
        script = ("import os,sys,time\nsys.stdin.read()\n"
                  f"print({self.INIT!r},flush=True)\nprint({self.TEXT!r},flush=True)\n"
                  "t=time.time()\nwhile not os.path.exists(GATE) and time.time()-t<5: time.sleep(0.02)\n"
                  f"print({self.DONE!r},flush=True)")
        d = self.delivery(script)
        start, got = time.monotonic(), []
        for ev in d.run({"turn_id": "turn-12345678"}, lambda: False):
            got.append((ev["type"], time.monotonic() - start))
            if ev["type"] == "text_delta":
                self.gate.touch()
        kinds = [k for k, _ in got]
        self.assertEqual(kinds, ["session", "text_delta", "usage", "final"])
        self.assertLess(dict(got)["text_delta"], 3.0, "the block waited for the end of the turn")
        self.assertLess(got[-1][1], 4.0)

    def test_stop_keeps_the_streamed_text_and_ends_cancelled(self):
        script = ("import signal,sys,time\nsignal.signal(signal.SIGTERM,lambda *a:sys.exit(0))\nsys.stdin.read()\n"
                  f"print({self.INIT!r},flush=True)\nprint({self.TEXT!r},flush=True)\ntime.sleep(60)")
        seen = []

        def cancelled():
            return "text_delta" in seen
        start = time.monotonic()
        for ev in self.delivery(script).run({"turn_id": "turn-12345678"}, cancelled):
            seen.append(ev["type"])
            if ev["type"] == "final":
                self.assertEqual(ev["stop_reason"], "cancelled")
        self.assertEqual(seen, ["session", "text_delta", "final"])
        self.assertLess(time.monotonic() - start, 10)


if __name__ == "__main__":
    unittest.main()
