"""[runner] hold_labels: the deployment's human-hold labels keep a card from an automatic continuation."""
import tempfile
import textwrap
import unittest
from pathlib import Path

from mesh_runner import queue_wait as q
from mesh_runner.config import ConfigError, load


class HoldLabelsConfig(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.dir = Path(self._t.name)

    def tearDown(self):
        self._t.cleanup()

    def cfg(self, extra=""):
        path = self.dir / "runner.toml"
        path.write_text(textwrap.dedent(f"""
            [runner]
            mesh_url = "https://mesh.example.com"
            state_dir = "{self.dir}/state"
        """) + textwrap.dedent(extra))
        return load(path)

    def test_default_holds_awaiting_owner(self):
        self.assertEqual(self.cfg().hold_labels, ("awaiting-owner",))

    def test_configured_list_replaces_the_default(self):
        self.assertEqual(self.cfg('hold_labels = ["awaiting-lead", "frozen"]\n').hold_labels,
                         ("awaiting-lead", "frozen"))
        self.assertEqual(self.cfg("hold_labels = []\n").hold_labels, ())

    def test_wrong_shape_is_refused(self):
        for bad in ('"awaiting-lead"', "[1]", '[""]', '[" padded "]', "true"):
            with self.subTest(bad=bad), self.assertRaises(ConfigError):
                self.cfg(f"hold_labels = {bad}\n")


class HeldPredicate(unittest.TestCase):
    def test_builtin_holds_stay_without_configuration(self):
        for label in ("no-runner", "rescue-exhausted", "park:manual"):
            with self.subTest(label=label):
                self.assertTrue(q.held(["fleet-starvation-wake", label]))

    def test_configured_label_holds_only_when_configured(self):
        self.assertTrue(q.held(["awaiting-lead"], ("awaiting-lead",)))
        self.assertFalse(q.held(["awaiting-lead"], ("awaiting-owner",)))   # control
        self.assertFalse(q.held(["fleet-starvation-wake"], ("awaiting-lead",)))


if __name__ == "__main__":
    unittest.main()
