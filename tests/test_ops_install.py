"""ops/install.py: staging, plist rewrite and refusals. launchd itself is not driven here."""
import importlib.util
import json
import plistlib
import time
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("ops_install", ROOT / "ops" / "install.py")
install = importlib.util.module_from_spec(spec)
spec.loader.exec_module(install)


class Install(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "tree"
        shutil.copytree(ROOT / "src", self.source / "src", ignore=shutil.ignore_patterns("__pycache__"))
        (self.source / "pyproject.toml").write_text("[project]\nname='x'\n")
        self.catalog = self.root / "catalog"

    def tearDown(self):
        self.tmp.cleanup()

    def plist(self, release: Path) -> dict:
        return {"Label": "x", "KeepAlive": True,
                "ProgramArguments": ["/usr/bin/python3", str(release / "src/mesh_runner/launch_guard.py"),
                                     "--target-release", str(release), "--config", "/etc/runner.toml"],
                "EnvironmentVariables": {"PATH": "/bin", "PYTHONPATH": str(release / "src")}}

    def test_stage_copies_the_tree_and_reuses_an_identical_one(self):
        target = install.stage(self.source, self.catalog, "9.9.9")
        self.assertEqual(target, self.catalog / "9.9.9")
        self.assertEqual(install.tree_digest(target), install.tree_digest(self.source))
        self.assertFalse(list(target.rglob("__pycache__")))
        self.assertEqual(install.stage(self.source, self.catalog, "9.9.9"), target)      # idempotent
        self.assertEqual([p.name for p in self.catalog.iterdir()], ["9.9.9"])            # no partial left behind

    def test_stage_refuses_a_version_that_holds_other_content(self):
        install.stage(self.source, self.catalog, "9.9.9")
        module = self.source / "src/mesh_runner/__init__.py"
        module.write_text(module.read_text() + "\n# changed\n")
        with self.assertRaises(install.Failed):
            install.stage(self.source, self.catalog, "9.9.9")

    def test_not_a_release(self):
        with self.assertRaises(install.Failed):
            install.tree_digest(self.root)

    def test_source_commit_is_read_from_the_exported_tree(self):
        self.assertIsNone(install.source_commit(self.source)["sha"])                     # a release from before it
        (self.source / "SOURCE_COMMIT").write_text("$Format:%H %P$\n")                   # a copy of a checkout
        self.assertIn("git archive", install.source_commit(self.source)["why"])
        sha, parent = "a" * 40, "b" * 40
        (self.source / "SOURCE_COMMIT").write_text(f"{sha} {parent}\n")
        self.assertEqual(install.source_commit(self.source), {"sha": sha, "parents": [parent]})
        install.refuse_merge_commit(self.source)                                         # the release commit passes
        target = install.stage(self.source, self.catalog, "9.9.9")
        self.assertEqual(install.source_commit(target)["sha"], sha)                       # the catalog keeps it

    def test_a_reused_catalog_entry_keeps_the_commit_it_was_exported_from(self):
        install.stage(self.source, self.catalog, "9.9.9")                                 # staged before SOURCE_COMMIT
        target = self.catalog / "9.9.9"
        first, other = "a" * 40, "f" * 40
        (self.source / "SOURCE_COMMIT").write_text(f"{first} {'b' * 40}\n")
        install.stage(self.source, self.catalog, "9.9.9")                                 # the missing file is filled in
        self.assertEqual(install.source_commit(target)["sha"], first)
        install.stage(self.source, self.catalog, "9.9.9")                                 # same commit: reused
        (self.source / "SOURCE_COMMIT").write_text(f"{other} {'b' * 40}\n")
        with self.assertRaisesRegex(install.Failed, "exported from commit " + first):    # same code, other commit
            install.stage(self.source, self.catalog, "9.9.9")
        (self.source / "SOURCE_COMMIT").write_text("$Format:%H %P$\n")
        with self.assertRaises(install.Failed):                                           # a checkout copy
            install.stage(self.source, self.catalog, "9.9.9")
        self.assertEqual(install.source_commit(target)["sha"], first)

    def test_a_tree_exported_from_a_merge_commit_is_refused(self):
        merge, main, release = "c" * 40, "d" * 40, "e" * 40
        (self.source / "SOURCE_COMMIT").write_text(f"{merge} {main} {release}\n")
        with self.assertRaisesRegex(install.Failed, "merge commit " + merge):
            install.refuse_merge_commit(self.source)
        out = install.main(["--source", str(self.source), "--catalog", str(self.catalog)])
        self.assertEqual(out, 2)
        self.assertFalse(self.catalog.exists())                                          # nothing staged

    def test_the_repository_marks_source_commit_for_git_archive(self):
        self.assertEqual((ROOT / "SOURCE_COMMIT").read_text().strip(), "$Format:%H %P$")
        self.assertIn("SOURCE_COMMIT export-subst", (ROOT / ".gitattributes").read_text().splitlines())

    def test_version_comes_from_the_tree(self):
        self.assertRegex(install.source_version(self.source), r"^\d+\.\d+\.\d+")

    def test_plist_points_at_the_new_release_and_nothing_else_changes(self):
        old, new = self.root / "old", self.root / "new"
        before = self.plist(old)
        before["ProgramArguments"] += ["--previous-config", "/tmp/snapshot.toml", "--log-level", "INFO"]
        after = install.plist_for(before, new)
        self.assertEqual(after["ProgramArguments"],
                         ["/usr/bin/python3", str(new / "src/mesh_runner/launch_guard.py"), "--target-release",
                          str(new), "--config", "/etc/runner.toml", "--log-level", "INFO"])
        self.assertEqual(after["EnvironmentVariables"], {"PATH": "/bin", "PYTHONPATH": str(new / "src")})
        self.assertEqual({k: v for k, v in after.items() if k not in ("ProgramArguments", "EnvironmentVariables")},
                         {"Label": "x", "KeepAlive": True})
        self.assertEqual(install.running_release(after), new)
        self.assertEqual(install.running_release(before), old)                           # the input is untouched
        plistlib.dumps(after)                                                            # still a valid plist

    def test_plist_that_does_not_launch_through_the_guard_is_not_rewritten(self):
        with self.assertRaises(install.Failed):
            install.plist_for({"ProgramArguments": ["/usr/bin/python3", "-m", "mesh_runner.cli", "run"]}, self.root)
        with self.assertRaises(install.Failed):
            install.plist_for({"ProgramArguments": ["/usr/bin/python3", "/opt/other/tool.py", "--config", "x"]}, self.root)

    def legacy_plist(self, release: Path) -> dict:
        """The job definition a deploy script copied over the installed one: no guard, release by PYTHONPATH."""
        return {"Label": "x", "KeepAlive": True,
                "ProgramArguments": ["/usr/bin/python3", "-m", "mesh_runner.cli", "run", "--config", "/etc/runner.toml"],
                "EnvironmentVariables": {"PATH": "/bin", "PYTHONPATH": str(release / "src")}}

    def test_a_job_definition_from_before_the_guard_is_rebuilt_in_the_guarded_form(self):
        old, new = self.root / "0.1.13", self.root / "new"
        before = self.legacy_plist(old)
        self.assertEqual(install.running_release(before), old)
        after = install.plist_for(before, new)
        self.assertEqual(after["ProgramArguments"],
                         ["/usr/bin/python3", str(new / "src/mesh_runner/launch_guard.py"), "--target-release",
                          str(new), "--config", "/etc/runner.toml"])
        self.assertEqual(after["EnvironmentVariables"], {"PATH": "/bin", "PYTHONPATH": str(new / "src")})
        self.assertEqual(after["Label"], "x")
        self.assertIsNone(install.running_release({"ProgramArguments": ["/usr/bin/python3", "x.py"]}))

    def test_a_live_process_of_another_tree_is_not_already_active(self):
        release = self.root / "rel"
        (release / "src" / "mesh_runner").mkdir(parents=True)
        state = self.root / "state"
        state.mkdir()
        self.assertFalse(install.runs_another_tree(state, release))                       # no status file: unknown
        status = {"unix": time.time(), "receipt": {"source": str(release / "src" / "mesh_runner"), "pid": 1}}
        (state / "status.json").write_text(json.dumps(status))
        self.assertFalse(install.runs_another_tree(state, release))                       # this release
        status["receipt"]["source"] = str(self.root / "0.1.13" / "src" / "mesh_runner")
        (state / "status.json").write_text(json.dumps(status))
        self.assertTrue(install.runs_another_tree(state, release))                        # another release
        (state / "status.json").write_text(json.dumps({"unix": time.time(), "receipt": None}))
        self.assertTrue(install.runs_another_tree(state, release))                        # a release without receipts
        (state / "status.json").write_text(json.dumps({"unix": time.time() - 3600, "receipt": None}))
        self.assertFalse(install.runs_another_tree(state, release))                       # stale: nothing runs

    def test_preflight_rejects_a_release_that_cannot_load_the_config(self):
        target = install.stage(self.source, self.catalog, "9.9.9")
        config = self.root / "runner.toml"
        config.write_text('[runner]\nmesh_url = "http://invalid.local"\nstate_dir = ' + json.dumps(str(self.root / "st")) + "\n")
        install.preflight(target, config, sys.executable)                                # loads
        (target / "src/mesh_runner/config.py").write_text("raise RuntimeError('broken release')\n")
        with self.assertRaises(install.Failed) as caught:
            install.preflight(target, config, sys.executable)
        self.assertIn("broken release", str(caught.exception))

    def test_marker_of_the_release_being_left_is_kept_and_put_back_for_an_older_guard(self):
        state = self.root / "state"
        state.mkdir()
        old, new = self.root / "0.1.44", install.stage(self.source, self.catalog, "9.9.9")
        (old / "src/mesh_runner").mkdir(parents=True)
        (old / "src/mesh_runner/launch_guard.py").write_text("CAPABILITY = 1\n")      # a guard without the journal rule
        bound_old = {"version": 1, "release": str(old), "tree": "t-old", "config": "c", "config_sha256": "s", "state": str(state)}
        (state / install.MARKER).write_text(json.dumps(bound_old))
        install.keep_marker(state, old)
        self.assertEqual(json.loads(install.saved_marker(state, old).read_text()), bound_old)
        # the new release ran and rebound the state to itself
        (state / install.MARKER).write_text(json.dumps({**bound_old, "release": str(new), "tree": "t-new"}))
        self.assertFalse(install.restore_marker(state, new))                            # it needs no help
        self.assertEqual(json.loads((state / install.MARKER).read_text())["tree"], "t-new")
        self.assertTrue(install.restore_marker(state, old))                             # the way back
        self.assertEqual(json.loads((state / install.MARKER).read_text()), bound_old)

    def test_a_marker_bound_to_another_release_is_not_kept_under_this_name(self):
        state = self.root / "state"
        state.mkdir()
        old = self.root / "0.1.44"
        (state / install.MARKER).write_text(json.dumps({"release": str(self.root / "elsewhere")}))
        install.keep_marker(state, old)
        self.assertFalse(install.saved_marker(state, old).exists())
        self.assertFalse(install.restore_marker(state, old))                            # nothing kept, nothing done

    def test_already_active_is_a_no_op(self):
        release = install.stage(self.source, self.catalog, "9.9.9")
        path = self.root / "job.plist"
        with open(path, "wb") as stream:
            plistlib.dump(self.plist(release), stream)
        args = SimpleNamespace(plist=str(path), state=str(self.root), config="unused", python=sys.executable,
                               label="vc.entire.test-not-loaded", restart=False, stop_seconds=1, wait_seconds=1)
        out = install.switch(release, args)
        self.assertEqual(out["result"], "already-active")
        self.assertEqual(plistlib.loads(path.read_bytes()), self.plist(release))


if __name__ == "__main__":
    unittest.main()
