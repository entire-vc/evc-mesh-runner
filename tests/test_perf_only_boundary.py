"""Perf-only candidate boundary: no supported configuration activates an execution publisher.

Real `config.load` + real `Runner._start_catalog`; only the transport/network boundary is patched."""
import tempfile
import textwrap
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from mesh_runner import runner as runner_mod
from mesh_runner.config import ConfigError, load

WS = "11111111-2222-3333-4444-555555555555"
EXECUTION_MODULES = ("execution_observations", "execution_canary", "execution_actual_canary")


class PerfOnlyBoundaryTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.key = self.dir / "reporter.key"
        self.key.write_text("k")
        self.key.chmod(0o600)
        (self.dir / "accounts.json").write_text("{}")

    def cfg(self, catalog_enabled, observations=None):
        line = "" if observations is None else f"execution_observations = {observations}"
        return load(self._write(f"""
            [runner]
            mesh_url = "https://mesh.example.com"
            state_dir = "{self.dir}/state"
            [catalog]
            enabled = {catalog_enabled}
            manifest = "{self.dir}/accounts.json"
            {line}
            [[catalog.workspace]]
            id = "{WS}"
            reporter_key_file = "{self.key}"
        """))

    def _write(self, text):
        path = self.dir / "runner.toml"
        path.write_text(textwrap.dedent(text))
        return path

    def start(self, cfg):
        ctl = SimpleNamespace(client=SimpleNamespace(base=""), controller_ref="mesh-runner", workspace_id=WS,
                              catalog_dir=self.dir)
        agent = SimpleNamespace(agent=SimpleNamespace(name="a"), observations=None, mesh=mock.MagicMock())
        r = runner_mod.Runner.__new__(runner_mod.Runner)
        r.agents = [agent]
        r.cfg = cfg
        stop = threading.Event()
        stop.set()
        before = {t.name for t in threading.enumerate()}
        with mock.patch("mesh_runner.catalog.build_controllers", return_value=[ctl]), \
                mock.patch("mesh_runner.catalog.catalog_loop", lambda *a, **k: None):
            r._start_catalog(stop)
        return agent, {t.name for t in threading.enumerate()} - before, ctl

    def test_true_is_refused_at_parse_before_any_side_effect(self):
        for enabled in ("true", "false"):
            with self.subTest(catalog_enabled=enabled):
                with self.assertRaises(ConfigError) as cm:
                    self.cfg(enabled, "true")
                self.assertIn("execution_observations", str(cm.exception))

    def test_wrong_type_is_refused(self):
        for bad in ('"true"', '"false"', "1", "0", "[true]"):
            with self.subTest(bad=bad), self.assertRaises(ConfigError):
                self.cfg("true", bad)

    def test_absent_and_false_start_no_publisher_hook_or_thread(self):
        for enabled in ("true", "false"):
            for value in (None, "false"):
                with self.subTest(catalog_enabled=enabled, value=value):
                    agent, started, ctl = self.start(self.cfg(enabled, value))
                    self.assertIsNone(agent.observations)
                    self.assertNotIn("execution-observations", started)
                    agent.mesh.task.assert_not_called()

    def test_no_execution_module_is_shipped_or_importable(self):
        for name in EXECUTION_MODULES:
            with self.subTest(module=name), self.assertRaises(ImportError):
                __import__(f"mesh_runner.{name}")

    def test_runner_has_no_observation_start_path(self):
        import inspect
        self.assertFalse(hasattr(runner_mod.Runner, "_start_observations"))
        self.assertNotIn("execution_observations", inspect.getsource(runner_mod))
        self.assertNotIn("self.observations", inspect.getsource(runner_mod.AgentRunner))


if __name__ == "__main__":
    unittest.main()
