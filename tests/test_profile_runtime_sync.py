"""The lane's Mesh profile (agent_type/model in the team directory) follows the harness and model its
turns actually ran on, not the registry entry someone typed once. Review routing and reviewers read the
directory: a lane moved from one harness to another (an operator switch, a quota fallback) must not keep
announcing the old family.

Only an observed model is reported (Claude's stream init, Codex's turn_context); a configured default
never is. Mesh is stubbed at its HTTP boundary (``request``)."""
import unittest
import unittest.mock
from types import SimpleNamespace

from mesh_runner import profile_runtime
from mesh_runner import runner as runner_mod
from mesh_runner.adapters.base import TurnResult
from mesh_runner.mesh import MeshClient, MeshError

AGENT = "33333333-2222-3333-4444-555555555555"


class PatchTest(unittest.TestCase):
    def test_family_change_is_reported_with_the_observed_model(self):
        me = {"agent_type": "codex", "model": "gpt-6.1-sol"}
        self.assertEqual(profile_runtime.profile_patch(me, "claude", "claude-opus-5-5"),
                         {"agent_type": "claude_code", "model": "claude-opus-5-5"})

    def test_only_the_differing_field_is_sent(self):
        me = {"agent_type": "codex", "model": "gpt-6.0"}
        self.assertEqual(profile_runtime.profile_patch(me, "codex", "gpt-6.1-sol"), {"model": "gpt-6.1-sol"})

    def test_matching_profile_costs_nothing(self):
        me = {"agent_type": "claude_code", "model": "claude-opus-5-5"}
        self.assertIsNone(profile_runtime.profile_patch(me, "claude", "claude-opus-5-5"))

    def test_no_observed_model_on_the_same_family_reports_nothing(self):
        me = {"agent_type": "codex", "model": "gpt-6.1-sol"}
        self.assertIsNone(profile_runtime.profile_patch(me, "codex", ""))
        self.assertIsNone(profile_runtime.profile_patch(me, "codex", None))

    def test_no_observed_model_after_a_family_change_clears_the_old_model(self):
        # the harness is a launch fact; the old family's model would be a guess, so it is cleared
        me = {"agent_type": "codex", "model": "gpt-6.1-sol"}
        self.assertEqual(profile_runtime.profile_patch(me, "claude", None),
                         {"agent_type": "claude_code", "model": ""})

    def test_harness_without_a_mesh_type_is_custom(self):
        # Kimi records no model in its stream
        me = {"agent_type": "claude_code", "model": "claude-opus-5-5"}
        self.assertEqual(profile_runtime.profile_patch(me, "kimi", ""), {"agent_type": "custom", "model": ""})
        self.assertEqual(profile_runtime.profile_patch(me, "zcode", "glm-5"), {"agent_type": "custom", "model": "glm-5"})

    def test_overlong_model_is_not_sent(self):
        self.assertIsNone(profile_runtime.profile_patch({}, "claude", "m" * 129))


class ClientTest(unittest.TestCase):
    def test_runtime_goes_by_the_column_scoped_heartbeat_route(self):
        c = MeshClient.__new__(MeshClient)
        calls = []
        c.request = lambda method, path, body=None, **k: calls.append((method, path, body)) or {"status": "ok"}
        c.report_runtime({"agent_type": "codex", "model": "x"})
        self.assertEqual(calls, [("POST", "/agents/heartbeat", {"agent_type": "codex", "model": "x"})])


class AgentSyncTest(unittest.TestCase):
    def runner(self, me, fail=False, routed=""):
        ar = runner_mod.AgentRunner.__new__(runner_mod.AgentRunner)
        ar.agent = SimpleNamespace(name="lane", harness="claude")
        ar.me = dict(me)
        ar._obs_home = {}
        ar.sent = []
        # the route classification taken when the turn launched (_spawn); never re-read at settle
        ar._lane_route = {"attempt-1": not routed}
        ar._task_route = lambda _tid: "" if routed else "codex"     # the card's labels changed since launch

        def report_runtime(fields):
            ar.sent.append(dict(fields))
            if fail:
                raise MeshError(503, "down")
        ar.mesh = SimpleNamespace(report_runtime=report_runtime)
        return ar

    def attempt(self, harness="claude"):
        return SimpleNamespace(id="attempt-1", task_id="t", harness=harness, session_id="thread-1234abcd")

    def test_switched_lane_rewrites_its_directory_entry_once(self):
        ar = self.runner({"id": AGENT, "agent_type": "codex", "model": "gpt-6.1-sol"})
        ar._sync_profile_runtime(self.attempt(), TurnResult(model="claude-opus-5-5"))
        ar._sync_profile_runtime(self.attempt(), TurnResult(model="claude-opus-5-5"))
        self.assertEqual(ar.sent, [{"agent_type": "claude_code", "model": "claude-opus-5-5"}])
        self.assertEqual((ar.me["agent_type"], ar.me["model"]), ("claude_code", "claude-opus-5-5"))

    def test_codex_fallback_reads_the_rollout_turn_context(self):
        # the reviewer lane registered as Claude ran this turn on Codex (quota fallback)
        ar = self.runner({"id": AGENT, "agent_type": "claude_code", "model": "claude-opus-5-5"})
        seen = {}

        def observed(sid, env):
            seen.update(sid=sid, env=env)
            return {"model": "gpt-6.1-sol", "effort": "high", "at": None}
        codex = SimpleNamespace(base=SimpleNamespace(observed_context=observed))
        ar.adapter_for = lambda _h: codex
        ar._obs_home["attempt-1"] = {"CODEX_HOME": "/c"}
        ar._sync_profile_runtime(self.attempt("codex"), TurnResult(session_id="thread-1234abcd"))
        self.assertEqual(ar.sent, [{"agent_type": "codex", "model": "gpt-6.1-sol"}])
        self.assertEqual(seen, {"sid": "thread-1234abcd", "env": {"CODEX_HOME": "/c"}})

    def test_unobserved_turn_of_the_same_family_leaves_the_profile_alone(self):
        ar = self.runner({"id": AGENT, "agent_type": "claude_code", "model": "claude-opus-5-5"})
        ar._sync_profile_runtime(self.attempt(), TurnResult())
        self.assertEqual(ar.sent, [])

    def test_a_card_on_its_own_harness_leaves_the_lane_entry_alone(self):
        # runner-harness:codex label on a Claude lane: one card's route is not the lane's runtime
        ar = self.runner({"id": AGENT, "agent_type": "claude_code", "model": "claude-opus-5-5"}, routed="codex")
        codex = SimpleNamespace(base=SimpleNamespace(observed_context=lambda sid, env: {"model": "gpt-6.1-sol"}))
        ar.adapter_for = lambda _h: codex
        ar._sync_profile_runtime(self.attempt("codex"), TurnResult(session_id="thread-1234abcd"))
        self.assertEqual(ar.sent, [])

    def test_mesh_failure_never_reaches_the_turn_and_is_retried_next_turn(self):
        ar = self.runner({"id": AGENT, "agent_type": "codex", "model": "gpt-6.1-sol"}, fail=True)
        ar._sync_profile_runtime(self.attempt(), TurnResult(model="claude-opus-5-5"))
        ar._sync_profile_runtime(self.attempt(), TurnResult(model="claude-opus-5-5"))
        self.assertEqual(len(ar.sent), 2)
        self.assertEqual(ar.me["agent_type"], "codex")        # the cache claims nothing Mesh did not take

    def test_route_is_the_one_taken_at_launch(self):
        # a runner-harness: label removed mid-turn: the turn still ran on the card's own harness
        ar = self.runner({"id": AGENT, "agent_type": "claude_code", "model": "claude-opus-5-5"}, routed="codex")
        ar._task_route = lambda _tid: ""
        codex = SimpleNamespace(base=SimpleNamespace(observed_context=lambda sid, env: {"model": "gpt-6.1-sol"}))
        ar.adapter_for = lambda _h: codex
        ar._sync_profile_runtime(self.attempt("codex"), TurnResult(session_id="thread-1234abcd"))
        self.assertEqual(ar.sent, [])

    def test_a_turn_this_process_never_launched_reports_nothing(self):
        ar = self.runner({"id": AGENT, "agent_type": "codex", "model": "gpt-6.1-sol"})
        ar._lane_route = {}
        ar._sync_profile_runtime(self.attempt(), TurnResult(model="claude-opus-5-5"))
        self.assertEqual(ar.sent, [])

    def test_apply_env_marks_a_per_card_route(self):
        ar = runner_mod.AgentRunner.__new__(runner_mod.AgentRunner)
        lane = SimpleNamespace(profile="p")
        alt = SimpleNamespace(profile="q", _base_profile="q")
        ar.agent = SimpleNamespace(name="lane", harness="claude", env={})
        ar.alt_adapters = {"codex": alt}
        ar.adapter_for = lambda h: alt if h == "codex" else lane
        ar.pool = SimpleNamespace(env={})
        with unittest.mock.patch.object(runner_mod, "agent_env", lambda agent: {}):
            ar._apply_env({}, task_route="codex")
            self.assertTrue(ar._card_routed)
            ar._apply_env({})
            self.assertFalse(ar._card_routed)

    def test_launch_keeps_the_codex_home_without_an_observation_catalog(self):
        # the reviewer lanes publish no execution observations; the rollout must still be found
        ar = runner_mod.AgentRunner.__new__(runner_mod.AgentRunner)
        ar._obs_home = {}
        ar._keep_launch_home(self.attempt("codex"), {"HOME": "/h", "CODEX_HOME": "/c", "SECRET": "x"})
        self.assertEqual(ar._obs_home["attempt-1"], {"HOME": "/h", "CODEX_HOME": "/c"})


if __name__ == "__main__":
    unittest.main()
