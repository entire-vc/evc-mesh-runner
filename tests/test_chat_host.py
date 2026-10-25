import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fake_chat_mesh import FakeChatMesh
from mesh_runner import config
from mesh_runner.chat_delivery import window_key
from mesh_runner.chat_host import ChatRefused, make_plan, session_id_for
from mesh_runner.chat_protocol import ChatClient
from mesh_runner.interactive import write_meta


class Stub:
    """An InteractiveAdapter look-alike: start/resume argv of the driver form."""
    session_id_known_upfront = True

    def __init__(self, state):
        self.dir = state / "interactive" / "a"

    def start_argv(self, sid, cwd, dirs):
        return ["python", "driver", "--mode", "start", "--", "claude", "--session-id", sid]

    def resume_argv(self, sid, cwd, dirs):
        return ["python", "driver", "--mode", "resume", "--", "claude", "--resume", sid]

    def with_read_only(self, argv):
        return argv + ["--disallowedTools", "Edit", "Write", "Bash"]


def runner(state, adapter):
    ar = SimpleNamespace(_routed_harness="claude", agent=SimpleNamespace(profile_dir=state, harness="claude"),
                         cfg=SimpleNamespace(state_dir=state), _fresh_env=lambda: {"HOME": "/h"},
                         adapter_for=lambda h: adapter)
    return ar


class Plan(unittest.TestCase):
    def setUp(self):
        self.state = Path(tempfile.mkdtemp())
        self.mesh = FakeChatMesh()
        self.client = ChatClient("https://mesh.test", "secret", opener=self.mesh)
        self.turn = {"turn_id": "t", "thread_id": "th-1", "message": {"body": "hi"}, "settings": {}}

    def test_cold_chat_starts_with_stable_id_and_window_key(self):
        from mesh_runner.interactive import InteractiveAdapter
        ad = Stub(self.state)
        ad.__class__ = type("A", (Stub, InteractiveAdapter), {})      # isinstance(InteractiveAdapter)
        argv, cwd, env, prompt, _harness = make_plan(runner(self.state, ad), self.client)(self.turn)
        self.assertIn(session_id_for("th-1"), argv)
        self.assertEqual(argv[argv.index("--key") + 1], window_key("th-1"))
        self.assertLess(argv.index("--key"), argv.index("--"))
        self.assertIn("MESH_CHAT_TOKEN_FILE", env)
        self.assertEqual(session_id_for("th-1"), session_id_for("th-1"))
        self.assertNotEqual(session_id_for("th-1"), session_id_for("th-2"))

    def test_warm_chat_resumes_by_recorded_session(self):
        from mesh_runner.interactive import InteractiveAdapter
        ad = Stub(self.state)
        ad.__class__ = type("A", (Stub, InteractiveAdapter), {})
        write_meta(ad.dir, window_key("th-1"), {"session_id": "sess-9"})
        argv, *_ = make_plan(runner(self.state, ad), self.client)(self.turn)
        self.assertIn("--resume", argv)
        self.assertIn("sess-9", argv)

    def test_headless_adapter_is_refused_with_stable_code(self):
        with self.assertRaises(ChatRefused) as cm:
            make_plan(runner(self.state, object()), self.client)(self.turn)
        self.assertEqual(cm.exception.code, "headless_forbidden")

    def test_none_and_read_workspace_deny_write_tools_on_start_and_resume(self):
        from mesh_runner.interactive import InteractiveAdapter
        ad = Stub(self.state)
        ad.__class__ = type("A", (Stub, InteractiveAdapter), {})
        for mode in ("none", "read"):
            argv, *_ = make_plan(runner(self.state, ad), self.client, mode)(self.turn)
            self.assertIn("--disallowedTools", argv)
        write_meta(ad.dir, window_key("th-1"), {"session_id": "sess-9"})
        argv, *_ = make_plan(runner(self.state, ad), self.client, "read")(self.turn)
        self.assertIn("--resume", argv)
        self.assertIn("--disallowedTools", argv)

    def test_write_workspace_is_refused(self):
        with self.assertRaises(ChatRefused):
            make_plan(runner(self.state, Stub(self.state)), self.client, "write")(self.turn)


class Cfg(unittest.TestCase):
    def load(self, extra):
        d = Path(tempfile.mkdtemp())
        p = d / "runner.toml"
        (d / "cred").write_text("c-secret")
        (d / "cred").chmod(0o600)
        p.write_text(f'[runner]\nmesh_url = "http://mesh"\n{extra.replace("CRED", str(d / "cred"))}\n')
        return config.load(p)

    def test_off_by_default_and_parsed_when_on(self):
        self.assertFalse(self.load("").chat_enabled)
        c = self.load('[chat]\nenabled = true\nagents = ["a"]\ncredential_file = "CRED"\nmax_chat_sessions = 2\n')
        self.assertEqual((c.chat_enabled, c.chat_agents, c.chat_max_per_agent), (True, ("a",), 2))
        self.assertNotIn("c-secret", repr(c))

    def test_write_workspace_rejected(self):
        with self.assertRaises(config.ConfigError):
            self.load('[chat]\nchat_workspace = "write"\n')


if __name__ == "__main__":
    unittest.main()
