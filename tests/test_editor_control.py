"""Operator lifecycle of the editor bridge inside one runner (editor_control).

Real boundaries: the real ExtensionService HTTP listener, signed requests, the real ProcessExecutor with a harmless child (and
grandchild) process, the real sqlite ledger and the real host-cap lock/lease files. The fakes are the external ones only: the
GitLab control plane, the tmux window list and the clock of the job envelope.
"""
import http.client
import json
import os
import signal
import tempfile
import threading
import time
import unittest
from pathlib import Path

from mesh_runner import editor_control as ec
from mesh_runner import editor_job as ej
from mesh_runner import host_admission as ha
from mesh_runner.config import ConfigError, load as load_config
from mesh_runner.editor_job import EditorBridge, JobAuthenticator, ProcessExecutor
from mesh_runner.extension_service import ExtensionService

from test_editor_host_admission import FakeTmux, write_harness
from test_editor_job import CONTRACT, LAUNCH, NOW, SAFE, SCHEMA, SHA, SYSTEM, TOKEN, Base, HmacSigner, good_observation

SECRET = "s" * 40
OBS = {"principal": "tgpeek-editor", "family": "openai", "model": "gpt-6.1-sol", "resolved_version": "gpt-6.1-sol-2026-10-01",
       "provenance_source": "wire-test", "tools_used": [], "attestation": dict(SAFE)}
RESULT = json.dumps({"claims": [], "limitations": []})


class Rig(Base):
    def setUp(self):
        super().setUp()
        self.bridge.ledger.db.close()
        root = Path(self.dir.name)
        self.hang = root / "hang"
        self.pids = root / "pids"
        self.argv = write_harness(root, f"""
            import json, os, subprocess, time
            if os.path.exists({str(self.hang)!r}):
                kid = subprocess.Popen(["sleep", "60"])             # a grandchild in the same process group
                open({str(self.pids)!r}, "w").write(str(kid.pid))
                time.sleep(60)
            print(json.dumps({{"text": {RESULT!r}, "observation": {OBS!r}}}))
        """)
        self.section = {"enabled": True, "contract_digest": CONTRACT, "job_names": ["tgpeek-editor-genuine"],
                        "gitlab_url": "https://gitlab.invalid", "project_id": 86, "allowed_shas": [SHA],
                        "system_hashes": [SYSTEM], "schema_hashes": [SCHEMA], "provenance_sources": ["wire-test"],
                        "project_path": "venturecrew/tgpeek", "argv": list(self.argv), "launch_digest": LAUNCH}
        self.secret = root / "operator.secret"
        self.secret.write_text(SECRET)
        os.chmod(self.secret, 0o600)
        self.tmux = FakeTmux()
        self.hostroot = root / "interactive"
        self.fail_build = False
        self.svc = ExtensionService(root, lambda *a: None, "0" * 8 + "-0000-0000-0000-" + "0" * 12, trust_first_install=True)
        self.ctl = ec.EditorController(self.svc, root, str(self.secret), lambda: dict(self.section), build=self.make,
                                       admission=lambda: ha.HostAdmission(self.hostroot, 2, self.tmux), source="/etc/runner.toml",
                                       drain_timeout=3.0)
        self.svc.controller = self.ctl
        cfg = ej.parse_config(self.section, root)
        self.ctl.adopt(self.make(cfg, admission=ha.HostAdmission(self.hostroot, 2, self.tmux)), cfg)
        self.addCleanup(self.stop_all)

    def stop_all(self):
        if self.svc.editor is not None:
            self.svc.editor.close(2)
            self.svc.editor.ledger.db.close()
        try:
            os.kill(int(self.pids.read_text()), signal.SIGKILL)
        except (OSError, ValueError):
            pass

    def make(self, cfg, admission=None):
        if self.fail_build:
            raise OSError("boom")

        def fetch(token):
            return (200, self.job) if token == TOKEN else (401, None)
        return EditorBridge(cfg, JobAuthenticator(cfg, fetch, self.control), ProcessExecutor(cfg.argv, Path(self.dir.name) / "editor-tmp"),
                            HmacSigner(), clock=lambda: NOW, admission=admission)

    # ------------------------------------------------------------------ helpers
    def submit(self, env=None):
        env = env or self.envelope()
        return self.svc.editor.submit({"JOB-TOKEN": TOKEN, "Idempotency-Key": ej.job_id_of(env)}, json.dumps(env).encode())

    def call(self, action, peer="127.0.0.1", secret=SECRET, extra_headers=None, ts=None, **req):
        req.setdefault("request_id", os.urandom(8).hex())
        if action != "status" and "expected_generation" in req:         # the process identity of the binding the caller read
            req.setdefault("expected_pid", self.ctl.pid)
            req.setdefault("expected_started_at", self.ctl.started_at)
            req.setdefault("expected_instance", self.ctl.instance)
        body = json.dumps(req).encode()
        path = ec.PREFIX + action
        ts = ts or str(int(time.time()))
        headers = {"X-Mesh-Timestamp": ts, "X-Operator-Signature": ec.sign_request(secret, ts, path, body)}
        headers.update(extra_headers or {})
        return self.ctl.handle("POST", path, headers, body, peer)

    def status(self):
        status, out = self.call("status")
        self.assertEqual(status, 200, out)
        return out

    def bind(self, **over):
        st = self.status()
        return dict({"expected_generation": st["generation"], "expected_digest": st["applied_digest"],
                     "expected_pid": st["pid"], "expected_started_at": st["started_at"],
                     "expected_instance": st["instance"]}, **over)

    @staticmethod
    def alive(pid):
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False

    def run_hanging_job(self):
        self.hang.write_text("1")
        out = {}
        t = threading.Thread(target=lambda: out.__setitem__("r", self.submit()))
        t.start()
        for _ in range(500):
            if self.pids.exists() and self.pids.read_text():
                break
            time.sleep(0.01)
        self.assertTrue(self.pids.exists(), "the harmless child never started")
        return t, out, int(self.pids.read_text())


class Boundary(Rig):
    def test_off_by_default_without_a_secret_or_with_a_loose_one(self):
        loose = Path(self.dir.name) / "loose"
        loose.write_text(SECRET)
        os.chmod(loose, 0o644)
        for f in ("", str(loose), str(Path(self.dir.name) / "missing")):
            ctl = ec.EditorController(self.svc, self.dir.name, f, lambda: {})
            self.assertEqual(ctl.handle("POST", ec.PREFIX + "status", {}, b"{}", "127.0.0.1"), (403, {"error": "operator_control_disabled"}))

    def test_signature_loopback_job_token_and_replay(self):
        path = ec.PREFIX + "status"
        body = json.dumps({"request_id": "r" * 12}).encode()
        self.assertEqual(self.ctl.handle("POST", path, {}, body, "127.0.0.1")[1], {"error": "bad_signature"})
        self.assertEqual(self.call("status", secret="x" * 40)[1], {"error": "bad_signature"})
        self.assertEqual(self.call("status", peer="10.0.0.5")[1], {"error": "loopback_only"})
        self.assertEqual(self.call("status", extra_headers={"JOB-TOKEN": TOKEN})[1], {"error": "job_token_not_accepted"})
        self.assertEqual(self.call("status", ts=str(int(time.time()) - 4000))[1], {"error": "bad_signature"})
        self.assertEqual(self.call("status", request_id="same-request-id")[0], 200)
        self.assertEqual(self.call("status", request_id="same-request-id"), (409, {"error": "replayed"}))
        body2 = json.dumps({"request_id": "q" * 12}).encode()          # a signature made for one action is no good for another
        ts = str(int(time.time()))
        sig = ec.sign_request(SECRET, ts, ec.PREFIX + "status", body2)
        self.assertEqual(self.ctl.handle("POST", ec.PREFIX + "disable", {"X-Mesh-Timestamp": ts, "X-Operator-Signature": sig},
                                         body2, "127.0.0.1")[1], {"error": "bad_signature"})
        self.assertIsNotNone(self.svc.editor)

    def test_the_real_listener_serves_a_signed_call_and_refuses_the_job_token(self):
        server = self.svc.serve("127.0.0.1", 0)
        self.addCleanup(lambda: (server.shutdown(), server.server_close()))
        port = server.server_address[1]

        def post(headers, req):
            body = json.dumps(req).encode()
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            c.request("POST", ec.PREFIX + "status", body, headers)
            r = c.getresponse()
            return r.status, json.loads(r.read())
        ts = str(int(time.time()))
        req = {"request_id": "http-request-1"}
        sig = ec.sign_request(SECRET, ts, ec.PREFIX + "status", json.dumps(req).encode())
        status, out = post({"X-Mesh-Timestamp": ts, "X-Operator-Signature": sig}, req)
        self.assertEqual((status, out["state"], out["generation"]), (200, "active", 1))
        status, out = post({"X-Mesh-Timestamp": ts, "X-Operator-Signature": sig, "JOB-TOKEN": TOKEN}, {"request_id": "http-request-2"})
        self.assertEqual((status, out), (403, {"error": "job_token_not_accepted"}))
        self.svc.controller = None
        self.assertEqual(post({}, req), (403, {"error": "operator_control_disabled"}))


class Detach(Rig):
    def test_a_request_never_dereferences_the_editor_pointer_twice(self):
        class Flip(ExtensionService):
            """``editor`` answers the bridge once and None after: a disable landing between two reads of a handler."""
            reads = 0

            @property
            def editor(self):
                type(self).reads += 1
                return self._ed if type(self).reads == 1 else None

            @editor.setter
            def editor(self, value):
                self._ed = value
        svc = Flip(self.dir.name, lambda *a: None, "0" * 8 + "-0000-0000-0000-" + "0" * 12, trust_first_install=True)
        svc.editor = self.svc.editor
        Flip.reads = 0
        server = svc.serve("127.0.0.1", 0)
        self.addCleanup(lambda: (server.shutdown(), server.server_close()))
        c = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
        c.request("POST", ej.POST_PATH, b"{}", {"JOB-TOKEN": TOKEN, "Idempotency-Key": "k"})
        status = c.getresponse().status
        self.assertNotEqual(status, 500)                         # a bridge answer (400 bad body), never an internal error


class Readback(Rig):
    def test_applied_binding_is_told_from_the_disk(self):
        st = self.status()
        self.assertEqual((st["state"], st["generation"], st["pid"], st["applied_matches_disk"]), ("active", 1, os.getpid(), True))
        self.assertEqual(st["source"], "/etc/runner.toml")
        self.assertLessEqual(abs(st["started_at"] - time.time()), 60)
        self.section["allowed_shas"] = ["b" * 40]                     # an edit on disk is prepared, not applied
        again = self.status()
        self.assertEqual(again["applied_digest"], st["applied_digest"])
        self.assertNotEqual(again["disk_digest"], st["applied_digest"])
        self.assertFalse(again["applied_matches_disk"])
        self.section["project_id"] = "86"
        self.assertEqual(self.status()["disk_state"], "invalid")

    def test_stale_or_missing_binding_is_refused_and_changes_nothing(self):
        st = self.status()
        for req, want in (({}, (400, "expected_generation_required")),
                          ({"expected_generation": 1}, (400, "expected_digest_required")),
                          ({"expected_generation": 9, "expected_digest": st["applied_digest"]}, (409, "stale_binding")),
                          ({"expected_generation": 1, "expected_digest": "0" * 64}, (409, "stale_binding"))):
            for action in ("reconfigure", "disable", "rollback"):
                status, out = self.call(action, **req)
                self.assertEqual((status, out["error"]), want, (action, req))
        self.assertEqual(self.status()["generation"], 1)
        self.assertEqual(self.submit()[1]["status"], "completed")


class Identity(Rig):
    def test_a_binding_signed_for_another_runner_process_is_stale_though_generation_and_digest_match(self):
        bind = self.bind()
        for key, wrong in (("expected_pid", self.ctl.pid + 1), ("expected_started_at", self.ctl.started_at - 1),
                           ("expected_instance", "0" * 32)):
            for action in ("reconfigure", "disable", "rollback", "connect"):
                status, out = self.call(action, **dict(bind, **{key: wrong}))
                self.assertEqual((status, out["error"]), (409, "stale_binding"), (action, key))
        bare = {"expected_generation": 1, "expected_digest": bind["expected_digest"]}
        status, out = self.ctl.handle("POST", ec.PREFIX + "disable", *self._signed("disable", bare))
        self.assertEqual((status, out["error"]), (400, "expected_generation_required"))   # the identity is not optional
        self.assertEqual(self.status()["state"], "active")
        # a restarted runner is a new controller: generation 1 again, same digest, other identity
        later = ec.EditorController(self.svc, self.dir.name, str(self.secret), lambda: dict(self.section), clock=lambda: time.time() + 100)
        later.pid, later.started_at = self.ctl.pid, self.ctl.started_at      # same pid and same second: a quick restart
        later.adopt(self.svc.editor, self.ctl.applied)
        self.svc.controller = later
        self.ctl = later
        self.assertEqual(later.generation, bind["expected_generation"])
        status, out = self.ctl.handle("POST", ec.PREFIX + "disable", *self._signed("disable", bind))
        self.assertEqual((status, out["error"]), (409, "stale_binding"))
        self.assertIsNotNone(self.svc.editor)

    def _signed(self, action, req):
        body = json.dumps(dict(req, request_id=os.urandom(8).hex())).encode()
        ts = str(int(time.time()))
        return ({"X-Mesh-Timestamp": ts, "X-Operator-Signature": ec.sign_request(SECRET, ts, ec.PREFIX + action, body)}, body, "127.0.0.1")


class Lifecycle(Rig):
    def test_reconfigure_idle_swaps_the_binding_and_keeps_the_budget(self):
        self.assertEqual(self.submit()[1]["status"], "completed")
        old, before = self.svc.editor, self.status()
        self.section["allowed_shas"] = [SHA, "b" * 40]
        status, out = self.call("reconfigure", **self.bind())
        self.assertEqual(status, 200, out)
        self.assertEqual((out["generation"], out["state"], out["applied_matches_disk"]), (3, "active", True))
        self.assertIsNot(self.svc.editor, old)
        self.assertTrue(old.closed)
        self.assertEqual(out["previous_digest"], before["applied_digest"])
        self.assertEqual(self.svc.editor.ledger.used(ej.sha256(ej.canonical([86, "pub-1", 1]))), 1)
        status, out = self.submit()                                    # the same key is never run again; the terminal cache is memory only
        self.assertEqual((status, out["status"]), (200, "failed"))
        self.assertEqual(self.svc.editor.ledger.used(ej.sha256(ej.canonical([86, "pub-1", 1]))), 1)

    def test_an_unchanged_reconfigure_stops_nothing(self):
        live = self.svc.editor
        status, out = self.call("reconfigure", **self.bind())
        self.assertEqual((status, out["unchanged"], out["generation"]), (200, True, 1))
        self.assertIs(self.svc.editor, live)
        self.assertFalse(live.closed)

    def test_a_running_job_is_killed_and_drained_before_the_swap_with_no_late_success(self):
        self.tmux.add("mr-builder", "card-window")                    # an unrelated card window must survive
        t, out, pid = self.run_hanging_job()
        self.assertTrue(self.alive(pid))
        old = self.svc.editor
        self.section["allowed_shas"] = [SHA, "c" * 40]
        status, res = self.call("reconfigure", **self.bind())
        self.assertEqual(status, 200, res)
        t.join(5)
        self.assertIn(out["r"][1]["status"], ("cancelled", "failed"))
        for _ in range(100):
            if not self.alive(pid):
                break
            time.sleep(0.02)
        self.assertFalse(self.alive(pid))
        self.assertEqual(old.residue(), [])
        self.assertEqual(list((Path(self.dir.name) / "editor-tmp").glob("job-*")), [])
        self.assertEqual(self.tmux.all_windows(), [("mr-builder", "card-window")])
        self.assertEqual(list((self.hostroot / ha.LEASE_DIR).glob("*.lease")), [])
        self.hang.unlink()
        self.assertEqual(self.submit(self.envelope(publication_id="pub-2"))[1]["status"], "completed")

    def test_disable_is_idempotent_and_connect_restores_with_the_same_budget(self):
        self.assertEqual(self.submit()[1]["status"], "completed")
        status, out = self.call("disable", **self.bind())
        self.assertEqual((status, out["state"], out["generation"]), (200, "disabled", 2))
        self.assertIsNone(self.svc.editor)
        status, again = self.call("disable", expected_generation=2)
        self.assertEqual((status, again.get("unchanged")), (200, True))
        self.assertEqual(self.status()["generation"], 2)
        status, out = self.call("connect", expected_generation=2)
        self.assertEqual((status, out["state"], out["generation"]), (200, "active", 3))
        self.assertEqual(self.svc.editor.ledger.used(ej.sha256(ej.canonical([86, "pub-1", 1]))), 1)
        status, out = self.submit(self.envelope(publication_id="pub-1", attempt=1, input_hash=ej.sha256(b"other")))
        self.assertNotEqual(out.get("status"), "completed")                  # a reconnect does not reset the budget

    def test_connect_refusals(self):
        self.assertEqual(self.call("connect", expected_generation=1)[1]["error"], "already_connected")
        self.call("disable", **self.bind())
        self.section["enabled"] = False
        self.assertEqual(self.call("connect", expected_generation=2)[1]["error"], "config_disabled_on_disk")
        self.section.update(enabled=True, project_id="86")
        self.assertEqual(self.call("connect", expected_generation=2)[1]["error"], "config_invalid")
        self.assertIsNone(self.svc.editor)

    def test_a_failed_build_stays_disabled_and_rollback_restores_the_previous_config(self):
        first = self.status()
        self.section["allowed_shas"] = [SHA, "d" * 40]
        self.fail_build = True
        status, out = self.call("reconfigure", **self.bind())
        self.assertEqual((status, out["error"], out["readback"]["state"]), (500, "build_failed", "disabled"))
        self.assertIsNone(self.svc.editor)
        self.fail_build = False
        status, out = self.call("rollback", expected_generation=self.status()["generation"])
        self.assertEqual((status, out["state"], out["applied_digest"]), (200, "active", first["applied_digest"]))
        self.assertEqual(self.submit()[1]["status"], "completed")

    def test_rollback_without_a_previous_config_is_refused(self):
        self.assertEqual(self.call("rollback", **self.bind())[1]["error"], "no_previous_config")


class FailClosed(Rig):
    def test_a_job_that_ignores_cancel_is_a_drain_timeout_not_a_success(self):
        release, started = threading.Event(), threading.Event()

        def stubborn(job_input, deadline, cancel):
            started.set()
            release.wait(10)
            return good_observation()
        self.svc.editor.executor = type("E", (), {"run": staticmethod(stubborn)})()
        self.ctl.drain_timeout = 0.3
        out = {}
        t = threading.Thread(target=lambda: out.__setitem__("r", self.submit()))
        t.start()
        self.assertTrue(started.wait(5))
        status, res = self.call("disable", **self.bind())
        self.assertEqual((status, res["error"], res["readback"]["state"]), (504, "drain_timeout", "closed"))
        self.assertIsNotNone(self.svc.editor)
        self.assertEqual(self.submit(self.envelope(publication_id="pub-9")), (503, {"error": "shutting_down"}))
        status, res = self.call("reconfigure", **self.bind())
        self.assertEqual((status, res["error"]), (504, "drain_timeout"))
        release.set()
        t.join(5)
        self.ctl.drain_timeout = 3.0
        status, res = self.call("disable", **self.bind())
        self.assertEqual((status, res["state"]), (200, "disabled"))

    def test_leftover_scratch_is_cleanup_failed_and_the_editor_stays_closed(self):
        scratch = Path(self.dir.name) / "editor-tmp" / "job-leftover"
        scratch.mkdir(parents=True)
        status, res = self.call("disable", **self.bind())
        self.assertEqual((status, res["error"]), (500, "cleanup_failed"))
        self.assertIsNotNone(self.svc.editor)
        self.assertTrue(self.svc.editor.closed)
        scratch.rmdir()
        self.assertEqual(self.call("disable", **self.bind())[0], 200)

    def test_submits_racing_a_disable_never_succeed_after_it(self):
        results, stop = [], threading.Event()

        def hammer(i):
            n = 0
            while not stop.is_set():
                n += 1
                bridge = self.svc.editor
                if bridge is None:
                    results.append(("detached", time.monotonic()))
                    time.sleep(0.005)
                    continue
                env = self.envelope(publication_id=f"race-{i}-{n}")
                try:
                    _, out = bridge.submit({"JOB-TOKEN": TOKEN, "Idempotency-Key": ej.job_id_of(env)}, json.dumps(env).encode())
                except Exception as e:                                  # the ledger of a closed bridge is gone: still a refusal
                    out = {"error": "detached:" + type(e).__name__}
                results.append((out.get("status") or out.get("error"), time.monotonic()))
                time.sleep(0.005)                                       # a client does not spin on a refusal
        threads = [threading.Thread(target=hammer, args=(i,)) for i in range(4)]
        for th in threads:
            th.start()
        for _ in range(500):                                            # let the hammer reach the executor first
            if any(r == "completed" for r, _ in list(results)):
                break
            time.sleep(0.01)
        status, res = self.call("disable", **self.bind())
        done = time.monotonic()
        time.sleep(0.2)
        stop.set()
        for th in threads:
            th.join(5)
        self.assertEqual(status, 200, res)
        self.assertEqual([r for r, at in results if at > done and r != "detached" and r != "shutting_down"
                          and not str(r).startswith("detached:")], [])
        self.assertIn("completed", [r for r, _ in results])             # the hammer did reach the executor before the stop
        self.assertIsNone(self.svc.editor)


class Config(unittest.TestCase):
    def test_the_control_section_is_off_unless_an_absolute_secret_file_is_named(self):
        with tempfile.TemporaryDirectory() as d:
            base = f'[runner]\nmesh_url = "https://mesh.invalid"\nstate_dir = "{d}"\n'
            p = Path(d) / "c.toml"
            p.write_text(base)
            cfg = load_config(p)
            self.assertEqual((cfg.tgpeek_editor_control_secret_file, cfg.tgpeek_editor), ("", None))
            self.assertEqual(cfg.config_path, str(p.resolve()))
            p.write_text(base + '[tgpeek_editor_control]\nsecret_file = "relative"\n')
            with self.assertRaises(ConfigError):
                load_config(p)
            p.write_text(base + '[tgpeek_editor_control]\nsecret_file = "/abs/operator.secret"\n')
            self.assertEqual(load_config(p).tgpeek_editor_control_secret_file, "/abs/operator.secret")


if __name__ == "__main__":
    unittest.main()
