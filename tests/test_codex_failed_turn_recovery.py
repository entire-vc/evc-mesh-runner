"""A Codex turn the provider refused leaves no notify: the bound rollout proves the send (2026-11-10).

Real files and a real tmux binary on a private socket cover gone and live windows without internal mocks.
"""
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

from mesh_runner import delivery_recovery, interactive
from mesh_runner.adapters.base import read_events

PROMPT = "Task #0f1e2d3c: continue the card.\nUse only these physical paths: [\"/w/slot-1\"]."
REFUSED = {"message": '{"type":"error","status":400,"error":{"type":"invalid_request_error",'
                      '"message":"The \'gpt-6.1-sol\' model requires a newer version of Codex."}}', "codex_error_info": "other"}


def rec(kind, payload):
    return {"timestamp": "2026-11-10T22:29:40.000Z", "type": kind, "payload": payload}


@unittest.skipUnless(shutil.which("tmux"), "tmux not installed")
class CodexFailedTurn(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.root = Path(self._t.name)
        self.dir, self.key = self.root / "state", str(uuid.uuid4())
        self.cwd = self.root / "work"
        self.cwd.mkdir()
        self.sid = str(uuid.uuid4())
        day = self.root / "codex-home" / "sessions" / "2026" / "11" / "11"
        day.mkdir(parents=True)
        self.rollout = day / f"rollout-2026-11-11T01-26-38-{self.sid}.jsonl"
        self.log = self.root / "turn2.codex.jsonl"
        self.tmux = interactive.Tmux(["tmux", "-L", "mr-test-" + uuid.uuid4().hex[:12]])
        self.token = uuid.uuid4().hex
        self.sha = hashlib.sha256(PROMPT.encode()).hexdigest()
        self.intent = {"version": 1, "kind": "interactive", "harness": "codex", "token": self.token,
                       "sha256": self.sha, "prompt": PROMPT, "log_path": str(self.log)}

    def build(self, after, notify=()):
        """The earlier turn, the journal's cursor, then `after` (our turn); `notify` after the base."""
        self.log.write_text("")
        before = [rec("session_meta", {"id": self.sid, "cwd": str(self.cwd), "cli_version": "0.159.3"}),
                  rec("event_msg", {"type": "task_complete", "turn_id": "t0", "last_agent_message": "old"})]
        head = b"".join(json.dumps(x).encode() + b"\n" for x in before)
        self.rollout.write_bytes(head + b"".join(json.dumps(x).encode() + b"\n" for x in after))
        earlier = {"type": "agent-turn-complete", "thread-id": self.sid, "turn-id": "t0",
                   "input-messages": ["earlier"], "last-assistant-message": "old"}
        events = [earlier, *notify]
        (self.dir).mkdir(parents=True, exist_ok=True)
        (self.dir / f"{self.key}.events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
        interactive.write_delivery(self.dir, self.key, {
            "version": 1, "token": self.token, "sha256": self.sha, "phase": "send-intent", "harness": "codex",
            "session_id": self.sid, "cwd": str(self.cwd), "codex_home": "", "native_base": 1,
            "native_prefix": hashlib.sha256(json.dumps([earlier], sort_keys=True).encode()).hexdigest(),
            "offset": 0, "meta": {}, "rollout": str(self.rollout), "rollout_cursor": len(head),
            "rollout_prefix": hashlib.sha256(head).hexdigest(), "totals": {}})

    def turn(self, prompt=PROMPT, error=REFUSED, message=None, extra=()):
        return [rec("event_msg", {"type": "task_started", "turn_id": "t1"}),
                rec("turn_context", {}),
                rec("response_item", {"type": "message", "role": "user",
                                      "content": [{"type": "input_text", "text": prompt}]}),
                *extra,
                rec("event_msg", {"type": "task_complete", "turn_id": "t1", "last_agent_message": message,
                                  "error": error})]

    def recover(self):
        return delivery_recovery.recover(self.dir, self.key, self.intent, "builder", self.tmux)

    def acks(self):
        return [e for e in read_events(self.log) if e.get("type") == "mesh_runner.prompt_ack"]

    def test_refused_turn_with_exact_prompt_is_a_delivered_turn_without_output(self):
        self.build(self.turn())
        self.assertEqual(self.recover(), "completed")
        events = read_events(self.log)
        self.assertEqual([(a["token"], a["sha256"], a["recovered_from"]) for a in self.acks()],
                         [(self.token, self.sha, "codex.session-rollout-failed-turn")])
        done = [e for e in events if e.get("type") == "turn.completed"]
        self.assertEqual(len(done), 1)
        self.assertIn("no model output", done[0]["no_reply"])
        self.assertIn("newer version of Codex", done[0]["no_reply"])
        print("CODEX_FAILED_TURN_RECEIPT: no notify + bound rollout (verified cursor, one task, exact prompt, "
              "typed error, no model output) -> completed with prompt_ack; turn has no final message", flush=True)

    def test_anything_short_of_that_record_stays_unproven(self):
        other = rec("response_item", {"type": "message", "role": "user",
                                      "content": [{"type": "input_text", "text": "another"}]})
        cases = {
            "no task end": self.turn()[:-1],
            "success without notify": self.turn(error=None, message="done"),
            "another prompt": self.turn(prompt="another prompt"),
            "two prompts": self.turn(extra=[other]),
            "second non-text user message": self.turn(extra=[rec("response_item", {
                "type": "message", "role": "user", "content": [{"type": "input_image"}]})]),
            "model output": self.turn(extra=[rec("response_item", {"type": "reasoning"})]),
            "aborted": self.turn(extra=[rec("event_msg", {"type": "turn_aborted"})]),
            "second task": self.turn() + [rec("event_msg", {"type": "task_started", "turn_id": "t2"})],
            "other turn's end": self.turn()[:-1] + [rec("event_msg", {
                "type": "task_complete", "turn_id": "t9", "last_agent_message": None, "error": REFUSED})],
            "completion without turn id": self.turn()[:-1] + [rec("event_msg", {
                "type": "task_complete", "last_agent_message": None, "error": REFUSED})],
        }
        for name, error in {"bare string": "refused", "no error info": {"message": "refused"},
                            "empty info": {"message": "refused", "codex_error_info": ""},
                            "extra field": {**REFUSED, "status": 400}}.items():
            cases["untyped error: " + name] = self.turn(error=error)
        for name, after in cases.items():
            with self.subTest(name):
                self.build(after)
                self.assertEqual(self.recover(), "codex-acceptance-unproven")
                self.assertEqual(self.acks(), [])

    def test_a_moved_cursor_or_a_present_foreign_rollout_proves_nothing(self):
        self.build(self.turn())
        journal = interactive.read_delivery(self.dir, self.key)
        interactive.write_delivery(self.dir, self.key, {**journal, "rollout_prefix": "0" * 64})
        self.assertEqual(self.recover(), "codex-acceptance-unproven")
        interactive.write_delivery(self.dir, self.key, {**journal, "rollout_cursor": journal["rollout_cursor"] + 1})
        self.assertEqual(self.recover(), "codex-acceptance-unproven")
        foreign = self.rollout.with_name(f"rollout-2026-11-11T01-26-38-{uuid.uuid4()}.jsonl")
        # Keep the verified prefix and completion intact so only the thread in the path differs.
        foreign.write_bytes(self.rollout.read_bytes())
        interactive.write_delivery(self.dir, self.key, {**journal, "rollout": str(foreign)})
        self.assertEqual(self.recover(), "codex-acceptance-unproven")
        self.assertEqual(self.acks(), [])

    def test_a_user_message_with_extra_or_malformed_content_stays_unproven(self):
        text = {"type": "input_text", "text": PROMPT}
        cases = [
            [text, {"type": "input_image", "image_url": "https://example.invalid/image.png"}],
            [text, {"type": "unknown", "data": "extra"}],
            [text, "extra"],
            [text, {"type": "input_text", "text": 0}],
            [{**text, "extra": "payload"}],
            {"type": "input_text", "text": PROMPT},
            [],
        ]
        for content in cases:
            with self.subTest(content=content):
                turn = self.turn()
                turn[2]["payload"]["content"] = content
                self.build(turn)
                self.assertEqual(self.recover(), "codex-acceptance-unproven")
                self.assertEqual(self.acks(), [])

    def test_a_gone_or_unreadable_rollout_stays_unproven_without_raising(self):
        self.build(self.turn())
        self.rollout.chmod(0)
        self.addCleanup(lambda: self.rollout.exists() and self.rollout.chmod(0o600))
        if not os.access(self.rollout, os.R_OK):          # root reads anything: the deletion case still runs
            self.assertEqual(self.recover(), "codex-acceptance-unproven")
        self.rollout.chmod(0o600)
        self.rollout.unlink()
        self.assertEqual(self.recover(), "codex-acceptance-unproven")
        self.assertEqual(self.acks(), [])

    def test_a_notify_keeps_the_notify_rule(self):
        note = {"type": "agent-turn-complete", "thread-id": self.sid, "turn-id": "t1",
                "input-messages": ["something else"], "last-assistant-message": ""}
        self.build(self.turn(), notify=[note])
        self.assertEqual(self.recover(), "codex-acceptance-unproven")
        self.assertEqual(self.acks(), [])

    def test_a_rollout_removed_during_live_settle_is_changing_evidence(self):
        self.build(self.turn())
        # External boundary fixture: the real tmux capture removes the file after the initial scan.
        wrapper = self.root / "tmux-remove-rollout.py"
        wrapper.write_text(
            "import os, sys\nfrom pathlib import Path\n"
            "if 'capture-pane' in sys.argv[1:]:\n"
            f"    Path({str(self.rollout)!r}).unlink(missing_ok=True)\n"
            "os.execvp(sys.argv[1], sys.argv[1:])\n")
        self.tmux = interactive.Tmux([sys.executable, str(wrapper), *self.tmux.cmd])
        self.addCleanup(self.tmux.run, "kill-server")
        created = self.tmux.run("new-session", "-d", "-s", interactive.session_name("builder"),
                                "-n", self.key, "sleep 30")
        self.assertEqual(created.returncode, 0, created.stderr)
        self.assertEqual(self.recover(), "native-evidence-changing")
        self.assertFalse(self.rollout.exists())
        self.assertEqual(self.acks(), [])


if __name__ == "__main__":
    unittest.main()
