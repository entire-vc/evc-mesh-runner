"""The path guard of a session whose working copies moved: the hook script, the map's chaining and the
settings the interactive driver writes."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from mesh_runner import interactive, path_guard
from mesh_runner.tui_driver import Driver, DriverError

OLD = "/w/worktrees/Builder/app/slot-1"
NEW = "/w/worktrees/Builder/app/slot-2"


class GuardScript(unittest.TestCase):
    """The hook as Claude Code runs it: a subprocess, the call as JSON on stdin, exit 2 refuses."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.map = Path(self.tmp.name) / "att.moves.json"

    def run_guard(self, tool_input, mapping=None, tool_name="Edit", raw=None):
        if mapping is not None:
            path_guard.write(self.map, mapping)
        stdin = raw if raw is not None else json.dumps(
            {"hook_event_name": "PreToolUse", "tool_name": tool_name, "tool_input": tool_input})
        return subprocess.run([sys.executable, str(interactive.GUARD), str(self.map)], input=stdin,
                              capture_output=True, text=True, timeout=30)

    def assert_refused(self, res, old=OLD, new=NEW):
        self.assertEqual(res.returncode, 2, res.stderr)
        self.assertIn(f"This working copy moved: {old} is now {new}. Use the new path; "
                      "the old one belongs to other work.", res.stderr)

    def test_stale_file_path_is_refused_and_names_the_new_path(self):
        self.assert_refused(self.run_guard({"file_path": OLD + "/src/app.py", "old_string": "a"}, {OLD: NEW}))
        self.assert_refused(self.run_guard({"file_path": OLD}, {OLD: NEW}))

    def test_stale_path_inside_a_bash_command_is_refused(self):
        for command in (f"cd {OLD} && git status", f"git -C '{OLD}' diff", f'ls "{OLD}"', f"echo x>{OLD}/f",
                        f"cat {OLD}:x"):
            with self.subTest(command=command):
                self.assert_refused(self.run_guard({"command": command}, {OLD: NEW}, tool_name="Bash"))

    def test_stale_path_nested_in_a_list_is_refused(self):
        tool_input = {"edits": [{"path": NEW + "/ok.py"}, {"nested": [["x", {"file": OLD + "/b.py"}]]}]}
        self.assert_refused(self.run_guard(tool_input, {OLD: NEW}, tool_name="MultiEdit"))

    def test_a_longer_slot_name_is_another_path(self):
        for text in (OLD + "0", OLD + "0/src", OLD + ".bak", OLD + "-old", OLD + "_x"):
            with self.subTest(text=text):
                res = self.run_guard({"command": f"ls {text}"}, {OLD: NEW}, tool_name="Bash")
                self.assertEqual((res.returncode, res.stderr), (0, ""))

    def test_the_new_path_and_unrelated_input_pass(self):
        res = self.run_guard({"file_path": NEW + "/src/app.py", "command": "pytest -q", "n": 3, "flag": True},
                             {OLD: NEW})
        self.assertEqual((res.returncode, res.stderr), (0, ""))

    def test_chained_moves_lead_every_old_path_to_the_current_one(self):
        third = "/w/worktrees/Builder/app/slot-3"
        mapping = path_guard.chain(path_guard.chain({}, {OLD: NEW}, {NEW}), {NEW: third}, {third})
        self.assertEqual(mapping, {OLD: third, NEW: third})
        self.assert_refused(self.run_guard({"file_path": OLD + "/a"}, mapping), OLD, third)
        self.assert_refused(self.run_guard({"file_path": NEW + "/a"}, mapping), NEW, third)
        self.assertEqual(self.run_guard({"file_path": third + "/a"}, mapping).returncode, 0)

    def test_without_a_map_every_call_runs(self):
        self.assertFalse(self.map.exists())
        self.assertEqual(self.run_guard({"file_path": OLD}).returncode, 0)
        res = subprocess.run([sys.executable, str(interactive.GUARD)], input="{}", capture_output=True, text=True)
        self.assertEqual(res.returncode, 0)

    def test_a_corrupt_map_refuses_every_call(self):
        for content in ("{not json", "[1, 2]", json.dumps({OLD: 3}), json.dumps({"relative/path": NEW})):
            with self.subTest(content=content):
                self.map.write_text(content)
                res = self.run_guard({"command": "pytest -q"}, tool_name="Bash")
                self.assertEqual(res.returncode, 2)
                self.assertIn("cannot be read", res.stderr)
                self.assertIn(str(self.map), res.stderr)

    def test_input_that_is_not_a_tool_call_never_blocks(self):
        path_guard.write(self.map, {OLD: NEW})
        for raw in ("", "not json", "[1]", json.dumps({"tool_name": "Bash"})):
            with self.subTest(raw=raw):
                self.assertEqual(self.run_guard(None, raw=raw).returncode, 0)


class ChainMoves(unittest.TestCase):
    def test_first_move(self):
        self.assertEqual(path_guard.chain({}, {OLD: NEW}, {NEW}), {OLD: NEW})

    def test_several_repositories_move_independently(self):
        prev = {"/w/app/slot-1": "/w/app/slot-2"}
        got = path_guard.chain(prev, {"/w/lib/slot-1": "/w/lib/slot-3"}, {"/w/app/slot-2", "/w/lib/slot-3"})
        self.assertEqual(got, {"/w/app/slot-1": "/w/app/slot-2", "/w/lib/slot-1": "/w/lib/slot-3"})

    def test_a_move_back_to_an_earlier_slot_makes_it_current_again(self):
        got = path_guard.chain({OLD: NEW}, {NEW: OLD}, {OLD})
        self.assertEqual(got, {NEW: OLD})

    def test_a_restore_into_the_same_slot_keeps_the_map(self):
        self.assertEqual(path_guard.chain({OLD: NEW}, {}, {NEW}), {OLD: NEW})


class DriverSettings(unittest.TestCase):
    """The per-session settings of an interactive Claude session carry the guard only when it has moves."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "interactive" / "builder"
        self.state.mkdir(parents=True)
        self.map = path_guard.moves_path(self.state, "att-1")

    def driver(self, harness="claude", **env):
        return Driver(harness=harness, agent="builder", state=self.state, tmux=interactive.Tmux("tmux"),
                      max_sessions=1, cwd=Path(self.tmp.name), session_id="s-1", mode="resume", key="att-1",
                      argv=["claude", "--resume", "s-1"], env={"PATH": "/usr/bin", **env})

    def settings(self, argv):
        return json.loads(Path(argv[argv.index("--settings") + 1]).read_text())

    def test_no_moves_no_pre_tool_use_hook(self):
        settings = self.settings(self.driver().harness_argv())
        self.assertNotIn("PreToolUse", settings["hooks"])
        self.assertEqual(sorted(settings["hooks"]), ["SessionStart", "Stop", "StopFailure", "UserPromptSubmit"])

    def test_moves_add_a_pre_tool_use_hook_for_every_tool(self):
        path_guard.write(self.map, {OLD: NEW})
        d = self.driver(**{interactive.PATH_GUARD_ENV: str(self.map)})
        self.assertNotIn(interactive.PATH_GUARD_ENV, d.env)               # transport only, not the harness's env
        settings = self.settings(d.harness_argv())
        (entry,) = settings["hooks"]["PreToolUse"]
        self.assertEqual(entry["matcher"], "*")
        command = entry["hooks"][0]["command"]
        self.assertIn(str(interactive.GUARD), command)
        self.assertIn(str(self.map), command)
        self.assertTrue(settings["skipDangerousModePermissionPrompt"])
        # the command, run as Claude Code runs it, refuses the stale path
        res = subprocess.run(command, shell=True, capture_output=True, text=True, input=json.dumps(
            {"tool_name": "Read", "tool_input": {"file_path": OLD + "/README"}}))
        self.assertEqual(res.returncode, 2)
        self.assertIn(NEW, res.stderr)

    def test_a_window_rebuild_that_forgot_the_map_writes_it_back(self):
        path_guard.write(self.map, {OLD: NEW})
        d = self.driver(**{interactive.PATH_GUARD_ENV: str(self.map)})
        interactive.forget(self.state, "att-1")                          # ensure_session before start_window
        self.assertFalse(self.map.exists())
        d.harness_argv()
        self.assertEqual(path_guard.read(self.map), {OLD: NEW})

    def test_a_named_map_that_is_missing_stops_the_start(self):
        d = self.driver(**{interactive.PATH_GUARD_ENV: str(self.map)})
        with self.assertRaises(DriverError):
            d.harness_argv()

    def test_a_window_started_without_the_guard_is_not_reused_for_a_moved_session(self):
        plain = self.driver().digest()
        path_guard.write(self.map, {OLD: NEW})
        guarded = self.driver(**{interactive.PATH_GUARD_ENV: str(self.map)}).digest()
        path_guard.write(self.map, {OLD: NEW, NEW + "0": NEW})
        rechained = self.driver(**{interactive.PATH_GUARD_ENV: str(self.map)}).digest()
        self.assertEqual(len({plain, guarded, rechained}), 3)


if __name__ == "__main__":
    unittest.main()
