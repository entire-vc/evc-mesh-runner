import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mesh_runner import config as config_mod
from mesh_runner.runner import Runner


def load(extra=""):
    d = tempfile.TemporaryDirectory()
    key = Path(d.name) / "k"
    key.write_text("secret")
    key.chmod(0o600)
    p = Path(d.name) / "runner.toml"
    p.write_text(f'[runner]\nmesh_url = "http://mesh"\nstate_dir = "{d.name}/s"\n{extra}\n'
                 f'[[agents]]\nname = "a"\nkey_file = "{key}"\nprofile_dir = "{d.name}"\n')
    cfg = config_mod.load(p)
    return cfg, d


class AnnounceControllers(unittest.TestCase):
    def test_off_by_default_and_read_from_runner_section(self):
        cfg, d = load()
        self.addCleanup(d.cleanup)
        self.assertFalse(cfg.announce_controllers)
        cfg2, d2 = load("announce_controllers = true\n")
        self.addCleanup(d2.cleanup)
        self.assertTrue(cfg2.announce_controllers)

    def _start(self, flag):
        agents = [SimpleNamespace(agent=SimpleNamespace(name=n), mesh=object()) for n in ("A", "B")]
        fake = SimpleNamespace(cfg=SimpleNamespace(announce_controllers=flag, state_dir="/nonexistent"), agents=agents)
        started = []
        with patch("mesh_runner.chat_announce.announce_loop", lambda *a, **k: started.append(a)), \
                patch("mesh_runner.chat_protocol.load_runner_id", lambda d: "rid"):
            Runner._start_announce(fake, threading.Event())
            for t in threading.enumerate():
                if t.name == "controller-announce":
                    t.join(2)
        return started

    def test_every_configured_agent_is_led_when_enabled(self):
        started = self._start(True)
        self.assertEqual(len(started), 1)
        clients, runner_id, _stop = started[0]
        self.assertEqual(sorted(clients), ["A", "B"])
        self.assertEqual(runner_id, "rid")

    def test_nothing_is_announced_when_disabled(self):
        self.assertEqual(self._start(False), [])


if __name__ == "__main__":
    unittest.main()
