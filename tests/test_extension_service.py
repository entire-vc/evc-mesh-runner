import http.client
import json
import logging
import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path

from mesh_runner import __version__
from mesh_runner.extension_service import ExtensionService, ExtensionToken, rotate_token, sign
from mesh_runner.mesh import MeshClient, MeshError

ROOT = Path(__file__).resolve().parent.parent

SECRET = "s3cr3t-" + "x" * 32
NOW = 1_800_000_000
WS = "11111111-2222-3333-4444-555555555555"


def install_body(secret=SECRET, ext="agent-runner"):
    return json.dumps({"extension_id": ext, "version": "0.1.0", "workspace_id": WS, "signing_secret": secret,
                       "events": ["task.assigned"], "events_path": "/mesh/events"}).encode()


def headers(body, secret=SECRET, ts=NOW):
    return {"X-Mesh-Timestamp": str(ts), "X-Mesh-Signature": sign(secret, str(ts), body)}


class ServiceTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.wakes = []
        self.svc = ExtensionService(Path(self.dir.name), lambda: self.wakes.append(1), WS, trust_first_install=True, clock=lambda: NOW)

    def install(self, **kw):
        body = install_body(**kw)
        return self.svc.install(headers(body, kw.get("secret", SECRET)), body)

    def test_healthz(self):
        self.assertEqual(self.svc.healthz(), (200, {"status": "ok", "installed": False}))

    def test_install_stores_secret_private(self):
        self.assertEqual(self.install()[0], 200)
        path = Path(self.dir.name) / "extension-signing-secret.json"
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(self.svc.healthz()[1]["installed"], True)

    def test_install_bad_signature_refused(self):
        body = install_body()
        bad = headers(body)
        bad["X-Mesh-Signature"] = "0" * 64
        self.assertEqual(self.svc.install(bad, body)[0], 401)
        self.assertIsNone(self.svc.store.load())

    def test_install_signed_with_other_secret_refused(self):
        body = install_body()
        self.assertEqual(self.svc.install(headers(body, secret="another-secret-0123456789"), body)[0], 401)

    def test_install_expired_timestamp_refused(self):
        body = install_body()
        self.assertEqual(self.svc.install(headers(body, ts=NOW - 301), body)[0], 401)
        self.assertEqual(self.svc.install(headers(body, ts=NOW + 301), body)[0], 401)
        self.assertEqual(self.svc.install(headers(body, ts=NOW - 299), body)[0], 200)

    def test_install_idempotent(self):
        self.assertEqual(self.install()[0], 200)
        status, out = self.install()
        self.assertEqual((status, out["idempotent"]), (200, True))

    def test_install_other_secret_after_install_is_409(self):
        self.install()
        self.assertEqual(self.install(secret="rotated-secret-0123456789")[0], 409)
        self.assertEqual(self.svc.store.load()["signing_secret"], SECRET)

    def test_rekey_allowed_by_operator(self):
        self.svc.allow_rekey = True
        self.install()
        self.assertEqual(self.install(secret="rotated-secret-0123456789")[0], 200)
        self.assertEqual(self.svc.store.load()["signing_secret"], "rotated-secret-0123456789")

    def test_install_for_other_workspace_refused(self):
        # a caller that reaches the port and signs with its own secret must not claim the install
        body = json.dumps({"extension_id": "agent-runner", "version": "1", "signing_secret": SECRET,
                           "workspace_id": "99999999-2222-3333-4444-555555555555"}).encode()
        self.assertEqual(self.svc.install(headers(body), body)[0], 403)
        body = json.dumps({"extension_id": "agent-runner", "version": "1", "signing_secret": SECRET}).encode()
        self.assertEqual(self.svc.install(headers(body), body)[0], 403)
        self.assertIsNone(self.svc.store.load())

    def test_preshared_secret_authorises_install(self):
        self.svc.trust_first_install, self.svc.expected_secret = False, SECRET
        # a caller with the right workspace but its own, correctly self-signed secret is refused
        self.assertEqual(self.install(secret="attacker-secret-0123456789")[0], 403)
        self.assertIsNone(self.svc.store.load())
        self.assertEqual(self.install()[0], 200)

    def test_no_preshared_secret_and_no_trust_refuses(self):
        self.svc.trust_first_install = False
        self.assertEqual(self.install()[0], 403)
        self.assertIsNone(self.svc.store.load())

    def test_install_wrong_extension_or_short_secret(self):
        self.assertEqual(self.install(ext="other")[0], 400)
        self.assertEqual(self.install(secret="short")[0], 400)

    def test_events_need_install_and_signature(self):
        body = json.dumps({"event": "task.assigned"}).encode()
        self.assertEqual(self.svc.events(headers(body), body)[0], 403)
        self.install()
        self.assertEqual(self.svc.events({"X-Mesh-Timestamp": str(NOW), "X-Mesh-Signature": "ab"}, body)[0], 401)
        self.assertEqual(self.svc.events(headers(body, ts=NOW - 1000), body)[0], 401)
        self.assertEqual(self.wakes, [])

    def test_events_wake_only_for_task_and_comment_events(self):
        self.install()
        for kind, woke in (("task.assigned", 1), ("task.updated", 1), ("comment.created", 1), ("project.created", 0)):
            self.wakes.clear()
            body = json.dumps({"event": kind}).encode()
            self.assertEqual(self.svc.events(headers(body), body)[0], 202)
            self.assertEqual(len(self.wakes), woke, kind)

    def test_event_kind_from_x_mesh_event_header(self):
        # Mesh puts the event type in the X-Mesh-Event header; the body carries only the payload
        self.install()
        body = json.dumps({"task_id": "t1", "project_id": "p1", "changes": {"status": "todo"}}).encode()
        for kind, status, woke in (("task.assigned", "wake", 1), ("comment.created", "wake", 1),
                                   ("task.deleted", "ignored", 0)):
            self.wakes.clear()
            hdr = dict(headers(body), **{"X-Mesh-Event": kind})
            self.assertEqual(self.svc.events(hdr, body), (202, {"status": status}), kind)
            self.assertEqual(len(self.wakes), woke, kind)

    def test_header_kind_wins_over_body(self):
        self.install()
        body = json.dumps({"event": "task.assigned"}).encode()
        hdr = dict(headers(body), **{"X-Mesh-Event": "project.created"})
        self.assertEqual(self.svc.events(hdr, body), (202, {"status": "ignored"}))
        self.assertEqual(self.wakes, [])

    def test_secret_never_logged(self):
        with self.assertLogs("mesh_runner", level="DEBUG") as cm:
            self.install()
            body = install_body()
            self.svc.install({"X-Mesh-Timestamp": str(NOW), "X-Mesh-Signature": "bad"}, body)
            logging.getLogger("mesh_runner").info("sentinel")      # assertLogs needs one record
        self.assertNotIn(SECRET, "\n".join(cm.output))

    def test_secret_leak_control_is_red(self):
        # positive control: the check above fails when a record does carry the secret
        with self.assertLogs("mesh_runner", level="INFO") as cm:
            logging.getLogger("mesh_runner").info("leak %s", SECRET)
        self.assertIn(SECRET, "\n".join(cm.output))


class HttpTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.wakes = []
        self.svc = ExtensionService(Path(self.dir.name), lambda: self.wakes.append(1), WS, expected_secret=SECRET)
        server = self.svc.serve("127.0.0.1", 0)
        self.addCleanup(self.svc.shutdown)
        self.port = server.server_address[1]

    def call(self, method, path, body=b"", hdr=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request(method, path, body, hdr or {})
        r = c.getresponse()
        return r.status, json.loads(r.read() or b"{}")

    def test_over_http(self):
        import time
        self.assertEqual(self.call("GET", "/healthz")[0], 200)
        body = install_body()
        now = int(time.time())
        self.assertEqual(self.call("POST", "/mesh/install", body, headers(body, ts=now))[0], 200)
        bad = headers(body, ts=now)
        bad["X-Mesh-Signature"] = "f" * 64
        self.assertIn(self.call("POST", "/mesh/install", body, bad)[0], (401, 403))
        ev = json.dumps({"event": "comment.created"}).encode()
        self.assertEqual(self.call("POST", "/mesh/events", ev, headers(ev, ts=now))[0], 202)
        self.assertEqual(len(self.wakes), 1)
        self.assertEqual(self.call("POST", "/nope", b"{}")[0], 404)

    def test_event_header_over_http_any_case(self):
        # the real handler passes self.headers (case-insensitive), not a dict
        import time
        body = install_body()
        now = int(time.time())
        self.assertEqual(self.call("POST", "/mesh/install", body, headers(body, ts=now))[0], 200)
        ev = json.dumps({"task_id": "t1", "changes": {}}).encode()
        self.assertEqual(self.call("POST", "/mesh/events", ev, dict(headers(ev, ts=now), **{"x-mesh-event": "task.updated"})),
                         (202, {"status": "wake"}))
        self.assertEqual(len(self.wakes), 1)


class TokenTest(unittest.TestCase):
    def test_bearer_header_replaces_agent_key(self):
        seen = {}

        class Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self, n=None): return b"{}"

        def opener(req, timeout=None):
            seen.update({k.lower(): v for k, v in req.header_items()})
            return Resp()
        MeshClient("http://m", "agent-key", opener=opener, bearer=lambda: "ext_abc").me()
        self.assertEqual(seen["authorization"], "Bearer ext_abc")
        self.assertNotIn("x-agent-key", seen)

    def test_token_file_rotation_atomic_600_and_hidden(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "tok"
            f.write_text("ext_old\n")
            os.chmod(f, 0o600)
            tok = ExtensionToken(f)
            self.assertEqual(tok(), "ext_old")

            class Client:
                def request(self, method, path, body=None):
                    assert (method, path) == ("POST", "/ext/me/token/rotate")
                    return {"extension_token": "ext_new"}
            tok.rotate(Client())
            self.assertEqual(tok(), "ext_new")
            self.assertEqual(stat.S_IMODE(f.stat().st_mode), 0o600)
            self.assertNotIn("ext_", repr(tok))

    def test_rotation_without_token_in_answer_keeps_old(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "tok"
            f.write_text("ext_old")
            tok = ExtensionToken(f)

            class Client:
                def request(self, *a, **k): return {"error": "x"}
            with self.assertRaises(RuntimeError):
                tok.rotate(Client())
            self.assertEqual(tok(), "ext_old")

    def test_rotation_refused_403_names_the_capability_and_keeps_going(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "tok"
            f.write_text("ext_old")
            tok = ExtensionToken(f)

            class Client:
                calls = 0

                def request(self, method, path, body=None):
                    Client.calls += 1
                    if Client.calls == 1:
                        raise MeshError(403, "extension lacks the consented capability api:ext_token:self_rotate",
                                        endpoint=path)
                    return {"extension_token": "ext_new"}
            with self.assertLogs("mesh_runner", "WARNING") as logs:
                self.assertFalse(rotate_token(tok, Client()))
            text = "\n".join(logs.output)
            self.assertIn("403", text)
            self.assertIn("ext_token:self_rotate", text)
            self.assertNotIn("ext_old", text)
            self.assertEqual(tok(), "ext_old")
            # the next round of the loop still rotates
            self.assertTrue(rotate_token(tok, Client()))
            self.assertEqual(tok(), "ext_new")


class ManifestTest(unittest.TestCase):
    def test_manifest_asks_for_self_rotate_and_versions_agree(self):
        manifest = json.loads((ROOT / "mesh-extension.json").read_text())
        self.assertIn("ext_token:self_rotate", manifest["capabilities"]["api"])
        pyproject = (ROOT / "pyproject.toml").read_text()
        self.assertIn(f'\nversion = "{manifest["version"]}"\n', pyproject)
        self.assertEqual(manifest["version"], __version__)


if __name__ == "__main__":
    unittest.main()


class WakeAndConfigTest(unittest.TestCase):
    def test_wake_ends_the_poll_sleep(self):
        import time
        from mesh_runner.runner import Runner
        r = Runner.__new__(Runner)
        r._wake_gen = 0
        stop = threading.Event()
        threading.Timer(0.2, r.wake).start()
        t0 = time.monotonic()
        r._sleep(stop, 30, 0)
        self.assertLess(time.monotonic() - t0, 3)

    def test_config_extension_section(self):
        from mesh_runner import config
        with tempfile.TemporaryDirectory() as d:
            tok = Path(d) / "tok"
            tok.write_text("ext_x")
            os.chmod(tok, 0o600)
            cfgf = Path(d) / "runner.toml"
            cfgf.write_text(f'[runner]\nmesh_url="http://m"\nstate_dir="{d}/st"\n'
                            f'[extension]\nenabled=true\nworkspace_id="{WS}"\ntrust_first_install=true\nport=9001\ntoken_file="{tok}"\nrotate_days=7\n')
            c = config.load(cfgf)
            self.assertEqual((c.extension_enabled, c.extension_port, c.extension_rotate_days), (True, 9001, 7.0))
            self.assertEqual(c.extension_token(), "ext_x")
            self.assertEqual(c.extension_workspace_id, WS)
            os.chmod(tok, 0o644)
            with self.assertRaises(config.ConfigError):
                config.load(cfgf)

    def test_agent_key_optional_with_extension_token(self):
        from mesh_runner import config
        with tempfile.TemporaryDirectory() as d:
            tok = Path(d) / "tok"
            tok.write_text("ext_x")
            os.chmod(tok, 0o600)
            base = f'[runner]\nmesh_url="http://m"\nstate_dir="{d}/st"\n'
            agent = f'[[agents]]\nname="a"\nharness="claude"\nprofile_dir="{d}/p"\n'
            with_tok = Path(d) / "a.toml"
            with_tok.write_text(base + f'[extension]\ntoken_file="{tok}"\n' + agent)
            self.assertEqual(config.load(with_tok).agents[0].key, "")
            without = Path(d) / "b.toml"          # red control: no token, no key -> still refused
            without.write_text(base + agent)
            with self.assertRaises(config.ConfigError):
                config.load(without)


class BoundedGetTokenTest(unittest.TestCase):
    def test_bounded_get_child_carries_bearer(self):
        from http.server import BaseHTTPRequestHandler, HTTPServer
        seen = {}

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a): pass

            def do_GET(self):
                seen["auth"], seen["key"] = self.headers.get("Authorization"), self.headers.get("X-Agent-Key")
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")
        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        client = MeshClient(f"http://127.0.0.1:{srv.server_address[1]}", "", bearer=lambda: "ext_child")
        client.bounded_get("/tasks/" + "a" * 8 + "-aaaa-aaaa-aaaa-" + "a" * 12, timeout=10)
        self.assertEqual(seen, {"auth": "Bearer ext_child", "key": None})

    def test_enabled_without_workspace_id_refused(self):
        from mesh_runner import config
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "runner.toml"
            f.write_text(f'[runner]\nmesh_url="http://m"\nstate_dir="{d}/st"\n[extension]\nenabled=true\n')
            with self.assertRaises(config.ConfigError):
                config.load(f)

    def test_enabled_needs_preshared_secret_or_trust(self):
        from mesh_runner import config
        with tempfile.TemporaryDirectory() as d:
            sec = Path(d) / "sec"
            sec.write_text(SECRET)
            os.chmod(sec, 0o600)
            base = f'[runner]\nmesh_url="http://m"\nstate_dir="{d}/st"\n[extension]\nenabled=true\nworkspace_id="{WS}"\n'
            f = Path(d) / "a.toml"
            f.write_text(base)
            with self.assertRaises(config.ConfigError):
                config.load(f)
            f.write_text(base + f'signing_secret_file="{sec}"\n')
            c = config.load(f)
            self.assertEqual((c.extension_secret, c.extension_trust_first_install), (SECRET, False))
            self.assertNotIn(SECRET, repr(c))
