"""An idle thread retained as gzip must remain resumable under its original id."""
import gzip
import json
import tempfile
import unittest
from pathlib import Path

from mesh_runner.adapters.codex import CodexAdapter


THREAD = "00000000-0000-4000-8000-000000000001"
OTHER = "00000000-0000-4000-8000-000000000002"


class CompressedSession(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.day = self.home / "sessions/2026/11/11"
        self.day.mkdir(parents=True)
        self.env = {"CODEX_HOME": str(self.home)}
        self.adapter = CodexAdapter()

    def archive(self, thread=THREAD, meta_id=THREAD, day=None):
        path = (day or self.day) / f"rollout-2026-11-11T21-40-32-{thread}.jsonl.gz"
        payload = (json.dumps({"type": "session_meta", "payload": {"id": meta_id}}) + "\n"
                   + json.dumps({"type": "turn_context", "payload": {"model": "fixture"}}) + "\n").encode()
        path.write_bytes(gzip.compress(payload))
        return path, payload

    def test_retained_thread_is_restored_before_the_existing_resume_check(self):
        archive, payload = self.archive()
        before = archive.read_bytes()
        self.assertFalse(self.adapter.session_known(THREAD, self.home, self.env))
        self.assertTrue(self.adapter.prepare_resume(THREAD, self.home, self.env))
        self.assertTrue(self.adapter.session_known(THREAD, self.home, self.env))
        self.assertEqual(archive.with_suffix("").read_bytes(), payload)
        self.assertEqual(archive.read_bytes(), before)
        self.assertEqual(self.adapter.prepare_resume(THREAD, self.home, self.env), "")
        argv = self.adapter.resume_argv(THREAD, self.home, [])
        self.assertEqual(argv[argv.index("resume") + 1], THREAD)

    def test_existing_native_rollout_is_never_replaced_by_an_older_archive(self):
        archive, _ = self.archive()
        native = archive.with_suffix("")
        native.write_bytes(b"current native transcript\n")
        self.assertEqual(self.adapter.prepare_resume(THREAD, self.home, self.env), "")
        self.assertEqual(native.read_bytes(), b"current native transcript\n")

    def test_selected_home_is_used_without_searching_another_home(self):
        archive, payload = self.archive()
        env = {"HOME": str(self.home / "unrelated"), "CODEX_HOME": str(self.home)}
        self.adapter.prepare_resume(THREAD, self.home, env)
        self.assertEqual(archive.with_suffix("").read_bytes(), payload)
        self.assertFalse((self.home / "unrelated").exists())

    def test_default_home_and_other_threads(self):
        day = self.home / ".codex/sessions/2026/11/11"
        day.mkdir(parents=True)
        archive, payload = self.archive(day=day)
        env = {"HOME": str(self.home)}
        self.assertEqual(self.adapter.prepare_resume(OTHER, self.home, env), "")
        self.assertFalse(archive.with_suffix("").exists())
        self.adapter.prepare_resume(THREAD, self.home, env)
        self.assertEqual(archive.with_suffix("").read_bytes(), payload)

    def test_truncated_gzip_leaves_no_native_or_temporary_rollout(self):
        archive, _ = self.archive()
        archive.write_bytes(archive.read_bytes()[:-5])
        with self.assertRaises(OSError):
            self.adapter.prepare_resume(THREAD, self.home, self.env)
        self.assertEqual(list(self.day.iterdir()), [archive])
        self.assertFalse(self.adapter.session_known(THREAD, self.home, self.env))

    def test_archive_metadata_must_match_the_requested_thread(self):
        archive, _ = self.archive(meta_id=OTHER)
        with self.assertRaises(OSError):
            self.adapter.prepare_resume(THREAD, self.home, self.env)
        self.assertEqual(list(self.day.iterdir()), [archive])

    def test_ambiguous_archives_are_not_selected_arbitrarily(self):
        first, _ = self.archive()
        other_day = self.home / "sessions/2026/11/12"
        other_day.mkdir()
        second, _ = self.archive(day=other_day)
        self.adapter.prepare_resume(THREAD, self.home, self.env)
        self.assertFalse(first.with_suffix("").exists())
        self.assertFalse(second.with_suffix("").exists())
        self.assertFalse(self.adapter.session_known(THREAD, self.home, self.env))


if __name__ == "__main__":
    unittest.main()
