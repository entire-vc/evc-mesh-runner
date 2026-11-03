"""host_local_qa_assignment_v1: the real receiver, journal, ledger, lock and iTerm injector; only the two external boundaries
(Mesh backend, iTerm) are fakes. Every conformance id of the frozen contract has a case below (see CONFORMANCE_MAP)."""
import io
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import uuid
from pathlib import Path

from mesh_runner import iterm_transport as it
from mesh_runner import meta_delivery as md
from mesh_runner import qa_assignment as qa
from mesh_runner import session_receiver as sr

import qa_bundle_fixture as fx

PROD_PINS = dict(qa.PINS)          # the literal production pins, kept for the pin test
fx.use(qa)                         # the private frozen bytes are not packaged: the suite runs on a synthetic bundle

NOW = 1_800_000_000
P = qa.pinned()
POLICY = P["policy"]
TARGET = dict(workspace_id=POLICY["workspace_id"], agent_id=POLICY["agent_id"], runner_id="aaaaaaaa-0000-4000-8000-000000000003",
              host_id="host-A", hostname=POLICY["hostname"], session_id="11111111-1111-1111-1111-111111111111")
SID, TTY, STARTED = "7A3C1F52-0B9D-4E61-8C2A-5D1E9F0B6A44", "/dev/ttys007", "Sun Nov 1 10:00:00 2026"
PIN = it.ITermPin(SID, TTY, 4242, STARTED)
PRODUCER = dict(transport="iterm", terminal_session_id=SID, tty=TTY, producer_pid=4242, producer_started=STARTED)
SENDER = POLICY["allowed_sender_agent_ids"][0]
RUNNER_TOKEN, QA_KEY = "runner-token-1", "qa-scoped-secret"
BASE = "http://127.0.0.1:9"
RECEIVER_ID = "bbbbbbbb-0000-4000-8000-000000000001"
REV = 3
EXPIRES = NOW + 3600


class Resp:
    def __init__(self, status=200, body=None):
        self.status, self._raw = status, (json.dumps(body).encode() if body is not None else b"")

    def read(self, n=-1):
        return self._raw


class Backend:
    """Mesh side of the QA routes, strict about the contract: exact paths, Authorization bearer only (never X-Agent-Key), the
    runner token header, exact bodies; any drift is an HTTP error so a client that leaves the contract fails here."""

    def __init__(self, clock):
        self.clock, self.queued, self.reports, self.checks, self.registrations = clock, [], [], [], []
        self.deleted, self.rejected, self.urls = [], [], []
        self.events = None                       # shared with the iTerm fake: the order of check / stage / submit
        self.check_mode, self.fail_check_from, self.receiver_id, self.revoked = "ok", None, RECEIVER_ID, False
        self.states: dict = {}

    def __call__(self, req, timeout=None):
        url, method = req.full_url, req.get_method()
        self.urls.append((method, url))
        hdr = {k.lower(): v for k, v in req.header_items()}
        if hdr.get("authorization") != "Bearer " + QA_KEY or hdr.get("x-runner-token") != RUNNER_TOKEN or "x-agent-key" in hdr:
            return self._err(req, 401)
        body = json.loads(req.data) if req.data else None
        route = "/api/v1/runner/host-local-qa/v1"
        if url.startswith(qa.CHECK_BASE + qa.ROUTE + "/deliveries/") and url.endswith("/check"):
            return self._check(req, url.rsplit("/", 2)[1], body)
        if not url.startswith(BASE + route):
            self.rejected.append(url)
            return self._err(req, 404)
        path = url[len(BASE + route):]
        if method == "POST" and path == "/receivers":
            if set(body) != {"schema_version", "capability", "policy_sha256", "target", "producer", "expires",
                             "allowed_sender_agent_ids"} or body["capability"] != qa.CAPABILITY:
                return self._err(req, 400)
            self.registrations.append(body)
            return Resp(201, {**body, "receiver_id": self.receiver_id, "admission_revision": REV,
                              "workspace_id": TARGET["workspace_id"], "agent_id": TARGET["agent_id"], "runner_id": TARGET["runner_id"]})
        if method == "DELETE" and path == f"/receivers/{self.receiver_id}":
            self.deleted.append(self.receiver_id)
            self.revoked = True
            return Resp(204)
        if method == "GET" and path.startswith("/deliveries?"):
            items, self.queued = self.queued, []
            return Resp(200, {"deliveries": items})
        if method == "POST" and path.endswith("/ack"):
            self.reports.append(body)
            return Resp(200, {"delivery_id": body["delivery_id"], "state": body["state"]})
        return self._err(req, 404)

    def _err(self, req, code):
        raise urllib.error.HTTPError(req.full_url, code, "x", {}, io.BytesIO(b"{}"))

    def _check(self, req, delivery_id, body):
        self.checks.append(body)
        if self.events is not None:
            self.events.append("check")
        if set(body) != set(qa.CHECK_FIELDS) or body["delivery_id"] != delivery_id:
            return self._err(req, 400)
        mode = self.check_mode
        if self.fail_check_from is not None and len(self.checks) >= self.fail_check_from:
            mode = "inactive"
        if self.revoked:
            mode = "inactive"
        if mode == "timeout":
            raise urllib.error.URLError("timed out")
        if mode == "http500":
            return self._err(req, 500)
        if mode == "http403":
            return self._err(req, 403)
        if mode == "malformed":
            return Resp(200, {"active": True})
        if mode == "inactive":
            return Resp(200, {"active": False})
        ans = {"active": True, **body, "checked_at": qa.utc(self.clock())}
        if mode == "extra":
            ans["extra"] = 1
        if mode == "mismatch-revision":
            ans["admission_revision"] += 1
        if mode == "mismatch-target":
            ans["target"] = dict(body["target"], host_id="other")
        if mode == "mismatch-text":
            ans["text_sha256"] = "0" * 64
        if mode == "mismatch-delivery":
            ans["delivery_id"] = str(uuid.uuid4())
        if mode == "stale":
            ans["checked_at"] = qa.utc(self.clock() - 3600)
        if mode == "bool-active":
            ans["active"] = 1
        return Resp(200, ans)


class FakeITerm:
    def __init__(self, events):
        self.sessions, self.events = {SID: TTY}, events
        self.pasted, self.submitted, self.legacy_writes = [], [], []
        self.fail_stage = False
        self.before_submit = None
        self.proc = ("4242", "4242", "ttys007", STARTED)
        self.argv = ["claude", "--resume", TARGET["session_id"]]

    def argv_of(self, pid):
        return self.argv

    def probe(self, sid):
        return (self.sessions[sid], "") if sid in self.sessions else ("", "target_missing")

    def stage(self, sid, tty, text, pid, started):
        if self.fail_stage or self.sessions.get(sid) != tty:
            return False
        self.events.append("stage")
        self.pasted.append(text)
        return True

    def submit(self, sid, tty, pid, started):
        if self.before_submit:
            self.before_submit()
        if self.sessions.get(sid) != tty:
            return False
        self.events.append("submit")
        self.submitted.append(sid)
        return True

    def write(self, *a):
        self.legacy_writes.append(a)
        return True

    def info(self, pid):
        return self.proc


class Clock:
    def __init__(self):
        self.t = NOW

    def __call__(self):
        return self.t


def envelope(**over):
    d = {"artifact_id": POLICY["artifact_id"], "candidate": POLICY["candidate"], "capability": qa.CAPABILITY,
         "expected_admission_revision": REV, "expires": EXPIRES, "operation_manifest_sha256": POLICY["operation_manifest_sha256"],
         "policy_sha256": P["policy_sha256"], "producer": dict(PRODUCER), "qa_package_sha256": POLICY["qa_package_sha256"],
         "receiver_id": RECEIVER_ID, "request_id": str(uuid.uuid4()), "schema_version": 1,
         "source_task_id": POLICY["source_task_id"], "target": dict(TARGET), "text": P["text"], "text_sha256": P["text_sha256"],
         "delivery_id": str(uuid.uuid4()), "created_at": "2026-11-03T00:00:00Z"}
    d.update(over)
    return d


class QATest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "ledger"
        self.clock = Clock()
        self.events = []
        self.backend = Backend(self.clock)
        self.backend.events = self.events
        self.iterm = FakeITerm(self.events)
        self.build()

    def build(self):
        self.ident = qa.QAIdentity(TARGET, PRODUCER, [SENDER], NOW + 7200)
        self.client = qa.QAClient(BASE, QA_KEY, RUNNER_TOKEN, self.ident, self.root, opener=self.backend, clock=self.clock)
        self.session = it.ITermSession(self.iterm, PIN, TARGET["session_id"], lambda s: True, self.iterm.info)
        self.journal = md.Journal(self.root / "journal")
        self.observe = self.observer()
        self.rx = qa.QAReceiver(self.ident, self.session, self.journal, self.client, self.root, clock=self.clock,
                                observe=self.observe)
        self.client.register(self.ident)

    def observer(self, runner=TARGET["runner_id"], known=lambda s: True):
        return qa.make_observer(self.ident, PIN, lambda: runner, known, self.iterm, self.iterm.info, self.iterm.argv_of,
                                TARGET["hostname"], self.clock)

    def deliver(self, **over):
        d = envelope(**over)
        return d, self.rx.handle(d)

    def assert_zero(self, receipt, code):
        self.assertEqual(receipt["state"], "failed", receipt)
        self.assertEqual(receipt["error_code"], code, receipt)
        self.assertEqual((self.iterm.pasted, self.iterm.submitted, self.iterm.legacy_writes), ([], [], []))

    # ------------------------------------------------------------------ exact
    def test_exact_positive_control_one_injection_received_only(self):
        d, r = self.deliver()
        self.assertEqual(r["state"], "received")
        self.assertEqual(self.events, ["check", "stage", "check", "submit"])     # authority before paste AND before Enter
        self.assertEqual(len(self.iterm.pasted), 1)
        self.assertIn(P["text"], self.iterm.pasted[0])
        self.assertEqual(self.iterm.legacy_writes, [])
        for k in ("capability", "target", "producer", "receiver_id", "admission_revision", "policy_sha256", "text_sha256", "expires"):
            self.assertIn(k, r)
        self.assertEqual(r["admission_revision"], REV)
        self.assertEqual([x["state"] for x in self.backend.reports], ["received"])
        rec = self.journal.get(d["request_id"])
        self.assertEqual(rec["capability"], qa.CAPABILITY)

    def test_check_body_is_exact_and_carries_no_text_or_token(self):
        self.deliver()
        body = self.backend.checks[0]
        self.assertEqual(set(body), set(qa.CHECK_FIELDS))
        self.assertNotIn(QA_KEY, json.dumps(body))
        self.assertNotIn("Host-local QA", json.dumps(body))
        self.assertTrue(all(u.startswith(qa.CHECK_BASE) for m, u in self.backend.urls if u.endswith("/check")))

    # ------------------------------------------------------------------ legacy <-> QA
    def test_legacy_rejects_qa_payload_zero_paste(self):
        legacy = md.MetaReceiver(md.Identity([SENDER], **TARGET), self.session, self.journal, _LegacyClient(), self.root / "acks")
        r = legacy.handle(envelope())
        self.assertEqual(r["state"], "failed")
        self.assertEqual((self.iterm.pasted, self.iterm.legacy_writes), ([], []))

    def test_qa_rejects_legacy_capability_and_envelope(self):
        legacy = dict(delivery_id=str(uuid.uuid4()), request_id=str(uuid.uuid4()), target=dict(TARGET), capability=md.CAPABILITY,
                      expected_admission_revision=REV, text="hello", text_sha256=md.text_sha256("hello"), created_at="x")
        self.assert_zero(self.rx.handle(legacy), "bad_request")             # required QA fields are missing
        self.assert_zero(self.rx.handle(envelope(capability=md.CAPABILITY)), "bad_request")

    def test_request_id_collision_across_capabilities_does_not_bypass(self):
        rid = str(uuid.uuid4())
        legacy_text = "legacy"
        self.journal.put(rid, {"delivery_id": str(uuid.uuid4()), "request_id": rid, "text_sha256": md.text_sha256(legacy_text),
                               "state": "received"})                      # an old record: no capability key = legacy
        self.assert_zero(self.rx.handle(envelope(request_id=rid)), "request_conflict")
        legacy = md.MetaReceiver(md.Identity([SENDER], **TARGET), self.session, self.journal, _LegacyClient(rev=REV),
                                 self.root / "acks")
        d, _ = self.deliver()                                              # a QA record now exists
        probe = dict(delivery_id=d["delivery_id"], request_id=d["request_id"], target=dict(TARGET), capability=md.CAPABILITY,
                     expected_admission_revision=REV, text="x", text_sha256=md.text_sha256("x"), created_at="x")
        self.assertEqual(legacy.handle(probe)["error_code"], "request_conflict")
        self.assertEqual(len(self.iterm.pasted), 1)

    # ------------------------------------------------------------------ schema strictness
    def test_unknown_fields_at_every_depth_refused_before_any_effect(self):
        for extra in ({"command": "ls"}, {"argv": ["x"]}, {"cwd": "/"}, {"tool": "x"}, {"url": "https://x"}):
            self.assert_zero(self.rx.handle(envelope(**extra)), "forbidden_field")
        self.assert_zero(self.rx.handle(envelope(target=dict(TARGET, cwd="/"))), "forbidden_field")
        self.assert_zero(self.rx.handle(envelope(producer=dict(PRODUCER, argv=[]))), "forbidden_field")
        self.assertEqual(self.backend.checks, [])

    def test_hash_any_pinned_byte_changed(self):
        for over in (dict(text=P["text"] + " "), dict(text=P["text"][:-1]), dict(text_sha256="0" * 64),
                     dict(qa_package_sha256="0" * 64), dict(operation_manifest_sha256="0" * 64), dict(policy_sha256="0" * 64),
                     dict(candidate="0" * 40), dict(artifact_id=1), dict(source_task_id=str(uuid.uuid4()))):
            self.assert_zero(self.rx.handle(envelope(**over)), "bad_request")

    def test_identity_each_field_alone_is_refused_identically(self):
        codes = set()
        for k in TARGET:
            other = "cccccccc-0000-4000-8000-000000000009" if k.endswith("_id") and md.is_uuid(TARGET[k]) else "other"
            r = self.rx.handle(envelope(target=dict(TARGET, **{k: other})))
            codes.add(r["error_code"])
            self.assertEqual((self.iterm.pasted, self.iterm.submitted), ([], []))
        self.assertTrue(codes <= {"bad_request", "wrong_target"}, codes)       # schema consts (ws/agent/hostname) or wrong_target
        self.assertEqual(self.backend.checks, [])

    def test_producer_fields_refused_before_paste(self):
        for k, v in (("terminal_session_id", "7A3C1F52-0B9D-4E61-8C2A-5D1E9F0B6A45"), ("tty", "/dev/ttys008"), ("producer_pid", 1),
                     ("producer_started", "Mon Nov 2 10:00:00 2026")):
            self.assert_zero(self.rx.handle(envelope(producer=dict(PRODUCER, **{k: v}))), "wrong_producer")

    def test_revision_missing_wrong_bool_stale_other_receiver(self):
        for over, code in ((dict(expected_admission_revision=REV + 1), "stale_revision"), (dict(expected_admission_revision=True), "bad_request"),
                           (dict(expected_admission_revision=0), "bad_request"), (dict(expected_admission_revision="3"), "bad_request"),
                           (dict(receiver_id="dddddddd-0000-4000-8000-000000000001"), "wrong_receiver")):
            self.assert_zero(self.rx.handle(envelope(**over)), code)
        d = envelope()
        del d["expected_admission_revision"]
        self.assert_zero(self.rx.handle(d), "bad_request")
        self.client.admission_revision = None                                 # unregistered = fail closed
        self.assert_zero(self.rx.handle(envelope()), "stale_revision")

    def test_expired_and_over_eight_hours(self):
        self.assert_zero(self.rx.handle(envelope(expires=NOW)), "expired")
        self.assert_zero(self.rx.handle(envelope(expires=NOW - 5)), "expired")
        self.assert_zero(self.rx.handle(envelope(expires=NOW + 28801)), "expired")
        self.clock.t = EXPIRES                                                # queued item expires before delivery
        self.assert_zero(self.rx.handle(envelope()), "expired")

    # ------------------------------------------------------------------ revoke races and authority failures
    def test_revoked_before_intent_boundary_is_zero_paste_and_not_uncertain(self):
        self.backend.check_mode = "inactive"
        d, r = self.deliver()
        self.assertEqual((r["state"], r["error_code"]), ("failed", "revoked"))
        self.assertEqual((self.iterm.pasted, self.iterm.submitted), ([], []))
        self.assertEqual(self.journal.get(d["request_id"])["state"], "failed")   # intent was journaled, no byte moved

    def test_revoked_between_paste_and_enter_no_enter_and_uncertain(self):
        self.backend.fail_check_from = 2
        d, r = self.deliver()
        self.assertEqual((r["state"], r["error_code"]), ("uncertain", "revoked"))
        self.assertEqual(len(self.iterm.pasted), 1)
        self.assertEqual(self.iterm.submitted, [])                            # the Enter never went out
        self.assertEqual(self.events, ["check", "stage", "check"])

    def test_local_revoke_disables_without_asking_the_backend(self):
        (self.root / qa.ADMISSION_FILE).replace(self.root / qa.REVOKED_FILE)
        self.assert_zero(self.rx.handle(envelope()), "revoked")

    def test_timeout_malformed_mismatch_unavailable_never_allow(self):
        for mode, code in (("timeout", "check_unavailable"), ("http500", "check_unavailable"), ("http403", "check_denied"),
                           ("malformed", "check_malformed"), ("extra", "check_malformed"), ("stale", "check_malformed"),
                           ("bool-active", "check_malformed"), ("mismatch-revision", "check_mismatch"),
                           ("mismatch-target", "check_mismatch"), ("mismatch-text", "check_mismatch"),
                           ("mismatch-delivery", "check_mismatch")):
            self.backend.check_mode = mode
            _, r = self.deliver()
            self.assertEqual((r["state"], r["error_code"]), ("failed", code), mode)
            self.assertEqual((self.iterm.pasted, self.iterm.submitted), ([], []), mode)

    def test_no_cached_success_every_boundary_asks_again(self):
        self.deliver()
        self.deliver()
        self.assertEqual(len(self.backend.checks), 4)

    def test_check_never_follows_a_redirect(self):
        import http.server
        hits = []

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                hits.append(self.path)
                self.send_response(307)
                self.send_header("Location", "http://127.0.0.1:1/elsewhere")
                self.end_headers()

            def log_message(self, *a):
                pass
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        client = qa.QAClient(f"http://127.0.0.1:{srv.server_port}", QA_KEY, RUNNER_TOKEN, self.ident, self.root)
        with self.assertRaises(md.MeshError):
            client._call("POST", f"http://127.0.0.1:{srv.server_port}/x", {})
        self.assertEqual(hits, ["/x"])

    # ------------------------------------------------------------------ foreground / terminal / producer
    def test_stale_ambiguous_replaced_foreground_terminal_producer_zero_paste(self):
        cases = {"replaced terminal": lambda: self.iterm.sessions.update({SID: "/dev/ttys099"}),
                 "terminal gone": lambda: self.iterm.sessions.clear(),
                 "producer replaced": lambda: setattr(self.iterm, "proc", ("4242", "4242", "ttys007", "Mon Nov 2 1:00:00 2026")),
                 "not foreground": lambda: setattr(self.iterm, "proc", ("4242", "9999", "ttys007", STARTED)),
                 "producer gone": lambda: setattr(self.iterm, "proc", ("", "", "", ""))}
        for name, mutate in cases.items():
            self.setUp()
            mutate()
            _, r = self.deliver()
            self.assertEqual(r["state"], "failed", name)
            self.assertEqual((self.iterm.pasted, self.iterm.submitted), ([], []), name)

    def test_binding_lost_between_paste_and_enter_no_enter(self):
        self.iterm.before_submit = None
        orig = self.iterm.stage

        def stage_then_lose(*a):
            ok = orig(*a)
            self.iterm.proc = ("4242", "9999", "ttys007", STARTED)               # producer loses the foreground right after paste
            return ok
        self.iterm.stage = stage_then_lose
        _, r = self.deliver()
        self.assertEqual(r["state"], "uncertain")
        self.assertEqual(self.iterm.submitted, [])

    # ------------------------------------------------------------------ duplicate / conflict / uncertain
    def test_duplicate_identical_replay_one_injection_same_receipt(self):
        d, r1 = self.deliver()
        r2 = self.rx.handle(d)
        self.assertEqual((r1["state"], r2["state"], r1["delivery_id"], r2["delivery_id"]), ("received",) * 2 + (d["delivery_id"],) * 2)
        self.assertEqual(len(self.iterm.pasted), 1)
        self.clock.t += 7200                                                    # replay after expiry is still the same receipt
        self.assertEqual(self.rx.handle(d)["state"], "received")

    def test_conflict_any_change_is_409_and_original_unchanged(self):
        d, _ = self.deliver()
        before = self.journal.get(d["request_id"])
        changes = (dict(expires=EXPIRES + 1), dict(producer=dict(PRODUCER, producer_pid=4243)),
                   dict(delivery_id=str(uuid.uuid4())), dict(capability="readonly_metadata"), dict(unknown_field=1),
                   dict(target=dict(TARGET, hostname="other")), dict(receiver_id=str(uuid.uuid4())),
                   dict(policy_sha256="0" * 64), dict(text="changed"), dict(created_at="2026-11-03T00:00:01Z"))
        for over in changes:                      # even a change that would NOT validate is a conflict, never a validation error
            r = self.rx.handle(dict(d, **over))
            self.assertEqual(r["error_code"], "request_conflict", over)
        self.assertEqual(self.rx.handle(dict(d, expected_admission_revision=REV))["state"], "received")   # no change at all
        r = self.rx.handle(dict(d, expires=EXPIRES + 1))
        self.assertEqual(r["error_code"], "request_conflict")
        self.assertEqual(self.journal.get(d["request_id"]), before)
        self.assertEqual(len(self.iterm.pasted), 1)

    def test_conflict_for_another_delivery_is_reported_failed_and_same_delivery_is_not(self):
        d, _ = self.deliver()
        before, n = self.journal.get(d["request_id"]), len(self.backend.reports)
        r = self.rx.handle(dict(d, delivery_id=str(uuid.uuid4()), expires=EXPIRES + 1))
        self.assertEqual(r["error_code"], "request_conflict")
        rep = self.backend.reports[n:]
        self.assertEqual([(x["state"], x["error_code"], x["request_id"]) for x in rep],
                         [("failed", "request_conflict", d["request_id"])])
        self.assertNotEqual(rep[0]["delivery_id"], d["delivery_id"])
        self.assertEqual(rep[0]["receiver_id"], RECEIVER_ID)
        n = len(self.backend.reports)
        self.rx.handle(dict(d, expires=EXPIRES + 1))                      # same delivery, changed: local answer only
        self.assertEqual(len(self.backend.reports), n)
        self.assertEqual(self.journal.get(d["request_id"]), before)
        self.assertEqual(len(self.iterm.pasted), 1)

    def test_uncertain_after_stage_failure_then_restart_never_replays(self):
        self.iterm.fail_stage = True
        d, r = self.deliver()
        self.assertEqual(r["state"], "uncertain")
        self.iterm.fail_stage = False
        self.build()                                                             # a restart over the same ledger
        self.assertEqual(self.rx.handle(d)["state"], "uncertain")
        self.assertEqual(self.iterm.pasted, [])

    def test_crash_after_intent_before_result_settles_uncertain_on_restart(self):
        d = envelope()
        rec = {"capability": qa.CAPABILITY, "delivery_id": d["delivery_id"], "request_id": d["request_id"],
               "text_sha256": d["text_sha256"], "envelope_sha256": qa.digest(qa.QAReceiver.envelope_of(d)), "created_at": d["created_at"],
               "state": "injecting",
               "bytes": 1, "qa": {"receiver_id": RECEIVER_ID, "admission_revision": REV, "policy_sha256": d["policy_sha256"],
                                  "target": d["target"], "producer": d["producer"], "expires": EXPIRES}}
        self.journal.put(d["request_id"], rec)
        self.rx.reconcile()
        self.assertEqual(self.journal.get(d["request_id"])["state"], "uncertain")
        self.assertEqual(self.rx.handle(d)["state"], "uncertain")
        self.assertEqual((self.iterm.pasted, self.iterm.submitted), ([], []))

    # ------------------------------------------------------------------ ack
    def ack_file(self, d, **over):
        q = self.journal.get(d["request_id"])["qa"]
        ack = {"delivery_id": d["delivery_id"], "request_id": d["request_id"], "receiver_id": q["receiver_id"],
               "admission_revision": q["admission_revision"], "capability": qa.CAPABILITY, "policy_sha256": q["policy_sha256"],
               "text_sha256": d["text_sha256"], "target": q["target"], "producer": q["producer"], "observed_at": "2026-11-03T01:00:00Z"}
        ack.update(over)
        (self.rx.ack_dir).mkdir(parents=True, exist_ok=True)
        (self.rx.ack_dir / f"{d['delivery_id']}.json").write_text(json.dumps(ack))

    def test_ack_is_not_install_and_must_name_every_identity_and_pin(self):
        d, _ = self.deliver()
        self.ack_file(d, producer=dict(PRODUCER, producer_pid=1))
        self.rx.reconcile()
        self.assertEqual(self.journal.get(d["request_id"])["state"], "received")
        self.ack_file(d, extra=1)
        self.rx.reconcile()
        self.assertEqual(self.journal.get(d["request_id"])["state"], "received")
        self.ack_file(d)
        self.rx.reconcile()
        rec = self.journal.get(d["request_id"])
        self.assertEqual(rec["state"], "acknowledged")
        self.assertEqual([r["state"] for r in self.backend.reports], ["received", "received", "acknowledged"][:len(self.backend.reports)])
        self.assertNotIn("installed", json.dumps(rec) + json.dumps(self.backend.reports))
        self.assertLessEqual({r["state"] for r in self.backend.reports}, {"received", "acknowledged"})

    def test_ack_bodies_are_bound_and_never_skip_received(self):
        d, _ = self.deliver()
        self.ack_file(d)
        self.rx.reconcile()
        states = [r["state"] for r in self.backend.reports]
        self.assertEqual(states[0], "received")
        self.assertEqual(states[-1], "acknowledged")
        for body in self.backend.reports:
            self.assertEqual({k: body[k] for k in qa.CHECK_FIELDS}, {k: v for k, v in self.backend.checks[0].items()})
            self.assertIn("observed_at", body)
            self.assertNotIn("text", body)

    # ------------------------------------------------------------------ guard (mutation-guard, mini-readback contract)
    def guard(self, boundary="asset_replace_main_js", d=None, **kw):
        settings = qa.QASettings(self.ident, QA_KEY, 5.0)
        return qa.guard(settings, self.root, self.root / "journal", self.client, (d or self.last)["delivery_id"], boundary,
                        kw.get("observe") or self.observe, clock=self.clock)

    def delivered(self):
        self.last, r = self.deliver()
        self.assertEqual(r["state"], "received")
        self.backend.checks.clear()

    def test_mutation_guard_allows_then_refuses_each_boundary_after_revoke(self):
        self.delivered()
        for b in qa.BOUNDARIES:
            res = self.guard(b)
            self.assertTrue(res["ok"])
            self.assertEqual(res["delivery_id"], self.last["delivery_id"])
        self.assertEqual(len(self.backend.checks), len(qa.BOUNDARIES))        # one live check per mutation, never cached
        self.backend.revoked = True
        for b in qa.BOUNDARIES:
            with self.assertRaises(qa.QARefusal) as cm:
                self.guard(b)
            self.assertEqual(cm.exception.code, "revoked")

    def test_guard_malformed_journal_record_is_a_typed_refusal_never_a_traceback(self):
        self.delivered()
        rid = self.last["request_id"]
        good = self.journal.get(rid)

        def broken(**over):
            rec = json.loads(json.dumps(good))
            for k, v in over.items():
                if k.startswith("qa."):
                    rec["qa"][k[3:]] = v
                else:
                    rec[k] = v
            return {k: v for k, v in rec.items() if v != "<drop>"} if not any(
                v == "<drop>" for k, v in rec["qa"].items()) else dict(rec, qa={k: v for k, v in rec["qa"].items() if v != "<drop>"})
        variants = [dict(state=7), dict(state="<drop>"), {"qa.target": "x"}, {"qa.producer": None}, {"qa.receiver_id": "nope"},
                    {"qa.expires": "soon"}, {"qa.expires": True}, {"qa.admission_revision": "<drop>"},
                    {"qa.policy_sha256": 5}, dict(text_sha256="zz"), dict(request_id=None)]
        for over in variants:
            self.journal.put(rid, broken(**over))
            self.backend.checks.clear()
            with self.assertRaises(qa.QARefusal, msg=str(over)) as cm:
                self.guard()
            self.assertEqual(cm.exception.code, "delivery_record_malformed", over)
            self.assertEqual(self.backend.checks, [], over)                    # refused before any backend call
        self.journal.put(rid, good)
        self.assertTrue(self.guard()["ok"])

    def test_damaged_journal_record_never_crashes_the_receiver_and_is_never_replayed(self):
        d, _ = self.deliver()
        rid, good = d["request_id"], self.journal.get(d["request_id"])
        for damage in ({"state": None}, {"delivery_id": 5}, {"request_id": "x"}, {"text_sha256": None}):
            self.journal.put(rid, dict(good, **damage))
            self.assertEqual(self.rx.reconcile(), [], damage)                       # skipped, no exception, nothing settled
            r = self.rx.handle(d)                                                   # replay: conflict, no injection
            self.assertEqual(r["error_code"], "request_conflict", damage)
        broken = {k: v for k, v in good.items() if k != "state"}
        self.journal.put(rid, broken)
        self.assertEqual(self.rx.reconcile(), [])
        self.assertEqual(self.rx.handle(d)["error_code"], "request_conflict")
        self.assertEqual(len(self.iterm.pasted), 1)

    def test_guard_refuses_unknown_boundary_failed_delivery_timeout_expiry_and_local_binding(self):
        self.delivered()
        with self.assertRaises(qa.QARefusal):
            self.guard("rm_rf")
        for mode in ("timeout", "malformed", "mismatch-text"):
            self.backend.check_mode = mode
            with self.assertRaises(qa.QARefusal):
                self.guard()
        self.backend.check_mode = "ok"
        self.iterm.proc = ("4242", "9999", "ttys007", STARTED)
        with self.assertRaises(qa.QARefusal) as cm:
            self.guard()
        self.assertEqual(cm.exception.code, "producer_not_foreground")
        self.iterm.proc = ("4242", "4242", "ttys007", STARTED)
        self.clock.t = EXPIRES
        with self.assertRaises(qa.QARefusal) as cm:
            self.guard()
        self.assertEqual(cm.exception.code, "expired")

    def test_guard_refuses_a_delivery_that_never_reached_received(self):
        self.backend.check_mode = "inactive"
        d, r = self.deliver()
        self.backend.check_mode = "ok"
        with self.assertRaises(qa.QARefusal) as cm:
            self.guard(d=d)
        self.assertEqual(cm.exception.code, "delivery_not_delivered")

    def test_guard_cli_exit_codes_and_no_secret_in_output(self):
        real = int(__import__("time").time())
        self.clock.t = real                                                     # the config's expiry must be real-clock valid
        self.last, r = self.deliver(expires=real + 3000)
        self.assertEqual(r["state"], "received")
        self.backend.checks.clear()
        cfg = _cfg(self, enabled_qa=True)
        out = io.StringIO()
        code = sr.qa_command(cfg, "qa-check", self.last["delivery_id"], "qa_start", opener=self.backend, api=self.iterm,
                             info=self.iterm.info, local_host=TARGET["hostname"], out=out, clock=self.clock,
                             known=lambda s: True, argv_of=self.iterm.argv_of)
        self.assertEqual(code, 0, out.getvalue())
        self.assertTrue(json.loads(out.getvalue())["ok"])
        self.assertNotIn(QA_KEY, out.getvalue())
        self.backend.check_mode = "timeout"
        self.assertEqual(sr.qa_command(cfg, "qa-check", self.last["delivery_id"], "qa_start", opener=self.backend, api=self.iterm,
                                       info=self.iterm.info, local_host=TARGET["hostname"], out=io.StringIO(), clock=self.clock,
                                       known=lambda s: True, argv_of=self.iterm.argv_of),
                         sr.EXIT_REFUSED)
        self.assertEqual(sr.qa_command(cfg, "qa-check", "", "", opener=self.backend, out=io.StringIO()), sr.EXIT_REFUSED)

    # ------------------------------------------------------------------ actual local binding proof (supported read-only API)
    def test_observe_returns_fresh_actual_target_and_producer_with_provenance(self):
        obs = self.observe()
        self.assertEqual(obs["target"], TARGET)
        self.assertEqual(obs["producer"], PRODUCER)
        self.assertEqual(obs["provenance"]["session_id"], "observed")
        self.assertEqual(obs["provenance"]["runner_id"], "observed")
        self.assertEqual(obs["provenance"]["workspace_id"], "backend")
        self.assertEqual((self.iterm.pasted, self.iterm.submitted, self.iterm.legacy_writes), ([], [], []))
        self.assertEqual(list((self.root / "journal").glob("*.json")), [])                  # no ledger write

    def test_observe_is_not_a_config_echo_it_follows_the_machine(self):
        self.iterm.proc = ("4242", "4242", "ttys007", "Mon Nov 2 1:00:00 2026")            # config says STARTED, machine says otherwise
        with self.assertRaises(qa.QARefusal) as cm:
            self.observe()
        self.assertEqual(cm.exception.code, "producer_changed")

    def test_observe_native_relation_must_come_from_the_producer_process(self):
        for argv in ([], ["claude"], ["claude", "--resume", "22222222-2222-2222-2222-222222222222"],
                     ["claude", TARGET["session_id"]], ["claude", "--model", TARGET["session_id"]]):
            self.iterm.argv = argv
            with self.assertRaises(qa.QARefusal) as cm:                                        # the transcript exists (known=True): not enough
                self.observe()
            self.assertEqual(cm.exception.code, "native_relation_unproven", argv)
        for argv in (["claude", "--session-id", TARGET["session_id"]], ["claude", f"--resume={TARGET['session_id']}"],
                     ["codex", "resume", TARGET["session_id"]]):
            self.iterm.argv = argv
            self.assertEqual(self.observe()["target"]["session_id"], TARGET["session_id"])

    def test_observe_runner_host_transcript_and_terminal_refusals(self):
        cases = (("runner_id_mismatch", self.observer(runner="eeeeeeee-0000-4000-8000-000000000001")),
                 ("session_unknown", self.observer(known=lambda s: False)))
        for code, obs in cases:
            with self.assertRaises(qa.QARefusal) as cm:
                obs()
            self.assertEqual(cm.exception.code, code)
        wrong_host = qa.make_observer(self.ident, PIN, lambda: TARGET["runner_id"], lambda s: True, self.iterm, self.iterm.info,
                                      self.iterm.argv_of, "another-mac.local", self.clock)
        with self.assertRaises(qa.QARefusal) as cm:
            wrong_host()
        self.assertEqual(cm.exception.code, "host_mismatch")
        self.iterm.sessions.clear()
        with self.assertRaises(qa.QARefusal):
            self.observe()

    def test_guard_returns_observed_binding_and_rechecks_locally_after_the_http_call(self):
        self.delivered()
        res = self.guard("qa_start")
        self.assertEqual((res["target"], res["producer"]), (TARGET, PRODUCER))
        self.assertEqual(res["provenance"]["session_id"], "observed")
        orig = self.backend._check

        def swap_after_answer(req, delivery_id, body):
            out = orig(req, delivery_id, body)
            self.iterm.proc = ("4242", "9999", "ttys007", STARTED)                              # foreground lost during the round trip
            return out
        self.backend._check = swap_after_answer
        with self.assertRaises(qa.QARefusal) as cm:
            self.guard("qa_start")
        self.assertEqual(cm.exception.code, "producer_not_foreground")

    def test_guard_refuses_when_the_binding_observed_differs_from_the_delivery(self):
        self.delivered()
        self.iterm.argv = ["claude", "--resume", "22222222-2222-2222-2222-222222222222"]
        with self.assertRaises(qa.QARefusal) as cm:
            self.guard("qa_start")
        self.assertEqual(cm.exception.code, "native_relation_unproven")

    def test_receiver_also_proves_the_native_relation_at_the_injection_boundaries(self):
        self.iterm.argv = ["claude"]
        _, r = self.deliver()
        self.assertEqual((r["state"], r["error_code"]), ("failed", "native_relation_unproven"))
        self.assertEqual((self.iterm.pasted, self.iterm.submitted), ([], []))

    def test_relation_lost_between_paste_and_enter_sends_no_enter(self):
        orig = self.iterm.stage

        def stage_then_swap(*a):
            ok = orig(*a)
            self.iterm.argv = ["claude", "--resume", "22222222-2222-2222-2222-222222222222"]
            return ok
        self.iterm.stage = stage_then_swap
        _, r = self.deliver()
        self.assertEqual((r["state"], r["error_code"]), ("uncertain", "native_relation_unproven"))
        self.assertEqual(self.iterm.submitted, [])

    def test_observe_cli_prints_the_observation_and_refuses_with_a_typed_code(self):
        cfg = _cfg(self, enabled_qa=True)
        out = io.StringIO()
        kw = dict(api=self.iterm, info=self.iterm.info, local_host=TARGET["hostname"], known=lambda s: True, argv_of=self.iterm.argv_of)
        self.assertEqual(sr.qa_command(cfg, "qa-observe", "", "", out=out, clock=self.clock, **kw), 0)
        self.assertEqual(json.loads(out.getvalue())["producer"], PRODUCER)
        self.assertNotIn(RUNNER_TOKEN, out.getvalue())
        self.iterm.argv = []
        self.assertEqual(sr.qa_command(cfg, "qa-observe", "", "", out=io.StringIO(), clock=self.clock, **kw), sr.EXIT_REFUSED)

    # ------------------------------------------------------------------ disable
    def test_disable_revokes_qa_only_keeps_ledger_producer_and_legacy(self):
        d, _ = self.deliver()
        legacy = self.root / sr.ADMISSION_FILE
        legacy.write_text(json.dumps({"legacy": True}))
        sr.bind_ledger(self.root, md.Identity([SENDER], **TARGET), True, transport=_transport_sha())
        pin_before = (self.root / sr.PIN_FILE).read_text()
        journal_before = self.journal.get(d["request_id"])
        proc_before = self.iterm.proc
        ids = qa.revoke(qa.QASettings(self.ident, QA_KEY, 5.0), self.root, self.client)
        self.assertEqual(ids, [RECEIVER_ID])
        self.assertEqual(self.backend.deleted, [RECEIVER_ID])
        self.assertTrue((self.root / qa.REVOKED_FILE).exists() and not (self.root / qa.ADMISSION_FILE).exists())
        self.assertEqual(legacy.read_text(), json.dumps({"legacy": True}))
        self.assertEqual((self.root / sr.PIN_FILE).read_text(), pin_before)
        self.assertEqual(self.journal.get(d["request_id"]), journal_before)
        self.assertEqual(self.iterm.proc, proc_before)
        r = self.rx.handle(envelope())
        self.assertEqual((r["state"], r["error_code"]), ("failed", "revoked"))
        self.assertEqual(len(self.iterm.pasted), 1)                                  # only the delivery made before the disable

    def test_repeated_revoke_after_success_is_an_idempotent_noop(self):
        self.deliver()
        ids = qa.revoke(qa.QASettings(self.ident, QA_KEY, 5.0), self.root, self.client)
        self.assertTrue(ids)
        self.assertEqual(qa.revoke(qa.QASettings(self.ident, QA_KEY, 5.0), self.root, self.client), [])

    def test_revoke_failure_keeps_the_record_for_retry(self):
        orig = self.backend.__call__
        self.client._open = lambda req, timeout=None: Resp(200, {}) if req.get_method() == "DELETE" else orig(req, timeout)
        with self.assertRaises(md.MeshError):
            qa.revoke(qa.QASettings(self.ident, QA_KEY, 5.0), self.root, self.client)
        self.assertTrue((self.root / qa.ADMISSION_FILE).exists())

    # ------------------------------------------------------------------ concurrency / shared lock / migration
    def test_shared_consumer_lock_second_consumer_refused(self):
        lock = sr.ConsumerLock(self.root)
        self.assertTrue(lock.acquire("controller"))
        self.addCleanup(lock.release)
        self.assertFalse(sr.ConsumerLock(self.root).acquire("session-receiver"))

    def test_two_receivers_racing_one_request_inject_once(self):
        d = envelope()
        rx2 = qa.QAReceiver(self.ident, self.session, md.Journal(self.root / "journal"), self.client, self.root, clock=self.clock)
        out = []
        ts = [threading.Thread(target=lambda r=r: out.append(r.handle(dict(d)))) for r in (self.rx, rx2, self.rx, rx2)]
        [t.start() for t in ts]
        [t.join(10) for t in ts]
        self.assertEqual(len(self.iterm.pasted), 1)
        self.assertEqual({o["state"] for o in out}, {"received"})

    def test_old_ledger_preserved_and_unbound_history_fails_closed(self):
        ident = md.Identity([SENDER], **TARGET)
        old = self.root / "journal"
        rid = str(uuid.uuid4())
        self.journal.put(rid, {"delivery_id": str(uuid.uuid4()), "request_id": rid, "text_sha256": "a" * 64, "state": "received"})
        with self.assertRaises(sr.Refusal) as cm:
            sr.bind_ledger(self.root, ident, False, transport=_transport_sha())
        self.assertEqual(cm.exception.code, "ledger_unbound")                      # unsafe unbound history fails closed
        sr.bind_ledger(self.root, ident, True, transport=_transport_sha())          # explicit adopt keeps every record
        self.assertEqual(self.journal.get(rid)["state"], "received")
        self.assertEqual(sr.ledger_state(self.root, ident, _transport_sha()), "bound")
        self.assertEqual(sr.ledger_state(self.root, ident, None), "mismatch")        # a tmux ledger/pin never serves QA
        self.assertTrue(old.is_dir())
        d, r = self.deliver()                                                       # the legacy record does not disturb QA
        self.assertEqual(r["state"], "received")

    def test_legacy_reconcile_never_touches_qa_records(self):
        d, _ = self.deliver()
        rec = self.journal.get(d["request_id"])
        rec["state"] = "injecting"
        self.journal.put(d["request_id"], rec)
        client = _LegacyClient(rev=REV)
        legacy = md.MetaReceiver(md.Identity([SENDER], **TARGET), self.session, self.journal, client, self.root / "acks-legacy")
        self.assertEqual(legacy.reconcile(), [])
        self.assertEqual(client.calls, [])
        self.assertEqual(self.journal.get(d["request_id"])["state"], "injecting")

    # ------------------------------------------------------------------ registration, config, pinned bytes
    def test_registration_body_exact_and_answer_is_verified(self):
        body = self.backend.registrations[-1]
        self.assertEqual(body, self.ident.registration())
        self.assertEqual(body["policy_sha256"], P["policy_sha256"])
        self.assertEqual(body["expires"], NOW + 7200)
        orig = self.backend.__call__

        def bad(req, timeout=None):
            r = orig(req, timeout)
            if req.get_method() == "POST" and req.full_url.endswith("/receivers"):
                data = json.loads(r._raw)
                data["target"] = dict(data["target"], host_id="x")
                return Resp(201, data)
            return r
        self.client._open = bad
        with self.assertRaises(md.MeshError):
            self.client.register(self.ident)

    def test_registration_record_failure_revokes_the_fresh_admission(self):
        stop = threading.Event()
        client = qa.QAClient(BASE, QA_KEY, RUNNER_TOKEN, self.ident, self.root / "ledger-file", opener=self.backend, stop=stop)
        (self.root / "ledger-file").write_text("a file where the ledger directory should be")
        with self.assertRaises(md.MeshError):
            client.register(self.ident)
        self.assertTrue(stop.is_set() and client.record_failed and self.backend.deleted)

    def test_pinned_bytes_tamper_is_a_refusal(self):
        orig = qa.DATA
        qa._loaded = None
        try:
            tmp = Path(self.tmp.name) / "pins"
            tmp.mkdir()
            for n in qa.PINS:
                (tmp / n).write_bytes((orig / n).read_bytes())
            (tmp / "brief.txt").write_bytes((orig / "brief.txt").read_bytes() + b" ")
            qa.DATA = tmp
            with self.assertRaises(qa.QARefusal) as cm:
                qa.pinned()
            self.assertEqual((cm.exception.code, cm.exception.detail), ("pinned_bytes_changed", "brief.txt"))
        finally:
            qa.DATA = orig
            qa._loaded = None
        self.assertEqual(qa.pinned()["text_sha256"], qa.PINS["brief.txt"])

    def test_production_pins_are_the_frozen_hashes_and_no_private_byte_is_packaged(self):
        self.assertEqual(PROD_PINS, {"brief.txt": "5422e262a6833b506e33203ed8212cc828e2393993ecd45da91ced5d7df0a96f",
                                     "policy.json": "3d3f06d516c5b4268e01342cb79c58f1be90ba1775de20e1ff5dc13e69f8e9cc",
                                     "schema.json": "eb3dc20f9d25ebd95bbd8f831ccd5c334d4bafd6b7b3e52c81942b05b48d9850"})
        self.assertEqual(sorted(p.name for p in qa.PUBLIC.iterdir() if p.is_file()), ["conformance.json"])

    def test_private_bundle_is_a_local_opt_in_that_fails_closed_before_any_mutation(self):
        meta = md.Identity([SENDER], **TARGET)
        exp = int(__import__("time").time()) + 600
        sec = {"enabled": True, "bundle_dir": str(fx.DIR), "credential_file": "x", "allowed_sender_agent_ids": [SENDER],
               "expires": exp, "target": dict(TARGET), "producer": dict(PRODUCER)}
        reads = []

        def read_secret(f, e, w):
            reads.append(w)
            return QA_KEY
        parse = lambda s, **kw: qa.parse_section(s, read_secret, meta, "metadata-secret", PIN, "iterm", **kw)
        orig_dir = qa.DATA
        try:
            # OFF (absent / empty / enabled=false): the bundle and the QA credential are never read, even if the dir is bogus
            qa.DATA, qa._loaded = Path(self.tmp.name) / "nowhere", None
            for off in (None, {}, {"enabled": False, "bundle_dir": "/nowhere"}):
                self.assertIsNone(parse(off))
            self.assertEqual(reads, [])
            # ON without bundle_dir / relative / missing dir / missing file / one changed byte: refusal, zero credential reads
            bad = Path(self.tmp.name) / "tampered"
            for name in qa.PINS:
                (bad).mkdir(exist_ok=True)
                (bad / name).write_bytes((fx.DIR / name).read_bytes())
            (bad / "policy.json").write_bytes((fx.DIR / "policy.json").read_bytes() + b" ")
            part = Path(self.tmp.name) / "partial"
            part.mkdir()
            (part / "brief.txt").write_bytes((fx.DIR / "brief.txt").read_bytes())
            for over, code in (({"bundle_dir": None}, "qa_bundle_dir_required"), ({"bundle_dir": "rel/dir"}, "qa_bundle_dir_required"),
                               ({"bundle_dir": str(Path(self.tmp.name) / "absent")}, "pinned_bytes_missing"),
                               ({"bundle_dir": str(part)}, "pinned_bytes_missing"),
                               ({"bundle_dir": str(bad)}, "pinned_bytes_changed")):
                s = dict(sec, **over)
                if over["bundle_dir"] is None:
                    del s["bundle_dir"]
                with self.assertRaises(qa.QARefusal) as cm:
                    parse(s)
                self.assertEqual(cm.exception.code, code, over)
            self.assertEqual(reads, [])
            # positive control: the intact bundle loads
            self.assertIsNotNone(parse(sec))
            self.assertEqual(reads, ["qa credential"])
            # disable path works without the bundle (it only deletes); strict path still refuses
            qa._loaded = None
            gone = dict(sec, bundle_dir=str(Path(self.tmp.name) / "absent"), expires=1)
            self.assertIsNotNone(parse(gone, allow_expired=True))
            partial = {k: v for k, v in PRODUCER.items() if k != "producer_started"}
            for bad_sec in (dict(gone, producer=partial), dict(gone, producer=dict(PRODUCER, extra=1)), dict(gone, target={}),
                            dict(gone, producer="x"), {k: v for k, v in gone.items() if k != "allowed_sender_agent_ids"},
                            dict(gone, allowed_sender_agent_ids=None), dict(gone, allowed_sender_agent_ids=[1]),
                            dict(gone, allowed_sender_agent_ids=[]), dict(gone, target=dict(TARGET, host_id=5)),
                            dict(gone, producer=dict(PRODUCER, producer_pid="4242")),
                            dict(gone, producer=dict(PRODUCER, producer_pid=True))):                    # malformed disable config: typed refusal, no traceback
                with self.assertRaises(qa.QARefusal) as cm:
                    parse(bad_sec, allow_expired=True)
                self.assertEqual(cm.exception.code, "config_invalid")
            with self.assertRaises(qa.QARefusal):
                parse(gone)
        finally:
            qa.DATA, qa._loaded = orig_dir, None
        self.assertEqual(qa.pinned()["text_sha256"], qa.PINS["brief.txt"])

    def test_pinned_without_configuration_refuses(self):
        orig = qa.DATA
        try:
            qa.DATA, qa._loaded = None, None
            with self.assertRaises(qa.QARefusal) as cm:
                qa.pinned()
            self.assertEqual(cm.exception.code, "pinned_bundle_not_configured")
        finally:
            qa.DATA, qa._loaded = orig, None

    def test_config_default_off_and_strict(self):
        meta = md.Identity([SENDER], **TARGET)
        parse = lambda sec: qa.parse_section(sec, lambda f, e, w: QA_KEY, meta, "metadata-secret", PIN, "iterm")
        self.assertIsNone(parse(None))
        self.assertIsNone(parse({}))
        for malformed in ([], "", 0, False, ["x"]):                              # not a table: typed refusal, never silently OFF
            with self.assertRaises(qa.QARefusal) as cm:
                parse(malformed)
            self.assertEqual((cm.exception.code, cm.exception.detail), ("config_invalid", "section"))
        self.assertIsNone(parse({"enabled": False}))
        self.assertIsNone(parse({"enabled": "true"}))                           # only the boolean true opts in
        with self.assertRaises(qa.QARefusal):
            parse({"enabled": False, "command": "x"})                             # unknown key, even when off
        good = {"enabled": True, "bundle_dir": str(fx.DIR), "credential_file": "x", "allowed_sender_agent_ids": [SENDER], "expires": int(__import__("time").time()) + 600,
                "target": dict(TARGET), "producer": dict(PRODUCER)}
        self.assertIsNotNone(parse(good))
        for over, code in ((dict(target=dict(TARGET, host_id="other")), "qa_target_differs_from_identity"),
                           (dict(producer=dict(PRODUCER, tty="/dev/ttys008")), "qa_producer_differs_from_pin"),
                           (dict(allowed_sender_agent_ids=[SENDER, str(uuid.uuid4())]), "qa_senders_differ_from_policy"),
                           (dict(expires=int(__import__("time").time()) + 40000), "qa_expiry_invalid"),
                           (dict(expires=1), "qa_expiry_invalid")):
            with self.assertRaises(qa.QARefusal) as cm:
                parse(dict(good, **over))
            self.assertEqual(cm.exception.code, code)
        with self.assertRaises(qa.QARefusal) as cm:
            qa.parse_section(good, lambda f, e, w: "metadata-secret", meta, "metadata-secret", PIN, "iterm")
        self.assertEqual(cm.exception.code, "qa_credential_not_separate")
        with self.assertRaises(qa.QARefusal) as cm:
            qa.parse_section(good, lambda f, e, w: QA_KEY, meta, "m", PIN, "tmux")
        self.assertEqual(cm.exception.code, "qa_needs_iterm_transport")

    def test_qa_revoke_still_works_after_the_configured_expiry_passed(self):
        self.deliver()                                                            # a recorded admission exists
        p = Path(self.tmp.name) / "expired.toml"
        p.write_text(_toml(ledger=self.root, qa_section=_qa_section(expires=int(__import__("time").time()) - 100)))
        p.chmod(0o600)
        for strict in (lambda: sr.load_config(p), lambda: sr.load_config(p, allow_expired_qa=False)):
            with self.assertRaises(sr.Refusal) as cm:                             # serving / guarding stays strict
                strict()
            self.assertEqual(cm.exception.code, "qa_expiry_invalid")
        cfg = sr.load_config(p, allow_expired_qa=True)
        out = io.StringIO()
        self.assertEqual(sr.qa_command(cfg, "qa-revoke", "", "", opener=self.backend, out=out, clock=self.clock), 0, out.getvalue())
        self.assertEqual(self.backend.deleted, [RECEIVER_ID])
        self.assertTrue((self.root / qa.REVOKED_FILE).exists() and not (self.root / qa.ADMISSION_FILE).exists())

    def test_qa_revoke_works_without_the_private_bundle(self):
        self.deliver()                                                            # a recorded admission exists
        p = Path(self.tmp.name) / "nobundle.toml"
        sec = _qa_section().replace(json.dumps(str(fx.DIR)), json.dumps(str(Path(self.tmp.name) / "absent-bundle")))
        self.assertIn("absent-bundle", sec)
        p.write_text(_toml(ledger=self.root, qa_section=sec))
        p.chmod(0o600)
        orig = qa.DATA
        try:
            with self.assertRaises(sr.Refusal) as cm:                             # serving / guarding stay strict
                sr.load_config(p)
            self.assertEqual(cm.exception.code, "pinned_bytes_missing")
            cfg = sr.load_config(p, allow_expired_qa=True)
            qa.DATA, qa._loaded = Path(self.tmp.name) / "absent-bundle", None    # the bundle really is absent at revoke time
            out = io.StringIO()
            self.assertEqual(sr.qa_command(cfg, "qa-revoke", "", "", opener=self.backend, out=out, clock=self.clock), 0,
                             out.getvalue())
        finally:
            qa.DATA, qa._loaded = orig, None
        self.assertEqual(self.backend.deleted, [RECEIVER_ID])
        self.assertTrue((self.root / qa.REVOKED_FILE).exists() and not (self.root / qa.ADMISSION_FILE).exists())

    def test_expired_qa_section_loads_for_disable_paths_and_stays_strict_for_check_and_observe(self):
        p = Path(self.tmp.name) / "expired2.toml"
        p.write_text(_toml(ledger=self.root, qa_section=_qa_section(expires=int(__import__("time").time()) - 100)))
        p.chmod(0o600)
        self.assertIsNotNone(sr.load_config(p, require_enabled=False, allow_expired_qa=True).qa)     # status / revoke load it
        self.assertEqual(sr.command(["qa-check", "--config", str(p), "--delivery-id", str(uuid.uuid4()),
                                     "--boundary", "qa_start"]), sr.EXIT_REFUSED)
        self.assertEqual(sr.command(["qa-observe", "--config", str(p)]), sr.EXIT_REFUSED)

    def test_load_config_malformed_section_shapes_are_typed_refusals(self):
        p = Path(self.tmp.name) / "shape.toml"
        runner = Path(self.tmp.name) / "shape-runner.json"
        runner.write_text(json.dumps({"runner_id": TARGET["runner_id"], "token": RUNNER_TOKEN}))
        runner.chmod(0o600)
        os.environ["META_TEST_KEY"] = "metadata-secret"
        ok = (f'[session_receiver]\nenabled = true\nmesh_url = "http://127.0.0.1:9"\nledger_root = "{self.root}"\n'
              f'harness = "claude"\nrunner_credential_file = "{runner}"\ntransport = "iterm"\niterm_session_id = "{SID}"\n'
              f'iterm_tty = "{TTY}"\nproducer_pid = 4242\nproducer_started = "{STARTED}"\n')
        cases = (("[[session_receiver]]\nenabled = true\n", "config_invalid_section"),
                 ("[[session_metadata]]\nenabled = true\n", "config_invalid_section"),
                 (ok + "[[host_local_qa_assignment_v1]]\nenabled = true\n", "config_invalid_section"),
                 (ok + "[[host_local_qa_assignment_v1]]\n", "config_invalid_section"),
                 ("[[host_local_qa_assignment_v1]]\nenabled = true\n[[host_local_qa_assignment_v1]]\nenabled = true\n",
                  "config_invalid_section"))
        for text, code in cases:
            p.write_text(text)
            p.chmod(0o600)
            for allow in (False, True):
                with self.assertRaises((sr.Refusal, qa.QARefusal)) as cm:       # typed refusal, never TypeError
                    sr.load_config(p, require_enabled=False, allow_expired_qa=allow)
                self.assertEqual(cm.exception.code, code, text)

    def test_load_config_rejects_unknown_qa_key_and_keeps_off_without_section(self):
        p = Path(self.tmp.name) / "c.toml"
        p.write_text(_toml(qa_section='[host_local_qa_assignment_v1]\nenabled = false\nargv = "x"\n'))
        p.chmod(0o600)
        with self.assertRaises(sr.Refusal) as cm:
            sr.load_config(p, require_enabled=False)
        self.assertEqual(cm.exception.code, "config_unknown_keys")
        p.write_text(_toml())
        self.assertIsNone(sr.load_config(p, require_enabled=False).qa)

    # ------------------------------------------------------------------ end to end through the standalone process
    def test_run_serves_qa_with_the_shared_lock_and_blocks_a_second_consumer(self):
        p = Path(self.tmp.name) / "c.toml"
        led = Path(self.tmp.name) / "e2e-ledger"
        p.write_text(_toml(ledger=led, qa_section=_qa_section()))
        p.chmod(0o600)
        cfg = sr.load_config(p)
        self.assertIsNotNone(cfg.qa)
        backend = Backend(lambda: int(__import__("time").time()))               # the real process runs on the real clock
        backend.events = self.events
        backend.queued = [envelope(expires=int(__import__("time").time()) + 3000)]
        refuse = lambda req, timeout=None: (_ for _ in ()).throw(urllib.error.URLError("legacy off"))
        stop = threading.Event()
        box = {}
        t = threading.Thread(target=lambda: box.update(code=sr.run(cfg, stop, self.iterm, lambda s: True, info=self.iterm.info,
                                                                  local_host=TARGET["hostname"], opener=refuse, qa_opener=backend,
                                                                  argv_of=self.iterm.argv_of)),
                             daemon=True)
        t.start()
        self.addCleanup(stop.set)
        for _ in range(200):
            if backend.reports:
                break
            threading.Event().wait(0.05)
        self.assertEqual(sr.run(cfg, threading.Event(), self.iterm, lambda s: True, info=self.iterm.info,
                                local_host=TARGET["hostname"]), sr.EXIT_BUSY)           # a second consumer is refused
        stop.set()
        t.join(10)
        self.assertEqual([r["state"] for r in backend.reports], ["received"])
        self.assertEqual(len(self.iterm.pasted), 1)
        self.assertEqual(self.iterm.legacy_writes, [])
        self.assertTrue((led / qa.ADMISSION_FILE).exists())
        self.assertFalse((led / sr.ADMISSION_FILE).exists())                              # legacy admission untouched


# ---------------------------------------------------------------------- helpers
class ProcessArgvTest(unittest.TestCase):
    """The native-session relation reads STRUCTURED argv: a rendered command line loses the argument boundaries."""

    @staticmethod
    def buf(argv, path="/usr/bin/claude", env=("A=1",)):
        return (len(argv).to_bytes(4, "little") + path.encode() + b"\0\0\0" + b"".join(a.encode() + b"\0" for a in argv)
                + b"".join(e.encode() + b"\0" for e in env))

    def test_parse_keeps_argument_boundaries_and_ignores_the_environment(self):
        sid = str(uuid.uuid4())
        argv = ["claude", "--resume", sid, "-p", "x"]
        self.assertEqual(it.parse_procargs2(self.buf(argv, env=("SECRET=do-not-read",))), argv)

    def test_prompt_argument_containing_resume_is_not_the_flag(self):
        sid = str(uuid.uuid4())
        forged = it.parse_procargs2(self.buf(["claude", "-p", f"please --resume {sid}"]))
        self.assertEqual(forged, ["claude", "-p", f"please --resume {sid}"])
        self.assertFalse(qa.native_session_in_argv(forged, sid))                   # one token, not the flag
        self.assertTrue(qa.native_session_in_argv(it.parse_procargs2(self.buf(["claude", "--resume", sid])), sid))

    def test_malformed_or_truncated_buffers_prove_nothing(self):
        good = self.buf(["claude", "--resume", "abc"], env=())
        for bad in (b"", b"\x01\0\0\0", good[:-3], (0).to_bytes(4, "little") + good[4:],
                    (9999).to_bytes(4, "little") + good[4:], (5).to_bytes(4, "little") + good[4:]):
            self.assertEqual(it.parse_procargs2(bad), [], bad)

    def test_live_own_process_argv_is_structured_on_darwin_and_unproven_elsewhere(self):
        import sys
        got = it.process_argv(os.getpid())
        if sys.platform == "darwin":
            self.assertEqual(got[-len(sys.argv):][-1:], sys.argv[-1:])
            self.assertTrue(all(isinstance(a, str) for a in got) and len(got) >= 2)
        else:
            self.assertEqual(got, [])
        self.assertEqual(it.process_argv(0), [])
        self.assertEqual(it.process_argv(2 ** 30), [])


class _LegacyClient:
    """The legacy metadata client's surface; records what a legacy receiver would have reported."""

    def __init__(self, rev=None):
        self.admission_revision, self.calls = rev, []

    def report(self, *a, **k):
        self.calls.append(a)
        return {}


def _transport_sha():
    f = TARGET
    import hashlib
    return hashlib.sha256(json.dumps({"transport": "iterm", "iterm_session_id": SID, "tty": TTY, "producer_pid": 4242,
                                      "producer_started": STARTED, "host_id": f["host_id"], "runner_id": f["runner_id"],
                                      "session_id": f["session_id"]}, sort_keys=True).encode()).hexdigest()


def _qa_section(expires=None):
    exp = expires or int(__import__("time").time()) + 3000
    prod = "\n".join(f'{k} = {json.dumps(v)}' for k, v in PRODUCER.items())
    tgt = "\n".join(f'{k} = {json.dumps(v)}' for k, v in TARGET.items())
    return (f'[host_local_qa_assignment_v1]\nenabled = true\nbundle_dir = {json.dumps(str(fx.DIR))}\ncredential_env = "QA_TEST_KEY"\nallowed_sender_agent_ids = ["{SENDER}"]\n'
            f'expires = {exp}\ninterval_seconds = 1\n[host_local_qa_assignment_v1.target]\n{tgt}\n'
            f'[host_local_qa_assignment_v1.producer]\n{prod}\n')


def _toml(ledger=None, qa_section=""):
    runner = Path(tempfile.mkdtemp()) / "runner.json"
    runner.write_text(json.dumps({"runner_id": TARGET["runner_id"], "token": RUNNER_TOKEN}))
    runner.chmod(0o600)
    os.environ["QA_TEST_KEY"], os.environ["META_TEST_KEY"] = QA_KEY, "metadata-secret"
    ident = "\n".join(f'{k} = "{v}"' for k, v in TARGET.items())
    return (f'[session_metadata]\nagent = "qa-agent"\ncredential_env = "META_TEST_KEY"\nallowed_sender_agent_ids = ["{SENDER}"]\n{ident}\n'
            f'[session_receiver]\nenabled = true\nmesh_url = "http://127.0.0.1:9"\nledger_root = "{ledger or "/tmp/none"}"\nharness = "claude"\n'
            f'runner_credential_file = "{runner}"\ntransport = "iterm"\niterm_session_id = "{SID}"\niterm_tty = "{TTY}"\n'
            f'producer_pid = 4242\nproducer_started = "{STARTED}"\n' + qa_section)


def _cfg(test, enabled_qa=True):
    p = Path(test.tmp.name) / "guard.toml"
    p.write_text(_toml(ledger=test.root, qa_section=_qa_section()))
    p.chmod(0o600)
    return sr.load_config(p)


CONFORMANCE_MAP = {
    "exact": "test_exact_positive_control_one_injection_received_only",
    "legacy-rejects-qa": "test_legacy_rejects_qa_payload_zero_paste",
    "qa-rejects-legacy": "test_qa_rejects_legacy_capability_and_envelope",
    "unknown-fields": "test_unknown_fields_at_every_depth_refused_before_any_effect",
    "hash": "test_hash_any_pinned_byte_changed",
    "identity": "test_identity_each_field_alone_is_refused_identically",
    "producer": "test_producer_fields_refused_before_paste",
    "revision": "test_revision_missing_wrong_bool_stale_other_receiver",
    "expired": "test_expired_and_over_eight_hours",
    "revoked": "test_revoked_between_paste_and_enter_no_enter_and_uncertain",
    "timeout": "test_timeout_malformed_mismatch_unavailable_never_allow",
    "duplicate": "test_duplicate_identical_replay_one_injection_same_receipt",
    "conflict": "test_conflict_any_change_is_409_and_original_unchanged",
    "uncertain": "test_uncertain_after_stage_failure_then_restart_never_replays",
    "concurrency": "test_two_receivers_racing_one_request_inject_once",
    "disable": "test_disable_revokes_qa_only_keeps_ledger_producer_and_legacy",
    "ack-is-not-install": "test_ack_is_not_install_and_must_name_every_identity_and_pin",
    "mutation-guard": "test_mutation_guard_allows_then_refuses_each_boundary_after_revoke",
    "mini-readback": "test_guard_refuses_a_delivery_that_never_reached_received",
}


class ConformanceCoverageTest(unittest.TestCase):
    def test_every_frozen_conformance_id_has_a_named_existing_case(self):
        ids = [c["id"] for c in json.loads((qa.PUBLIC / "conformance.json").read_text())["cases"]]
        self.assertEqual(sorted(ids), sorted(CONFORMANCE_MAP))
        for name in CONFORMANCE_MAP.values():
            self.assertTrue(hasattr(QATest, name), name)


if __name__ == "__main__":
    unittest.main()
