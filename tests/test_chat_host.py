import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fake_chat_mesh import FakeChatMesh
from mesh_runner import config
from mesh_runner.chat_delivery import window_key
from mesh_runner.chat_host import ChatHost, ChatRefused, make_plan, session_id_for
from mesh_runner.chat_protocol import ChatClient
from mesh_runner.interactive import write_meta


class Stub:
    """An InteractiveAdapter look-alike: start/resume argv of the driver form."""
    session_id_known_upfront = True

    def __init__(self, state):
        self.dir = state / "interactive" / "a"
        self.seen, self.cwds = [], []                   # extra directories and cwd of each launch

    def start_argv(self, sid, cwd, dirs):
        self.seen.append(list(dirs))
        self.cwds.append(cwd)
        return ["python", "driver", "--mode", "start", "--", "claude", "--session-id", sid]

    def resume_argv(self, sid, cwd, dirs):
        return ["python", "driver", "--mode", "resume", "--", "claude", "--resume", sid]

    def with_read_only(self, argv):
        return argv + ["--disallowedTools", "Edit", "Write", "Bash"]

    def with_slot_confined(self, argv):
        return argv + ["--confined"]

    def without_local_memory(self, env):
        return env

    def prepare_resume(self, sid, cwd, env):
        return ""

    def session_known(self, sid, cwd, env):
        return True


def runner(state, adapter):
    ar = SimpleNamespace(_routed_harness="claude", agent=SimpleNamespace(profile_dir=state, harness="claude"),
                         cfg=SimpleNamespace(state_dir=state), _read_env=lambda: {"HOME": "/h"}, _route_env=lambda raw: (dict(raw), "claude", None),
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

    def adapter(self):
        from mesh_runner.interactive import InteractiveAdapter
        ad = Stub(self.state)
        ad.__class__ = type("A", (Stub, InteractiveAdapter), {})
        return ad

    def write_runner(self, pool, repos=("r",)):
        ar = runner(self.state, self.adapter())
        ar.agent.repos = [SimpleNamespace(name=n) for n in repos]
        ar.pool = pool
        return ar

    def test_write_workspace_leases_a_slot_and_keeps_write_tools(self):
        pool = FakePool(self.state)
        ar = self.write_runner(pool)
        ad = ar.adapter_for(None)
        argv, plan_cwd, *_ = make_plan(ar, self.client, "write")(self.turn)
        self.assertNotIn("--disallowedTools", argv)         # no read-only denial ...
        self.assertIn("--confined", argv)                   # ... but the harness confines writes to the slot
        self.assertEqual(ad.seen, [[]])
        self.assertEqual(ad.cwds, [pool.path])                  # in the slot, never in the profile directory
        self.assertEqual(plan_cwd, pool.path)
        self.assertEqual(pool.leases, [(window_key("th-1"), "r")])
        make_plan(ar, self.client, "write")(self.turn)          # next turn of the same chat: same slot
        self.assertEqual(len(pool.leases), 1)

    def test_write_workspace_of_an_unconfinable_harness_is_refused_and_returns_the_slot(self):
        pool = FakePool(self.state)
        ar = self.write_runner(pool)
        ad = ar.adapter_for(None)
        ad.with_slot_confined = lambda argv: (_ for _ in ()).throw(NotImplementedError("no sandbox"))
        with self.assertRaises(ChatRefused) as cm:
            make_plan(ar, self.client, "write")(self.turn)
        self.assertEqual(cm.exception.code, "chat_workspace_unconfinable")
        self.assertEqual(pool.released, [window_key("th-1")])

    def test_unconfinable_refusal_keeps_a_slot_the_chat_already_held(self):
        pool = FakePool(self.state)
        pool.held[window_key("th-1")] = pool.path           # an earlier turn's slot, its window may be live
        ar = self.write_runner(pool)
        ar.adapter_for(None).with_slot_confined = lambda argv: (_ for _ in ()).throw(NotImplementedError("x"))
        with self.assertRaises(ChatRefused):
            make_plan(ar, self.client, "write")(self.turn)
        self.assertEqual(pool.released, [])

    def test_write_workspace_without_free_slot_is_refused_with_stable_code(self):
        pool = FakePool(self.state, full=True)
        with self.assertRaises(ChatRefused) as cm:
            make_plan(self.write_runner(pool), self.client, "write")(self.turn)
        self.assertEqual(cm.exception.code, "chat_workspace_busy")

    def test_write_workspace_without_repository_is_refused(self):
        with self.assertRaises(ChatRefused) as cm:
            make_plan(self.write_runner(FakePool(self.state), repos=()), self.client, "write")(self.turn)
        self.assertEqual(cm.exception.code, "chat_workspace_no_repo")

    def test_read_workspace_takes_no_slot(self):
        pool = FakePool(self.state)
        make_plan(self.write_runner(pool), self.client, "read")(self.turn)
        self.assertEqual(pool.leases, [])


class Confinement(unittest.TestCase):
    def test_codex_runs_in_its_sandbox_and_claude_is_unconfinable(self):
        from mesh_runner.adapters.claude import ClaudeAdapter
        from mesh_runner.adapters.codex import CodexAdapter
        from mesh_runner.interactive import wrap
        codex = wrap(CodexAdapter(profile="p"), "a", "interactive", Path(tempfile.mkdtemp()), "tmux", 4)
        tail = codex.with_slot_confined(codex.start_argv("s", Path("/slot"), []))
        tail = tail[tail.index("--") + 1:]
        self.assertEqual(tail[tail.index("--sandbox") + 1], "workspace-write")
        extra = wrap(CodexAdapter(profile="p", extra_args=["--add-dir", "/profile", "--add-dir=/other"]), "a",
                     "interactive", Path(tempfile.mkdtemp()), "tmux", 4)
        tail = extra.with_slot_confined(extra.start_argv("s", Path("/slot"), [Path("/more")]))
        tail = tail[tail.index("--") + 1:]
        self.assertNotIn("--add-dir", tail)                         # no other writable root survives
        self.assertNotIn("--add-dir=/other", tail)
        self.assertNotIn("/profile", tail)
        self.assertIn("sandbox_workspace_write.writable_roots=[]", tail)
        claude = wrap(ClaudeAdapter(), "a", "interactive", Path(tempfile.mkdtemp()), "tmux", 4)
        with self.assertRaises(NotImplementedError):
            claude.with_slot_confined(claude.start_argv("s", Path("/slot"), []))


class FakePool:
    def __init__(self, state, full=False):
        self.path, self.full, self.leases, self.held, self.released = state / "slot-1", full, [], {}, []

    def leased_path(self, attempt):
        return self.held.get(attempt)

    def lease(self, attempt, repo):
        from mesh_runner.workspace import WorkspaceError
        if self.full:
            raise WorkspaceError("no free worktree for r (max 1)")
        self.leases.append((attempt, repo))
        self.held[attempt] = self.path
        return self.path

    def release(self, attempt):
        self.released.append(attempt)
        self.held.pop(attempt, None)
        return []


class LostTranscript(unittest.TestCase):
    """A chat whose transcript was removed from the machine continues in a new session fed from Mesh."""

    def setUp(self):
        self.state = Path(tempfile.mkdtemp())
        self.home = Path(tempfile.mkdtemp())
        self.mesh = FakeChatMesh()
        self.mesh.history["th-1"] = [{"role": "user", "body": "the code word is heron-417"},
                                     {"role": "assistant", "body": "Noted: heron-417."}]
        self.client = ChatClient("https://mesh.test", "secret", opener=self.mesh)
        self.turn = {"turn_id": "t", "thread_id": "th-1", "message": {"body": "what is the code word?"},
                     "settings": {}}

    def plan(self, base, sid):
        from mesh_runner.interactive import wrap
        ad = wrap(base, "a", "interactive", self.state, "tmux", 4)
        write_meta(ad.dir, window_key("th-1"), {"session_id": sid, "harness": base.name})
        ar = runner(self.state, ad)
        ar._routed_harness = base.name
        ar._read_env = ar._fresh_env = lambda: {"HOME": str(self.home)}
        return make_plan(ar, self.client, "read")(self.turn)

    def history_calls(self):
        return [r for r in self.mesh.requests if r == ("GET", "/threads/th-1/history")]

    def test_claude_without_transcript_starts_anew_with_the_history(self):
        from mesh_runner.adapters.claude import ClaudeAdapter
        sid = session_id_for("th-1")
        with self.assertLogs("mesh_runner", "INFO") as logs:
            argv, _, _, prompt, _ = self.plan(ClaudeAdapter(), sid)
        tail = argv[argv.index("--") + 1:]
        self.assertNotIn("--resume", tail)
        self.assertNotIn(sid, tail)                          # a new id: the old one may still have side data
        self.assertIn("--session-id", tail)
        self.assertEqual(len(self.history_calls()), 1)
        self.assertIn("heron-417", prompt)
        self.assertLess(prompt.index("heron-417"), prompt.index("what is the code word?"))
        text = "\n".join(logs.output)
        self.assertIn("has no transcript on this machine", text)
        self.assertIn("history (limit=20, before=-) gave 2 messages", text)

    def test_a_failed_history_call_logs_one_line_without_the_server_text(self):
        from mesh_runner.adapters.claude import ClaudeAdapter
        self.mesh.history.clear()
        self.mesh.fail_next.append((500, "boom\nFORGED entry"))
        with self.assertLogs("mesh_runner", "WARNING") as logs:
            _, _, _, prompt, _ = self.plan(ClaudeAdapter(), session_id_for("th-1"))
        line = next(o for o in logs.output if "history" in o)
        self.assertNotIn("\n", line)
        self.assertIn("failed: 500 'boom\\nFORGED entry'", line)
        self.assertEqual(prompt.split("\n\n")[-1], "what is the code word?")

    def test_claude_with_transcript_resumes_without_history(self):
        from mesh_runner.adapters.claude import ClaudeAdapter
        sid = session_id_for("th-1")
        proj = self.home / ".claude" / "projects" / "-p"
        proj.mkdir(parents=True)
        (proj / f"{sid}.jsonl").write_text("{}\n")
        argv, _, _, prompt, _ = self.plan(ClaudeAdapter(), sid)
        tail = argv[argv.index("--") + 1:]
        self.assertEqual(tail[tail.index("--resume") + 1], sid)
        self.assertEqual(self.history_calls(), [])
        self.assertNotIn("heron-417", prompt)

    def test_codex_without_rollout_starts_anew_with_the_history(self):
        from mesh_runner.adapters.codex import CodexAdapter
        (self.home / ".codex" / "sessions" / "2026" / "10" / "29").mkdir(parents=True)
        argv, _, _, prompt, _ = self.plan(CodexAdapter(), "0199aaaa-bbbb-7ccc-8ddd-eeeeffff0000")
        tail = argv[argv.index("--") + 1:]
        self.assertNotIn("resume", tail)
        self.assertEqual(len(self.history_calls()), 1)
        self.assertIn("heron-417", prompt)

    def test_codex_with_rollout_resumes(self):
        from mesh_runner.adapters.codex import CodexAdapter
        thread = "0199aaaa-bbbb-7ccc-8ddd-eeeeffff0000"
        day = self.home / ".codex" / "sessions" / "2026" / "10" / "29"
        day.mkdir(parents=True)
        (day / f"rollout-2026-10-29T08-00-00-{thread}.jsonl").write_text("{}\n")
        argv, *_ = self.plan(CodexAdapter(), thread)
        tail = argv[argv.index("--") + 1:]
        self.assertEqual(tail[tail.index("resume") + 1], thread)
        self.assertEqual(self.history_calls(), [])


class ChatMcp(unittest.TestCase):
    """The chat session's Mesh MCP runs with the thread's token, never the agent key (real adapters)."""

    def setUp(self):
        self.state = Path(tempfile.mkdtemp())
        self.client = ChatClient("https://mesh.test", "secret", opener=FakeChatMesh())
        self.turn = {"turn_id": "t", "thread_id": "th-1", "message": {"body": "hi"}, "settings": {}}
        from mesh_runner.chat_machine import chat_dir
        self.token = str(chat_dir(self.state, "th-1") / "session-token")

    def plan(self, base, command=("/opt/evc-mesh-mcp", "--transport", "stdio")):
        from mesh_runner.interactive import wrap
        ad = wrap(base, "a", "interactive", self.state, "tmux", 4)
        ar = runner(self.state, ad)
        ar._routed_harness = base.name
        ar._fresh_env = lambda: {"HOME": "/h", "MESH_AGENT_KEY": "agent-secret"}
        ar.cfg = SimpleNamespace(state_dir=self.state, mesh_url="https://mesh.test",
                                 chat_mcp_command=tuple(command), chat_mcp_url="")
        return make_plan(ar, self.client, "read")(self.turn)

    def test_claude_gets_only_the_chat_token_server(self):
        import json
        from mesh_runner.adapters.claude import ClaudeAdapter
        argv, _, env, *_ = self.plan(ClaudeAdapter())
        tail = argv[argv.index("--") + 1:]
        self.assertIn("--strict-mcp-config", tail)
        server = json.loads(tail[tail.index("--mcp-config") + 1])["mcpServers"]["evc-mesh"]
        self.assertEqual((server["command"], server["args"]), ("/opt/evc-mesh-mcp", ["--transport", "stdio"]))
        self.assertEqual(server["env"], {"MESH_API_URL": "https://mesh.test", "MESH_CHAT_TOKEN_FILE": self.token})
        self.assertEqual(env["MESH_CHAT_TOKEN_FILE"], self.token)
        self.assertNotIn("MESH_AGENT_KEY", env)
        self.assertNotIn("agent-secret", " ".join(argv))
        self.assertIn("--disallowedTools", tail)            # read-only still applies

    def test_codex_overrides_the_evc_mesh_entry(self):
        import tomllib
        from mesh_runner.adapters.codex import CodexAdapter
        argv, _, env, *_ = self.plan(CodexAdapter(profile="p"))
        tail = argv[argv.index("--") + 1:]
        server = {}
        for i, a in enumerate(tail):
            if a == "-c":
                server.update(tomllib.loads(tail[i + 1]).get("mcp_servers", {}).get("evc-mesh", {}))
        self.assertEqual((server["command"], server["args"]), ("/opt/evc-mesh-mcp", ["--transport", "stdio"]))
        self.assertEqual(server["env"], {"MESH_API_URL": "https://mesh.test", "MESH_CHAT_TOKEN_FILE": self.token})
        self.assertNotIn("MESH_AGENT_KEY", env)
        self.assertNotIn("agent-secret", " ".join(argv))
        self.assertLess(tail.index("-c"), tail.index("--sandbox"))

    def test_claude_chat_has_no_local_memory_and_codex_env_is_unchanged(self):
        # with auto-memory on, "remember this" went to a memory file (denied read-only) and never to Mesh
        from mesh_runner.adapters.claude import ClaudeAdapter
        from mesh_runner.adapters.codex import CodexAdapter
        _, _, env, *_ = self.plan(ClaudeAdapter())
        self.assertEqual(env.get("CLAUDE_CODE_DISABLE_AUTO_MEMORY"), "1")
        _, _, env, *_ = self.plan(ClaudeAdapter(), command=())
        self.assertEqual(env.get("CLAUDE_CODE_DISABLE_AUTO_MEMORY"), "1")
        _, _, env, *_ = self.plan(CodexAdapter(profile="p"))
        self.assertNotIn("CLAUDE_CODE_DISABLE_AUTO_MEMORY", env)

    def test_memory_note_names_the_remember_tool_for_claude_and_codex(self):
        import tomllib
        from mesh_runner.adapters.claude import ClaudeAdapter
        from mesh_runner.adapters.codex import CodexAdapter
        from mesh_runner.chat_host import MEMORY_NOTE
        self.assertIn("remember", MEMORY_NOTE)
        self.assertIn("evc-mesh", MEMORY_NOTE)
        argv, *_ = self.plan(ClaudeAdapter())
        tail = argv[argv.index("--") + 1:]
        self.assertEqual(tail[tail.index("--append-system-prompt") + 1], MEMORY_NOTE)
        argv, *_ = self.plan(CodexAdapter(profile="p"))
        tail = argv[argv.index("--") + 1:]
        notes = [tomllib.loads(tail[i + 1]).get("developer_instructions") for i, a in enumerate(tail)
                 if a == "-c" and tail[i + 1].startswith("developer_instructions=")]
        self.assertEqual(notes, [MEMORY_NOTE])
        argv, *_ = self.plan(ClaudeAdapter(), command=())     # no server injected: nothing to point at
        self.assertNotIn("--append-system-prompt", argv)

    def test_unconfigured_leaves_the_command_line_alone(self):
        from mesh_runner.adapters.claude import ClaudeAdapter
        argv, *_ = self.plan(ClaudeAdapter(), command=())
        self.assertNotIn("--mcp-config", argv)


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

    def test_mcp_command_parsed(self):
        c = self.load('[chat]\nmcp_command = ["/b/mesh-mcp", "--transport", "stdio"]\nmcp_url = "https://m"\n')
        self.assertEqual((c.chat_mcp_command, c.chat_mcp_url), (("/b/mesh-mcp", "--transport", "stdio"), "https://m"))
        self.assertEqual(self.load("").chat_mcp_command, ())
        with self.assertRaises(config.ConfigError):
            self.load('[chat]\nmcp_command = "/b/mesh-mcp"\n')

    def test_write_workspace_parsed_and_unknown_rejected(self):
        self.assertEqual(self.load('[chat]\nchat_workspace = "write"\n').chat_workspace, "write")
        with self.assertRaises(config.ConfigError):
            self.load('[chat]\nchat_workspace = "rw"\n')


if __name__ == "__main__":
    unittest.main()


class FakeTmux:
    def __init__(self, windows):
        self.windows, self.killed = windows, []

    def all_windows(self):
        return list(self.windows)

    def windows_strict(self):
        return [w for w in self.windows if w not in self.killed]

    def kill_window(self, session, window):
        self.killed.append((session, window))


class HostWindows(unittest.TestCase):
    """The tmux server is shared with the other runners of the host: only this runner's windows count."""

    def host(self, windows):
        h = ChatHost.__new__(ChatHost)
        h.runners = {"e2e-chat-claude": None}
        h.tmux = FakeTmux(windows)
        return h

    def test_other_runners_card_windows_do_not_fill_the_host_cap(self):
        h = self.host([("mr-Khan", "card-1"), ("mr-Bill", "card-2"), ("mr-Bill", "chat-x"),
                       ("mr-e2e-chat-claude", "card-3"), ("mr-e2e-chat-claude", "chat-y")])
        self.assertEqual(h._card_sessions(), 1)

    def test_orphan_sweep_leaves_other_runners_chats_alone(self):
        h = self.host([("mr-Bill", "chat-x"), ("mr-e2e-chat-claude", "chat-y")])
        h._stop_orphans()
        self.assertEqual(h.tmux.killed, [("mr-e2e-chat-claude", "chat-y")])

    def test_loop_does_not_sweep(self):
        # the sweep covers every agent of the runner: it runs once before the loops, a loop that swept
        # would kill a chat another loop had just started
        import threading
        from types import SimpleNamespace as NS
        h = self.host([("mr-e2e-chat-claude", "chat-y")])
        h.service = NS(recover=lambda: None, step=lambda t: None, reap_idle=lambda: None,
                       free_slots=lambda: 1, hold_until=0)
        stop = threading.Event()
        stop.set()
        h.loop(stop)
        self.assertEqual(h.tmux.killed, [])


class SlotReturn(unittest.TestCase):
    def host(self, mode, windows=()):
        from types import SimpleNamespace as NS
        pool = FakePool(Path(tempfile.mkdtemp()))
        ar = NS(pool=pool, agent=NS(name="A"))
        host = ChatHost(NS(cfg=NS(tmux_binary="tmux", state_dir=Path(tempfile.mkdtemp())), agents=[ar]),
                        object(), {"id-a": "A"}, 1, 6, 10, mode)
        class Tmux:
            def __init__(self, live):
                self.live, self.killed = list(live), []

            def windows(self, session):                     # stop_window's per-session view
                return {w: True for s, w in self.live if s == session}

            def windows_strict(self):
                return [x for x in self.live if x not in self.killed]

            def kill_window(self, session, window):
                self.killed.append((session, window))
        host.tmux = Tmux(windows)
        return host, pool

    def test_stop_returns_the_chat_slot_only_in_write_mode(self):
        for mode, expect in (("write", [window_key("th-1")]), ("read", [])):
            host, pool = self.host(mode)
            host._stop("id-a", "th-1")
            self.assertEqual(pool.released, expect)

    def test_slot_is_kept_while_the_window_is_not_confirmed_stopped(self):
        host, pool = self.host("write", [("mr-A", window_key("th-1"))])
        host.tmux.kill_window = lambda s, w: None           # the kill did not take
        host._stop("id-a", "th-1")
        self.assertEqual(pool.released, [])
        host, pool = self.host("write")
        host.tmux.windows_strict = lambda: None             # tmux failed: nothing is confirmed
        host._stop("id-a", "th-1")
        self.assertEqual(pool.released, [])


class OrphanSlots(unittest.TestCase):
    def test_restart_sweep_returns_chat_leases_but_not_card_leases(self):
        from types import SimpleNamespace as NS
        released = []
        pool = NS(rows=lambda: [{"state": "leased", "attempt": "chat-abc"}, {"state": "leased", "attempt": "card-1"},
                                {"state": "held", "attempt": "chat-old"}],
                  release=lambda a: released.append(a) or [])
        ar = NS(pool=pool, agent=NS(name="A"))
        host = ChatHost(NS(cfg=NS(tmux_binary="tmux", state_dir=Path(tempfile.mkdtemp())), agents=[ar]),
                        object(), {"id-a": "A"}, 1, 6, 10, "write")
        host.tmux = FakeTmux([])
        host._stop_orphans()
        self.assertEqual(released, ["chat-abc"])


class OrphanSweepFailure(unittest.TestCase):
    def test_leases_stay_when_the_window_sweep_failed(self):
        from types import SimpleNamespace as NS
        released = []
        pool = NS(rows=lambda: [{"state": "leased", "attempt": "chat-abc"}], release=lambda a: released.append(a) or [])
        ar = NS(pool=pool, agent=NS(name="A"))
        host = ChatHost(NS(cfg=NS(tmux_binary="tmux", state_dir=Path(tempfile.mkdtemp())), agents=[ar]),
                        object(), {"id-a": "A"}, 1, 6, 10, "write")
        host.tmux = FakeTmux([])
        host.tmux.windows_strict = lambda: (_ for _ in ()).throw(OSError("tmux gone"))
        host._stop_orphans()
        self.assertEqual(released, [])                  # a live window may still use the slot

    def test_leases_stay_when_a_chat_window_survives_the_kill(self):
        from types import SimpleNamespace as NS
        released = []
        pool = NS(rows=lambda: [{"state": "leased", "attempt": "chat-abc"}], release=lambda a: released.append(a) or [])
        host = ChatHost(NS(cfg=NS(tmux_binary="tmux", state_dir=Path(tempfile.mkdtemp())),
                           agents=[NS(pool=pool, agent=NS(name="A"))]), object(), {"id-a": "A"}, 1, 6, 10, "write")
        host.tmux = FakeTmux([("mr-A", "chat-abc")])
        host.tmux.kill_window = lambda s, w: None       # the kill did not take
        host._stop_orphans()
        self.assertEqual(released, [])

    def test_leases_stay_when_the_window_list_is_unreadable(self):
        from types import SimpleNamespace as NS
        released = []
        pool = NS(rows=lambda: [{"state": "leased", "attempt": "chat-abc"}], release=lambda a: released.append(a) or [])
        host = ChatHost(NS(cfg=NS(tmux_binary="tmux", state_dir=Path(tempfile.mkdtemp())),
                           agents=[NS(pool=pool, agent=NS(name="A"))]), object(), {"id-a": "A"}, 1, 6, 10, "write")
        host.tmux = FakeTmux([])
        host.tmux.windows_strict = lambda: None         # tmux failed: an empty list would mean nothing
        host._stop_orphans()
        self.assertEqual(released, [])


class SharedPool(unittest.TestCase):
    """Mesh authorises a claim per agent key, so each agent has a host of its own; the host cap stays one."""

    def runner(self):
        from types import SimpleNamespace as NS
        cfg = NS(tmux_binary="tmux", state_dir=None)
        return NS(cfg=cfg, agents=[])

    def test_hosts_of_two_agents_share_one_pool_but_claim_only_their_own_agent(self):
        a = ChatHost(self.runner(), object(), {"id-a": "A"}, 1, 6, 10)
        b = ChatHost(self.runner(), object(), {"id-b": "B"}, 1, 6, 10, pool=a.pool)
        self.assertIs(a.pool, b.pool)
        self.assertEqual(a.service.agents, ["id-a"])
        self.assertEqual(b.service.agents, ["id-b"])

    def test_claim_key_is_the_agents_own_and_the_credential_only_as_fallback(self):
        from types import SimpleNamespace as NS
        from mesh_runner.chat_host import claim_key
        self.assertEqual(claim_key(NS(agent=NS(key="own")), "shared"), "own")
        self.assertEqual(claim_key(NS(agent=NS(key="")), "shared"), "shared")
