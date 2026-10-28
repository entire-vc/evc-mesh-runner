import io
import json
import tempfile
import textwrap
import unittest
import urllib.error
from pathlib import Path

from mesh_runner import RUNNER_NAME, __version__
from mesh_runner.catalog import (ControllerClient, PoolObserver, WorkspaceController, iso, load_thresholds,
                                 read_applied, read_json)
from mesh_runner.config import ConfigError, load

WS = "11111111-2222-3333-4444-555555555555"
NOW = 1_800_000_000.0
D1, D2 = "a" * 64, "b" * 64
CRED = "cred:claude-accounts/alpha"
# domain.RuntimeReport in evc-mesh before it gained version. The controller is named by the URL path.
REPORT_FIELDS = {"schema_version", "revision", "digest", "status", "capabilities", "emergency_paused", "pools",
                 "profiles"}

MANIFEST = {
    "schema_version": 1, "workspace_id": WS, "profile_controller": RUNNER_NAME,
    "controllers": {RUNNER_NAME: {"capabilities": ["claude_code"]}},
    "defaults": {"evidence_max_age_seconds": 1800},
    "accounts": {"claude/alpha": {
        "credential_ref": CRED, "harness": "claude_code", "model": "claude-default",
        "model_developer": "anthropic", "model_family": "claude", "capabilities": ["claude_code"],
        "modes": {"subscription": {"pools": ["claude/alpha/5h", "claude/alpha/7d"]}},
        "pools": {"claude/alpha/5h": {}, "claude/alpha/7d": {}}}},
}


def desired(revision=3, digest=D1, harness="claude_code", credential=CRED, capabilities=("claude_code",),
            pools=("claude/alpha/5h", "claude/alpha/7d")):
    pools = {ref: {"provider": "anthropic", "resource_ref": ref.replace("/", "."), "aliases": []} for ref in pools}
    return {
        "revision": revision, "digest": digest, "controller_ref": RUNNER_NAME, "enabled": True,
        "drain_requested": False, "controller": {"capabilities": list(capabilities), "version": __version__},
        "catalog": {
            "accounts": {"claude/alpha": {"provider": "anthropic", "credential_ref": credential,
                                          "quota_pools_by_mode": {"subscription": list(pools)}}},
            "pools": pools,
            "profiles": {"claude/alpha/subscription": {
                "controller_ref": RUNNER_NAME, "account_ref": "claude/alpha", "execution_mode": "subscription",
                "harness": harness, "capabilities": list(capabilities)}},
            "bindings": {},
        },
    }


class _Resp(io.BytesIO):
    def __init__(self, status, body, etag=None):
        super().__init__(json.dumps(body).encode() if body is not None else b"")
        self.status = status
        self.headers = {"ETag": etag} if etag else {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeMesh:
    """The two controller endpoints of the Mesh runtime API."""

    fields = REPORT_FIELDS

    def __init__(self, body):
        self.body = body
        self.reports = []
        self.gets = []

    def etag(self):
        return '"' + ("%016x" % (hash(json.dumps(self.body, sort_keys=True)) & (2 ** 64 - 1))) + '"'

    def open(self, req, timeout=None):
        assert req.get_header("X-agent-key") == "reporter-key"
        if req.get_method() == "GET":
            assert req.full_url.endswith(f"/api/v1/workspaces/{WS}/runtime/controllers/{RUNNER_NAME}/desired")
            sent = req.get_header("If-none-match")
            self.gets.append(sent)
            if sent == self.etag():
                return _Resp(304, None)
            return _Resp(200, self.body, self.etag())
        assert req.full_url.endswith(f"/runtime/controllers/{RUNNER_NAME}/report")
        report = json.loads(req.data)
        # the server decodes the report with unknown fields disallowed: any other key is a 400
        if set(report) - self.fields:
            return _Resp(400, {"error": "invalid runtime request"})
        self.reports.append(report)
        return _Resp(200, {"recorded": True, "launch_authorized": False})


class CatalogPassTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "accounts.json").write_text(json.dumps(MANIFEST))

    def tearDown(self):
        self.tmp.cleanup()

    def controller(self, mesh, observer=None):
        client = ControllerClient("https://mesh.example.com", WS, "reporter-key", opener=mesh, sleep=lambda _s: None)
        return WorkspaceController(WS, client, self.dir / "accounts.json", self.dir / "home", self.dir / "catalog",
                                   observer or PoolObserver())

    def test_report_revision_equals_desired_revision(self):
        mesh = FakeMesh(desired(revision=7, digest=D2))
        st = self.controller(mesh).tick(NOW)
        self.assertEqual(st["status"], "ok")
        self.assertEqual(st["controller_ref"], "mesh-runner")
        self.assertEqual(st["version"], __version__)
        self.assertEqual(len(mesh.reports), 1)
        rep = mesh.reports[0]
        self.assertEqual((rep["revision"], rep["digest"], rep["status"]), (7, D2, "applied"))
        self.assertEqual(rep["schema_version"], 2)
        self.assertLessEqual(set(rep), REPORT_FIELDS)
        self.assertEqual(read_applied(self.dir / "catalog"), {"revision": 7, "digest": D2})
        self.assertEqual(sorted(rep["profiles"]), ["claude/alpha/subscription"])
        self.assertEqual(sorted(rep["pools"]), ["claude/alpha/5h", "claude/alpha/7d"])

    def test_malformed_digest_is_an_error_and_nothing_is_applied(self):
        mesh = FakeMesh(desired(revision=7, digest="not-a-digest"))
        st = self.controller(mesh).tick(NOW)
        self.assertEqual(st["status"], "error")
        self.assertEqual(st["desired_errors"], ["digest"])
        self.assertIsNone(read_applied(self.dir / "catalog"))
        self.assertEqual(mesh.reports, [])

    def test_malformed_digest_keeps_the_previous_applied_revision(self):
        mesh = FakeMesh(desired(revision=3, digest=D1))
        ctl = self.controller(mesh)
        ctl.tick(NOW)
        mesh.body = desired(revision=4, digest="Z" * 64)
        st = ctl.tick(NOW + 120)
        self.assertEqual(st["status"], "error")
        self.assertEqual(read_applied(self.dir / "catalog"), {"revision": 3, "digest": D1})
        self.assertEqual([r["revision"] for r in mesh.reports], [3, 3])

    def test_unexecutable_slice_is_reported_rejected_for_its_revision(self):
        mesh = FakeMesh(desired(revision=5, digest=D2, harness="codex"))
        st = self.controller(mesh).tick(NOW)
        self.assertEqual(st["status"], "error")
        self.assertEqual(st["outcome"], "desired_rejected")
        self.assertEqual([(r["revision"], r["digest"], r["status"]) for r in mesh.reports], [(5, D2, "rejected")])
        self.assertIsNone(read_applied(self.dir / "catalog"))

    def test_foreign_credential_is_rejected(self):
        mesh = FakeMesh(desired(credential="cred:claude-accounts/other"))
        st = self.controller(mesh).tick(NOW)
        self.assertIn("account", st["desired_errors"])
        self.assertEqual(mesh.reports[0]["status"], "rejected")

    def test_capability_the_host_does_not_offer_is_rejected(self):
        mesh = FakeMesh(desired(capabilities=("claude_code", "browser")))
        st = self.controller(mesh).tick(NOW)
        self.assertEqual(st["desired_errors"], ["capability"])
        self.assertEqual(mesh.reports[0]["status"], "rejected")

    def test_pool_the_manifest_does_not_give_the_mode_is_rejected(self):
        mesh = FakeMesh(desired(pools=("claude/alpha/5h", "claude/alpha/30d")))
        st = self.controller(mesh).tick(NOW)
        self.assertEqual(st["desired_errors"], ["pools"])
        self.assertIsNone(read_applied(self.dir / "catalog"))

    def test_not_modified_reports_the_applied_revision_again(self):
        mesh = FakeMesh(desired(revision=3))
        ctl = self.controller(mesh)
        ctl.tick(NOW)
        st = ctl.tick(NOW + 120)
        self.assertEqual(st["desired_outcome"], "not_modified")
        self.assertEqual(mesh.gets[1], mesh.etag())
        self.assertEqual([r["revision"] for r in mesh.reports], [3, 3])

    def test_older_desired_never_walks_applied_back(self):
        mesh = FakeMesh(desired(revision=9, digest=D2))
        ctl = self.controller(mesh)
        ctl.tick(NOW)
        mesh.body = desired(revision=8, digest=D1)
        st = ctl.tick(NOW + 120)
        self.assertEqual(st["desired_outcome"], "stale")
        self.assertEqual(read_applied(self.dir / "catalog"), {"revision": 9, "digest": D2})

    def test_version_mismatch_is_recorded(self):
        body = desired()
        body["controller"]["version"] = "9.9.9"
        st = self.controller(FakeMesh(body)).tick(NOW)
        self.assertEqual(st["version_mismatch"], {"catalog": "9.9.9", "host": __version__})

    def test_report_carries_the_host_version(self):
        class Current(FakeMesh):
            fields = REPORT_FIELDS | {"version"}
        mesh = Current(desired())
        st = self.controller(mesh).tick(NOW)
        self.assertTrue(st["recorded"])
        self.assertEqual([r["version"] for r in mesh.reports], [__version__])

    def test_server_without_version_field_still_records_the_report(self):
        mesh = FakeMesh(desired())
        st = self.controller(mesh).tick(NOW)
        self.assertTrue(st["recorded"])
        self.assertEqual(len(mesh.reports), 1)
        self.assertNotIn("version", mesh.reports[0])

    def test_forbidden_report_is_an_error(self):
        class Denied(FakeMesh):
            def open(self, req, timeout=None):
                if req.get_method() == "POST":
                    raise urllib.error.HTTPError(req.full_url, 403, "forbidden", {}, io.BytesIO(b"{}"))
                return super().open(req, timeout)
        st = self.controller(Denied(desired())).tick(NOW)
        self.assertEqual((st["status"], st["outcome"], st["recorded"]), ("error", "forbidden", False))

    def test_state_files_are_private(self):
        self.controller(FakeMesh(desired())).tick(NOW)
        for name in ("home/state.json", "home/pools.json", "catalog/applied.json", "catalog/desired.json"):
            self.assertEqual((self.dir / name).stat().st_mode & 0o077, 0, name)


class PoolObserverTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, schema, records):
        path = self.dir / name
        path.write_text(json.dumps({"schema": schema, "records": records}))
        return path

    def quota(self, pool, frac, status="available", age=60):
        return {"catalog_pool": pool, "pool_id": pool, "status": status, "used_fraction": frac,
                "observed_at": iso(NOW - age), "max_age": 1800, "source": "claude_ratelimit_headers", "window": "5h"}

    def test_no_data_is_unknown_not_available(self):
        obs = PoolObserver().observe({"p/5h"}, NOW)
        self.assertEqual((obs["p/5h"]["state"], obs["p/5h"]["verified"]), ("unknown", False))

    def test_threshold_carries_its_threshold_ref(self):
        q = self.write("q.json", "quota-observations/v1", [self.quota("p/5h", 0.92)])
        o = PoolObserver(observations=q, thresholds=load_thresholds(None))
        local = o.observe({"p/5h"}, NOW)
        self.assertEqual(local["p/5h"]["state"], "threshold")
        rep = o.for_report(local)["p/5h"]
        self.assertEqual(rep, {"state": "threshold", "verified": True, "observed_at": iso(NOW - 60),
                               "threshold_ref": "thr-default-v1", "evidence_kind": "configured_threshold"})

    def test_transfer_is_reported_as_threshold(self):
        q = self.write("q.json", "quota-observations/v1", [self.quota("p/5h", 0.97)])
        o = PoolObserver(observations=q)
        local = o.observe({"p/5h"}, NOW)
        self.assertEqual(local["p/5h"]["state"], "transfer")
        self.assertEqual(o.for_report(local)["p/5h"]["state"], "threshold")

    def test_stale_reading_is_unknown(self):
        q = self.write("q.json", "quota-observations/v1", [self.quota("p/5h", 0.1, age=4000)])
        self.assertEqual(PoolObserver(observations=q).observe({"p/5h"}, NOW)["p/5h"]["state"], "unknown")

    def test_hysteresis_holds_threshold_after_a_block(self):
        q = self.write("q.json", "quota-observations/v1", [self.quota("p/5h", 0.5)])
        prior = {"p/5h": {"state": "threshold", "blocked_since": NOW - 100}}
        self.assertEqual(PoolObserver(observations=q).observe({"p/5h"}, NOW, prior)["p/5h"]["state"], "threshold")
        prior = {"p/5h": {"state": "threshold", "blocked_since": NOW - 1000}}
        self.assertEqual(PoolObserver(observations=q).observe({"p/5h"}, NOW, prior)["p/5h"]["state"], "available")

    def test_harness_limit_exhausts_the_pool(self):
        q = self.write("q.json", "quota-observations/v1", [self.quota("p/7d", 0.1)])
        h = self.write("h.json", "harness-limits/v1", [{
            "account_ref": "a", "pools": ["p/7d"], "observed_at": iso(NOW - 60), "expires_at": iso(NOW + 3600)}])
        local = PoolObserver(observations=q, harness_limits=h).observe({"p/7d"}, NOW)
        self.assertEqual((local["p/7d"]["state"], local["p/7d"]["source"]), ("exhausted", "harness_limit"))

    def test_expired_harness_limit_no_longer_counts(self):
        q = self.write("q.json", "quota-observations/v1", [self.quota("p/7d", 0.1)])
        h = self.write("h.json", "harness-limits/v1", [{
            "account_ref": "a", "pools": ["p/7d"], "observed_at": iso(NOW - 7200), "expires_at": iso(NOW - 60)}])
        self.assertEqual(PoolObserver(observations=q, harness_limits=h).observe({"p/7d"}, NOW)["p/7d"]["state"],
                         "available")


class CatalogConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        key = self.dir / "reporter.key"
        key.write_text("reporter-key\n")
        key.chmod(0o600)
        self.key = key

    def tearDown(self):
        self.tmp.cleanup()

    def cfg(self, extra=""):
        path = self.dir / "runner.toml"
        path.write_text(textwrap.dedent(f"""
            [runner]
            mesh_url = "https://mesh.example.com"
            state_dir = "{self.dir}/state"
        """) + textwrap.dedent(extra))
        return load(path)

    def test_off_by_default(self):
        self.assertFalse(self.cfg().catalog.enabled)

    def test_enabled_reads_workspaces(self):
        c = self.cfg(f"""
            [catalog]
            enabled = true
            manifest = "{self.dir}/accounts.json"
            [[catalog.workspace]]
            id = "{WS}"
            reporter_key_file = "{self.key}"
        """).catalog
        self.assertEqual(c.controller_ref, "mesh-runner")
        self.assertEqual(len(c.workspaces), 1)
        ws = c.workspaces[0]
        self.assertEqual(ws.reporter_key, "reporter-key")
        self.assertEqual(ws.home, self.dir / "state" / "catalog" / WS)
        self.assertNotIn("reporter-key", repr(c))

    def test_controller_ref_cannot_differ_from_the_runner_name(self):
        with self.assertRaises(ConfigError):
            self.cfg(f"""
                [catalog]
                enabled = true
                controller_ref = "other-runner"
                manifest = "{self.dir}/accounts.json"
                [[catalog.workspace]]
                id = "{WS}"
                reporter_key_file = "{self.key}"
            """)

    def test_enabled_without_workspace_is_refused(self):
        with self.assertRaises(ConfigError):
            self.cfg(f"""
                [catalog]
                enabled = true
                manifest = "{self.dir}/accounts.json"
            """)

    def test_state_json_names_the_runner(self):
        self.assertEqual(RUNNER_NAME, "mesh-runner")
        self.assertIsNone(read_json(self.dir / "absent.json"))


class DigestCheckRedRunTest(unittest.TestCase):
    """The digest tests must go red when the controller stops checking the digest; otherwise they
    would stay green whatever the controller does."""

    CHECK = '    if not hex_digest(digest):\n        errors.append("digest")\n'
    TESTS = ("test_malformed_digest_is_an_error_and_nothing_is_applied",
             "test_malformed_digest_keeps_the_previous_applied_revision")

    def test_digest_tests_fail_without_the_digest_check(self):
        import sys
        import types
        from unittest import mock

        import mesh_runner.catalog as catalog
        source = Path(catalog.__file__).read_text()
        self.assertEqual(source.count(self.CHECK), 1, "digest check moved; update the mutant")
        mutant = types.ModuleType("mesh_runner.catalog_mutant")
        # dataclasses resolve their module through sys.modules while the source executes
        with mock.patch.dict(sys.modules, {mutant.__name__: mutant}):
            exec(compile(source.replace(self.CHECK, ""), catalog.__file__, "exec"), mutant.__dict__)
        with mock.patch.object(catalog, "validate_desired", mutant.validate_desired):
            result = unittest.TestResult()
            unittest.TestSuite(CatalogPassTest(name) for name in self.TESTS).run(result)
        self.assertEqual(result.testsRun, 2)
        self.assertEqual(len(result.failures) + len(result.errors), 2,
                         [str(t) for t, _ in result.failures + result.errors])


if __name__ == "__main__":
    unittest.main()
