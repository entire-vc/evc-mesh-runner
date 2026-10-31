"""Cross-component contract: the editor-job bridge against the *actual* reviewed product client.

The client is ``CodexEditorClient`` of the private product repository at a pinned commit (see ``tests/data/
tgpeek_editor_client_v1.json`` -> ``source``). It is not mirrored here. Two layers:

* ``LiveClient`` (needs ``TGPEEK_CLIENT_SRC`` = the pinned checkout's ``services/worker/src``): drives the real client over
  loopback HTTP against a real ``EditorBridge`` served by the real ``ExtensionService``. The only replaced parts are the
  CI job auth / provenance doubles and the client's wall clock (frozen so every byte is reproducible). It regenerates the
  fixture in memory and requires it to equal the stored one, and it binds the run to the pinned source by file hash.
* ``GoldenReplay`` (always runs, standard library plus ``cryptography``): replays the requests the real client produced
  into a fresh bridge and requires byte-identical responses, the recorded cancel answers, and the recorded client
  verdicts (accepted / rejected with the client's own reason code) for each genuine and tampered response.

Offline synthetic metadata only. This proves wire compatibility, not server auth, isolation, a model or live acceptance.

Regenerate (after a deliberate wire change or a new client pin):
    TGPEEK_CLIENT_SRC=<checkout>/services/worker/src PYTHONPATH=src:tests python3 tests/test_editor_job_client_contract.py \
        regenerate <checkout-git-dir> <commit>
"""
import base64
import hashlib
import http.client
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import urlsplit
from urllib.request import OpenerDirector

from mesh_runner import editor_job as ej
from mesh_runner.extension_service import ExtensionService
from test_editor_job import FakeControl, FakeExecutor, SHA, gitlab_job, good_observation

try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
except ImportError:                                     # the signer needs it too; tests skip without it
    Ed25519PrivateKey = None

FIXTURE = Path(__file__).parent / "data" / "tgpeek_editor_client_v1.json"
T0 = 1_800_000_000.0                                    # frozen wall clock of both sides
ENDPOINT = "https://bridge.invalid/v1/tgpeek/editor-jobs"
TOKEN = "disposable-capability-control"                 # synthetic CI job token
SEED, OTHER_SEED = "07" * 32, "09" * 32                 # synthetic signing keys
CONFIG_DIGEST = "d" * 64
PIPELINE, JOB = "456", "123"
IDENTITY = ("tgpeek-editor", "openai", "gpt-6.1-sol", "gpt-6.1-sol-2026-10-01")
SOURCE_FILES = ("editorial_bridge.py", "editorial_contract.py", "editorial_wire.py", "editorial.py", "editorial_models.py")
SCHEMA = {"type": "object", "additionalProperties": False}
H = lambda b: hashlib.sha256(b).hexdigest()


def b64pub(seed: str) -> str:
    raw = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(seed)).public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode()


def resign(resp: dict, seed: str = SEED) -> dict:
    body = {k: v for k, v in resp.items() if k != "signature"}
    sig = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(seed)).sign(ej.canonical(body))
    return dict(body, signature=base64.b64encode(sig).decode())


def _set(path: tuple, value):
    def mutate(resp: dict) -> dict:
        node = resp
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value(node[path[-1]]) if callable(value) else value
        return resign(resp)
    return mutate


def _flip_signature(resp: dict) -> dict:
    raw = bytearray(base64.b64decode(resp["signature"]))
    raw[0] ^= 1
    return dict(resp, signature=base64.b64encode(bytes(raw)).decode())


# name -> (mutation of the genuine parsed response, reason code the real client must answer with)
TAMPER = {
    "signature_bit_flip": (_flip_signature, "model_unavailable"),
    "signed_by_another_key": (lambda r: resign(r, OTHER_SEED), "model_unavailable"),
    "binding_attempt": (_set(("bindings", "attempt"), 2), "bridge_binding_mismatch"),
    "binding_commit_sha": (_set(("bindings", "commit_sha"), "b" * 40), "bridge_binding_mismatch"),
    "binding_deadline": (_set(("bindings", "deadline_at"), lambda v: v + 1000), "bridge_binding_mismatch"),
    "binding_input_hash": (_set(("bindings", "input_hash"), "0" * 64), "bridge_binding_mismatch"),
    "job_id": (_set(("job_id",), "0" * 64), "bridge_binding_mismatch"),
    "identity_version": (_set(("identity", "version"), "gpt-6.1-sol-other"), "model_identity_mismatch"),
    "status_failed": (_set(("status",), "failed"), "incomplete_output"),
    "receipt_config_digest": (_set(("executor_receipt", "config_digest"), "e" * 64), "bridge_receipt_invalid"),
    "receipt_finished_in_future": (_set(("executor_receipt", "finished_at"), int(T0 * 1000) + 1), "bridge_receipt_invalid"),
    "receipt_tools_used": (_set(("executor_receipt", "tools_used"), ["shell"]), "tools_forbidden"),
    "receipt_isolation": (_set(("executor_receipt", "isolation"), "none"), "tools_forbidden"),
    "result_hash": (_set(("result_hash",), "0" * 64), "bridge_binding_mismatch"),
    "text_changed_hash_kept": (_set(("text",), '{"claims":[],"limitations":["x"]}'), "bridge_binding_mismatch"),
    "unknown_response_key": (lambda r: resign(dict(r, extra=1)), "invalid_model_output"),
}
# (name, publication, attempt, tamper) in ledger order; pub-1 walks the whole 1..3 budget
CASES = [("attempt_1", "pub-1", 1, None), ("attempt_2", "pub-1", 2, None), ("attempt_3", "pub-1", 3, None)] + [
    ("tamper_" + name, "pub-t%d" % i, 1, name) for i, name in enumerate(TAMPER)] + [
    ("out_of_order_attempt", "pub-ooo", 3, None)]


class Rig:
    """A real EditorBridge behind the real ExtensionService on loopback; only auth/provenance/clock are doubles."""

    def __init__(self, pins: dict):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        key = root / "signing.key"
        key.write_text(SEED)
        os.chmod(key, 0o600)
        cfg = ej.EditorConfig(root, ej.PROTOCOL, pins["contract_digest"], frozenset({pins["system_hash"]}),
                              frozenset({pins["schema_hash"]}), "https://gitlab.invalid", 86, frozenset({"tgpeek-editor-genuine"}),
                              frozenset({SHA}), launch_digest=CONFIG_DIGEST, provenance_sources=frozenset({"wire-test"}),
                              project_path="venturecrew/tgpeek", signing_key_file=str(key), signing_public_key=b64pub(SEED))
        control = FakeControl()
        control.retarget(int(JOB), int(PIPELINE))
        job = gitlab_job(id=int(JOB), pipeline_id=int(PIPELINE))
        self.bridge = ej.EditorBridge(cfg, ej.JobAuthenticator(cfg, lambda t: (200, job) if t == TOKEN else (401, None), control),
                                      FakeExecutor(good_observation(principal=IDENTITY[0], model=IDENTITY[2],
                                                                    resolved_version=IDENTITY[3],
                                                                    attestation=dict(ej.SAFE_ATTESTATION, launch_digest=CONFIG_DIGEST))),
                                      ej.Ed25519Signer(str(key), b64pub(SEED)), clock=lambda: T0)
        self.svc = ExtensionService(root, lambda *a: None, "11111111-2222-3333-4444-555555555555", trust_first_install=True)
        self.svc.editor = self.bridge
        self.port = self.svc.serve("127.0.0.1", 0).server_address[1]

    def close(self):
        self.svc.shutdown()
        self.bridge.ledger.db.close()
        self.tmp.cleanup()

    def call(self, method: str, path: str, headers: dict, body: bytes | None) -> tuple[int, str]:
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request(method, path, body=body, headers=headers)
        r = c.getresponse()
        return r.status, r.read().decode()


def pins_from(client, system: str, schema: dict) -> dict:
    return {"contract": client.contract_binding, "contract_digest": ej.digest(client.contract_binding),
            "system_hash": ej.digest(system), "schema_hash": ej.digest(schema)}


def prompt(attempt: int) -> dict:
    return {"locale": "en", "fixture": {"id": "f", "version": 1, "input": "synthetic", "hash": "0" * 64},
            "scenario": {"id": "s", "version": 1, "rubric": ["r"], "hash": "1" * 64},
            "run": {"outcome": "ok", "stop_reason": None, "conditions": ["c"]},
            "observations": [{"id": "o1", "value": "v", "knowledge": "k", "evidence_id": "e1", "observed_at": "2026-01-01"}],
            "evidence": [{"id": "e1", "kind": "transcript", "text": "synthetic transcript"}],
            "claim_markers": {"test_only": True, "synthetic": True, "provenance": {
                "source_id": None, "source_type": None, "collected_at": None, "observed_at": None, "case_id": None}},
            "claim_schema": SCHEMA,
            "corrections": [{"attempt": i, "reason": "schema_invalid"} for i in range(1, attempt)]}


def source_hashes(src: Path) -> dict:
    return {name: H((src / "tgpeek_worker" / name).read_bytes()) for name in SOURCE_FILES}


def generate(src: Path, commit: str) -> dict:
    """Drive the real client through every case. ``src`` is the pinned checkout's ``services/worker/src``."""
    sys.path.insert(0, str(src))
    try:
        from tgpeek_worker import editorial, editorial_bridge as eb
    finally:
        sys.path.remove(str(src))
    identity = editorial.ModelIdentity(*IDENTITY)
    client = eb.CodexEditorClient(identity, endpoint=ENDPOINT, token=TOKEN, public_key=b64pub(SEED), claim_schema=SCHEMA,
                                  config_digest=CONFIG_DIGEST, pipeline_id=PIPELINE, ci_job_id=JOB, commit_sha=SHA)
    pins = pins_from(client, editorial.EDITOR_SYSTEM, SCHEMA)
    rig = Rig(pins)
    records, state = [], {}

    def opened(_opener, request, *, timeout):
        method, path = request.get_method(), urlsplit(request.full_url).path
        headers = {k.lower(): v for k, v in request.header_items()}
        status, raw = rig.call(method, path, headers, request.data)
        rec = {"method": method, "path": path, "headers": headers, "body": request.data.decode() if request.data else None,
               "status": status, "response": raw}
        if method == "POST":
            mutate = state["tamper"]
            delivered = json.dumps(mutate(json.loads(raw))) if mutate else raw
            state["current"] = dict(rec, delivered=delivered, cancels=[], case=state["case"], tamper=state["name"])
            records.append(state["current"])
        else:
            state["current"]["cancels"].append(rec)
            delivered = raw
        if status != 200:
            raise urllib.error.HTTPError(request.full_url, status, "refused", {}, BytesIO(raw.encode()))
        return BytesIO(delivered.encode())

    try:
        with mock.patch.object(OpenerDirector, "open", opened), mock.patch.object(eb, "time", SimpleNamespace(time=lambda: T0)):
            for name, publication, attempt, tamper in CASES:
                state.update(tamper=TAMPER[tamper][0] if tamper else None, case=name, name=tamper)
                before = len(records)
                try:
                    reply = client.complete_attempt(system=editorial.EDITOR_SYSTEM, data=prompt(attempt), timeout_seconds=45,
                                                    context=editorial.EditorAttempt(publication, 1, attempt, H(name.encode())))
                    verdict = "accept"
                    assert reply.provenance["signature"] and reply.identity == identity
                except eb.Rejected as exc:
                    verdict = "reject:" + str(exc)
                assert len(records) == before + 1, name                    # exactly one POST per case, none refused client-side
                records[-1]["verdict"] = verdict
    finally:
        rig.close()
    return {"source": {"repo": "venturecrew/tgpeek", "commit": commit, "files": source_hashes(src)},
            "synthetic_seed_hex": SEED, "client_public_key": b64pub(SEED), "frozen_time": T0, "endpoint": ENDPOINT,
            "identity": dict(zip(("principal", "family", "model", "version"), IDENTITY)), "config_digest": CONFIG_DIGEST,
            "pins": pins, "exchanges": records}


def write_fixture(src: Path, commit: str) -> None:
    FIXTURE.write_text(json.dumps(generate(src, commit), indent=1, sort_keys=True) + "\n")


@unittest.skipIf(Ed25519PrivateKey is None, "needs the optional cryptography package")
class GoldenReplay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fx = json.loads(FIXTURE.read_text())

    def setUp(self):
        self.rig = Rig(self.fx["pins"])
        self.addCleanup(self.rig.close)

    def replay(self):
        """Feed every recorded client request to the bridge; yield (record, genuine response text, cancel answers)."""
        for rec in self.fx["exchanges"]:
            status, raw = self.rig.call(rec["method"], rec["path"], rec["headers"], rec["body"].encode())
            cancels = [self.rig.call(c["method"], c["path"], c["headers"], None) for c in rec["cancels"]]
            yield rec, status, raw, cancels

    def test_fixture_is_bound_to_the_reviewed_client(self):
        src = self.fx["source"]
        self.assertEqual(src["commit"], "566b8c1eb374d0203f065d3958657b1e20073f2a")
        self.assertEqual(set(src["files"]), set(SOURCE_FILES))
        self.assertTrue(all(len(v) == 64 for v in src["files"].values()))
        self.assertEqual([r["case"] for r in self.fx["exchanges"]], [c[0] for c in CASES])

    def test_every_request_of_the_real_client_is_accepted_and_answered_byte_for_byte(self):
        for rec, status, raw, cancels in self.replay():
            self.assertEqual((status, raw), (rec["status"], rec["response"]), rec["case"])
            self.assertEqual([(s, r) for s, r in cancels], [(c["status"], c["response"]) for c in rec["cancels"]], rec["case"])

    def test_the_wire_the_client_sent_is_the_reviewed_scalar_form(self):
        for rec in self.fx["exchanges"]:
            body = json.loads(rec["body"])
            self.assertEqual(body["protocol_version"], "tgpeek-editor-bridge-v1")
            self.assertTrue(type(body["deadline_at"]) is int and body["deadline_at"] == int(T0 * 1000) + 45_000)
            self.assertIn(body["attempt"], (1, 2, 3))
            self.assertEqual(rec["headers"]["idempotency-key"], ej.job_id_of(body))
            self.assertEqual(rec["headers"]["job-token"], TOKEN)                  # header only, never in the JSON
            self.assertNotIn(TOKEN, rec["body"])

    def test_third_attempt_is_served_and_a_skipped_attempt_is_refused(self):
        by = {r["case"]: r for r in self.fx["exchanges"]}
        self.assertEqual(json.loads(by["attempt_3"]["body"])["attempt"], 3)
        self.assertEqual((by["attempt_3"]["verdict"], json.loads(by["attempt_3"]["response"])["status"]), ("accept", "completed"))
        ooo = by["out_of_order_attempt"]
        self.assertEqual(json.loads(ooo["response"])["status"], "rejected")
        self.assertTrue(ooo["verdict"].startswith("reject:"))
        self.assertEqual(len(ooo["cancels"]), 1)                                    # the client cancels once, never retries

    def test_genuine_responses_are_accepted_by_the_client(self):
        for rec in self.fx["exchanges"]:
            if rec["tamper"] is None and rec["case"].startswith("attempt_"):
                self.assertEqual(rec["verdict"], "accept", rec["case"])
                self.assertEqual(rec["delivered"], rec["response"])
                resp = json.loads(rec["response"])
                self.assertEqual(sorted(resp), sorted(ej.RESPONSE_KEYS))
                self.assertEqual(resp["identity"], self.fx["identity"])
                self.assertEqual(resp["result_hash"], ej.digest(json.loads(resp["text"])))
                self.assertEqual(resp["executor_receipt"]["config_digest"], CONFIG_DIGEST)
                self.assertEqual(rec["cancels"], [])

    def test_every_tampered_response_was_rejected_by_the_client_with_its_own_code_and_cancelled(self):
        for rec in self.fx["exchanges"]:
            if rec["tamper"] is None:
                continue
            mutate, code = TAMPER[rec["tamper"]]
            self.assertEqual(rec["verdict"], "reject:" + code, rec["tamper"])
            self.assertEqual(json.loads(rec["delivered"]), mutate(json.loads(rec["response"])), rec["tamper"])   # the documented mutation
            self.assertNotEqual(rec["delivered"], rec["response"])
            self.assertEqual(len(rec["cancels"]), 1, rec["tamper"])                                          # one DELETE
            self.assertEqual(rec["cancels"][0]["path"], ej.POST_PATH + "/" + json.loads(rec["response"])["job_id"])
            self.assertEqual(rec["cancels"][0]["headers"]["job-token"], TOKEN)

    def test_positive_control_a_resigned_but_unmodified_response_is_not_a_tamper(self):
        rec = self.fx["exchanges"][0]
        genuine = json.loads(rec["response"])
        self.assertEqual(resign(genuine), genuine)                                   # Ed25519 is deterministic
        self.assertNotEqual(resign(genuine, OTHER_SEED), genuine)                    # negative control for the helper

    def test_replay_of_a_completed_key_never_reruns_the_executor(self):
        list(self.replay())
        calls = self.rig.bridge.executor.calls
        rec = self.fx["exchanges"][0]
        status, raw = self.rig.call(rec["method"], rec["path"], rec["headers"], rec["body"].encode())
        self.assertEqual((status, self.rig.bridge.executor.calls), (200, calls))


@unittest.skipUnless(os.environ.get("TGPEEK_CLIENT_SRC") and Ed25519PrivateKey, "set TGPEEK_CLIENT_SRC to the pinned client source")
class LiveClient(unittest.TestCase):
    def test_pinned_source_and_regenerated_fixture_equal_the_stored_one(self):
        src = Path(os.environ["TGPEEK_CLIENT_SRC"])
        stored = json.loads(FIXTURE.read_text())
        self.assertEqual(source_hashes(src), stored["source"]["files"])                # this really is the pinned client
        self.assertEqual(generate(src, stored["source"]["commit"]), stored)


if __name__ == "__main__":
    if sys.argv[1:2] == ["regenerate"]:
        git_dir, commit = sys.argv[2], sys.argv[3]
        tree = Path(tempfile.mkdtemp(prefix="tgpeek-client-"))
        archive = subprocess.run(["git", "-C", git_dir, "archive", commit, "services/worker/src"], check=True, capture_output=True)
        subprocess.run(["tar", "-x", "-C", str(tree)], input=archive.stdout, check=True)
        write_fixture(tree / "services/worker/src", commit)
    else:
        unittest.main()
