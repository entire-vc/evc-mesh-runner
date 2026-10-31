import json
import tempfile
import unittest
from pathlib import Path

from mesh_runner.adapters.codex import CodexAdapter

THREAD = "00000000-0000-4000-8000-000000000001"


def line(kind, **payload):
    return json.dumps({"timestamp": payload.pop("ts", "2026-10-30T16:01:00.000Z"), "type": kind, "payload": payload})


class CodexTurnContextTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        home = Path(self.tmp.name)
        self.dir = home / "sessions" / "2026" / "10" / "30"
        self.dir.mkdir(parents=True)
        self.env = {"CODEX_HOME": str(home)}
        self.adapter = CodexAdapter("codex")

    def tearDown(self):
        self.tmp.cleanup()

    def rollout(self, *lines):
        (self.dir / f"rollout-2026-10-30T16-01-00-{THREAD}.jsonl").write_text("\n".join(lines) + "\n")

    def test_latest_turn_context_model_effort_and_time(self):
        self.rollout(line("session_meta", id=THREAD),
                     line("turn_context", model="old", effort="low", ts="2026-10-30T15:00:00Z"),
                     line("turn_context", model="gpt-6.1-sol", effort="high", cwd="/secret/path", ts="2026-10-30T16:02:00Z"))
        got = self.adapter.observed_context(THREAD, self.env)
        self.assertEqual(got, {"model": "gpt-6.1-sol", "effort": "high", "at": "2026-10-30T16:02:00Z"})
        self.assertNotIn("/secret", json.dumps(got))

    def test_no_record_or_unknown_thread_is_empty_never_the_profile_default(self):
        self.rollout(line("session_meta", id=THREAD))
        self.assertEqual(self.adapter.observed_context(THREAD, self.env), {})
        self.assertEqual(self.adapter.observed_context("../../etc", self.env), {})
        self.assertEqual(self.adapter.observed_context("ffffffff-0000-0000-0000-000000000000", self.env), {})

    def test_model_null_in_record_stays_absent(self):
        self.rollout(line("turn_context", model=None, effort="high"))
        self.assertEqual(self.adapter.observed_context(THREAD, self.env), {"effort": "high", "at": "2026-10-30T16:01:00.000Z"})


if __name__ == "__main__":
    unittest.main()
