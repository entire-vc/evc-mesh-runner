"""The release archive carries the reviewer merge-only files; versions agree; new files stay public-safe."""
import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

REQUIRED = [
    "src/mesh_runner/reviewer_merge.py",
    "src/mesh_runner/cli.py",
    "src/mesh_runner/config.py",
    "src/mesh_runner/runner.py",
    "src/mesh_runner/extension_service.py",
    "scripts/select-reviewer.py",
    "scripts/reviewer-canary.py",
    "docs/reviewer-merge.md",
    "docs/release-package.md",
]
PRIVATE = re.compile(r"#[0-9a-f]{8}\b|Mesh Doc [0-9a-f]{8}|/Users/|/Volumes/|~/ClaudeCowork|~/\.fiddler|\bbob's\b")


class ReleasePackage(unittest.TestCase):
    def test_versions_agree(self):
        import mesh_runner
        py = re.search(r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(), re.M).group(1)
        ext = json.loads((ROOT / "mesh-extension.json").read_text())["version"]
        self.assertEqual((py, ext), (mesh_runner.__version__, mesh_runner.__version__))

    def test_required_files_are_in_the_archive(self):
        res = subprocess.run(["git", "-C", str(ROOT), "ls-files", "--", *REQUIRED], capture_output=True, text=True)
        if res.returncode != 0:
            self.skipTest("not a git checkout")
        self.assertEqual(sorted(res.stdout.split()), sorted(REQUIRED))

    def test_reviewer_files_are_public_safe(self):
        for rel in REQUIRED:
            for n, line in enumerate((ROOT / rel).read_text().splitlines(), 1):
                self.assertIsNone(PRIVATE.search(line), f"{rel}:{n}: {line[:80]}")

    def test_check_detects_a_private_line(self):                       # red control: the pattern fires
        for bad in ("see #0123abcd", "~/ClaudeCowork/bob", "/Users/x/y"):
            self.assertIsNotNone(PRIVATE.search(bad))


class SelectReviewerFailsClosed(unittest.TestCase):
    """No router or state location -> `unknown: inputs`, lane null, exit 1; flags and env are read."""

    def run_adapter(self, *args, **env):
        base = {k: v for k, v in os.environ.items() if not k.startswith("MESH_ROUTER_")}
        res = subprocess.run([sys.executable, str(ROOT / "scripts" / "select-reviewer.py"), *args],
                             capture_output=True, text=True, env={**base, **env})
        return res.returncode, json.loads(res.stdout)

    def test_missing_locations_refuse(self):
        rc, out = self.run_adapter("proj", "lane")
        self.assertEqual((rc, out["lane"]), (1, None))
        self.assertTrue(out["reason"].startswith("unknown: inputs"))

    def test_only_one_location_refuses(self):
        rc, out = self.run_adapter("proj", "lane", "--router-home", "/nonexistent")
        self.assertEqual((rc, out["lane"]), (1, None))
        rc, out = self.run_adapter("proj", "lane", MESH_ROUTER_STATE_DIR="/nonexistent")
        self.assertEqual((rc, out["lane"]), (1, None))

    def test_flags_and_env_reach_the_router_import(self):             # positive control: both set -> import fails, not the guard
        for args, env in ((("--router-home", "/nonexistent", "--state-dir=/nonexistent"), {}),
                          ((), {"MESH_ROUTER_HOME": "/nonexistent", "MESH_ROUTER_STATE_DIR": "/nonexistent"})):
            rc, out = self.run_adapter("proj", "lane", *args, **env)
            self.assertEqual((rc, out["reason"]), (1, "unknown: inputs (ModuleNotFoundError)"))

    def test_usage(self):
        rc, out = self.run_adapter("only-one")
        self.assertEqual((rc, out["reason"]), (2, "unknown: usage"))


if __name__ == "__main__":
    unittest.main()
