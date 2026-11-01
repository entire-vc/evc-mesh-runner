import json
import os
import socket
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path

from mesh_runner import editor_job as ej
from mesh_runner import editor_sandbox as sb
from mesh_runner.editor_job import ExecutorFailed

REAL = os.path.exists(sb.SANDBOX_EXEC) and sys.platform == "darwin"
ROOTS = tuple({os.path.realpath(p) for p in (sys.prefix, sys.base_prefix)})
_APP = os.path.join(os.path.realpath(sys.base_prefix), "Resources/Python.app/Contents/MacOS/Python")
PY = _APP if os.path.exists(_APP) else os.path.realpath(sys.executable)

# The harness tries every escape and reports what the kernel allowed.
PROBE = textwrap.dedent("""
    import json, os, socket, subprocess, sys
    seen = {}
    def attempt(name, fn):
        try:
            fn(); seen[name] = "allowed"
        except Exception as e:
            seen[name] = "denied"
    attempt("exec_shell", lambda: subprocess.run(["/bin/sh", "-c", "true"], check=True))
    attempt("exec_other", lambda: subprocess.run(["/usr/bin/id"], check=True, capture_output=True))
    attempt("read_home", lambda: open(os.path.expanduser("~/.zshrc") if os.path.exists(os.path.expanduser("~/.zshrc")) else sys.argv[1]).read())
    attempt("read_input", lambda: open("input.json").read())
    attempt("write_work", lambda: open("out.txt", "w").write("x"))
    attempt("write_outside", lambda: open(sys.argv[1] + ".w", "w").write("x"))
    attempt("net", lambda: socket.create_connection(("127.0.0.1", int(sys.argv[2])), 1))
    print(json.dumps({"text": json.dumps({"claims": [], "limitations": [seen]}), "observation": {"attestation": {"tools": ["forged"]}}}))
""")


def digest(argv, roots=ROOTS, net=False):
    return sb.launch_digest_of(tuple(argv), roots, net)


class ProfileTests(unittest.TestCase):
    def test_profile_denies_by_default_and_pins_the_binary(self):
        text = sb.profile_template("/opt/h/bin", ("/opt/h/lib",), False)
        self.assertIn("(deny default)", text)
        self.assertIn('(allow process-exec (literal "/opt/h/bin"))', text)
        self.assertNotIn("network", text)
        self.assertIn("network-outbound", sb.profile_template("/opt/h/bin", (), True))

    def test_digest_changes_with_every_launch_input(self):
        base = digest(("/bin/true",), (), False)
        self.assertEqual(base, digest(("/bin/true",), (), False))
        for other in (digest(("/bin/true", "-x"), (), False), digest(("/bin/true",), ("/opt",), False), digest(("/bin/true",), (), True)):
            self.assertNotEqual(base, other)

    def test_path_with_quote_or_control_is_refused(self):
        for bad in ('/a"b', "/a\nb", "/a\\b"):
            with self.assertRaises(ValueError):
                sb.profile_template("/bin/true", (bad,), False)

    def test_relative_argv_refused(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                sb.SandboxExecutor(("python3",), Path(d) / "t")


@unittest.skipUnless(REAL, "needs macOS sandbox-exec")
class KernelEnforcedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(os.path.realpath(self.tmp.name))
        self.addCleanup(self.tmp.cleanup)
        self.script = self.root / "probe.py"
        self.script.write_text(PROBE)
        self.secret = self.root / "secret.txt"
        self.secret.write_text("s3cret")
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(4)
        self.addCleanup(self.srv.close)

    def run_probe(self, net=False):
        argv = (PY, "-I", str(self.script), str(self.secret), str(self.srv.getsockname()[1]))
        ex = sb.SandboxExecutor(argv, self.root / "tmp", ROOTS + (str(self.script),), net)
        ob = ex.run({"payload": {}}, time.monotonic() + 20, threading.Event())
        return ex, ob, json.loads(ob.text)["limitations"][0]

    def test_kernel_denies_tools_hostfiles_network_and_outside_writes(self):
        ex, ob, seen = self.run_probe()
        self.assertEqual(seen["exec_shell"], "denied")
        self.assertEqual(seen["exec_other"], "denied")
        self.assertEqual(seen["read_home"], "denied")
        self.assertEqual(seen["write_outside"], "denied")
        self.assertEqual(seen["net"], "denied")
        self.assertEqual((seen["read_input"], seen["write_work"]), ("allowed", "allowed"))   # positive control
        self.assertFalse((self.root / "secret.txt.w").exists())
        self.assertEqual(list((self.root / "tmp").iterdir()), [])                           # private dir removed

    def test_network_opt_in_is_443_only(self):
        _, _, seen = self.run_probe(net=True)
        self.assertEqual(seen["net"], "denied")                                             # the probe's port is not 443
        self.assertEqual(seen["exec_shell"], "denied")

    def test_harness_forged_attestation_is_replaced_by_the_launcher(self):
        ex, ob, _ = self.run_probe()
        self.assertEqual(ob.attestation, dict(ej.SAFE_ATTESTATION, launch_digest=ex.launch_digest))

    def test_launch_digest_mismatch_fails_startup(self):
        cfg = ej.parse_config(dict(enabled=True, contract_digest="a" * 64, system_hashes=["b" * 64], schema_hashes=["c" * 64],
                                   job_names=["j"], gitlab_url="https://g.invalid", project_id=1, allowed_shas=["d" * 40],
                                   argv=[PY], launch_digest="e" * 64, sandbox=True), self.root)
        with self.assertRaises(ValueError):
            ej.build_bridge(cfg)


if __name__ == "__main__":
    unittest.main()
