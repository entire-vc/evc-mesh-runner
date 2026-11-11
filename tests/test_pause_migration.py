"""The old pause marker <state_dir>/pause/<name> becomes the token pause.d/<name>/legacy-pause-file at start."""
import os
import tempfile
import unittest
from pathlib import Path

from fakes import FakeMesh
from test_runner import Bench

from mesh_runner import op_pause
from mesh_runner.runner import Runner


class Migrate(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.legacy = self.d / "pause"
        self.legacy.mkdir()

    def token(self, name):
        return self.d / "pause.d" / name / op_pause.LEGACY_TOKEN

    def test_a_file_becomes_a_token_with_its_text_and_a_second_run_does_nothing(self):
        (self.legacy / "builder").write_text("drain for the 0.2.5 install\n")
        with self.assertLogs("mesh_runner", "INFO") as logs:
            self.assertEqual(op_pause.migrate_legacy(self.d, ["builder"]), set())
        self.assertEqual(self.token("builder").read_text(), "drain for the 0.2.5 install\n")
        self.assertFalse((self.legacy / "builder").exists())
        self.assertEqual(op_pause.tokens(self.d, "builder"), ["builder/legacy-pause-file"])
        self.assertEqual(len([line for line in logs.output if "migrated" in line]), 1)
        self.assertEqual(op_pause.migrate_legacy(self.d, ["builder"]), set())          # idempotent
        self.assertEqual(self.token("builder").read_text(), "drain for the 0.2.5 install\n")

    def test_a_crash_between_token_and_unlink_finishes_on_the_next_start(self):
        (self.legacy / "builder").write_text("x")
        self.token("builder").parent.mkdir(parents=True)
        self.token("builder").write_text("x")
        self.assertEqual(op_pause.migrate_legacy(self.d, ["builder"]), set())
        self.assertFalse((self.legacy / "builder").exists())
        self.assertTrue(self.token("builder").exists())

    def test_a_name_that_is_not_an_agent_is_migrated_and_logged(self):
        (self.legacy / "retired").write_text("")
        with self.assertLogs("mesh_runner", "INFO") as logs:
            op_pause.migrate_legacy(self.d, ["builder"])
        self.assertTrue(self.token("retired").exists())
        self.assertIn("not a configured agent", "\n".join(logs.output))

    def test_symlinks_and_directories_are_left_alone(self):
        target = self.d / "elsewhere"
        target.write_text("keep")
        (self.legacy / "linked").symlink_to(target)
        (self.legacy / "adir").mkdir()
        with self.assertLogs("mesh_runner", "WARNING") as logs:
            self.assertEqual(op_pause.migrate_legacy(self.d, ["linked", "adir"]), set())
        self.assertTrue((self.legacy / "linked").is_symlink())
        self.assertTrue((self.legacy / "adir").is_dir())
        self.assertEqual(target.read_text(), "keep")
        self.assertFalse((self.d / "pause.d").exists())
        self.assertEqual(len([line for line in logs.output if "left alone" in line]), 2)
        self.assertFalse(op_pause.legacy_paused(self.d, "linked", set()))            # and no longer read

    def test_an_unwritable_token_directory_keeps_the_file_and_it_still_pauses(self):
        (self.legacy / "builder").write_text("x")
        (self.d / "pause.d").mkdir(mode=0o500)
        try:
            if os.access(self.d / "pause.d", os.W_OK):
                self.skipTest("running with privileges that ignore modes")
            with self.assertLogs("mesh_runner", "WARNING"):
                fallback = op_pause.migrate_legacy(self.d, ["builder"])
        finally:
            (self.d / "pause.d").chmod(0o755)
        self.assertEqual(fallback, {"builder"})
        self.assertTrue((self.legacy / "builder").exists())
        self.assertTrue(op_pause.legacy_paused(self.d, "builder", fallback))
        self.assertFalse(op_pause.legacy_paused(self.d, "other", fallback))

    def test_a_name_that_cannot_be_a_token_stays_honoured(self):
        (self.legacy / "has space").write_text("")
        fallback = op_pause.migrate_legacy(self.d, [])
        self.assertEqual(fallback, {"has space"})
        self.assertTrue(op_pause.legacy_paused(self.d, "has space", fallback))

    def test_no_legacy_directory_is_nothing_to_do(self):
        self.legacy.rmdir()
        self.assertEqual(op_pause.migrate_legacy(self.d, ["builder"]), set())


class AtRunnerStart(Bench):
    def test_paused_holds_across_the_migration_and_a_new_marker_is_not_read(self):
        (self.cfg.state_dir / "pause").mkdir(parents=True, exist_ok=True)
        (self.cfg.state_dir / "pause" / "builder").write_text("held by the operator")
        r = self.runner(FakeMesh([]))
        r.legacy_pause = op_pause.migrate_legacy(self.cfg.state_dir, ["builder"])
        self.assertTrue(r.paused())
        self.assertIn("builder/legacy-pause-file", r.status()["pause_tokens"])
        op_pause.release(self.cfg.state_dir, "builder", op_pause.LEGACY_TOKEN)
        self.assertFalse(r.paused())
        (self.cfg.state_dir / "pause" / "builder").write_text("")          # the old file no longer pauses
        self.assertFalse(r.paused())

    def test_the_runner_migrates_once_before_any_tick_and_shares_the_fallback(self):
        (self.cfg.state_dir / "pause").mkdir(parents=True, exist_ok=True)
        (self.cfg.state_dir / "pause" / "builder").write_text("note")
        runner = Runner(self.cfg)
        try:
            self.assertFalse((self.cfg.state_dir / "pause" / "builder").exists())
            self.assertEqual((self.cfg.state_dir / "pause.d" / "builder" / op_pause.LEGACY_TOKEN).read_text(), "note")
            self.assertTrue(all(ar.paused() for ar in runner.agents))
            self.assertTrue(all(ar.legacy_pause is runner.agents[0].legacy_pause for ar in runner.agents))
        finally:
            runner._lock.close()

    def test_an_unwritable_token_directory_keeps_the_legacy_pause_for_the_run(self):
        (self.cfg.state_dir / "pause").mkdir(parents=True, exist_ok=True)
        (self.cfg.state_dir / "pause" / "builder").write_text("")
        (self.cfg.state_dir / "pause.d").mkdir(mode=0o500)
        try:
            if os.access(self.cfg.state_dir / "pause.d", os.W_OK):
                self.skipTest("running with privileges that ignore modes")
            runner = Runner(self.cfg)
            try:
                self.assertTrue(runner.agents[0].paused())
                (self.cfg.state_dir / "pause" / "builder").unlink()           # the fallback reads the live file
                self.assertFalse(runner.agents[0].paused())
            finally:
                runner._lock.close()
        finally:
            (self.cfg.state_dir / "pause.d").chmod(0o755)


if __name__ == "__main__":
    unittest.main()
