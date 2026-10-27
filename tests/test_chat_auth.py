import os
import stat
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from mesh_runner.chat_protocol import ChatClient, load_runner_credential, load_runner_id, registered_client
from mesh_runner.tui_driver import timeouts_from
from mesh_runner.chat_host import CHAT_POLL, CHAT_SETTLE


class ChatAuthTests(unittest.TestCase):
    def test_agent_key_and_runner_id_headers(self):
        seen = []

        def opener(req, timeout=None):
            seen.append({k.lower(): v for k, v in req.header_items()})
            raise AssertionError("stop")
        rid = "6f1c3a52-0000-4000-8000-000000000001"
        client = ChatClient("https://mesh.test", "k", opener=opener, runner_id=rid)
        with self.assertRaises(Exception):
            client.active()
        self.assertEqual(seen[0]["x-agent-key"], "k")
        self.assertEqual(seen[0]["x-chat-runner-id"], rid)
        self.assertNotIn("authorization", seen[0])

    def test_runner_id_is_stable(self):
        with tempfile.TemporaryDirectory() as d:
            a = load_runner_id(d)
            self.assertEqual(str(uuid.UUID(a)), a)
            self.assertEqual(load_runner_id(d), a)

    def test_runner_id_file_is_0600_even_if_it_existed_wider(self):
        with tempfile.TemporaryDirectory() as d:
            rid = load_runner_id(d)
            path = os.path.join(d, "chat-runner-id")
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            os.chmod(path, 0o644)
            self.assertEqual(load_runner_id(d), rid)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_registered_runner_sends_token_and_names_itself_on_commands(self):
        seen = []

        def opener(req, timeout=None):
            seen.append(({k.lower(): v for k, v in req.header_items()}, req.full_url))
            raise AssertionError("stop")
        rid = "6f1c3a52-0000-4000-8000-000000000002"
        client = ChatClient("https://mesh.test", "k", opener=opener, runner_id=rid, runner_token="tok")
        with self.assertRaises(Exception):
            client.commands(["a1"])
        headers, url = seen[0]
        self.assertEqual(headers["x-runner-token"], "tok")
        self.assertIn("runner_id=" + rid, url)

    def test_unregistered_client_sends_no_token(self):
        seen = []

        def opener(req, timeout=None):
            seen.append(({k.lower(): v for k, v in req.header_items()}, req.full_url))
            raise AssertionError("stop")
        client = ChatClient("https://mesh.test", "k", opener=opener)
        with self.assertRaises(Exception):
            client.commands(["a1"])
        self.assertNotIn("x-runner-token", seen[0][0])
        self.assertNotIn("runner_id=", seen[0][1])

    def test_runner_credential_is_registered_once_and_kept_0600(self):
        calls = []
        rid = "6f1c3a52-0000-4000-8000-000000000003"

        def register():
            calls.append(1)
            return {"runner_id": rid, "token": "tok-1"}
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(load_runner_credential(d, register), (rid, "tok-1"))
            self.assertEqual(load_runner_credential(d, register), (rid, "tok-1"))
            self.assertEqual(len(calls), 1)
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(d, "chat-runner.json")).st_mode), 0o600)

    def test_each_agent_registers_its_own_runner_credential(self):
        # Mesh binds the runner token to the key that registered it: Hugh's key with Garfield's token is a 403
        n = []

        def register():
            n.append(1)
            return {"runner_id": "6f1c3a52-0000-4000-8000-00000000000%d" % len(n), "token": "tok-%d" % len(n)}
        with tempfile.TemporaryDirectory() as d:
            a = load_runner_credential(d, register, "Garfield")
            b = load_runner_credential(d, register, "Hugh")
            self.assertNotEqual(a, b)
            self.assertEqual(load_runner_credential(d, register, "Hugh"), b)
            self.assertEqual(len(n), 2)
            self.assertNotEqual(load_runner_credential(d, register, "foo.bar"), load_runner_credential(d, register, "foobar"))
            for f in ("chat-runner-Garfield.json", "chat-runner-Hugh.json"):
                self.assertEqual(stat.S_IMODE(os.stat(os.path.join(d, f)).st_mode), 0o600)

    def test_registration_failure_falls_back_to_plain_client_without_the_token(self):
        def opener(req, timeout=None):
            raise OSError("down")
        with tempfile.TemporaryDirectory() as d, self.assertLogs("mesh_runner", "WARNING") as logs:
            c = registered_client("https://mesh.test", "k", d, "6f1c3a52-0000-4000-8000-000000000004", "n", opener=opener)
            self.assertEqual(c._runner_token, "")
            self.assertFalse(os.path.exists(os.path.join(d, "chat-runner.json")))
        self.assertNotIn("down", " ".join(logs.output))

    def test_chat_timing_reaches_driver_timeouts_and_cards_keep_defaults(self):
        t = timeouts_from({"poll": CHAT_POLL, "settle": CHAT_SETTLE})
        self.assertEqual((t["poll"], t["settle"]), (0.2, 0.3))
        self.assertNotIn("poll", timeouts_from({}))
        self.assertNotIn("settle", timeouts_from({}))

    def test_driver_report_lines_are_logged_and_only_those(self):
        from pathlib import Path
        from mesh_runner.chat_delivery import DriverDelivery
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "e.stderr"
            f.write_text("harness noise\ninteractive: restart: session\n"
                         "interactive: background agents still running after 5 min, the prompt goes in anyway: "
                         "TOKEN=pane-text\n"
                         "interactive: 1 background agent(s) running (pane-text): the prompt waits up to 5 min\n"
                         "interactive: codex ended the turn with no final message (no model output)\n"
                         "timing: session=0.0 model_stop=5.1 emit=0.2\n")
            with self.assertLogs("mesh_runner.chat_delivery", "INFO") as cap:
                DriverDelivery._log_driver(f)
            self.assertEqual(len(cap.output), 3)
            self.assertIn("restart: session", cap.output[0])
            self.assertIn("no final message", cap.output[1])
            self.assertIn("model_stop=5.1", cap.output[2])
            self.assertNotIn("harness noise", "".join(cap.output))
            self.assertNotIn("pane-text", "".join(cap.output))

    def test_print_timing_names_phases(self):
        import contextlib, io
        from mesh_runner.tui_driver import Driver
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            Driver.print_timing(None, {"start": 0.0, "ready": 1.0, "taken": 1.5, "stop": 7.0, "emit": 7.25})
        self.assertEqual(buf.getvalue().strip(), "timing: session=1.0 idle_paste=0.5 model_stop=5.5 emit=0.2")

    def test_routed_env_is_cached_for_ttl_and_refreshed_after(self):
        from mesh_runner.chat_host import routed_env, _env_cache, _refreshing
        calls, now = [], [100.0]

        class Adapter:
            profile = "p1"

        class AR:
            _routed_harness = "claude"
            adapter = Adapter()

            def adapter_for(self, harness):
                return self.adapter

            def _route_env(self, raw):
                return dict(raw), "claude", "p1"

            def _read_env(self):
                calls.append(1)
                return {"A": str(len(calls))}
        ar = AR()
        _env_cache.clear()
        self.assertEqual(routed_env(ar, clock=lambda: now[0], ttl=30)["A"], "1")
        ar.adapter.profile = "changed-by-a-card"                # another start rerouted the shared adapter
        now[0] = 120.0
        env = routed_env(ar, clock=lambda: now[0], ttl=30)
        env["A"] = "mutated"                                  # a caller's edits never reach the cache
        self.assertEqual(len(calls), 1)
        self.assertEqual(ar.adapter.profile, "changed-by-a-card")   # a hit leaves the shared adapter alone
        self.assertEqual(routed_env(ar, clock=lambda: now[0], ttl=30)["A"], "1")
        now[0] = 131.0
        # past the ttl the old entry is served at once; one background refresh replaces it
        self.assertEqual(routed_env(ar, clock=lambda: now[0], ttl=30)["A"], "1")
        import time as _t
        for _ in range(200):
            if len(calls) == 2 and not _refreshing:
                break
            _t.sleep(0.01)
        self.assertEqual(len(calls), 2)
        self.assertEqual(routed_env(ar, clock=lambda: now[0], ttl=30)["A"], "2")
        self.assertEqual(len(calls), 2)

    def test_a_turn_builds_its_command_on_a_private_adapter_pinned_to_its_entry(self):
        from types import SimpleNamespace as NS
        from mesh_runner.chat_host import pinned, routed, _env_cache

        class Base:
            profile = "shared"

        class Wrapper:
            def __init__(self):
                self.base = Base()

            @property
            def profile(self):
                return self.base.profile

        class AR:
            _routed_harness = "codex"

            def __init__(self):
                self.adapter = Wrapper()

            def adapter_for(self, harness):
                return self.adapter

            def _read_env(self):
                return {"A": "1"}

            def _route_env(self, raw):
                return dict(raw), "codex", "entry-profile"
        ar = AR()
        _env_cache.clear()
        env, harness, profile = routed(ar)
        self.assertEqual((harness, profile), ("codex", "entry-profile"))
        ar.adapter.base.profile = "set-by-a-card"               # a card turn re-routes the shared adapter
        mine = pinned(ar.adapter, profile)
        self.assertEqual(mine.profile, "entry-profile")           # the chat's copy keeps its entry's profile
        self.assertEqual(ar.adapter.base.profile, "set-by-a-card")  # and never writes the shared one

    def test_the_cached_env_carries_the_allowlisted_runner_env(self):
        # a session without HOME/PATH finds no login ("Not logged in"); the read env alone is not a session env
        from mesh_runner.chat_host import routed, _env_cache

        class AR:
            def adapter_for(self, harness):
                return object()

            def _read_env(self):
                return {"LANE": "1"}

            def _route_env(self, raw):
                return dict(raw), "claude", None
        old = os.environ.get("HOME")
        os.environ["HOME"] = "/home-of-runner"
        try:
            _env_cache.clear()
            env, _, _ = routed(AR())
        finally:
            if old is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = old
        self.assertEqual(env.get("HOME"), "/home-of-runner")
        self.assertEqual(env.get("LANE"), "1")

    def test_pinned_real_codex_adapter_puts_its_profile_on_the_command_line(self):
        from mesh_runner.adapters.codex import CodexAdapter
        from mesh_runner.chat_host import pinned
        from mesh_runner.interactive import InteractiveAdapter
        shared = InteractiveAdapter(CodexAdapter("codex", [], "shared"), "Hugh", Path(tempfile.mkdtemp()))
        mine = pinned(shared, "entry")

        def argv(a):
            return " ".join(a.start_argv("", Path("."), []))
        self.assertIn("entry", argv(mine))
        self.assertNotIn("entry", argv(shared))
        self.assertIn("shared", argv(shared))

    def test_a_slow_refresh_does_not_block_stale_callers(self):
        import threading
        from mesh_runner.chat_host import routed, routed_env, _env_cache, _refreshing
        gate, started, calls = threading.Event(), threading.Event(), []

        class AR:
            _routed_harness = "claude"

            def adapter_for(self, harness):
                return object()

            def _route_env(self, raw):
                return dict(raw), ("rerouted" if raw.get("reroute") else "claude"), None

            def _read_env(self):
                calls.append(1)
                if len(calls) > 1:
                    started.set()
                    gate.wait(10)
                    return {"A": str(len(calls)), "reroute": "1"}
                return {"A": str(len(calls))}
        ar, now = AR(), [100.0]
        _env_cache.clear()
        routed_env(ar, clock=lambda: now[0], ttl=30)
        now[0] = 200.0
        self.assertEqual(routed_env(ar, clock=lambda: now[0], ttl=30)["A"], "1")   # starts the refresh
        self.assertTrue(started.wait(5))
        out = []
        t = threading.Thread(target=lambda: out.append(routed_env(ar, clock=lambda: now[0], ttl=30)))
        t.start()
        t.join(2)
        self.assertFalse(t.is_alive())                      # answered while the refresh is still running
        self.assertEqual([o["A"] for o in out], ["1"])
        self.assertEqual(routed(ar, clock=lambda: now[0], ttl=30)[1], "claude")   # the router has not answered yet
        gate.set()
        for _ in range(200):
            if not _refreshing:
                break
            time.sleep(0.01)
        self.assertEqual(routed(ar, clock=lambda: now[0], ttl=30)[1], "rerouted")   # the new entry, once stored

    def test_a_failed_refresh_keeps_the_stale_entry_and_retries(self):
        from mesh_runner.chat_host import routed_env, _env_cache, _refreshing
        calls = []

        class AR:
            _routed_harness = "claude"

            def adapter_for(self, harness):
                return object()

            def _route_env(self, raw):
                return dict(raw), ("rerouted" if raw.get("reroute") else "claude"), None

            def _read_env(self):
                calls.append(1)
                if len(calls) == 2:
                    raise OSError("router down")
                return {"A": str(len(calls))}
        ar, now = AR(), [100.0]
        _env_cache.clear()
        routed_env(ar, clock=lambda: now[0], ttl=30)
        now[0] = 200.0
        with self.assertLogs("mesh_runner", "WARNING") as logs:
            self.assertEqual(routed_env(ar, clock=lambda: now[0], ttl=30)["A"], "1")
            for _ in range(200):
                if len(calls) == 2 and not _refreshing:
                    break
                time.sleep(0.01)
        self.assertIn("chat env refresh: OSError", " ".join(logs.output))
        self.assertEqual(routed_env(ar, clock=lambda: now[0], ttl=30)["A"], "1")   # stale kept, retry started
        for _ in range(200):
            if len(calls) == 3 and not _refreshing:
                break
            time.sleep(0.01)
        self.assertEqual(routed_env(ar, clock=lambda: now[0], ttl=30)["A"], "3")

    def test_plan_time_is_logged_without_the_prompt_or_env(self):
        from mesh_runner.chat_delivery import DriverDelivery
        d = DriverDelivery("claude", lambda turn: (["true"], Path("."), {"SECRET_X": "v"}, "private prompt", "claude"),
                           Path(tempfile.mkdtemp()))
        orig = DriverDelivery._drive
        DriverDelivery._drive = lambda self, *a: iter(())
        try:
            with self.assertLogs("mesh_runner.chat_delivery", "INFO") as logs:
                list(d.run({"turn_id": "12345678-aaaa", "thread_id": "t"}, lambda: False))
        finally:
            DriverDelivery._drive = orig
        text = " ".join(logs.output)
        self.assertRegex(text, r"chat turn 12345678 plan=\d+\.\d")
        self.assertNotIn("private prompt", text)
        self.assertNotIn("SECRET_X", text)

    def test_plan_time_is_logged_when_the_plan_raises(self):
        from mesh_runner.chat_delivery import DriverDelivery

        def plan(turn):
            raise RuntimeError("routing failed")
        d = DriverDelivery("claude", plan, Path(tempfile.mkdtemp()))
        with self.assertLogs("mesh_runner.chat_delivery", "INFO") as logs:
            with self.assertRaises(RuntimeError):
                list(d.run({"turn_id": "12345678-aaaa", "thread_id": "t"}, lambda: False))
        self.assertRegex(" ".join(logs.output), r"chat turn 12345678 plan=\d+\.\d")

    def test_the_plan_names_the_harness_the_driver_output_is_read_with(self):
        from mesh_runner.chat_delivery import DriverDelivery
        d = DriverDelivery("claude", lambda turn: (["true"], Path("."), {}, "p", "codex"), Path(tempfile.mkdtemp()))
        seen = []
        orig = DriverDelivery._drive

        def spy(self, *a):
            seen.append(a[-1])
            return iter(())
        DriverDelivery._drive = spy
        try:
            list(d.run({"turn_id": "12345678", "thread_id": "t"}, lambda: False))
        finally:
            DriverDelivery._drive = orig
        self.assertEqual(seen, ["codex"])               # not the one the delivery was built with


if __name__ == "__main__":
    unittest.main()


class TransportDetail(unittest.TestCase):
    def test_a_socket_failure_names_its_cause_in_the_chat_error(self):
        from mesh_runner.chat_protocol import _wrap
        from mesh_runner.mesh import MeshError
        err = _wrap(MeshError(0, "ConnectionResetError: [Errno 54] Connection reset by peer"))
        self.assertEqual(err.error_code, "transport")
        self.assertIn("ConnectionResetError", str(err))


class ClaimReadTimeout(unittest.TestCase):
    def test_a_silent_claim_is_given_up_a_few_seconds_after_the_server_deadline(self):
        from mesh_runner import chat_host
        from mesh_runner.chat_protocol import CLAIM_READ_SLACK
        from mesh_runner.chat_service import ORDERS_EVERY
        self.assertEqual((chat_host.STEP_TIMEOUT, CLAIM_READ_SLACK, ORDERS_EVERY), (3, 3, 20.0))
        self.assertLessEqual(chat_host.STEP_TIMEOUT + CLAIM_READ_SLACK, 6)
