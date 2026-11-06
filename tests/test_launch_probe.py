"""Launch-context probe: real helper subprocesses over the private stdin protocol; the
helper's provider side is the only fixture (it is an external boundary)."""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from mesh_runner import interactive, launch_probe, spawn_gate

HELPER = r'''
import json, os, subprocess, sys, time
out, mode = sys.argv[1], sys.argv[2]
raw = sys.stdin.buffer.read()
if mode == "eof-early":
    sys.exit(0)
req = json.loads(raw)
open(os.path.join(out, "request.json"), "wb").write(raw)
open(os.path.join(out, "runs.log"), "a").write("run\n")
open(os.path.join(out, "argv.json"), "w").write(json.dumps(sys.argv))
REF = "ab" * 32
reply = {"version": 1, "nonce": req["nonce"], "status": "available", "reason": "available",
         "account_ref": REF, "quota_bucket": "openai:" + REF + ":codex", "observed_at": 1762344000,
         "cleanup_ok": True}
if req["version"] == 2 and mode != "v1-reply":
    reply.update(version=2, primary_used_percent=37, secondary_used_percent=None)
if mode == "pct-unknown-null":
    reply.update(status="account_unknown", reason="timeout", account_ref=None, quota_bucket=None,
                 primary_used_percent=None, secondary_used_percent=None, cleanup_ok=None)
elif mode == "pct-unknown-measured":
    reply.update(status="account_unknown", reason="timeout", account_ref=None, quota_bucket=None)
elif mode == "pct-high":
    reply["primary_used_percent"] = 101
elif mode == "pct-bool":
    reply["primary_used_percent"] = True
elif mode == "pct-float":
    reply["secondary_used_percent"] = 1.5
elif mode == "pct-missing":
    del reply["secondary_used_percent"]
elif mode == "pct-full":
    reply.update(primary_used_percent=0, secondary_used_percent=100)
if mode == "second-denies" and len(open(os.path.join(out, "runs.log")).read().split()) >= 2:
    reply.update(status="quota_blocked", reason="quota_blocked")
if mode == "dup":
    r = json.dumps(reply)
    print(r[:-1] + ', "reason": "available"}'); sys.exit(0)
if mode == "blocked":
    reply.update(status="quota_blocked", reason="quota_blocked")
elif mode == "unknown":
    reply.update(status="account_unknown", reason="scope_invalid", account_ref=None, quota_bucket=None)
elif mode == "nonce":
    reply["nonce"] = "0" * 32
elif mode == "foreign":
    reply["account_ref"] = "cd" * 32; reply["quota_bucket"] = "openai:" + "cd" * 32 + ":codex"
elif mode == "dirty":
    reply["cleanup_ok"] = False
elif mode == "extra":
    reply["token"] = "x"
elif mode == "unknown-with-identity":
    reply.update(status="account_unknown", reason="scope_invalid")
elif mode == "bucket":
    reply["quota_bucket"] = "other"
elif mode == "iso-time":
    reply["observed_at"] = "2026-11-05T12:00:00Z"
elif mode == "unknown-null-cleanup":
    reply.update(status="account_unknown", reason="timeout", account_ref=None, quota_bucket=None, cleanup_ok=None)
elif mode == "reason-list":
    reply["reason"] = ["available"]
elif mode == "reason-object":
    reply["reason"] = {"a": 1}
elif mode == "status-list":
    reply["status"] = [1]
elif mode == "cleanup-object":
    reply["cleanup_ok"] = {"x": 1}
elif mode == "bad-reason":
    reply["reason"] = "free text from the helper"
elif mode == "hang":
    time.sleep(60)
elif mode == "exit1":
    print(json.dumps(reply)); sys.exit(1)
elif mode == "oversize":
    sys.stdout.write("x" * 9000); sys.exit(0)
elif mode == "garbage":
    print("not json"); sys.exit(0)
elif mode == "descendant":
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    open(os.path.join(out, "child.pid"), "w").write(str(p.pid))
print(json.dumps(reply))
sys.stdout.flush()
'''


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.helper_py = self.root / "helper.py"
        self.helper_py.write_text(HELPER)
        self.addCleanup(self._tmp.cleanup)

    def helper(self, mode="ok"):
        return [sys.executable, str(self.helper_py), str(self.root), mode]

    def context(self, **env):
        home = self.root / "codex-home"
        home.mkdir(exist_ok=True)
        full = {"PATH": os.defpath, "HOME": str(self.root), "CODEX_HOME": str(home), "SECRET_TOKEN": "s3cret-value", **env}
        return launch_probe.build_context([sys.executable, "-c", "pass"], full, self.root)


class ProtocolTests(Base):
    def test_available_receives_the_exact_context_over_stdin_only(self):
        ctx = self.context()
        obs = launch_probe.observe(self.helper(), ctx)
        self.assertEqual((obs.status, obs.reason, obs.account_ref, obs.cleanup_ok), ("available", "available", "ab" * 32, True))
        self.assertTrue(obs.available)
        request = json.loads((self.root / "request.json").read_text())
        self.assertEqual(request["version"], 1)
        self.assertRegex(request["nonce"], r"^[0-9a-f]{32}$")
        self.assertEqual(request["context"], ctx)
        argv = (self.root / "argv.json").read_text()
        self.assertNotIn("s3cret-value", argv)                        # never in arguments
        self.assertNotIn(request["nonce"], argv)
        self.assertNotIn("s3cret-value", json.dumps(obs.gate_env()))  # never in the carried facts
        self.assertNotIn("s3cret-value", repr(obs))

    def test_v2_carries_both_measurements_and_never_synthesizes_zero(self):
        obs = launch_probe.observe(self.helper(), self.context(), version=2)
        self.assertEqual((obs.status, obs.primary_used_percent, obs.secondary_used_percent), ("available", 37, None))
        self.assertEqual(json.loads((self.root / "request.json").read_text())["version"], 2)
        env = obs.gate_env()
        self.assertEqual((env["MESH_RUNNER_LAUNCH_PRIMARY_USED_PERCENT"],
                          env["MESH_RUNNER_LAUNCH_SECONDARY_USED_PERCENT"]), ("37", ""))
        full = launch_probe.observe(self.helper("pct-full"), self.context(), version=2)
        self.assertEqual((full.primary_used_percent, full.secondary_used_percent), (0, 100))
        v1 = launch_probe.observe(self.helper(), self.context())
        self.assertEqual((v1.primary_used_percent, v1.secondary_used_percent), (None, None))
        self.assertEqual(v1.gate_env()["MESH_RUNNER_LAUNCH_PRIMARY_USED_PERCENT"], "")
        unknown = launch_probe.observe(self.helper("pct-unknown-null"), self.context(), version=2)
        self.assertEqual((unknown.status, unknown.primary_used_percent, unknown.secondary_used_percent),
                         ("account_unknown", None, None))

    def test_v2_rejects_invalid_or_mismatched_replies(self):
        for mode in ("pct-high", "pct-bool", "pct-float", "pct-missing", "pct-unknown-measured", "v1-reply"):
            with self.subTest(mode=mode):
                obs = launch_probe.observe(self.helper(mode), self.context(), version=2)
                self.assertEqual(obs.status, "account_unknown")
                self.assertEqual((obs.account_ref, obs.primary_used_percent), ("", None))
        self.assertEqual(launch_probe.observe(self.helper(), self.context(), version=3).reason, "invalid_request")

    def test_quota_blocked_is_distinct_from_unknown(self):
        blocked = launch_probe.observe(self.helper("blocked"), self.context())
        self.assertEqual((blocked.status, blocked.reason), ("quota_blocked", "quota_blocked"))
        unknown = launch_probe.observe(self.helper("unknown"), self.context())
        self.assertEqual(unknown.status, "account_unknown")
        self.assertFalse(blocked.available or unknown.available)

    def test_every_malformed_answer_is_unknown_without_identity(self):
        for mode in ("nonce", "dirty", "extra", "unknown-with-identity", "bad-reason", "reason-list", "reason-object", "status-list", "cleanup-object", "bucket", "iso-time", "dup", "exit1",
                     "oversize", "garbage", "eof-early"):
            with self.subTest(mode=mode):
                obs = launch_probe.observe(self.helper(mode), self.context())
                self.assertEqual(obs.status, "account_unknown")
                self.assertFalse(obs.available)
                self.assertEqual((obs.account_ref, obs.quota_bucket), ("", ""))
                self.assertIn(obs.reason, launch_probe.REASONS)

    def test_duplicate_json_keys_and_missing_helper_are_unknown(self):
        self.assertEqual(launch_probe.observe(["/nonexistent/helper"], self.context()).status, "account_unknown")
        self.assertEqual(launch_probe.observe([], self.context()).reason, "invalid_request")

    def test_oversize_request_never_reaches_the_helper(self):
        ctx = self.context(BIG="x" * (launch_probe.MAX_REQUEST + 10))
        obs = launch_probe.observe(self.helper(), ctx)
        self.assertEqual(obs.reason, "invalid_request")
        self.assertFalse((self.root / "request.json").exists())

    def test_timeout_is_bounded_and_the_group_is_cleaned(self):
        started = time.monotonic()
        obs = launch_probe.observe(self.helper("hang"), self.context(), deadline=1.5)
        self.assertLess(time.monotonic() - started, 4)
        self.assertEqual((obs.status, obs.reason), ("account_unknown", "timeout"))

    def test_surviving_descendant_is_a_cleanup_failure_or_killed(self):
        obs = launch_probe.observe(self.helper("descendant"), self.context())
        pid = int((self.root / "child.pid").read_text())
        time.sleep(0.2)
        try:
            os.kill(pid, 0)
            alive = True
        except ProcessLookupError:
            alive = False
        self.assertFalse(alive)                    # the whole group was killed
        self.assertTrue(obs.status == "account_unknown" or obs.cleanup_ok)

    def test_helper_group_that_cannot_be_reaped_is_a_cleanup_failure_never_available(self):
        from unittest import mock
        with mock.patch.object(launch_probe, "_kill_group", return_value=False):
            obs = launch_probe.observe(self.helper("ok"), self.context())
        self.assertEqual((obs.status, obs.reason), ("account_unknown", "cleanup_failed"))
        self.assertFalse(obs.available)
        self.assertEqual((obs.account_ref, obs.quota_bucket), ("", ""))


class SupervisorTests(Base):
    """spawn_gate: the context the helper sees is the one Popen receives."""

    NATIVE = r'''
import json, os, sys
open(os.path.join(os.environ["ROOT"], "native.json"), "w").write(json.dumps(
    {"argv": sys.argv, "env": dict(os.environ), "cwd": os.getcwd()}))
'''

    def run_gate(self, mode, expect="", probe=True, version=1, big=0):
        native = self.root / "native.py"
        native.write_text(self.NATIVE)
        directory = self.root / "gate"
        directory.mkdir(exist_ok=True)
        prompt, token = "complete input", "tok"
        digest = hashlib.sha256(prompt.encode()).hexdigest()
        interactive.write_delivery(directory, "k", {"token": token, "sha256": digest, "phase": "reserved"})
        env = {"PATH": os.defpath, "ROOT": str(self.root), "CODEX_HOME": str(self.root / "ch"),
               "HOME": str(self.root), "_MESH_RUNNER_PROMPT_INTENT": token,
               "_MESH_RUNNER_PROMPT_SHA256": digest}
        for i in range(0, big, 50000):          # Linux caps one env string at 128 KiB: spread the bulk
            env[f"BIG{i}"] = "x" * min(50000, big - i)
        if probe:
            env["_MESH_RUNNER_LAUNCH_PROBE"] = json.dumps({"argv": self.helper(mode), "version": version})
        if expect:
            env["_MESH_RUNNER_LAUNCH_EXPECT"] = expect
        read_fd, write_fd = os.pipe()
        out = open(self.root / "gate.out", "wb")
        err = open(self.root / "gate.err", "wb")
        try:
            result = subprocess.run([sys.executable, str(Path(spawn_gate.__file__)),
                str(interactive.delivery_path(directory, "k")), token, digest, str(write_fd), "--",
                sys.executable, "-I", str(native), "--profile", "p1"], input=prompt.encode(),
                pass_fds=(write_fd,), stdout=out, stderr=err, timeout=30, env=env, cwd=str(self.root))
        finally:
            os.close(write_fd)
            out.close()
            err.close()
        result.stdout, result.stderr = (self.root / 'gate.out').read_bytes(), (self.root / 'gate.err').read_bytes()
        status = os.read(read_fd, 4096)
        os.close(read_fd)
        return result, status, interactive.read_delivery(directory, "k")

    def test_helper_context_equals_native_popen_snapshot(self):
        result, status, journal = self.run_gate("ok")
        self.assertEqual(result.returncode, 0, result.stderr)
        native = json.loads((self.root / "native.json").read_text())
        seen = json.loads((self.root / "request.json").read_text())["context"]
        self.assertEqual({k: native["env"].get(k) for k in seen["env"]}, seen["env"])   # same env into Popen
        self.assertEqual(seen["argv"][2:], native["argv"][:1] + ["--profile", "p1"][0:0] + native["argv"][1:])
        self.assertEqual(seen["argv"][-2:], ["--profile", "p1"])
        self.assertEqual(seen["cwd"], native["cwd"])
        self.assertEqual(seen["profile"], "p1")
        self.assertEqual(seen["codex_home"], str(self.root / "ch"))
        for name in ("_MESH_RUNNER_PROMPT_INTENT", "_MESH_RUNNER_PROMPT_SHA256",
                     "_MESH_RUNNER_LAUNCH_PROBE", "_MESH_RUNNER_LAUNCH_EXPECT"):
            self.assertNotIn(name, seen["env"])                       # transport removed first
        self.assertEqual(journal["phase"], "native-exited")
        self.assertEqual(journal["account_ref"], "ab" * 32)
        self.assertEqual(json.loads(status), {"exec_confirmed": True})

    def test_denials_never_start_native(self):
        for mode, expect in (("blocked", ""), ("unknown", ""), ("nonce", ""), ("dirty", ""), ("dup", ""),
                             ("oversize", ""), ("eof-early", ""),
                             ("hang", ""), ("exit1", ""), ("garbage", ""), ("foreign", "ab" * 32)):
            with self.subTest(mode=mode):
                marker = self.root / "native.json"
                if marker.exists():
                    marker.unlink()
                result, status, journal = self.run_gate(mode, expect)
                self.assertEqual(result.returncode, 1)
                self.assertFalse(marker.exists())
                self.assertIn("launch_denied", json.loads(status))
                self.assertEqual(journal["phase"], "launch-probe-denied")
                self.assertNotIn("s3cret", result.stdout.decode() + result.stderr.decode() + json.dumps(journal))

    def test_v2_supervisor_records_measurements_in_the_journal(self):
        result, status, journal = self.run_gate("ok", version=2)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads((self.root / "request.json").read_text())["version"], 2)
        self.assertEqual((journal["primary_used_percent"], journal["secondary_used_percent"]), (37, None))

    def test_oversize_request_is_denied_by_the_supervisor_without_a_native_child(self):
        result, status, journal = self.run_gate("ok", big=launch_probe.MAX_REQUEST + 10)
        self.assertEqual(result.returncode, 1)
        self.assertFalse((self.root / "native.json").exists())
        self.assertFalse((self.root / "request.json").exists())
        self.assertEqual(journal["phase"], "launch-probe-denied")

    def test_without_a_probe_the_gate_is_unchanged(self):
        result, status, journal = self.run_gate("ok", probe=False)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(journal["phase"], "native-exited")
        self.assertFalse((self.root / "request.json").exists())


if __name__ == "__main__":
    unittest.main()
