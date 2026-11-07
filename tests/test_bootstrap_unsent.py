"""A codex window that died while the TUI was bootstrapping never took input: proven unsent."""
import hashlib
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from mesh_runner import delivery_recovery, interactive

LINE = ("interactive: codex exited during start-up, starting again (2/2) in 30s: Error: account/read failed "
        "during TUI bootstrap: account/read failed: workspace routing discovery unauthorized (401) (code -32603)")


class Bootstrap(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.root = Path(self._t.name)
        self.addCleanup(self._t.cleanup)
        self.dir, self.key = self.root / "state", "attempt1"
        self.cwd = self.root / "work"
        self.cwd.mkdir()
        self.home = self.root / "codex-home"
        self.home.mkdir()
        self.old = os.environ.get("CODEX_HOME")
        os.environ["CODEX_HOME"] = str(self.home)
        self.addCleanup(lambda: os.environ.__setitem__("CODEX_HOME", self.old) if self.old is not None
                        else os.environ.pop("CODEX_HOME", None))
        self.log = self.root / "turn1.codex.jsonl"
        self.log.write_text("")
        self.intent = {"token": "tok", "sha256": "sha", "prompt": "do it", "log_path": str(self.log)}
        self.journal = {"token": "tok", "sha256": "sha", "phase": "send-intent", "harness": "codex",
                        "session_id": "", "cwd": str(self.cwd), "native_base": 0,
                        "native_prefix": hashlib.sha256(json.dumps([], sort_keys=True).encode()).hexdigest()}

    def put(self, **over):
        interactive.write_delivery(self.dir, self.key, {**self.journal, **over})

    def recover(self):
        return delivery_recovery.recover(self.dir, self.key, self.intent, "a", object())

    def test_typed_bootstrap_failed_phase_is_unsent(self):
        self.put(phase="bootstrap-failed", bootstrap_error="Error: account/read failed during TUI bootstrap")
        self.assertEqual(self.recover(), "unsent")

    def test_bootstrap_failed_needs_the_typed_error(self):
        self.put(phase="bootstrap-failed")
        self.assertNotEqual(self.recover(), "unsent")

    def test_legacy_journal_with_bootstrap_stderr_and_no_rollout_is_unsent(self):
        self.put()
        self.log.with_suffix(".stderr").write_text(LINE + "\nwindow dead\n")
        self.assertEqual(self.recover(), "unsent")

    def test_legacy_stays_pending_without_proof(self):
        self.put()
        self.assertEqual(self.recover(), "session-unbound")                      # no stderr evidence
        self.log.with_suffix(".stderr").write_text("prompt mentions failed during TUI bootstrap\n")
        self.assertEqual(self.recover(), "session-unbound")                      # not the driver's start-up line
        self.log.with_suffix(".stderr").write_text(LINE + "\ntiming: session=1.0\n")
        self.assertEqual(self.recover(), "session-unbound")                      # a turn finished
        self.log.with_suffix(".stderr").write_text(LINE + "\n")
        day = self.home / "sessions" / "2026" / "11" / "06"
        day.mkdir(parents=True)
        (day / "rollout-2026-11-06T00-00-00-0f0f0f0f-0f0f-0f0f-0f0f-0f0f0f0f0f0f.jsonl").write_text(
            json.dumps({"payload": {"cwd": str(self.cwd)}}) + "\n")
        self.assertEqual(self.recover(), "session-unbound")                      # a rollout for this cwd exists

    def test_a_rollout_of_another_cwd_does_not_block(self):
        self.put()
        self.log.with_suffix(".stderr").write_text(LINE + "\n")
        day = self.home / "sessions" / "2026" / "11" / "06"
        day.mkdir(parents=True)
        (day / "rollout-x.jsonl").write_text(json.dumps({"payload": {"cwd": "/elsewhere"}}) + "\n")
        self.assertEqual(self.recover(), "unsent")


class Typed(unittest.TestCase):
    def test_the_final_driver_error_is_an_auth_refusal(self):
        from mesh_runner.auth import is_codex_auth_error
        self.assertTrue(is_codex_auth_error("codex exited during start-up 3 times: Error: account/read failed "
            "during TUI bootstrap: account/read failed: workspace routing discovery unauthorized (401) (code -32603)"))
        self.assertFalse(is_codex_auth_error("codex exited during start-up 3 times: Error: account/read failed "
            "during TUI bootstrap: workspace routing discovery timed out"))


if __name__ == "__main__":
    unittest.main()
