"""Strict existing-session metadata delivery, over a real HTTP boundary (a loopback server)."""
import json
import re
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from mesh_runner.meta_delivery import (CAPABILITY, Identity, Journal, MetaClient, MetaReceiver, parse_settings,
                                       text_sha256)

IDENT = dict(workspace_id="aaaaaaaa-0000-4000-8000-000000000001", agent_id="aaaaaaaa-0000-4000-8000-000000000002",
             runner_id="aaaaaaaa-0000-4000-8000-000000000003", host_id="host-A", hostname="receiver-a.example.test",
             session_id="11111111-1111-1111-1111-111111111111")
SENDER = "aaaaaaaa-0000-4000-8000-000000000009"
REV = 3                                                     # assigned by the backend at registration
AGENT_KEY, RUNNER_TOKEN = "secret", "runner-token-1"
BASE = "/api/v1/runner/existing-session"


class FakeBackend(BaseHTTPRequestHandler):
    """Mesh side of the FROZEN routes (doc v3). Strict: any other path, a missing X-Agent-Key or
    X-Runner-Token, the old X-Chat-Runner-Id header, an unknown body field or a wrong state transition is
    answered with an error status, so a client that drifts from the contract fails here."""
    queued: list = []
    reports: list = []                    # (state, body) of accepted acks
    rejected: list = []                   # (path, why) of everything the contract refuses
    registrations: list = []
    states: dict = {}

    def log_message(self, *a):
        pass

    def _send(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _refuse(self, why, status=400):
        type(self).rejected.append((self.path, why))
        self._send({"error": why}, status)

    def _auth(self) -> bool:
        if self.headers.get("X-Agent-Key") != AGENT_KEY or self.headers.get("X-Runner-Token") != RUNNER_TOKEN:
            self._refuse("auth", 401)
            return False
        if self.headers.get("X-Chat-Runner-Id") is not None:
            self._refuse("legacy_header", 400)
            return False
        return True

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        if url.path != BASE + "/deliveries":
            return self._refuse("unknown_route", 404)
        if not self._auth():
            return
        q = dict(urllib.parse.parse_qsl(url.query))
        if q != {"host_id": IDENT["host_id"], "session_id": IDENT["session_id"]}:
            return self._refuse("bad_query")
        items, type(self).queued = list(type(self).queued), []
        self._send({"deliveries": items})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        body = json.loads(raw or b"{}")
        if not self._auth():
            return
        if self.path == BASE + "/receivers":
            if set(body) != {"host_id", "hostname", "session_id", "capability", "allowed_sender_agent_ids"}:
                return self._refuse("registration_fields")
            if body["capability"] != CAPABILITY or not isinstance(body["allowed_sender_agent_ids"], list):
                return self._refuse("registration_values")
            type(self).registrations.append(body)
            return self._send({"receiver_id": str(uuid.uuid4()), **{k: IDENT[k] for k in IDENT if k != "hostname"},
                               "hostname": body["hostname"], "capability": CAPABILITY, "admission_revision": REV,
                               "allowed_sender_agent_ids": body["allowed_sender_agent_ids"]}, 201)
        m = re.fullmatch(re.escape(BASE) + r"/deliveries/([0-9a-f-]{36})/ack", self.path)
        if not m:
            return self._refuse("unknown_route", 404)
        if not {"host_id", "session_id", "admission_revision", "state"} <= set(body) <= \
                {"host_id", "session_id", "admission_revision", "state", "result_comment_url", "error_code"}:
            return self._refuse("ack_fields")
        if (body["host_id"], body["session_id"], body["admission_revision"]) != (
                IDENT["host_id"], IDENT["session_id"], REV):
            return self._refuse("ack_identity", 403)
        cur = type(self).states.get(m.group(1), "queued")
        ok = {"queued": {"received", "failed"}, "received": {"acknowledged", "failed"}}
        if body["state"] != cur and body["state"] not in ok.get(cur, set()):
            return self._refuse("delivery_state_conflict", 409)
        type(self).states[m.group(1)] = body["state"]
        type(self).reports.append((body["state"], body))
        self._send({"delivery_id": m.group(1), "state": body["state"]})


class FakeSession:
    def __init__(self, known=True, target="=s:=w", paste_ok=True):
        self._known, self._target, self.paste_ok = known, target, paste_ok
        self.pastes: list = []
        self.created = 0                                    # nothing here may ever create a session

    def known(self):
        return self._known

    def target(self):
        return self._target

    def paste(self, target, text):
        self.pastes.append((target, text))
        return self.paste_ok


def delivery(text="read the file", **over):
    d = {"delivery_id": str(uuid.uuid4()), "request_id": str(uuid.uuid4()), "target": dict(IDENT),
         "expected_admission_revision": REV, "capability": CAPABILITY, "text": text,
         "text_sha256": over.get("text_sha256") or text_sha256(text), "created_at": "2026-11-01T12:00:00Z"}
    d.update(over)
    return d


class MetaDeliveryTest(unittest.TestCase):
    def setUp(self):
        FakeBackend.queued, FakeBackend.reports, FakeBackend.rejected = [], [], []
        FakeBackend.registrations, FakeBackend.states = [], {}
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeBackend)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.tmp = Path(tempfile.mkdtemp()) / "w"                 # own parent: an escape is visible there
        self.tmp.mkdir()
        self.client = MetaClient(f"http://127.0.0.1:{self.srv.server_address[1]}", AGENT_KEY, RUNNER_TOKEN)
        self.ident = Identity([SENDER], **IDENT)
        self.client.register(self.ident)
        self.session = FakeSession()
        self.rx = self._receiver(self.session)

    def _receiver(self, session):
        return MetaReceiver(self.ident, session, Journal(self.tmp / "journal"), self.client, self.tmp / "acks")

    def states(self):
        return [s for s, _ in FakeBackend.reports]

    def ack(self, d, **over):
        body = {"delivery_id": d["delivery_id"], "request_id": d["request_id"],
                "hostname": IDENT["hostname"], "session_id": IDENT["session_id"]}
        body.update(over)
        (self.tmp / "acks" / f"{d['delivery_id']}.json").write_text(json.dumps(body))

    # ---- AC1
    def test_exact_session_gets_text_once_and_same_request_same_receipt(self):
        d = delivery()
        r1 = self.rx.handle(d)
        r2 = self.rx.handle(d)
        self.assertEqual((r1["state"], r1["delivery_id"]), ("received", d["delivery_id"]))
        self.assertEqual(r1, r2)
        self.assertEqual(len(self.session.pastes), 1)
        self.assertIn("read the file", self.session.pastes[0][1])

    def test_duplicate_retries_a_lost_report_without_reinjecting(self):
        class Flaky:
            calls = 0

            def __init__(self, real):
                self.real = real

            admission_revision = REV

            def report(self, did, state, identity, error_code=""):
                Flaky.calls += 1
                if Flaky.calls == 1:
                    raise OSError("connection reset")
                return self.real.report(did, state, identity, error_code)
        rx = MetaReceiver(self.ident, self.session, Journal(self.tmp / "journal"), Flaky(self.client), self.tmp / "acks")
        d = delivery()
        rx.handle(d)
        self.assertEqual(self.states(), [])                      # the first report was lost
        r = rx.handle(d)
        self.assertEqual((r["state"], self.states()), ("received", ["received"]))
        self.assertEqual(len(self.session.pastes), 1)

    def test_acknowledged_only_after_producer_confirms_hostname_session_delivery(self):
        d = delivery()
        self.rx.handle(d)
        self.ack(d, hostname="other-host")                       # wrong hostname: not an ack
        self.rx.reconcile()
        self.assertNotIn("acknowledged", self.states())
        self.ack(d, session_id="22222222-2222-2222-2222-222222222222")
        self.rx.reconcile()
        self.ack(d, delivery_id=str(uuid.uuid4()))
        self.rx.reconcile()
        self.assertNotIn("acknowledged", self.states())
        self.ack(d)
        self.rx.reconcile()
        self.assertEqual(self.states(), ["received", "received", "acknowledged"])   # repeated state is a backend no-op
        self.assertEqual(FakeBackend.rejected, [])
        self.assertEqual(self.rx.handle(d)["state"], "acknowledged")
        self.assertEqual(len(self.session.pastes), 1)

    def test_step_over_http_claims_and_reports(self):
        d = delivery()
        FakeBackend.queued = [d]
        self.assertEqual(self.rx.step(), 1)
        self.assertEqual(self.states(), ["received"])
        self.assertEqual(FakeBackend.rejected, [])

    # ---- AC2
    def assert_refused(self, d, code, session=None):
        s = session or self.session
        r = self._receiver(s).handle(d)
        self.assertEqual((r["state"], r["error_code"]), ("failed", code))
        self.assertEqual(s.pastes, [])
        self.assertEqual(s.created, 0)

    def test_wrong_host_or_session_is_refused_with_zero_injection(self):
        self.assert_refused(delivery(target={**IDENT, "host_id": "host-B"}), "wrong_target")
        self.assert_refused(delivery(target={**IDENT, "hostname": "elsewhere"}), "wrong_target")
        self.assert_refused(delivery(target={**IDENT, "session_id": str(uuid.uuid4())}), "wrong_target")
        partial = {k: v for k, v in IDENT.items() if k != "runner_id"}
        self.assert_refused(delivery(target=partial), "wrong_target")

    def test_stale_revision_missing_transcript_missing_window(self):
        self.assert_refused(delivery(expected_admission_revision=REV - 1), "stale_revision")
        self.assert_refused(delivery(), "session_missing", FakeSession(known=False))
        self.assert_refused(delivery(), "session_missing", FakeSession(target=None))

    def test_crash_after_possible_injection_fails_closed_without_reinjection(self):
        d = delivery()
        Journal(self.tmp / "journal").put(d["request_id"], {
            "delivery_id": d["delivery_id"], "request_id": d["request_id"],
            "text_sha256": d["text_sha256"], "state": "injecting"})      # what a crash mid-paste leaves
        r = self.rx.handle(d)
        self.assertEqual((r["state"], r["error_code"]), ("uncertain", "delivery_uncertain"))
        self.assertEqual(self.session.pastes, [])
        self.assertEqual(self.states(), ["failed"])

    def test_refused_paste_is_uncertain_not_retried(self):
        s = FakeSession(paste_ok=False)
        rx = self._receiver(s)
        d = delivery()
        self.assertEqual(rx.handle(d)["state"], "uncertain")
        self.assertEqual(rx.handle(d)["state"], "uncertain")
        self.assertEqual(len(s.pastes), 1)

    def test_changed_text_under_same_request_id_conflicts(self):
        d = delivery()
        self.rx.handle(d)
        other = delivery("different", request_id=d["request_id"], delivery_id=d["delivery_id"])
        r = self.rx.handle(other)
        self.assertEqual(r["error_code"], "request_conflict")
        self.assertEqual(len(self.session.pastes), 1)

    # ---- AC3
    def test_no_command_fields_oversize_or_bad_hash(self):
        self.assert_refused(delivery(argv=["rm", "-rf", "/"]), "forbidden_field")
        self.assert_refused(delivery(command="ls"), "forbidden_field")
        self.assert_refused(delivery(capability="execute"), "forbidden_field")
        big = "x" * (16 * 1024 + 1)
        self.assert_refused(delivery(big), "bad_request")
        self.assert_refused(delivery(text_sha256="0" * 64), "bad_request")
        self.assert_refused(delivery(request_id="not-a-uuid"), "bad_request")

    def test_unpaired_surrogate_text_is_a_typed_refusal(self):
        self.assert_refused(delivery("bad \ud800 text", text_sha256="0" * 64), "bad_request")

    def test_traversal_ids_never_touch_the_filesystem_or_url(self):
        for bad in ("../../escape", "a/b", "x" * 5000, "", None, 7):
            for field in ("request_id", "delivery_id"):
                before = sorted(p.name for p in self.tmp.rglob("*"))
                reports = len(FakeBackend.reports)
                self.assert_refused(delivery(**{field: bad}), "bad_request")
                self.assertEqual(sorted(p.name for p in self.tmp.rglob("*")), before)
                if field == "delivery_id":                                # no URL is built from a bad id
                    self.assertEqual(len(FakeBackend.reports), reports)
        self.assertFalse((self.tmp.parent / "escape.json").exists())
        with self.assertRaises(ValueError):
            Journal(self.tmp / "j2").put("../../escape", {})
        r = self.rx.handle(delivery(text_sha256="z" * 5000, request_id="../x"))
        self.assertEqual(r["text_sha256"], "")                             # receipt stays bounded

    def test_receipt_carries_no_text_and_journal_stores_no_text(self):
        d = delivery("secret-looking-payload-123")
        r = self.rx.handle(d)
        self.assertNotIn("secret-looking-payload-123", json.dumps(r))
        self.assertNotIn("secret-looking-payload-123", json.dumps(FakeBackend.reports))
        stored = (self.tmp / "journal" / f"{d['request_id']}.json").read_text()
        self.assertNotIn("secret-looking-payload-123", stored)
        self.assertEqual(oct((self.tmp / "journal" / f"{d['request_id']}.json").stat().st_mode & 0o777), "0o600")

    # ---- frozen HTTP contract v3: strict fixture, negative control for the old client, positive for the new
    def test_old_provisional_client_is_rejected_by_the_strict_backend(self):
        import urllib.request
        url = f"http://127.0.0.1:{self.srv.server_address[1]}/api/v1/runner/session-metadata/deliveries"
        req = urllib.request.Request(url + "?host_id=host-A&session_id=" + IDENT["session_id"])
        req.add_header("X-Agent-Key", AGENT_KEY)
        req.add_header("X-Chat-Runner-Id", IDENT["runner_id"])
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req)
        self.assertEqual(cm.exception.code, 404)
        self.assertEqual(FakeBackend.rejected[-1][1], "unknown_route")

    def test_new_client_registration_ack_and_poll_match_the_contract(self):
        self.assertEqual(self.client.admission_revision, REV)
        self.assertEqual(FakeBackend.registrations, [{
            "host_id": "host-A", "hostname": IDENT["hostname"], "session_id": IDENT["session_id"],
            "capability": CAPABILITY, "allowed_sender_agent_ids": [SENDER]}])
        d = delivery()
        FakeBackend.queued = [d]                                   # server delivery carries created_at
        self.assertEqual(self.rx.step(), 1)
        self.assertEqual(FakeBackend.states[d["delivery_id"]], "received")
        self.assertEqual(FakeBackend.reports[0][1], {"host_id": "host-A", "session_id": IDENT["session_id"],
                                                     "admission_revision": REV, "state": "received"})
        self.assertEqual(FakeBackend.rejected, [])

    def test_wrong_token_is_a_backend_refusal_not_a_delivery(self):
        bad = MetaClient(f"http://127.0.0.1:{self.srv.server_address[1]}", AGENT_KEY, "other-token")
        with self.assertRaises(Exception):
            bad.register(self.ident)
        self.assertEqual(FakeBackend.registrations, [{**FakeBackend.registrations[0]}])   # only the setUp one

    def test_unregistered_receiver_has_no_revision_and_injects_nothing(self):
        fresh = MetaClient(f"http://127.0.0.1:{self.srv.server_address[1]}", AGENT_KEY, RUNNER_TOKEN)
        s = FakeSession()
        rx = MetaReceiver(self.ident, s, Journal(self.tmp / "journal2"), fresh, self.tmp / "acks2")
        r = rx.handle(delivery())
        self.assertEqual((r["state"], r["error_code"], s.pastes), ("failed", "stale_revision", []))

    def test_registration_answer_with_another_hostname_is_rejected(self):
        fresh = MetaClient(f"http://127.0.0.1:{self.srv.server_address[1]}", AGENT_KEY, RUNNER_TOKEN)
        real = fresh._mesh._request

        def forged(method, path, body=None, query=None, **kw):       # the backend answers another hostname
            out = real(method, path, body, query, **kw)
            return {**out, "hostname": "elsewhere.local"} if path == "/receivers" else out
        fresh._mesh._request = forged
        with self.assertRaises(Exception):
            fresh.register(self.ident)
        self.assertIsNone(fresh.admission_revision)

    def test_registration_answer_for_another_runner_is_rejected(self):
        other = Identity([SENDER], **{**IDENT, "runner_id": str(uuid.uuid4())})
        with self.assertRaises(Exception):
            self.client.register(other)

    def test_client_refuses_plain_http_to_non_loopback(self):
        with self.assertRaises(ValueError):
            MetaClient("http://example.com", "secret", "tok")

    def test_identity_needs_every_field_and_a_revision(self):
        with self.assertRaises(ValueError):
            Identity([SENDER], **{**IDENT, "host_id": ""})
        with self.assertRaises(ValueError):
            Identity(["not-a-uuid"], **IDENT)
        with self.assertRaises(ValueError):
            Identity(None, **IDENT)


class SettingsTest(unittest.TestCase):
    SECTION = {"enabled": True, "agent": "agent-a", "credential_env": "META_TEST_KEY", "allowed_sender_agent_ids": [SENDER],
               **IDENT}

    def read(self, file, env, what):
        return "key"

    def test_absent_or_disabled_is_off(self):
        self.assertIsNone(parse_settings(None, self.read))
        self.assertIsNone(parse_settings({}, self.read))
        self.assertIsNone(parse_settings({**self.SECTION, "enabled": False}, self.read))

    def test_enabled_needs_full_admission(self):
        self.assertEqual(parse_settings(self.SECTION, self.read).identity.fields, IDENT)
        for k in ("session_id", "host_id", "allowed_sender_agent_ids", "agent"):
            with self.assertRaises(ValueError, msg=k):
                parse_settings({**self.SECTION, k: ""}, self.read)
        with self.assertRaises(ValueError):
            parse_settings({**self.SECTION, "command": "ls"}, self.read)

    def load(self, extra=""):
        from mesh_runner import config
        d = Path(tempfile.mkdtemp())
        (d / "cred").write_text("c-secret")
        (d / "cred").chmod(0o600)
        p = d / "runner.toml"
        p.write_text(f'[runner]\nmesh_url = "http://mesh"\n{extra.replace("CRED", str(d / "cred"))}\n')
        return config.load(p)

    def test_config_off_by_default_and_on_when_admitted(self):
        self.assertIsNone(self.load().session_metadata)
        body = "\n".join(f'{k} = "{v}"' for k, v in IDENT.items())
        cfg = self.load(f'[session_metadata]\nenabled = true\nagent = "agent-a"\ncredential_file = "CRED"\n'
                        f'allowed_sender_agent_ids = ["{SENDER}"]\n{body}\n')
        self.assertEqual(cfg.session_metadata.identity.fields, IDENT)
        self.assertNotIn("c-secret", repr(cfg))

    def test_config_enabled_without_allowed_senders_fails_closed(self):
        from mesh_runner import config
        with self.assertRaises(config.ConfigError):
            self.load('[session_metadata]\nenabled = true\nagent = "agent-a"\ncredential_file = "CRED"\n')


if __name__ == "__main__":
    unittest.main()
