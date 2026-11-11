"""Options removed from the loader are ignored when an older config file still carries them."""
import dataclasses
import tempfile
import unittest
from pathlib import Path

from mesh_runner import config as config_mod

# removed in 0.2.1: the behaviour each one switched on is unconditional
RETIRED = ("mention_requests", "turn_cap_continue_on_progress", "queue_wait_freshness_v1", "recovery_wait_v1",
           "lifecycle_wait_v1", "stall_wait_v1", "requeue_on_cap", "cap_wake_next_day",
           "soft_person_asks_to_lead", "merge_follow_head", "checkpoint_gc")


class RetiredOptions(unittest.TestCase):
    def load(self, extra=""):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        key = Path(d.name) / "k"
        key.write_text("secret")
        key.chmod(0o600)
        p = Path(d.name) / "runner.toml"
        p.write_text(f'[runner]\nmesh_url = "http://mesh"\nstate_dir = "{d.name}/s"\n{extra}\n'
                     f'[[agents]]\nname = "a"\nkey_file = "{key}"\nprofile_dir = "{d.name}"\n')
        return config_mod.load(p)

    def test_an_older_file_loads_whatever_the_retired_keys_hold(self):
        plain = self.load("turn_cap_progress_min = 5\n")
        for value in ("true", "false", '"yes"', "7"):
            with self.subTest(value=value):
                cfg = self.load("".join(f"{name} = {value}\n" for name in RETIRED) + "turn_cap_progress_min = 5\n")
                self.assertEqual(dataclasses.replace(cfg, state_dir=plain.state_dir, agents=plain.agents,
                                                     config_path=plain.config_path), plain)

    def test_the_loader_has_no_such_fields(self):
        fields = {f.name for f in dataclasses.fields(config_mod.RunnerConfig)}
        self.assertEqual(fields & set(RETIRED), set())


if __name__ == "__main__":
    unittest.main()
