"""Kimi and ZCode are harnesses a lane may run on or be routed to (``[agents.alt.*]``)."""
import tempfile
import unittest
from pathlib import Path

from mesh_runner.adapters.agy import AgyAdapter
from mesh_runner.adapters.kimi import KimiAdapter
from mesh_runner.adapters.zcode import ZCodeAdapter
from mesh_runner.config import ConfigError, load
from mesh_runner.runner import make_adapter, make_alt_adapter


def config(agent: str) -> Path:
    root = Path(tempfile.mkdtemp())
    (root / "k").write_text("x")
    (root / "k").chmod(0o600)
    (root / "p").mkdir()
    path = root / "runner.toml"
    path.write_text(f'[runner]\nmesh_url="http://mesh"\nstate_dir="{root}/state"\n'
                    f'[[agents]]\nname="lane"\nkey_file="{root}/k"\nprofile_dir="{root}/p"\n{agent}\n')
    return path


class HarnessKinds(unittest.TestCase):
    def test_kimi_and_zcode_are_accepted_as_alt_harnesses(self):
        cfg = load(config('harness="claude"\n[agents.alt.kimi]\nargs=["--model","k3.1"]\n'
                          '[agents.alt.zcode]\nbinary="/x/glm-zcode"\n'))
        lane, = cfg.agents
        kimi = make_alt_adapter("kimi", lane.alt["kimi"])
        zcode = make_alt_adapter("zcode", lane.alt["zcode"])
        self.assertIsInstance(kimi, KimiAdapter)
        self.assertEqual(kimi.extra_args, ["--model", "k3.1"])
        self.assertIsInstance(zcode, ZCodeAdapter)
        self.assertEqual(zcode.binary, "/x/glm-zcode")

    def test_a_lane_may_run_on_kimi(self):
        lane, = load(config('harness="kimi"\nbinary="/opt/kimi"\n')).agents
        adapter = make_adapter(lane)
        self.assertIsInstance(adapter, KimiAdapter)
        self.assertEqual(adapter.binary, "/opt/kimi")

    def test_a_lane_may_run_on_agy_with_its_admission_commands(self):
        lane, = load(config('harness="agy"\nbinary="/u/.local/bin/agy"\nargs=["--model","gemini-pro-orchestrator"]\n'
                            'admission_commands=[["python3","guard.py","--gemini-dir","{gemini_dir}"],["egress","check"]]\n')).agents
        adapter = make_adapter(lane)
        self.assertIsInstance(adapter, AgyAdapter)
        self.assertEqual(adapter.binary, "/u/.local/bin/agy")
        self.assertEqual(adapter.extra_args, ["--model", "gemini-pro-orchestrator"])
        self.assertEqual(adapter.admission_commands,
                         [["python3", "guard.py", "--gemini-dir", "{gemini_dir}"], ["egress", "check"]])

    def test_agy_is_accepted_as_an_alt_harness(self):
        lane, = load(config('harness="claude"\n[agents.alt.agy]\nargs=["--model","gemini-4-carbon-high"]\n'
                            'admission_commands=[["true"]]\n')).agents
        agy = make_alt_adapter("agy", lane.alt["agy"])
        self.assertIsInstance(agy, AgyAdapter)
        self.assertEqual(agy.binary, "agy")
        self.assertEqual(agy.admission_commands, [["true"]])

    def test_malformed_admission_commands_are_refused(self):
        for bad in ('admission_commands=["true"]\n', 'admission_commands=[[]]\n', 'admission_commands=[[1]]\n'):
            with self.assertRaises(ConfigError):
                load(config('harness="agy"\n' + bad))
            with self.assertRaises(ConfigError):
                load(config('harness="claude"\n[agents.alt.agy]\n' + bad))

    def test_unknown_harness_and_alt_are_refused(self):
        with self.assertRaises(ConfigError):
            load(config('harness="gemini"\n'))
        with self.assertRaises(ConfigError):
            load(config('harness="claude"\n[agents.alt.gemini]\nargs=[]\n'))
        with self.assertRaises(ValueError):
            make_alt_adapter("gemini", {})


if __name__ == "__main__":
    unittest.main()
