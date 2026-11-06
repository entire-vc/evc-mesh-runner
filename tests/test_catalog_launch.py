"""Native catalog reader <- actual-launch cache + independent binding (never a grant)."""
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from mesh_runner import RUNNER_NAME, catalog_launch as cl
from mesh_runner.catalog import ControllerClient, PoolObserver, WorkspaceController, iso, read_json
from mesh_runner.config import load
from test_catalog import FakeMesh, NOW, WS, D1, desired as claude_desired

REF = "a1" * 32
POOL = "codex/example/unknown"
ACCOUNT = {"provider": "openai", "credential_ref": "cred:codex-accounts/example", "harness": "codex",
           "model": "codex-default", "model_developer": "openai", "model_family": "gpt",
           "capabilities": ["codex"], "enabled": True,
           "modes": {"subscription": {"pools": [POOL]}}, "pools": {POOL: {"window": "unknown"}}}
OTHER = {"credential_ref": "cred:codex-accounts/other", "harness": "codex", "model": "codex-default",
         "model_developer": "openai", "model_family": "gpt", "capabilities": ["codex"], "provider": "openai",
         "enabled": True, "modes": {"subscription": {"pools": ["codex/other/unknown"]}},
         "pools": {"codex/other/unknown": {"window": "unknown"}}}


def manifest(*, account=ACCOUNT):
    return {"schema_version": 1, "workspace_id": WS, "profile_controller": RUNNER_NAME,
            "controllers": {RUNNER_NAME: {"capabilities": ["codex"]}},
            "defaults": {"evidence_max_age_seconds": 1800},
            "accounts": {"codex/example": account, "codex/other": OTHER}}


def snap(status="available", primary=10, secondary=20, at=None, ref=REF, cleanup=True):
    return {"status": status, "reason": status, "account_ref": ref, "quota_bucket": f"openai:{ref}:codex",
            "observed_at": int(NOW - 30) if at is None else at, "cleanup_ok": cleanup,
            "primary_used_percent": primary, "secondary_used_percent": secondary}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.bind = self.dir / "bindings.json"
        self.cache = self.dir / "cache.json"
        self.m = manifest()

    def tearDown(self):
        self.tmp.cleanup()

    def private(self, path, doc):
        path.write_text(json.dumps(doc))
        path.chmod(0o600)

    def enroll(self, accounts=None):
        self.private(self.bind, {"version": 1, "accounts": accounts if accounts is not None else {"codex/example": REF}})

    def cached(self, snapshot, *, account=None, ref="codex/example", extra=None):
        entry = {"binding": cl.binding_digest(self.m, ref, REF), "observation": snapshot, "fingerprint": "f" * 64}
        self.private(self.cache, {"version": 1, "accounts": {ref: entry, **(extra or {})}})

    def observer(self):
        return PoolObserver(launch_bindings=self.bind, launch_cache=self.cache)

    def state(self, pool=POOL):
        return self.observer().observe({pool}, NOW, None, self.m)[pool]


class ReaderTest(Fixture):
    def test_fresh_available_is_available_with_worst_slot(self):
        self.enroll(); self.cached(snap(primary=10, secondary=20))
        st = self.state()
        self.assertEqual((st["state"], st["verified"], st["used_fraction"]), ("available", True, 0.2))

    def test_thresholds_are_the_native_ones(self):
        self.enroll(); self.cached(snap(primary=10, secondary=91))
        self.assertEqual(self.state()["state"], "threshold")
        self.cached(snap(primary=10, secondary=96))
        self.assertEqual(self.state()["state"], "transfer")

    def test_blocked_and_hundred_are_exhausted(self):
        self.enroll(); self.cached(snap("quota_blocked", 100, 5))
        self.assertEqual(self.state()["state"], "exhausted")
        self.cached(snap("quota_blocked", None, None))
        self.assertEqual(self.state()["state"], "exhausted")

    def test_quota_blocked_without_or_with_partial_measurements_is_exhausted_never_unknown(self):
        self.enroll()
        for primary, secondary in ((None, None), (40, None), (None, 40), (100, None), (None, 100)):
            with self.subTest(primary=primary, secondary=secondary):
                self.cached(snap("quota_blocked", primary, secondary))
                st = self.state()
                self.assertEqual((st["state"], st["verified"]), ("exhausted", True))

    def test_one_missing_slot_is_partial_so_never_available(self):
        self.enroll(); self.cached(snap(primary=5, secondary=None))
        self.assertEqual(self.state()["state"], "unknown")

    def test_available_without_measurements_is_unknown_not_a_grant(self):
        self.enroll(); self.cached(snap(primary=None, secondary=None))
        self.assertEqual(self.state()["state"], "unknown")      # no number: native rules never call it available

    def test_hysteresis_applies(self):
        self.enroll(); self.cached(snap(primary=92, secondary=1))
        prior = {POOL: self.state()}
        self.assertEqual(prior[POOL]["state"], "threshold")
        self.cached(snap(primary=10, secondary=1))
        again = self.observer().observe({POOL}, NOW + 5, prior, self.m)[POOL]
        self.assertEqual(again["state"], "threshold")

    def test_every_invalid_input_is_unknown_never_available(self):
        self.enroll()
        bad = {
            "stale": snap(at=int(NOW - 4000)), "future": snap(at=int(NOW + 100)),
            "foreign ref": snap(ref="b2" * 32), "dirty": snap(cleanup=False), "unclean none": snap(cleanup=None),
            "unknown status": {**snap(), "status": "account_unknown", "reason": "malformed_response"},
            "reason mismatch": {**snap(), "reason": "quota_blocked"},
            "bad bucket": {**snap(), "quota_bucket": "openai:x:codex"},
            "percent": snap(primary=101), "bool percent": snap(primary=True), "float time": snap(at=NOW - 3.5),
            "avail 100": snap(primary=100, secondary=1), "extra key": {**snap(), "x": 1},
            "missing key": {k: v for k, v in snap().items() if k != "reason"},
        }
        for name, snapshot in bad.items():
            with self.subTest(name):
                self.cached(snapshot)
                st = self.state()
                self.assertEqual((st["state"], st["verified"]), ("unknown", False))

    def test_binding_digest_change_invalidates(self):
        self.enroll(); self.cached(snap())
        self.m = manifest(account={**ACCOUNT, "model": "other"})
        self.assertEqual(self.state()["state"], "unknown")

    def test_catalog_subset_is_required(self):
        self.enroll()
        for name, acct in {"disabled": {**ACCOUNT, "enabled": False},
                           "two pools": {**ACCOUNT, "pools": {POOL: {"window": "unknown"}, "x": {"window": "unknown"}}},
                           "window": {**ACCOUNT, "pools": {POOL: {"window": "5h"}}}}.items():
            with self.subTest(name):
                self.m = manifest(account=acct)
                self.cached(snap())
                self.assertEqual(self.state()["state"], "unknown")

    def test_enrolled_without_cache_or_revoked_is_unknown(self):
        self.enroll()
        self.assertEqual(self.state()["state"], "unknown")       # no cache file at all
        self.cached(snap())
        self.enroll({})
        # revoked: the account is no longer overridden, so only ordinary records count (none here)
        self.assertEqual(self.state()["state"], "unknown")
        self.enroll({"codex/example": "c3" * 32})                   # different independent identity
        self.assertEqual(self.state()["state"], "unknown")

    def test_ordinary_quota_cannot_make_an_enrolled_account_available(self):
        self.enroll()
        q = self.dir / "q.json"
        q.write_text(json.dumps({"schema": "quota-observations/v1", "records": [
            {"catalog_pool": POOL, "pool_id": POOL, "status": "available", "used_fraction": 0.01, "window": "5h",
             "observed_at": iso(NOW - 10), "max_age": 1800, "source": "x", "account_ref": "codex/example"},
            {"catalog_pool": "codex/other/unknown", "pool_id": "codex/other/unknown", "status": "available",
             "used_fraction": 0.3, "window": "5h", "observed_at": iso(NOW - 10), "max_age": 1800, "source": "x",
             "account_ref": "codex/other"}]}))
        obs = PoolObserver(observations=q, launch_bindings=self.bind, launch_cache=self.cache)
        out = obs.observe({POOL, "codex/other/unknown"}, NOW, None, self.m)
        self.assertEqual(out[POOL]["state"], "unknown")
        self.assertEqual(out["codex/other/unknown"]["state"], "available")   # not enrolled: nothing lost

    def test_cache_alone_without_enrollment_grants_nothing(self):
        self.cached(snap())
        self.assertEqual(self.state()["state"], "unknown")

    def test_unsafe_files_are_ignored(self):
        self.enroll(); self.cached(snap())
        self.cache.chmod(0o644)
        self.assertEqual(self.state()["state"], "unknown")
        self.cache.chmod(0o600)
        self.assertEqual(self.state()["state"], "available")
        link = self.dir / "link.json"; link.symlink_to(self.cache)
        self.assertEqual(PoolObserver(launch_bindings=self.bind, launch_cache=link).observe({POOL}, NOW, None, self.m)[POOL]["state"], "unknown")
        self.cache.write_text("{" + " " * 140000 + "}"); self.cache.chmod(0o600)
        self.assertEqual(self.state()["state"], "unknown")
        self.cache.write_text('{"version":1,"version":1,"accounts":{}}'); self.cache.chmod(0o600)
        self.assertEqual(self.state()["state"], "unknown")

    def test_other_cached_accounts_do_not_leak(self):
        self.enroll({"codex/example": REF, "codex/other": "d4" * 32})
        self.cached(snap(), extra={"codex/other": {"binding": "0" * 64, "observation": snap(ref="d4" * 32)}})
        out = self.observer().observe({POOL, "codex/other/unknown"}, NOW, None, self.m)
        self.assertEqual((out[POOL]["state"], out["codex/other/unknown"]["state"]), ("available", "unknown"))

    def test_without_launch_paths_nothing_changes(self):
        self.enroll(); self.cached(snap())
        self.assertEqual(PoolObserver().observe({POOL}, NOW, None, self.m)[POOL]["state"], "unknown")

    def test_digest_is_bob_algorithm(self):
        want = hashlib.sha256(json.dumps({"account": ACCOUNT, "provider_ref": REF}, sort_keys=True,
                                         separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        self.assertEqual(cl.binding_digest(self.m, "codex/example", REF), want)


def codex_desired():
    d = claude_desired()
    pools = {POOL: {"provider": "openai", "resource_ref": "openai.example.unknown", "aliases": []},
             "codex/other/unknown": {"provider": "openai", "resource_ref": "openai.other.unknown", "aliases": []}}
    d["controller"]["capabilities"] = ["codex"]
    d["catalog"] = {
        "accounts": {"codex/example": {"provider": "openai", "credential_ref": "cred:codex-accounts/example",
                                     "quota_pools_by_mode": {"subscription": [POOL]}},
                     "codex/other": {"provider": "openai", "credential_ref": "cred:codex-accounts/other",
                                     "quota_pools_by_mode": {"subscription": ["codex/other/unknown"]}}},
        "pools": pools,
        "profiles": {f"{a}/subscription": {"controller_ref": RUNNER_NAME, "account_ref": a,
                                           "execution_mode": "subscription", "harness": "codex",
                                           "capabilities": ["codex"]} for a in ("codex/example", "codex/other")},
        "bindings": {}}
    return d


class ControllerReportTest(Fixture):
    def controller(self, mesh, home):
        (self.dir / "accounts.json").write_text(json.dumps(self.m))
        client = ControllerClient("https://mesh.example.com", WS, "reporter-key", opener=mesh, sleep=lambda _s: None)
        return WorkspaceController(WS, client, self.dir / "accounts.json", home, home, self.observer())

    def test_actual_report_carries_the_launch_evidence_for_both_workspaces(self):
        self.enroll(); self.cached(snap("quota_blocked", 100, 40))
        reports = []
        for n in ("primary", "vc"):          # one fixed cache serves every workspace; homes differ
            mesh = FakeMesh(codex_desired())
            st = self.controller(mesh, self.dir / n).tick(NOW)
            self.assertEqual(st["status"], "ok", st)
            reports.append(mesh.reports[0])
        for rep in reports:
            self.assertEqual(rep["pools"][POOL]["state"], "exhausted")
            self.assertTrue(rep["pools"][POOL]["verified"])
            self.assertEqual(rep["pools"]["codex/other/unknown"]["state"], "unknown")   # no data: not available
        self.assertEqual(reports[0]["pools"], reports[1]["pools"])
        self.assertEqual((reports[0]["revision"], reports[0]["digest"]), (3, D1))

    def test_report_available_then_revocation_drops_it(self):
        self.enroll(); self.cached(snap())
        mesh = FakeMesh(codex_desired())
        self.controller(mesh, self.dir / "h").tick(NOW)
        self.assertEqual(mesh.reports[-1]["pools"][POOL]["state"], "available")
        self.assertEqual(read_json(self.dir / "h" / "pools.json")["pools"][POOL]["state"], "available")
        self.enroll({})
        self.controller(mesh, self.dir / "h").tick(NOW + 1)
        self.assertEqual(mesh.reports[-1]["pools"][POOL]["state"], "unknown")


class ConfigTest(unittest.TestCase):
    def test_defaults_and_overrides_are_workspace_independent(self):
        with tempfile.TemporaryDirectory() as d:
            key = Path(d, "k"); key.write_text("x\n"); key.chmod(0o600)
            def cfg(extra=""):
                p = Path(d, "r.toml")
                p.write_text(f'[runner]\nmesh_url="https://m.example.com"\nstate_dir="{d}/s"\n'
                             f'[catalog]\nenabled=true\nmanifest="{d}/a.json"\n{extra}\n'
                             f'[[catalog.workspace]]\nid="{WS}"\nreporter_key_file="{key}"\nhome="{d}/h1"\n'
                             f'[[catalog.workspace]]\nid="{WS[:-1]}6"\nreporter_key_file="{key}"\nhome="{d}/h2"\n')
                return load(p).catalog
            c = cfg()
            self.assertEqual(str(c.launch_bindings), os.path.expanduser(cl.DEFAULT_BINDINGS))
            self.assertEqual(str(c.launch_cache), os.path.expanduser(cl.DEFAULT_CACHE))
            c = cfg(f'launch_cache="{d}/c.json"')
            self.assertEqual(str(c.launch_cache), f"{d}/c.json")


if __name__ == "__main__":
    unittest.main()
