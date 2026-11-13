"""Repeat-safe writes: the runner's comments and spend reports carry an Idempotency-Key
derived from what the write is; a lost answer is repeated with the same key; a restart rebuilds the
same key; ``idempotency_keys = false`` sends no key and repeats nothing.

``MeshDouble`` plays Mesh's idempotency middleware (internal/middleware/idempotency.go): the same key and
body answer the stored response and write nothing, the same key with another body is 409
``idempotency_key_reused``. It applies a write before it can lose the answer, as a real timeout may."""
import hashlib
import io
import json
import tempfile
import unittest
import unittest.mock
import urllib.error
from email.message import Message
from pathlib import Path

from mesh_runner import config as config_mod
from mesh_runner.mesh import MeshClient, MeshError, session_report_key, write_key


class MeshDouble:
    def __init__(self, lose=0, honour=True):
        self.lose, self.honour = lose, honour
        self.sent, self.stored, self.comments, self.reports = [], {}, [], []

    def __call__(self, req, timeout=None):
        self.sent.append(req)
        key = req.get_header("Idempotency-key")
        body = req.data or b""
        scope = (req.get_method(), req.full_url, key)
        if key and self.honour and scope in self.stored:
            digest, answer = self.stored[scope]
            if digest != hashlib.sha256(body).hexdigest():
                raise urllib.error.HTTPError(req.full_url, 409, "Conflict", Message(), io.BytesIO(
                    b'{"code":409,"error_code":"idempotency_key_reused","message":"used with another body"}'))
            return self._answer(answer)
        if req.full_url.endswith("/comments"):
            self.comments.append(json.loads(body)["body"])
            answer = json.dumps({"id": f"c{len(self.comments)}"}).encode()
        else:
            self.reports.append(json.loads(body))
            answer = b"{}"
        if key and self.honour:
            self.stored[scope] = (hashlib.sha256(body).hexdigest(), answer)
        return self._answer(answer)

    def _answer(self, answer):
        if self.lose > 0:
            self.lose -= 1
            raise TimeoutError("timed out")          # applied (or replayed), the answer is gone
        return io.BytesIO(answer)


def client(double, keys=True):
    c = MeshClient("https://m", "k", opener=double)
    c.idempotency_keys = keys
    return c


class Base(unittest.TestCase):
    def setUp(self):
        p = unittest.mock.patch("mesh_runner.mesh.time.sleep", lambda s: None)
        p.start()
        self.addCleanup(p.stop)


class CommentKeys(Base):
    def test_lost_answer_is_repeated_with_the_same_key_and_posts_once(self):
        mesh = MeshDouble(lose=1)
        out = client(mesh).add_comment("t1", "[runner] done", key="completion|a1")
        self.assertEqual(out, {"id": "c1"})                       # the stored first answer
        self.assertEqual(len(mesh.sent), 2)
        self.assertEqual(mesh.sent[0].get_header("Idempotency-key"), mesh.sent[1].get_header("Idempotency-key"))
        self.assertEqual(mesh.comments, ["[runner] done"])

    def test_restart_between_send_and_answer_posts_once(self):
        mesh = MeshDouble(lose=3)                                 # every repeat of the first process lost too
        with self.assertRaises(MeshError):
            client(mesh).add_comment("t1", "[runner] NO OUTCOME at 10:00", key="no-outcome|a1|2")
        # the runner restarts and rebuilds the note: another timestamp, the same kind/attempt/turn
        out = client(mesh).add_comment("t1", "[runner] NO OUTCOME at 10:05", key="no-outcome|a1|2")
        self.assertEqual(out, {"idempotent_reused": True})
        self.assertEqual(mesh.comments, ["[runner] NO OUTCOME at 10:00"])

    def test_key_names_the_write_not_its_text(self):
        k = write_key("comment", "t1", "quiet|a1|3")
        self.assertEqual(k, write_key("comment", "t1", "quiet|a1|3"))
        self.assertNotEqual(k, write_key("comment", "t2", "quiet|a1|3"))       # another task
        self.assertNotEqual(k, write_key("comment", "t1", "quiet|a1|4"))       # another turn

    def test_in_progress_waits_retry_after_then_gets_the_answer(self):
        calls = []

        def opener(req, timeout=None):
            calls.append(req)
            if len(calls) == 1:
                h = Message()
                h["Retry-After"] = "1"
                raise urllib.error.HTTPError(req.full_url, 409, "Conflict", h, io.BytesIO(
                    b'{"code":409,"error_code":"idempotency_request_in_progress","message":"retry"}'))
            return io.BytesIO(b'{"id":"c1"}')
        self.assertEqual(client(opener).add_comment("t1", "x", key="k"), {"id": "c1"})
        self.assertEqual(len(calls), 2)

    def test_in_progress_longer_than_the_cap_is_not_repeated_early(self):
        sent = []

        def busy(req, timeout=None):
            sent.append(req)
            m = Message()
            m["Retry-After"] = "120"
            raise urllib.error.HTTPError(req.full_url, 409, "Conflict", m, io.BytesIO(
                b'{"code":409,"error_code":"idempotency_request_in_progress","message":"busy"}'))
        with self.assertRaises(MeshError) as e:
            client(busy).add_comment("t1", "hello", key="completion|a1")
        self.assertEqual(e.exception.status, 409)
        self.assertEqual(len(sent), 1)

    def test_unkeyed_comment_is_never_repeated(self):
        mesh = MeshDouble(lose=1)
        with self.assertRaises(MeshError):
            client(mesh).add_comment("t1", "agent text")
        self.assertEqual(len(mesh.sent), 1)
        self.assertIsNone(mesh.sent[0].get_header("Idempotency-key"))

    def test_rollback_sends_no_key_and_repeats_nothing(self):
        mesh = MeshDouble(lose=1)
        with self.assertRaises(MeshError):
            client(mesh, keys=False).add_comment("t1", "x", key="completion|a1")
        self.assertEqual(len(mesh.sent), 1)
        self.assertIsNone(mesh.sent[0].get_header("Idempotency-key"))

    def test_other_409_is_not_swallowed(self):
        def opener(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 409, "Conflict", Message(), io.BytesIO(
                b'{"code":409,"error_code":"checkout_precondition_failed","message":"lost"}'))
        with self.assertRaises(MeshError):
            client(opener).add_comment("t1", "x", key="k")


class SpendReportKeys(Base):
    def test_lost_answer_counts_once(self):
        mesh = MeshDouble(lose=1)
        client(mesh).session_report("t1", 100, 5, "m", 0.01, report_id="a1:1")
        self.assertEqual(len(mesh.sent), 2)
        self.assertEqual(len(mesh.reports), 1)
        self.assertEqual(mesh.sent[1].get_header("Idempotency-key"), session_report_key("a1:1"))

    def test_restart_between_send_and_answer_counts_once(self):
        mesh = MeshDouble(lose=3)
        with self.assertRaises(MeshError):
            client(mesh).session_report("t1", 100, 5, "m", 0.01, report_id="a1:1")
        client(mesh).session_report("t1", 100, 5, "m", 0.01, report_id="a1:1")
        self.assertEqual(len(mesh.reports), 1)


class RunnerNotes(unittest.TestCase):
    """The runner names its own notes: the same note of the same attempt/turn is one write."""

    def runner(self):
        from mesh_runner.runner import AgentRunner
        r = AgentRunner.__new__(AgentRunner)
        r.agent = type("A", (), {"name": "lane"})()
        r.mesh = type("M", (), {})()
        r.mesh.calls = []
        r.mesh.add_comment = lambda tid, body, key="": r.mesh.calls.append((tid, body, key))
        return r

    def test_a_note_must_name_its_key(self):
        r = self.runner()
        with self.assertRaises(TypeError):
            r._comment("t1", "hello")

    def test_every_runner_note_names_its_key(self):
        import ast
        import mesh_runner.runner as runner_mod
        tree = ast.parse(Path(runner_mod.__file__).read_text())
        unkeyed = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Call)
                   and getattr(n.func, "attr", "") == "_comment"
                   and not any(k.arg == "key" for k in n.keywords)]
        self.assertEqual(unkeyed, [])

    def test_no_note_key_is_built_from_its_content(self):
        """A key is the write's identity (kind, attempt/turn, day, delivered count), never a digest
        of what it says: changed facts after a restart would get a new key and post twice."""
        import ast
        import mesh_runner.runner as runner_mod
        tree = ast.parse(Path(runner_mod.__file__).read_text())
        hashed = []
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and getattr(n.func, "attr", "") in ("_comment", "add_comment"):
                for k in n.keywords:
                    if k.arg == "key" and any(isinstance(x, ast.Name) and x.id == "hashlib"
                                              or isinstance(x, ast.Attribute) and x.attr in ("hexdigest", "fingerprint")
                                              for x in ast.walk(k.value)):
                        hashed.append(n.lineno)
        self.assertEqual(hashed, [])

    def test_a_rebuilt_note_keeps_its_key_until_delivered(self):
        from mesh_runner.store import Store
        r = self.runner()
        r.store = Store(Path(tempfile.mkdtemp()) / "s.sqlite")
        first = r._note_key("t1", "repo-preflight")
        self.assertEqual(r._note_key("t1", "repo-preflight"), first)      # restart before the answer
        r._note_delivered("t1", "repo-preflight")
        self.assertNotEqual(r._note_key("t1", "repo-preflight"), first)   # the next diagnosis is a new write
        self.assertEqual(r._note_key("t2", "repo-preflight"), "repo-preflight|t2|0")

    def test_explicit_key_wins(self):
        r = self.runner()
        r._comment("t1", "x", key="completion|a1")
        self.assertEqual(r.mesh.calls, [("t1", "[runner] x", "completion|a1")])


class Config(unittest.TestCase):
    def load(self, extra):
        d = Path(tempfile.mkdtemp())
        p = d / "runner.toml"
        p.write_text(f'[runner]\nmesh_url = "https://m"\nstate_dir = "{d}"\n{extra}')
        return config_mod.load(p)

    def test_default_on_and_rollback_key(self):
        self.assertTrue(self.load("").idempotency_keys)
        self.assertFalse(self.load("idempotency_keys = false\n").idempotency_keys)

    def test_not_a_bool_is_refused(self):
        with self.assertRaises(config_mod.ConfigError):
            self.load('idempotency_keys = "no"\n')


if __name__ == "__main__":
    unittest.main()


class GmailService(unittest.TestCase):
    """The Gmail service builds its own clients: the rollback key reaches them too."""

    def clients(self, keys):
        from mesh_runner.service_process import gmail_clients
        agent = type("Ag", (), {"name": "lane", "key": "k"})()
        cfg = type("Cfg", (), {"agents": [agent], "mesh_url": "https://m", "mesh_basic_auth": "",
                               "extension_token": None, "idempotency_keys": keys})()
        return gmail_clients(cfg)

    def test_rollback_reaches_the_gmail_clients(self):
        self.assertEqual([m.idempotency_keys for _, m in self.clients(False)], [False])
        self.assertEqual([m.idempotency_keys for _, m in self.clients(True)], [True])
