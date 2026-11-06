"""Real Runner/Store/listener adoption; CI only (SQLite-backed).

The only fake is the external Mesh HTTP API. No internal controller, receiver,
CAS, Ledger, filesystem or config-load implementation is mocked.
"""
import copy
import hashlib
import hmac
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from uuid import uuid4

from mesh_runner import native_result_config as nc
from mesh_runner.config import load
from mesh_runner.extension_service import sign
from mesh_runner.native_result_ledger import Ledger, canonical
from mesh_runner.runner import Runner
from mesh_runner.store import Store


class AdoptionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        os.chmod(self.root, 0o700)
        state = self.root / "state"
        state.mkdir(mode=0o700)
        self.owner, self.task_id, self.session = (str(uuid4()) for _ in range(3))
        store = Store(state / "runner.sqlite")
        a = store.create("Lane", self.task_id, "codex")
        store.update(a, state="waiting", session_id=self.session, checkout={"generation": 7},
                     wait={"kind": "card", "target": "abcdef00"})
        store.db.close()
        self.target = {"task": self.task_id, "attempt": a.id, "session": self.session, "generation": 7}
        self.card = {"id": self.task_id, "assignee_id": self.owner, "assignee_type": "agent",
                     "checked_out_by": None, "checkout_session_id": a.id, "checkout_generation": 7}
        rig = self

        # mock: external boundary — isolated Mesh API only; real HTTP client and fresh reads.
        class Mesh(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.endswith("/agents/me"):
                    value = {"id": rig.owner}
                elif self.path.endswith("/tasks/" + rig.task_id):
                    value = {"task": rig.card}
                else:
                    self.send_error(404)
                    return
                raw = canonical(value)
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *a):
                pass

        mesh = ThreadingHTTPServer(("127.0.0.1", 0), Mesh)
        threading.Thread(target=mesh.serve_forever, daemon=True).start()
        self.addCleanup(mesh.server_close)
        self.addCleanup(mesh.shutdown)
        self.key = b"native-operator-distinct-capability-key-123456789"
        self.key_path = self.root / "operator.key"
        self.write(self.key_path, self.key)
        self.write(self.root / "agent.key", b"external-mesh-only-key")
        self.write(self.root / "extension.key", b"extension-only-distinct-signing-key")
        self.journal, self.channels = self.root / "journal", self.root / "channels"
        now = int(time.time())
        self.policy = {"version": 1, "purpose": nc.PURPOSE, "operator": str(uuid4()), "agent": "Lane",
                       "receiver": self.target, "paths": [str(self.journal), str(self.channels)],
                       "key_file": str(self.key_path), "key_sha256": nc.sha(self.key),
                       "not_before": now - 60, "expires": now + 3600, "min_revision": 1}
        self.policy_path, self.rev_path = self.root / "policy.json", self.root / "revocations.json"
        self.write(self.policy_path, canonical(self.policy))
        self.pin = nc.sha(self.policy_path.read_bytes())
        self.rev(1)
        self.config = self.root / "fixture.toml"
        self.initial = (f'[runner]\nmesh_url = "http://127.0.0.1:{mesh.server_port}"\n'
                        f'state_dir = {json.dumps(str(state))}\n'
                        '[native_result_adoption]\n'
                        f'policy = {json.dumps(str(self.policy_path))}\npolicy_sha256 = "{self.pin}"\n'
                        f'revocations = {json.dumps(str(self.rev_path))}\n'
                        '[extension]\nenabled = true\nhost = "127.0.0.1"\nport = 18791\n'
                        f'workspace_id = "{uuid4()}"\n'
                        f'signing_secret_file = {json.dumps(str(self.root / "extension.key"))}\n'
                        '[[agents]]\nname = "Lane"\nharness = "codex"\n'
                        f'profile_dir = {json.dumps(str(self.root / "profile"))}\n'
                        f'key_file = {json.dumps(str(self.root / "agent.key"))}\n')
        self.write(self.config, self.initial.encode())
        self.cfg = load(self.config)
        self.runner = Runner(self.cfg, lock=False)
        self.addCleanup(self.runner.store.db.close)
        for ar in self.runner.agents:
            ar.me = ar.mesh.me()
            self.addCleanup(ar.store.db.close)
        self.cfg.extension_port = 0  # test-only ephemeral existing listener, no second controller
        self.service = self.runner._start_extension(threading.Event())
        self.addCleanup(self.service.shutdown)
        self.control = self.service.native_config
        self.before_db = list(self.runner.store.db.iterdump())

    @staticmethod
    def write(path, raw):
        path.write_bytes(raw)
        path.chmod(0o600)

    def rev(self, revision, revoked=False):
        value = {"version": 1, "policy_sha256": self.pin, "revision": revision,
                 "expires": self.policy["expires"], "revoked": revoked}
        signature = hmac.new(self.key, canonical(value), hashlib.sha256).hexdigest()
        self.write(self.rev_path, canonical({**value, "signature": signature}))

    def disk(self, pair=True, unrelated=False):
        text = self.initial
        if pair:
            text = text.replace('[native_result_adoption]',
                f'native_result_dir = "{self.journal}"\nnative_result_channels_dir = "{self.channels}"\n'
                '[native_result_adoption]')
        if unrelated:
            text = text.replace('[native_result_adoption]', 'poll_seconds = 29\n[native_result_adoption]')
        self.write(self.config, text.encode())

    def call(self, action, request, key=None, headers=None):
        path, body = nc.PREFIX + action, canonical(request)
        timestamp = str(int(time.time()))
        h = {"X-Native-Timestamp": timestamp,
             "X-Native-Signature": sign((key or self.key).decode(), timestamp, path.encode() + b"\n" + body)}
        h.update(headers or {})
        connection = http.client.HTTPConnection("127.0.0.1", self.service.server.server_port, timeout=5)
        try:
            connection.request("POST", path, body, h)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def request(self, action="adopt", binding=None):
        status, receipt = self.call("status", {})
        self.assertEqual(status, 200, receipt)
        return {"request_id": str(uuid4()), "policy_sha256": self.pin, "authority_revision": receipt["authority_revision"],
                "receiver": copy.deepcopy(self.target),
                "expected": binding or {k: receipt[k] for k in ("pid", "started_ns", "epoch", "generation",
                                                              "loaded_digest", "startup_config_sha256")},
                "candidate_config_sha256": nc.sha(self.config.read_bytes()),
                "paths": [str(self.journal), str(self.channels)] if action == "adopt" else [None, None],
                "rollback_of": receipt["operation"] if action == "rollback" else ""}

    def unchanged(self):
        self.assertEqual(nc.state(self.cfg).value, nc.Pair(None, None))
        self.assertEqual(list(self.runner.store.db.iterdump()), self.before_db)

    def test_real_load_repo_admission_ignores_receipt_metadata_but_keeps_semantic_drift(self):
        # Exercise the real config.load called inside AgentRunner._admission_drift;
        # no mocked loader/deepcopy can conceal a new dataclass comparison field.
        agent_runner = self.runner.agents[0]
        self.cfg.extension_port = 18791  # restore loaded value; listener already uses its own ephemeral socket
        startup = self.cfg.native_config_preimage
        self.write(self.config, (self.initial + "\n# comment-only operator edit\n").encode())
        self.assertNotEqual(load(self.config).native_config_preimage, startup)
        self.assertEqual(agent_runner._admission_drift("extra")[0], "repository not configured")
        added = (self.config.read_text() + '\n[[agents.repos]]\nname = "extra"\n'
                 + f'base = {json.dumps(str(self.root / "extra-repo"))}\n')
        self.write(self.config, added.encode())
        fresh = load(self.config)
        self.assertNotEqual(fresh.native_config_preimage, startup)
        self.assertNotEqual(fresh.native_config_unrelated, self.cfg.native_config_unrelated)
        reason, spec = agent_runner._admission_drift("extra")
        self.assertEqual(reason, "")
        self.assertEqual(spec, fresh.agents[0].repos[0])
        for changed in (
                added.replace('[native_result_adoption]', 'poll_seconds = 29\n[native_result_adoption]'),
                added.replace(self.pin, "0" * 64)):
            self.write(self.config, changed.encode())
            self.assertEqual(agent_runner._admission_drift("extra")[0], "runner config drift")
        # Native adoption's stronger exact-preimage/unrelated-drift checks remain explicit.
        self.write(self.config, added.encode())
        self.assertEqual(self.cfg.native_config_preimage, startup)
        self.unchanged()

    def test_unsigned_calls_never_write_history_and_status_does_not_rewrite_it(self):
        record = self.cfg.state_dir / ("native-config-" + self.pin + ".json")
        self.assertFalse(record.exists())
        self.assertEqual(self.call("status", {}, key=b"untrusted-no-control-state-writes-key")[0], 409)
        self.assertFalse(record.exists())
        self.assertEqual(self.call("status", {}, headers={"JOB-TOKEN": ""})[0], 409)
        self.assertFalse(record.exists())
        self.assertEqual(self.call("status", {})[0], 200)
        before = (record.read_bytes(), record.stat().st_ino, record.stat().st_mtime_ns)
        self.assertEqual(self.call("status", {}, key=b"untrusted-no-control-state-writes-key")[0], 409)
        self.assertEqual(self.call("status", {})[0], 200)
        self.assertEqual((record.read_bytes(), record.stat().st_ino, record.stat().st_mtime_ns), before)
        self.rev(2)
        self.assertEqual(self.call("status", {})[1]["authority_revision"], 2)
        self.assertEqual(json.loads(record.read_bytes())["revision"], 2)
        self.rev(3, revoked=True)
        self.assertEqual(self.call("status", {})[0], 409)
        self.assertTrue(json.loads(record.read_bytes())["revoked"])
        self.unchanged()

    def test_real_runner_disk_is_not_loaded_then_signed_pair_is_loaded(self):
        self.disk()
        self.unchanged()  # deliberate red control: disk paths do NOT satisfy the loaded predicate
        self.assertNotEqual(nc.state(self.cfg).value.json(), [str(self.journal), str(self.channels)])
        req = self.request()
        status, receipt = self.call("adopt", req)
        self.assertEqual(status, 200, receipt)
        self.assertEqual(receipt["pid"], os.getpid())
        self.assertEqual(receipt["epoch"], self.control.epoch)
        self.assertEqual(receipt["generation"], 1)
        self.assertEqual(receipt["startup_config_sha256"], nc.sha(self.initial.encode()))
        self.assertEqual(receipt["candidate_config_sha256"], nc.sha(self.config.read_bytes()))
        with nc.paths(self.cfg) as pair:
            self.assertEqual(pair.json(), receipt["paths"])
        self.assertFalse(self.journal.exists())  # no source enrollment or implicit ledger initialization
        self.assertFalse(self.channels.exists())
        self.assertEqual(list(self.runner.store.db.iterdump()), self.before_db)
        self.assertEqual(self.call("adopt", req)[0], 409)

    def test_wrong_operator_job_token_and_modified_policy_refuse(self):
        self.disk()
        req = self.request()
        self.assertEqual(self.call("adopt", req, key=b"foreign-key-should-not-authorize-native")[0], 409)
        self.assertEqual(self.call("adopt", req, headers={"JOB-TOKEN": "not-authority"})[0], 409)
        original = self.policy_path.read_bytes()
        self.write(self.policy_path, original + b" ")
        self.assertEqual(self.call("adopt", req)[0], 409)
        self.write(self.policy_path, original)
        self.rev_path.unlink()
        self.assertEqual(self.call("adopt", req)[0], 409)
        self.unchanged()
        self.rev(1)
        self.assertEqual(self.call("adopt", req)[0], 200)  # same request, restored trusted authority

    def test_task_attempt_epoch_preimage_authority_and_path_negatives(self):
        self.disk()
        baseline = self.request()
        for field, value in (("epoch", "foreign"), ("generation", 7), ("pid", os.getpid() + 1),
                             ("loaded_digest", "0" * 64), ("startup_config_sha256", "0" * 64)):
            req = copy.deepcopy(baseline)
            req["expected"][field] = value
            self.assertEqual(self.call("adopt", req)[0], 409, field)
            self.unchanged()

        for field in ("task", "attempt", "session"):
            req = copy.deepcopy(baseline)
            req["receiver"][field] = str(uuid4())
            self.assertEqual(self.call("adopt", req)[0], 409, field)
            self.unchanged()
        for field, value in (("candidate_config_sha256", "0" * 64), ("policy_sha256", "0" * 64),
                             ("authority_revision", 8), ("paths", ["/foreign", "/unknown"])):
            req = copy.deepcopy(baseline)
            req[field] = value
            self.assertEqual(self.call("adopt", req)[0], 409, field)
            self.unchanged()

        self.assertEqual(self.call("adopt", baseline)[0], 200)  # negative fixture is actually usable

    def test_actual_mesh_and_store_revocation_reject_not_just_caller_pin(self):
        self.disk()
        req = self.request()
        for field, value in (("checkout_generation", 8), ("checkout_session_id", str(uuid4())),
                             ("assignee_id", str(uuid4())), ("human_gate", True)):
            before = self.card.copy()
            self.card[field] = value
            self.assertEqual(self.call("adopt", req)[0], 409, field)
            self.card = before
            self.unchanged()
        ar = self.runner.agents[0]
        attempt = ar.store.get(self.target["attempt"])
        ar.store.update(attempt, session_id=str(uuid4()))
        self.assertEqual(self.call("adopt", req)[0], 409)
        self.assertEqual(nc.state(self.cfg).value, nc.Pair(None, None))

    def test_unrelated_drift_and_preexisting_unknown_history_refuse(self):
        self.disk(unrelated=True)
        self.assertEqual(self.call("adopt", self.request())[0], 409)
        self.unchanged()
        self.disk()
        self.journal.mkdir(mode=0o700)
        self.write(self.journal / "unknown-history", b"must remain")
        self.assertEqual(self.call("adopt", self.request())[0], 409)
        self.assertEqual((self.journal / "unknown-history").read_bytes(), b"must remain")
        self.unchanged()

    def test_atomic_readers_block_publish_and_narrow_rollback_preserves_drift(self):
        self.disk()
        req = self.request()
        result = []
        with nc.paths(self.cfg) as before:
            worker = threading.Thread(target=lambda: result.append(self.call("adopt", req)))
            worker.start()
            worker.join(.1)
            self.assertTrue(worker.is_alive())
            self.assertEqual(before, nc.Pair(None, None))
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result[0][0], 200, result)
        self.rev(2)  # independently signed later revocation revision survives rollback
        revocation_bytes = self.rev_path.read_bytes()
        self.disk(pair=False, unrelated=True)
        disk_bytes = self.config.read_bytes()
        self.assertEqual(self.call("rollback", self.request("rollback"))[0], 200)
        self.assertEqual(self.config.read_bytes(), disk_bytes)
        self.assertEqual(self.rev_path.read_bytes(), revocation_bytes)
        self.unchanged()
        self.rev(1)  # replay of old signed revocation document is rejected durably
        self.assertEqual(self.call("status", {})[0], 409)

    def test_rollback_refuses_real_registration_without_clobber(self):
        self.disk()
        self.assertEqual(self.call("adopt", self.request())[0], 200)
        ledger = Ledger.initialize(self.journal)
        binding = {"producer": str(uuid4()), "task": str(uuid4()), "stage": "fixture",
                   "package_sha256": "a" * 64, "test": True}
        rid = ledger.register(binding, self.target)["registration"]
        self.channels.mkdir(mode=0o700)
        self.write(self.channels / "revocations", b"preserve-channel-revocations")
        state_bytes = (self.journal / "state.json").read_bytes()
        self.disk(pair=False)
        self.assertEqual(self.call("rollback", self.request("rollback"))[0], 409)
        self.assertEqual((self.journal / "state.json").read_bytes(), state_bytes)
        self.assertEqual(ledger.readback(rid)["cursor"], 0)
        self.assertEqual((self.channels / "revocations").read_bytes(), b"preserve-channel-revocations")
        self.assertEqual(nc.state(self.cfg).value.journal, self.journal)

    def test_durable_revoked_authority_cannot_be_unrevoked_by_older_file(self):
        self.rev(3, revoked=True)
        self.assertEqual(self.call("status", {})[0], 409)
        self.rev(1)
        # A fresh process epoch still reads the real persisted revocation floor.
        self.service.native_config = nc.Adoption(self.runner)
        self.assertEqual(self.call("status", {})[0], 409)
        self.unchanged()

    def test_missing_authority_default_is_inert(self):
        start = self.initial.index('[native_result_adoption]')
        end = self.initial.index('[extension]')
        self.write(self.config, (self.initial[:start] + self.initial[end:]).encode())
        fresh = load(self.config)
        self.assertIsNone(fresh.native_result_adoption)
        self.assertIsNone(fresh.native_result_dir)
        self.assertIsNone(fresh.native_result_channels_dir)
        self.service.native_config = None
        self.assertEqual(self.call("adopt", {})[0], 403)
        self.unchanged()


if __name__ == "__main__":
    unittest.main()
