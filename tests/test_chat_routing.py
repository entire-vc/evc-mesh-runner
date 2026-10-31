"""The standing chat routes by its own command's harness and never writes the card routing."""
import sys
import unittest
from unittest import mock

from fakes import FakeMesh, FakeProfileAdapter
from test_runner import Bench

ROUTE = [sys.executable, "-c", "print('MESH_RUNNER_HARNESS=codex'); print('MESH_RUNNER_PROFILE=riker'); "
                              "print('CODEX_HOME=/acct/riker')"]


class ChatRouting(Bench):
    def chat_runner(self, command, base="claude", route=ROUTE):
        self.agent.harness = base
        self.agent.chat = {"session": "lane-chat", "command": command}
        self.agent.env_command = route
        r = self.runner(FakeMesh([]))
        r.alt_adapters = {"codex": FakeProfileAdapter(r.adapter.script, "codex-base")}
        r.chat.state = mock.Mock(return_value="gone")
        r.chat.start = mock.Mock(return_value=True)
        r._chat_due = 0
        return r

    def test_codex_chat_on_a_claude_base_lane_is_created_with_its_routed_env(self):
        r = self.chat_runner(["codex", "--profile", "riker"])
        r.keep_chat()
        r.chat.start.assert_called_once()
        env, digest = r.chat.start.call_args.args
        self.assertEqual(env["CODEX_HOME"], "/acct/riker")
        self.assertNotIn("MESH_RUNNER_HARNESS", env)
        self.assertNotIn("MESH_RUNNER_PROFILE", env)
        self.assertTrue(digest)
        self.assertEqual(r._chat_hold, "")

    def test_chat_routing_leaves_the_card_routing_alone(self):
        r = self.chat_runner(["codex", "--profile", "riker"])
        r._routed_harness, r.pool.env = "claude", {"POOL": "1"}
        agent_env, adapter = dict(self.agent.env), r.adapter_for("codex")
        r.keep_chat()
        self.assertEqual((r._routed_harness, r.pool.env, self.agent.env), ("claude", {"POOL": "1"}, agent_env))
        self.assertEqual(adapter.profile, "codex-base")
        self.assertIsNot(r.env, None)

    def test_a_command_of_another_harness_never_runs_with_a_foreign_env(self):
        r = self.chat_runner(["claude", "--model", "opus"])          # routed to codex, command is claude
        r.keep_chat()
        r.chat.start.assert_not_called()
        self.assertIn("restart pending", r._chat_hold)
        r2 = self.chat_runner(["codex"], route=[sys.executable, "-c", "print('X=1')"])   # routed home: claude
        r2.keep_chat()
        r2.chat.start.assert_not_called()

    def test_a_live_session_is_not_restarted_while_busy(self):
        r = self.chat_runner(["codex", "--profile", "riker"])
        r.chat.state = mock.Mock(return_value="ours")
        r.chat.option = mock.Mock(return_value="stale-digest")
        r.chat.idle = mock.Mock(return_value=False)
        r.chat.stop = mock.Mock()
        r.keep_chat()
        r.chat.stop.assert_not_called()
        r.chat.start.assert_not_called()
        self.assertIn("restart pending", r._chat_hold)

    def test_baseline_claude_chat_on_a_claude_lane_starts(self):
        r = self.chat_runner(["claude"], route=[sys.executable, "-c", "print('CLAUDE_CONFIG_DIR=/acct/a')"])
        r.keep_chat()
        r.chat.start.assert_called_once()
        self.assertEqual(r.chat.start.call_args.args[0]["CLAUDE_CONFIG_DIR"], "/acct/a")

    def test_baseline_codex_lane_without_a_command_binary_name_uses_the_base_harness(self):
        r = self.chat_runner(["/opt/bin/riker-chat"], base="codex")
        r.alt_adapters = {}
        r.keep_chat()
        r.chat.start.assert_called_once()


if __name__ == "__main__":
    unittest.main()
