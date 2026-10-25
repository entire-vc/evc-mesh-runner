import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from mesh_runner import chat as chat_mod
from mesh_runner.chat import ChatConfigError, ChatKeeper, ChatSpec, env_digest

TMUX = shutil.which("tmux")


@unittest.skipUnless(TMUX, "tmux not installed")
class Chat(unittest.TestCase):
    """Runs on a private tmux server (-L): never touches the host's sessions."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.sock = f"mr-test-{os.getpid()}"
        self.tmux = [TMUX, "-L", self.sock]
        # a server holding a value that must NOT reach the agent's session
        subprocess.run([*self.tmux, "-f", "/dev/null", "new-session", "-d", "-s", "keepalive", "sleep 600"],
                       env={**os.environ, "SERVER_SECRET": "leak", "CLAUDE_CONFIG_DIR": "/server/acct"},
                       check=True, capture_output=True)
        self.out = self.root / "seen"
        self.idle = self.root / "idle"
        script = (f'echo "$LANE_ACCOUNT ${{SERVER_SECRET:-none}} ${{CLAUDE_CONFIG_DIR:-unset}}" >> {self.out}; '
                  "sleep 600")
        self.spec = ChatSpec(session="agentchat", command=["/bin/sh", "-c", script],
                             idle_command=["/bin/sh", "-c", f"test -f {self.idle}"], tmux=self.tmux)
        self.clock = [1000.0]
        self.keeper = ChatKeeper(self.spec, "agent", self.root, self.root / "state", clock=lambda: self.clock[0])
        self.env = {"PATH": os.environ["PATH"], "CLAUDE_CONFIG_DIR": "/acct/a", "LANE_ACCOUNT": "a"}

    def tearDown(self):
        subprocess.run([*self.tmux, "kill-server"], capture_output=True)
        self.tmp.cleanup()

    def env_of(self, env):
        return lambda: (env, env_digest(env))

    def seen(self, n):
        for _ in range(60):
            if self.out.exists() and len(self.out.read_text().splitlines()) >= n:
                return self.out.read_text().splitlines()
            time.sleep(0.1)
        self.fail("session did not run")

    def test_clean_environment_owner_marks_and_idle_only_restart(self):
        self.assertEqual(self.keeper.check(self.env_of(self.env)), "started")
        self.assertEqual(self.seen(1), ["a none /acct/a"])            # no server leak, own account
        self.assertFalse((self.root / "state" / "chat" / "agent.env").exists())
        self.assertEqual(self.keeper.option("@mr_owner"), "agent")
        moved = dict(self.env, CLAUDE_CONFIG_DIR="/acct/b", LANE_ACCOUNT="b")
        self.keeper._compared_at = -1e18
        self.assertEqual(self.keeper.check(self.env_of(moved)), "held")   # busy: not interrupted
        self.idle.write_text("1")
        self.assertEqual(self.keeper.check(self.env_of(moved)), "restarted")
        self.assertEqual(self.seen(2)[-1], "b none /acct/b")

    def test_a_restarted_runner_reads_the_session_facts(self):
        self.keeper.check(self.env_of(self.env))
        fresh = ChatKeeper(self.spec, "agent", self.root, self.root / "state", clock=lambda: self.clock[0])
        self.assertEqual(fresh.check(self.env_of(self.env)), "kept")   # same routing: recorded digest matches
        moved = dict(self.env, CLAUDE_CONFIG_DIR="/acct/b")
        again = ChatKeeper(self.spec, "agent", self.root, self.root / "state", clock=lambda: self.clock[0])
        self.assertEqual(again.check(self.env_of(moved)), "held")       # routing changed while it was down

    def test_foreign_session_is_left_alone_unless_adopt(self):
        subprocess.run([*self.tmux, "new-session", "-d", "-s", "agentchat", "sleep 600"], check=True)
        self.assertEqual(self.keeper.check(self.env_of(self.env)), "foreign")
        self.assertIn("not the runner's", self.keeper.error)
        self.spec.adopt = True
        self.idle.write_text("1")
        self.assertEqual(self.keeper.check(self.env_of(self.env)), "restarted")
        self.assertEqual(self.keeper.option("@mr_owner"), "agent")

    def test_crash_loop_backs_off(self):
        spec = ChatSpec(session="dies", command=["/bin/sh", "-c", "exit 3"], tmux=self.tmux)
        k = ChatKeeper(spec, "agent", self.root, self.root / "state", clock=lambda: self.clock[0])
        self.assertEqual(k.check(self.env_of(self.env)), "started")
        time.sleep(0.5)
        self.clock[0] += 10
        self.assertEqual(k.check(self.env_of(self.env)), "backoff")
        self.assertIn("keeps dying", k.error)
        self.clock[0] += chat_mod.CRASH_BACKOFF + 1
        self.assertEqual(k.check(self.env_of(self.env)), "started")

    def test_names_are_validated(self):
        with self.assertRaises(ChatConfigError):
            ChatSpec(session="a.b", command=["x"])

    def test_digest_ignores_values_that_do_not_route(self):
        self.assertEqual(env_digest({"A": "1", "CLAUDE_CONFIG_DIR": "x"}), env_digest({"A": "2", "CLAUDE_CONFIG_DIR": "x"}))


if __name__ == "__main__":
    unittest.main()
