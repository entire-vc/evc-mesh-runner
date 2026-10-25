import os
import stat
import tempfile
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

    def test_driver_timing_line_is_logged_and_only_that_line(self):
        from pathlib import Path
        from mesh_runner.chat_delivery import DriverDelivery
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "e.stderr"
            f.write_text("interactive: noise\ntiming: session=0.0 model_stop=5.1 emit=0.2\n")
            with self.assertLogs("mesh_runner.chat_delivery", "INFO") as cap:
                DriverDelivery._log_timing(f)
            self.assertEqual(len(cap.output), 1)
            self.assertIn("model_stop=5.1", cap.output[0])
            self.assertNotIn("noise", cap.output[0])

    def test_print_timing_names_phases(self):
        import contextlib, io
        from mesh_runner.tui_driver import Driver
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            Driver.print_timing(None, {"start": 0.0, "ready": 1.0, "taken": 1.5, "stop": 7.0, "emit": 7.25})
        self.assertEqual(buf.getvalue().strip(), "timing: session=1.0 idle_paste=0.5 model_stop=5.5 emit=0.2")

    def test_routed_env_is_cached_for_ttl_and_refreshed_after(self):
        from mesh_runner.chat_host import routed_env, _env_cache
        calls, now = [], [100.0]

        class Adapter:
            profile = "p1"

        class AR:
            _routed_harness = "claude"
            adapter = Adapter()

            def adapter_for(self, harness):
                return self.adapter

            def _fresh_env(self):
                calls.append(1)
                return {"A": str(len(calls))}
        ar = AR()
        _env_cache.clear()
        self.assertEqual(routed_env(ar, clock=lambda: now[0], ttl=30), {"A": "1"})
        ar.adapter.profile = "changed-by-a-card"                # another start rerouted the shared adapter
        now[0] = 120.0
        env = routed_env(ar, clock=lambda: now[0], ttl=30)
        env["A"] = "mutated"                                  # a caller's edits never reach the cache
        self.assertEqual(len(calls), 1)
        self.assertEqual(ar.adapter.profile, "p1")             # a hit restores the profile that went with the env
        self.assertEqual(routed_env(ar, clock=lambda: now[0], ttl=30), {"A": "1"})
        now[0] = 131.0
        self.assertEqual(routed_env(ar, clock=lambda: now[0], ttl=30), {"A": "2"})
        self.assertEqual(len(calls), 2)

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
