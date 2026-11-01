"""status.json receipt: digest of the cfg the Runner holds in memory, no secrets, no false success."""
import dataclasses
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mesh_runner import config_receipt as cr
from mesh_runner.config import AgentConfig, RunnerConfig
from mesh_runner.runner import Runner

SECRET = "sekrit-VALUE-123"


def make_cfg(root, **kw):
    agent = AgentConfig(name="A", key=SECRET, harness="claude", profile_dir=root / "p", repos=[],
                        env={"TOK": SECRET}, inline_env={"TOK": SECRET})
    return RunnerConfig("https://mesh.invalid", root / "state", [agent], gitlab_token=SECRET,
                        mesh_basic_auth=f"u:{SECRET}", **kw)


def status(runner):
    runner.write_status([])
    return json.loads((runner.cfg.state_dir / "status.json").read_text())


class ReceiptTest(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.root = Path(self._t.name)

    def tearDown(self):
        self._t.cleanup()

    def test_receipt_in_snapshot(self):
        r = Runner(make_cfg(self.root), lock=False)
        st = status(r)
        rc = st["receipt"]
        self.assertEqual(rc["pid"], os.getpid())
        self.assertEqual(rc["status_unix"], st["unix"])
        self.assertEqual(rc["cfg_sha256"], cr.digest(r.cfg))
        self.assertEqual(len(rc["cfg_sha256"]), 64)
        self.assertTrue(rc["release"] and rc["source"])

    def test_no_secret_in_status_or_serialization(self):
        r = Runner(make_cfg(self.root), lock=False)
        self.assertNotIn(SECRET, json.dumps(status(r)))
        self.assertNotIn(SECRET, cr.serialize(r.cfg))

    def test_secret_value_change_does_not_move_digest_but_defaults_and_overrides_do(self):
        a = make_cfg(self.root)
        b = make_cfg(self.root)
        b.gitlab_token = "other"
        self.assertEqual(cr.digest(a), cr.digest(b))           # secrets are outside the scope
        c = dataclasses.replace(a, poll_seconds=a.poll_seconds + 1)   # a resolved default changed
        self.assertNotEqual(cr.digest(a), cr.digest(c))
        d = make_cfg(self.root)
        d.agents[0].paused = True
        self.assertNotEqual(cr.digest(a), cr.digest(d))

    def test_disk_edit_after_start_keeps_actual_cfg_receipt(self):
        f = self.root / "runner.toml"
        f.write_text("poll_seconds = 20\n")
        r = Runner(make_cfg(self.root), lock=False)
        before = status(r)["receipt"]["cfg_sha256"]
        f.write_text("poll_seconds = 99\n")
        self.assertEqual(status(r)["receipt"]["cfg_sha256"], before)
        r.cfg.poll_seconds = 99                                # supported swap of the held cfg
        self.assertNotEqual(status(r)["receipt"]["cfg_sha256"], before)

    def test_failure_is_not_fresh_success(self):
        r = Runner(make_cfg(self.root), lock=False)
        good = status(r)["receipt"]["cfg_sha256"]
        with mock.patch.object(cr, "digest", side_effect=RuntimeError("boom")):
            rc = status(r)["receipt"]
        self.assertNotIn("cfg_sha256", rc)
        self.assertEqual(rc["error"], "RuntimeError")
        self.assertTrue(good)


if __name__ == "__main__":
    unittest.main()
