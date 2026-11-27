"""A standing chat held for a restart (busy, or its idle probe says no) does not rebuild the agent's
environment (env command: secrets from the keep, tool config) on every check; its hold outlives a runner restart and a
hold older than 30 minutes is one operator alert."""
import json
import sys
import time
import unittest
from unittest import mock

from fakes import FakeMesh
from test_runner import Bench

from mesh_runner import chat as chat_mod
from mesh_runner.chat import ChatKeeper, ChatSpec

CHECK = 60.0                                   # CHAT_CHECK_SECONDS


class Keeper(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.clock = [100_000.0]
        self.spec = ChatSpec(session="leadchat", command=["claude"], idle_command=["probe"])

    def keeper(self, busy=True):
        k = ChatKeeper(self.spec, "lead", self.root, self.root / "state", clock=lambda: self.clock[0])
        k.state = mock.Mock(return_value="ours")
        k.option = mock.Mock(side_effect=lambda n: {"@mr_digest": "old-route", "@mr_started": "0"}.get(n, ""))
        k.idle = mock.Mock(return_value=not busy)
        k.stop, k.start = mock.Mock(), mock.Mock(return_value=True)
        return k

    def env_of(self, builds):
        def build():
            builds.append(self.clock[0])
            return {"CLAUDE_CONFIG_DIR": "/acct/ops-team"}, "new-route"
        return build

    def test_held_busy_chat_builds_the_env_at_most_once_per_compare_window(self):
        k, builds = self.keeper(busy=True), []
        for _ in range(10):
            self.assertEqual(k.check(self.env_of(builds)), "held")
            self.clock[0] += CHECK
        self.assertGreaterEqual(k.idle.call_count, 8)              # the cheap probe runs each check
        for a, b in zip(builds, builds[1:]):
            self.assertGreaterEqual(b - a, chat_mod.COMPARE_EVERY)
        self.assertLessEqual(len(builds), 2)                        # 10 checks over 9 min: 2 windows
        k.start.assert_not_called()

    def test_an_idle_moment_builds_the_env_and_restarts_at_once(self):
        k, builds = self.keeper(busy=True), []
        k.check(self.env_of(builds))
        self.clock[0] += CHECK
        k.idle.return_value = True
        self.assertEqual(k.check(self.env_of(builds)), "restarted")
        self.assertEqual(len(builds), 2)
        self.assertIsNone(k.held_since)
        self.assertFalse(k.hold_file.exists())

    def test_a_failing_env_build_is_not_retried_every_minute(self):
        k, builds = self.keeper(busy=True), []
        k.check(self.env_of(builds))                                # held
        k.idle.return_value = True
        calls = []

        def broken():
            calls.append(self.clock[0])
            raise RuntimeError("env command timed out")
        for _ in range(4):
            self.clock[0] += CHECK
            try:
                k.check(broken)
            except RuntimeError:
                pass
        self.assertEqual(len(calls), 1)

    def test_the_hold_survives_a_runner_restart(self):
        k = self.keeper()
        k.check(self.env_of([]))
        since = k.held_since
        self.clock[0] += 3 * 3600                                   # the runner restarts hours later
        again = self.keeper()
        self.assertEqual(again.held_since, since)
        self.assertEqual(again.check(self.env_of([])), "held")
        self.assertEqual(again.held_since, since)                   # the episode keeps its start
        self.assertTrue(again.hold_overdue())


class Alert(Bench):
    def alerts(self, r):
        path = r.cfg.state_dir / "events.jsonl"
        rows = [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []
        return [e for e in rows if e["kind"] == "operator_alert"]

    def chat_runner(self, clock):
        self.agent.chat = {"session": "lane-chat", "command": ["claude"], "idle_command": ["probe"]}
        self.agent.env_command = [sys.executable, "-c", "print('CLAUDE_CONFIG_DIR=/acct/b')"]
        r = self.runner(FakeMesh([]))
        r.chat.clock = lambda: clock[0]
        r.chat.state = mock.Mock(return_value="ours")
        r.chat.option = mock.Mock(side_effect=lambda n: {"@mr_digest": "old", "@mr_started": "0"}.get(n, ""))
        r.chat.idle = mock.Mock(return_value=False)
        r.chat.start, r.chat.stop = mock.Mock(return_value=True), mock.Mock()
        r._chat_due = 0
        return r

    def test_a_hold_over_thirty_minutes_across_a_restart_alerts_once(self):
        clock = [time.time() - 3600]
        r = self.chat_runner(clock)
        r.keep_chat()
        since = r.chat.held_since
        self.assertIsNotNone(since)
        self.assertEqual(self.alerts(r), [])
        clock[0] += 20 * 60
        r2 = self.chat_runner(clock)                               # runner restart: the hold is read back
        self.assertEqual(r2.chat.held_since, since)
        r2.keep_chat()
        self.assertEqual(self.alerts(r2), [])                       # 20 min: no alert yet
        clock[0] += 11 * 60
        r2._chat_due = 0
        r2.keep_chat()
        r2._chat_due = 0
        r2.keep_chat()
        alerts = self.alerts(r2)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["alert"], "chat_hold:builder")
        self.assertIn("31 min", alerts[0]["text"])
        self.assertIn(time.strftime("%Y-%m-%d", time.localtime(since)), r2.status()["chat_hold"])

    def test_env_reason_reaches_the_env_command(self):
        clock = [time.time()]
        r = self.chat_runner(clock)
        out = self.root / "reason"
        r.agent.env_command = [sys.executable, "-c",
                               f"import os;open({str(out)!r},'a').write(os.environ.get('MESH_RUNNER_ENV_REASON','-')+'\\n');"
                               "print('CLAUDE_CONFIG_DIR=/acct/b')"]
        r.keep_chat()
        env = r._fresh_env("t" * 36, reason="resume")
        self.assertEqual(out.read_text().split(), ["chat-check", "resume"])
        self.assertNotIn("MESH_RUNNER_ENV_REASON", env)
