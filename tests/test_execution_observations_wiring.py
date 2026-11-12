"""Execution observations in the shared Runner: opt-in by `[catalog] execution_observations`, one
publisher per catalog workspace, and the AgentRunner hooks around a turn.

Real `config.load` + real `Runner._start_catalog`; only the catalog controllers (network) are stubbed."""
import tempfile
import textwrap
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from mesh_runner import runner as runner_mod
from mesh_runner.adapters.base import TurnResult
from mesh_runner.config import load
from mesh_runner.execution_observations import Router

WS = "11111111-2222-3333-4444-555555555555"
WS2 = "99999999-2222-3333-4444-555555555555"
PROJECT = "22222222-2222-3333-4444-555555555555"
AGENT = "33333333-2222-3333-4444-555555555555"
TASK = "44444444-2222-3333-4444-555555555555"
CANARY_MODULES = ("execution_canary", "execution_actual_canary")


class StartTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.key = self.dir / "reporter.key"
        self.key.write_text("k")
        self.key.chmod(0o600)
        (self.dir / "accounts.json").write_text("{}")

    def cfg(self, observations=None):
        line = "" if observations is None else f"execution_observations = {observations}"
        path = self.dir / "runner.toml"
        path.write_text(textwrap.dedent(f"""
            [runner]
            mesh_url = "https://mesh.example.com"
            state_dir = "{self.dir}/state"
            [catalog]
            enabled = true
            manifest = "{self.dir}/accounts.json"
            {line}
            [[catalog.workspace]]
            id = "{WS}"
            reporter_key_file = "{self.key}"
        """))
        return load(path)

    def start(self, cfg, workspaces=(WS,)):
        ctls = [SimpleNamespace(client=SimpleNamespace(base="https://mesh.example.com/api/v1/workspaces/"
                                                       + ws + "/runtime/controllers/"),
                                controller_ref="mesh-runner", workspace_id=ws, catalog_dir=self.dir)
                for ws in workspaces]
        agents = [SimpleNamespace(agent=SimpleNamespace(name="a"), observations=None),
                  SimpleNamespace(agent=SimpleNamespace(name="b"), observations=None)]
        r = runner_mod.Runner.__new__(runner_mod.Runner)
        r.agents, r.cfg = agents, cfg
        stop = threading.Event()
        stop.set()                                   # the loop thread returns at once
        started = []
        real = threading.Thread

        def spy(*a, **k):
            started.append(k.get("name"))
            return real(*a, **k)
        with mock.patch("mesh_runner.catalog.build_controllers", return_value=ctls), \
                mock.patch("mesh_runner.catalog.catalog_loop", lambda *a, **k: None), \
                mock.patch.object(runner_mod.threading, "Thread", spy):
            r._start_catalog(stop)
        return agents, started

    def test_absent_and_false_start_no_publisher(self):
        for value in (None, "false"):
            with self.subTest(value=value):
                agents, started = self.start(self.cfg(value))
                self.assertEqual([a.observations for a in agents], [None, None])
                self.assertNotIn("execution-observations", started)

    def test_true_shares_one_router_with_a_publisher_per_workspace(self):
        agents, started = self.start(self.cfg("true"), workspaces=(WS, WS2))
        router = agents[0].observations
        self.assertIsInstance(router, Router)
        self.assertIs(agents[1].observations, router)
        self.assertEqual([h.workspace_id for h in router.hooks], [WS, WS2])
        self.assertEqual(len({id(h.pub) for h in router.hooks}), 2)
        self.assertEqual(started.count("execution-observations"), 1)

    def test_canary_tools_are_not_shipped(self):
        for name in CANARY_MODULES:
            with self.subTest(module=name), self.assertRaises(ImportError):
                __import__(f"mesh_runner.{name}")


class RecordingRouter:
    def __init__(self, bound=True):
        self._bound = bound
        self.calls = []

    def bound(self, agent_id):
        return self._bound

    def turn_started(self, *a, **k):
        self.calls.append(("start", a, k))

    def turn_settled(self, *a, **k):
        self.calls.append(("settle", a, k))

    def attempt_ended(self, *a, **k):
        self.calls.append(("end", a, k))


class AgentHooksTest(unittest.TestCase):
    def runner(self, router, task=None):
        ar = runner_mod.AgentRunner.__new__(runner_mod.AgentRunner)
        ar.observations, ar._obs_project, ar._obs_home = router, {}, {}
        ar.agent = SimpleNamespace(name="lane", harness="claude")
        ar.me = {"id": AGENT}
        ar.fetched = 0

        def task_of(_tid):
            ar.fetched += 1
            return task if task is not None else {"project_id": PROJECT}
        ar._task = task_of
        return ar

    def attempt(self, harness="claude"):
        return SimpleNamespace(id="attempt-1", task_id=TASK, harness=harness, session_id="thread-1234abcd")

    def test_start_reports_launch_route_and_project_once_per_task(self):
        router = RecordingRouter()
        ar = self.runner(router)
        argv = ["claude", "--model", "claude-opus-5-5", "--effort", "high"]
        ar._observe_start(self.attempt(), SimpleNamespace(), argv, {"HOME": "/h", "SECRET": "x"})
        ar._observe_start(self.attempt(), SimpleNamespace(), argv, {"HOME": "/h"})
        kind, args, kw = router.calls[0]
        self.assertEqual((kind, args[:4]), ("start", ("attempt-1", TASK, AGENT, "claude")))
        self.assertEqual((args[4].model, args[4].effort, kw["project_id"]), ("claude-opus-5-5", "high", PROJECT))
        self.assertEqual(ar.fetched, 1)
        self.assertEqual(ar._obs_home["attempt-1"], {"HOME": "/h"})        # nothing but the two paths kept

    def test_unbound_lane_costs_no_mesh_call(self):
        router = RecordingRouter(bound=False)
        ar = self.runner(router)
        ar._observe_start(self.attempt(), SimpleNamespace(), ["claude"], {})
        self.assertEqual((router.calls, ar.fetched), ([], 0))

    def test_settle_passes_only_what_the_harness_recorded(self):
        router = RecordingRouter()
        ar = self.runner(router)
        ar._observe_settle(self.attempt(), TurnResult(model="claude-opus-5-5"))
        ar._observe_settle(self.attempt(), TurnResult())
        self.assertEqual([c[1][1] for c in router.calls], [{"model": "claude-opus-5-5"}, {}])
        codex = SimpleNamespace(base=SimpleNamespace(
            observed_context=lambda sid, env: {"model": "gpt-6.1-sol", "effort": "high", "at": None, "sid": sid}))
        ar.adapter_for = lambda _h: codex
        ar._obs_home["attempt-1"] = {"CODEX_HOME": "/c"}
        ar._observe_settle(self.attempt("codex"), TurnResult(session_id="thread-1234abcd"))
        kind, args, _ = router.calls[-1]
        self.assertEqual((args[1]["model"], args[1]["sid"], args[3]), ("gpt-6.1-sol", "thread-1234abcd",
                                                                      "adapter_turn_context"))

    def test_a_broken_router_never_raises_into_the_turn(self):
        class Broken(RecordingRouter):
            def turn_started(self, *a, **k):
                raise RuntimeError("boom")
            turn_settled = turn_started
        ar = self.runner(Broken())
        ar._observe_start(self.attempt(), SimpleNamespace(), ["claude"], {})
        ar._observe_settle(self.attempt(), TurnResult(model="m"))


if __name__ == "__main__":
    unittest.main()
