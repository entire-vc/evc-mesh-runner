"""The standing chat routes by its own command's harness and never writes the card routing."""
import sys
import time
import unittest
from unittest import mock

from fakes import FakeMesh, FakeProfileAdapter
from test_runner import Bench

ROUTE = [sys.executable, "-c", "print('MESH_RUNNER_HARNESS=codex'); print('MESH_RUNNER_PROFILE=steward'); "
                              "print('CODEX_HOME=/acct/steward')"]


class ChatRouting(Bench):
    def chat_runner(self, command, base="claude", route=ROUTE, commands=None):
        self.agent.harness = base
        self.agent.chat = {"session": "lane-chat", "command": command}
        if commands:
            self.agent.chat["commands"] = commands
        self.agent.env_command = route
        r = self.runner(FakeMesh([]))
        r.alt_adapters = {"codex": FakeProfileAdapter(r.adapter.script, "codex-base")}
        r.chat.state = mock.Mock(return_value="gone")
        r.chat.start = mock.Mock(return_value=True)
        r._chat_due = 0
        return r

    def test_codex_chat_on_a_claude_base_lane_is_created_with_its_routed_env(self):
        r = self.chat_runner(["codex", "--profile", "steward"])
        r.keep_chat()
        r.chat.start.assert_called_once()
        env, digest = r.chat.start.call_args.args[:2]
        self.assertEqual(env["CODEX_HOME"], "/acct/steward")
        self.assertNotIn("MESH_RUNNER_HARNESS", env)
        self.assertNotIn("MESH_RUNNER_PROFILE", env)
        self.assertTrue(digest)
        self.assertEqual(r._chat_hold, "")

    def test_chat_routing_leaves_the_card_routing_alone(self):
        r = self.chat_runner(["codex", "--profile", "steward"])
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
        r = self.chat_runner(["codex", "--profile", "steward"])
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
        r = self.chat_runner(["/opt/bin/steward-chat"], base="codex")
        r.alt_adapters = {}
        r.keep_chat()
        r.chat.start.assert_called_once()

    # a gone chat routed to another harness used to be held silently for hours
    def test_a_gone_chat_routed_to_another_harness_starts_with_that_harness_command(self):
        r = self.chat_runner(["claude", "--model", "opus"], commands={"codex": ["codex", "--profile", "steward"]})
        r.keep_chat()
        r.chat.start.assert_called_once()
        env, digest, command = r.chat.start.call_args.args
        self.assertEqual(command, ["codex", "--profile", "steward"])
        self.assertEqual(env["CODEX_HOME"], "/acct/steward")
        self.assertEqual(r._chat_hold, "")

    def test_an_alt_harness_session_returns_home_when_the_route_does(self):
        claude_route = [sys.executable, "-c", "print('CLAUDE_CONFIG_DIR=/acct/a')"]
        r = self.chat_runner(["claude"], commands={"codex": ["codex"]})
        r.keep_chat()                                             # routed to codex: started on codex
        alt_digest = r.chat.start.call_args.args[1]
        self.agent.env_command = claude_route
        r2 = self.chat_runner(["claude"], route=claude_route, commands={"codex": ["codex"]})
        r2.keep_chat()                                            # routed home: the home command
        home_digest = r2.chat.start.call_args.args[1]
        self.assertEqual(r2.chat.start.call_args.args[2], ["claude"])
        self.assertNotEqual(alt_digest, home_digest)              # a live alt session is restarted (when idle)

    def test_a_held_chat_is_logged_and_exported(self):
        r = self.chat_runner(["claude", "--model", "opus"])       # routed to codex, no codex command
        with self.assertLogs("mesh_runner", "WARNING") as logs:
            r.keep_chat()
        self.assertTrue(any("held" in m for m in logs.output))
        self.assertGreater(r.status()["chat_hold_since"], 0)

    def live_runner(self, command, commands=None, recorded="home-digest"):
        r = self.chat_runner(command, commands=commands)
        r.chat.state = mock.Mock(return_value="ours")
        r.chat.option = mock.Mock(side_effect=lambda name: {"@mr_digest": recorded, "@mr_started": "0"}.get(name, ""))
        r.chat.stop = mock.Mock()
        return r

    # a live session routed to a harness it has no command for was reported "kept": no hold, no alert
    def test_a_live_chat_routed_to_a_harness_without_a_command_is_held_visibly_and_left_alone(self):
        r = self.live_runner(["claude", "--model", "opus"])         # routed to codex, no codex command
        r.chat.clock = lambda: time.time() - 120
        route_env = r._route_env
        r._route_env = mock.Mock(side_effect=route_env)
        with self.assertLogs("mesh_runner", "WARNING") as logs:
            r.keep_chat()
            r._chat_due = 0
            r.keep_chat()                                            # same episode, next minute
        self.assertEqual(sum("held" in m for m in logs.output), 1)
        self.assertEqual(r._route_env.call_count, 1)                 # a route hold re-reads the route every 5 min
        r.chat.stop.assert_not_called()
        r.chat.start.assert_not_called()
        self.assertIn("no chat command", r._chat_hold)
        st = r.status()
        self.assertGreater(st["chat_hold_since"], 0)
        from mesh_runner.runner import Runner as R
        from mesh_runner.store import Store
        self.cfg.metrics_file = self.root / "runner.prom"
        wrapper = R.__new__(R)
        wrapper.cfg, wrapper.store, wrapper._lock = self.cfg, Store(self.root / "state" / "r.sqlite"), None
        wrapper.agents = [r]
        wrapper.write_status([st])
        line = next(x for x in self.cfg.metrics_file.read_text().splitlines()
                    if x.startswith('mesh_runner_chat_hold_seconds{agent="builder"}'))
        self.assertGreaterEqual(int(line.split()[-1]), 120)

    def test_a_live_home_session_switches_to_the_routed_harness_command_when_idle(self):
        r = self.live_runner(["claude"], commands={"codex": ["codex", "--profile", "steward"]})
        r.chat.idle = mock.Mock(return_value=True)
        r.keep_chat()
        r.chat.stop.assert_called_once()
        r.chat.start.assert_called_once()
        self.assertEqual(r.chat.start.call_args.args[2], ["codex", "--profile", "steward"])
        self.assertEqual((r._chat_hold, r.status()["chat_hold_since"]), ("", 0))

    def test_a_live_chat_whose_route_gains_a_command_leaves_the_hold(self):
        r = self.live_runner(["claude"])
        r.keep_chat()
        self.assertTrue(r.chat.held_since)
        r._chat_commands["codex"] = ["codex"]
        r.chat._compared_at = -1e18                                  # the next route check is due
        r.chat.idle = mock.Mock(return_value=True)
        r._chat_due = 0
        r.keep_chat()
        self.assertEqual(r.chat.start.call_args.args[2], ["codex"])
        self.assertEqual((r.chat.held_since, r._chat_hold), (None, ""))

    # the chat was checked only as the tick's last step, so a session that died in a
    # 12-repository start (up to 14 min) or a 35-minute tick stayed down until the tick ended
    def test_a_gone_chat_is_started_between_the_steps_of_a_long_tick(self):
        r = self.chat_runner(["codex", "--profile", "steward"])
        r._between(step=True)
        r.chat.start.assert_called_once()

    def test_a_gone_chat_is_started_between_the_repositories_of_one_start(self):
        r = self.chat_runner(["codex", "--profile", "steward"])
        r._between_repos()
        r.chat.start.assert_called_once()

    def test_the_chat_check_between_steps_keeps_its_once_a_minute_pace(self):
        r = self.chat_runner(["codex", "--profile", "steward"])
        r._between(step=True)
        r._between()
        r._between_repos()
        r.chat.start.assert_called_once()                        # the next check is a minute away

    def test_a_failing_chat_check_between_steps_does_not_fail_the_start(self):
        r = self.chat_runner(["codex", "--profile", "steward"])
        r.chat.check = mock.Mock(side_effect=ValueError("broken"))
        with self.assertLogs("mesh_runner", "WARNING"):
            r._between_repos()
        self.assertIn("broken", r._chat_error)


if __name__ == "__main__":
    unittest.main()
