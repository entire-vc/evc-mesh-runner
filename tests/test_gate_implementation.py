"""Installed gate changes recheck a cached refusal through the ordinary hook."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

from fakes import FakeMesh, task
from mesh_runner.config import ConfigError, load
from test_runner import Bench


class GateImplementation(Bench):
    def setUp(self):
        super().setUp()
        self.helper = self.root / "gate_helper.py"
        self.hook = self.root / "gate.py"
        self.events = self.root / "gate_calls.jsonl"
        self.wake = self.root / "gate.wake"
        self.wake.write_text("unchanged")
        self.helper.write_text("REFUSAL = 'original review waits for verified official native route'\n")
        self.hook.write_text(
            "import json, os, runpy\n"
            f"with open({str(self.events)!r}, 'a') as f:\n"
            "    f.write(json.dumps(os.environ['MESH_RUNNER_PHASE']) + '\\n')\n"
            f"reason = runpy.run_path({str(self.helper)!r})['REFUSAL']\n"
            "if reason:\n"
            "    print(reason)\n"
            f"    print('HOLD_WAKE_FILE=' + {str(self.wake)!r})\n"
            "    print('HOLD_WAKE_SIG=unchanged')\n"
            "    raise SystemExit(76)\n"
        )
        self.agent.start_gate_command = [sys.executable, str(self.hook)]
        self.agent.start_gate_implementation_files = [self.hook, self.helper]

    def phases(self):
        return [json.loads(line) for line in self.events.read_text().splitlines()] if self.events.exists() else []

    def held_runner(self):
        card = task(1, cat="in_progress", updated_at="unchanged", input_revision=7, pinned_head="same-head")
        mesh = FakeMesh([card])
        runner = self.runner(mesh)
        runner.tick()
        self.assertEqual(self.phases(), ["reserve"])
        self.assertFalse(mesh.checkouts)
        return runner, mesh, card

    def test_helper_change_runs_full_gate_and_admission_without_task_or_wake_change(self):
        runner, mesh, card = self.held_runner()
        original = dict(card)
        for _ in range(3):
            runner.tick()
        self.assertEqual(self.phases(), ["reserve"])
        self.helper.write_text("REFUSAL = ''\n")
        runner.tick()
        self.assertEqual(self.phases(), ["reserve", "reserve", "admit"])
        self.assertEqual(len(mesh.checkouts), 1)
        self.assertEqual(card, original)
        self.assertEqual(self.wake.read_text(), "unchanged")
        self.assertEqual(len(runner.procs), 1)

    def test_changed_hook_new_refusal_is_cached_again(self):
        runner, mesh, card = self.held_runner()
        self.hook.write_text(self.hook.read_text() + "# installed hook revision\n")
        runner.tick()
        for _ in range(3):
            runner.tick()
        self.assertEqual(self.phases(), ["reserve", "reserve"])
        self.assertFalse(mesh.checkouts)
        self.assertIn(card["id"], runner._gate_hold)

    def test_lane_hold_source_rechecks_without_resetting_another_task(self):
        self.hook.write_text(self.hook.read_text().replace("SystemExit(76)", "SystemExit(75)"))
        runner, mesh, card = self.held_runner()
        runner._gate_hold["unrelated"] = ("another refusal", float("inf"), "unchanged")
        self.helper.write_text("REFUSAL = ''\n")
        runner.tick()
        self.assertEqual(self.phases(), ["reserve", "reserve", "admit"])
        self.assertEqual(len(mesh.checkouts), 1)
        self.assertIn("unrelated", runner._gate_hold)
        self.assertNotIn(card["id"], runner._gate_implementations)

    def test_unreadable_helper_is_no_change_event_and_same_bytes_stay_held(self):
        runner, mesh, _ = self.held_runner()
        old = self.helper.read_text()
        self.helper.unlink()
        self.helper.mkdir()
        runner.tick()
        self.helper.rmdir()
        self.helper.write_text(old)
        runner.tick()
        self.assertEqual(self.phases(), ["reserve"])
        self.assertFalse(mesh.checkouts)

    def test_foreign_checkout_still_blocks_after_changed_gate_passes(self):
        runner, mesh, card = self.held_runner()
        mesh.refuse_checkout.add(card["id"])
        self.helper.write_text("REFUSAL = ''\n")
        runner.tick()
        self.assertEqual(self.phases(), ["reserve", "reserve"])
        self.assertFalse(mesh.checkouts)
        self.assertFalse(runner.procs)

    def test_no_declaration_keeps_existing_hold_behavior(self):
        self.agent.start_gate_implementation_files = []
        runner, mesh, _ = self.held_runner()
        self.helper.write_text("REFUSAL = ''\n")
        runner.tick()
        self.assertEqual(self.phases(), ["reserve"])
        self.assertFalse(mesh.checkouts)

    def test_change_during_hook_is_not_attached_to_earlier_verdict(self):
        code = self.hook.read_text().replace(
            "if reason:\n", f"if reason:\n    open({str(self.helper)!r}, 'w').write(\"REFUSAL = ''\\n\")\n")
        self.hook.write_text(code)
        runner, mesh, _ = self.held_runner()
        runner.tick()
        self.assertEqual(self.phases(), ["reserve", "reserve", "admit"])
        self.assertEqual(len(mesh.checkouts), 1)

    def test_new_admission_refusal_is_not_bypassed(self):
        runner, mesh, _ = self.held_runner()
        self.helper.write_text("import os\nREFUSAL = 'source drift' if os.environ['MESH_RUNNER_PHASE'] == 'admit' else ''\n")
        runner.tick()
        self.assertEqual(self.phases(), ["reserve", "reserve", "admit"])
        self.assertFalse(mesh.checkouts)
        self.assertFalse(runner.procs)


class GateImplementationConfig(unittest.TestCase):
    def test_load_expands_paths_and_rejects_malformed_declarations(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            key = root / "key"
            key.write_text("fixture-key")
            key.chmod(0o600)
            cfg = root / "runner.toml"
            base = (f'[runner]\nmesh_url = "http://mesh"\nstate_dir = "{root / "state"}"\n'
                    f'[[agents]]\nname = "lane"\nkey_file = "{key}"\nprofile_dir = "{root}"\n')
            cfg.write_text(base)
            self.assertEqual(load(cfg).agents[0].start_gate_implementation_files, [])
            cfg.write_text(base + 'start_gate_implementation_files = ["~/gate.py", "/opt/gate/helper.py"]\n')
            self.assertEqual(load(cfg).agents[0].start_gate_implementation_files,
                             [Path.home() / "gate.py", Path("/opt/gate/helper.py")])
            for bad in ('"/opt/gate.py"', '["relative.py"]', '[3]', '[""]'):
                with self.subTest(bad=bad):
                    cfg.write_text(base + f'start_gate_implementation_files = {bad}\n')
                    with self.assertRaises(ConfigError):
                        load(cfg)
