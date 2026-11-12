import io
import json
import tempfile
import textwrap
import unittest
import urllib.error
from pathlib import Path

from mesh_runner import RUNNER_NAME, __version__
from mesh_runner.catalog import (BLOCKED, ControllerClient, PoolObserver, WorkspaceController, iso, load_thresholds,
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

    def thresholds(self, **defaults):
        path = self.dir / "thresholds.json"
        path.write_text(json.dumps({"defaults": defaults}))
        return load_thresholds(path)

    def test_fallback_thresholds_are_the_owner_rule(self):
        # leave a pool only at 99 %, come back below 95 %
        self.assertEqual(load_thresholds(None)["defaults"],
                         {"new_launch_block": 0.99, "transfer": 0.99, "readmit_below": 0.95, "min_pause_sec": 600})
        for frac, state in ((0.98, "available"), (0.99, "transfer"), (1.0, "exhausted")):
            q = self.write("q.json", "quota-observations/v1", [self.quota("p/5h", frac)])
            self.assertEqual(PoolObserver(observations=q).observe({"p/5h"}, NOW)["p/5h"]["state"], state, frac)

    def test_threshold_carries_its_threshold_ref(self):
        q = self.write("q.json", "quota-observations/v1", [self.quota("p/5h", 0.92)])
        o = PoolObserver(observations=q, thresholds=self.thresholds(new_launch_block=0.90, transfer=0.95))
        local = o.observe({"p/5h"}, NOW)
        self.assertEqual(local["p/5h"]["state"], "threshold")
        rep = o.for_report(local)["p/5h"]
        self.assertEqual(rep, {"state": "threshold", "verified": True, "observed_at": iso(NOW - 60),
                               "threshold_ref": "thr-default-v1", "evidence_kind": "configured_threshold"})

    def test_available_observation_with_future_reset_keeps_threshold_policy(self):
        record = self.quota("p/5h", 0.92)
        record["reset_at"] = iso(NOW + 4 * 86400)
        q = self.write("q.json", "quota-observations/v1", [record])
        local = PoolObserver(observations=q, thresholds=self.thresholds(new_launch_block=0.90)).observe(
            {"p/5h"}, NOW)
        # A reset is metadata, not exhaustion; the configured threshold policy still applies.
        self.assertEqual(local["p/5h"]["state"], "threshold")
        self.assertEqual(local["p/5h"]["reset_at"], record["reset_at"])

    def test_transfer_is_reported_as_threshold(self):
        q = self.write("q.json", "quota-observations/v1", [self.quota("p/5h", 0.995)])
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

    # a blocked pool returns by its reset time, not by whether readings keep coming
    def blocked_then(self, records, reset_in=3600, then=600):
        """Block p/5h at NOW with a reading whose window resets in ``reset_in`` s; observe again
        ``then`` s later with ``records`` as the only observations, the first pass as prior."""
        first = self.quota("p/5h", 0.995, age=10)
        first["reset_at"] = iso(NOW + reset_in)
        o = PoolObserver(observations=self.write("q.json", "quota-observations/v1", [first]))
        prior = o.observe({"p/5h"}, NOW)
        self.assertEqual((prior["p/5h"]["state"], prior["p/5h"]["reset_at"]), ("transfer", iso(NOW + reset_in)))
        self.write("q.json", "quota-observations/v1", records)
        return o, prior, o.observe({"p/5h"}, NOW + then, prior)["p/5h"]

    def test_blocked_pool_with_stale_reading_stays_blocked_until_its_reset(self):
        stale = self.quota("p/5h", 0.995, age=10)       # the same record, 30+ min old at the next pass
        stale["observed_at"] = iso(NOW - 10)
        o, prior, now = self.blocked_then([stale], reset_in=3 * 3600, then=2 * 3600)
        self.assertEqual((now["state"], now["source"], now["blocked_since"]), ("transfer", "reset_pending", NOW))
        self.assertEqual(o.for_report({"p/5h": now})["p/5h"]["state"], "threshold")
        # an auth_error record (the collector lost its token) is no reading either
        err = {"catalog_pool": "p/5h", "pool_id": "p/5h", "status": "auth_error", "observed_at": iso(NOW + 7000),
               "max_age": 1800, "source": "claude_ratelimit_headers", "window": "5h"}
        self.assertEqual(self.blocked_then([err], reset_in=3 * 3600, then=7200)[2]["state"], "transfer")

    def test_blocked_pool_is_available_after_its_reset_until_a_fresh_reading(self):
        o, prior, now = self.blocked_then([], reset_in=3600, then=3601)
        self.assertEqual((now["state"], now["source"], now["verified"]), ("available", "reset_elapsed", True))
        self.assertNotIn("blocked_since", now)
        self.assertEqual(o.for_report({"p/5h": now})["p/5h"]["state"], "available")
        later = o.observe({"p/5h"}, NOW + 9000, {"p/5h": now})["p/5h"]          # still no reading
        self.assertEqual((later["state"], later["source"]), ("available", "reset_elapsed"))
        self.write("q.json", "quota-observations/v1", [self.quota("p/5h", 0.995, age=-9000 + 10)])
        fresh = o.observe({"p/5h"}, NOW + 9000, {"p/5h": later})["p/5h"]        # a fresh reading decides again
        self.assertEqual((fresh["state"], fresh["source"]), ("transfer", "claude_ratelimit_headers"))

    def test_blocked_pool_without_reset_time_is_unknown_when_stale(self):
        first = self.quota("p/5h", 0.995, age=10)
        o = PoolObserver(observations=self.write("q.json", "quota-observations/v1", [first]))
        prior = o.observe({"p/5h"}, NOW)
        self.write("q.json", "quota-observations/v1", [])
        self.assertEqual(o.observe({"p/5h"}, NOW + 7200, prior)["p/5h"]["state"], "unknown")

    def test_fresh_reading_before_reset_keeps_hysteresis(self):
        low = self.quota("p/5h", 0.5, age=-300 + 10)
        self.assertEqual(self.blocked_then([low], reset_in=3600, then=300)[2]["state"], "threshold")
        self.assertEqual(self.blocked_then([self.quota("p/5h", 0.5, age=-900 + 10)], then=900)[2]["state"],
                         "available")

    def test_partial_reading_keeps_the_block_until_reset_then_returns(self):
        # regression: the 7d window blocked the pool, then only the 5h window stays fresh and low
        def win(window, frac, age, reset=None):
            r = self.quota("p", frac, age=age)
            r.update(window=window, reset_at=reset)
            return r
        o = PoolObserver(observations=self.write("q.json", "quota-observations/v1",
                                                 [win("5h", 0.2, 10), win("7d", 0.995, 10, iso(NOW + 7200))]))
        prior = o.observe({"p"}, NOW)
        self.assertEqual((prior["p"]["state"], prior["p"]["reset_at"]), ("transfer", iso(NOW + 7200)))
        dead_7d = {**win("7d", 0.995, 10), "status": "auth_error", "used_fraction": None}
        self.write("q.json", "quota-observations/v1", [win("5h", 0.2, -3600 + 10), dead_7d])
        before = o.observe({"p"}, NOW + 3600, prior)["p"]
        self.assertEqual((before["state"], before["source"], before["blocked_since"]),
                         ("transfer", "reset_pending", NOW))
        self.write("q.json", "quota-observations/v1", [win("5h", 0.2, -7300 + 10), dead_7d])
        after = o.observe({"p"}, NOW + 7300, {"p": before})["p"]
        self.assertEqual((after["state"], after["source"]), ("available", "reset_elapsed"))
        # without a reset time a partial pool stays unknown, as before
        o2 = PoolObserver(observations=self.write("q.json", "quota-observations/v1",
                                                  [win("5h", 0.2, 10), win("7d", 0.995, 10)]))
        p2 = o2.observe({"p"}, NOW)
        self.write("q.json", "quota-observations/v1", [win("5h", 0.2, -3600 + 10), dead_7d])
        self.assertEqual(o2.observe({"p"}, NOW + 3600, p2)["p"]["state"], "unknown")

    def test_partial_polls_from_the_first_interval_keep_the_blocking_reset(self):
        # regression: the first partial poll lands inside min_pause_sec (t+120)
        def win(window, frac, at, reset=None, status="available"):
            r = self.quota("p", frac, age=-(at - 10))
            r.update(window=window, reset_at=reset, status=status)
            return r
        o = PoolObserver(observations=self.write("q.json", "quota-observations/v1",
                                                 [win("5h", 0.2, 0, iso(NOW + 1800)),
                                                  win("7d", 0.995, 0, iso(NOW + 7200))]))
        seen = [o.observe({"p"}, NOW)["p"]]
        for at in (120, 720, 7300):
            self.write("q.json", "quota-observations/v1",
                       [win("5h", 0.2, at, iso(NOW + 1800)), win("7d", None, at, status="auth_error")])
            seen.append(o.observe({"p"}, NOW + at, {"p": seen[-1]})["p"])
        got = [(e["state"], e.get("reset_at"), e.get("window")) for e in seen]
        block = iso(NOW + 7200)
        self.assertEqual(got[0], ("transfer", block, "7d"))
        self.assertIn(got[1][0], BLOCKED)
        self.assertEqual(got[1][1:], (block, "7d"))           # not the 5h window's reset, not lost
        self.assertEqual(got[2], (got[1][0], block, "7d"))
        self.assertEqual(seen[2]["source"], "reset_pending")
        self.assertEqual((got[3][0], seen[3]["source"]), ("available", "reset_elapsed"))
        self.assertEqual(seen[1]["blocked_since"], NOW)

    def test_a_fresh_blocking_window_does_not_shorten_the_block_of_a_window_without_data(self):
        # regression: 7d blocks until t+7200; at t+120 only 5h is fresh, blocking until t+1800
        def win(window, frac, at, reset=None, status="available"):
            r = self.quota("p", frac, age=-(at - 10))
            r.update(window=window, reset_at=reset, status=status)
            return r
        o = PoolObserver(observations=self.write("q.json", "quota-observations/v1",
                                                 [win("5h", 0.2, 0, iso(NOW + 1800)),
                                                  win("7d", 0.995, 0, iso(NOW + 7200))]))
        seen = [o.observe({"p"}, NOW)["p"]]
        for at, five in ((120, 0.995), (1900, 0.2), (7300, 0.2)):
            self.write("q.json", "quota-observations/v1",
                       [win("5h", five, at, iso(NOW + 1800 if at < 1800 else NOW + 1800 + 18000)),
                        win("7d", None, at, status="auth_error")])
            seen.append(o.observe({"p"}, NOW + at, {"p": seen[-1]})["p"])
        got = [(e["state"], e.get("reset_at"), e.get("window")) for e in seen]
        self.assertEqual(got[1], ("transfer", iso(NOW + 7200), "7d"))     # the later of the two resets
        self.assertIn(got[2][0], BLOCKED)                                   # 5h renewed, 7d still blocked
        self.assertEqual((seen[2]["source"], got[2][1]), ("reset_pending", iso(NOW + 7200)))
        self.assertEqual((got[3][0], seen[3]["source"]), ("available", "reset_elapsed"))
        # a fresh blocking window with the later reset keeps its own
        o2 = PoolObserver(observations=self.write("q.json", "quota-observations/v1",
                                                  [win("5h", 0.995, 0, iso(NOW + 9000)),
                                                   win("7d", None, 0, status="auth_error")]))
        mine = o2.observe({"p"}, NOW + 120, {"p": seen[0]})["p"]
        self.assertEqual((mine["reset_at"], mine["window"]), (iso(NOW + 9000), "5h"))

    def test_hysteresis_on_a_full_low_reading_keeps_the_blocking_reset(self):
        low = self.quota("p/5h", 0.5, age=-120 + 10)
        low["reset_at"] = iso(NOW + 600)
        held = self.blocked_then([low], reset_in=3600, then=120)[2]
        self.assertEqual((held["state"], held["reset_at"]), ("threshold", iso(NOW + 3600)))

    def test_codex_record_with_reset_time_carries_it_and_its_window(self):
        rec = {"provider": "openai", "account_ref": "codex/acct", "catalog_pool": "codex/acct/5h",
               "pool_id": "codex/acct/5h", "status": "available", "used_fraction": 0.995, "window": "5h",
               "reset_at": iso(NOW + 5400), "observed_at": iso(NOW - 30), "max_age": 1800,
               "source": "codex_rate_limits"}
        o = PoolObserver(observations=self.write("q.json", "quota-observations/v1", [rec]))
        got = o.observe({"codex/acct/5h"}, NOW)["codex/acct/5h"]
        self.assertEqual((got["state"], got["reset_at"], got["window"]), ("transfer", iso(NOW + 5400), "5h"))
        self.write("q.json", "quota-observations/v1", [])
        back = o.observe({"codex/acct/5h"}, NOW + 5401, {"codex/acct/5h": got})["codex/acct/5h"]
        self.assertEqual((back["state"], back["source"]), ("available", "reset_elapsed"))

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

    def observations_cfg(self, value=None):
        line = "" if value is None else f"execution_observations = {value}"
        return self.cfg(f"""
            [catalog]
            enabled = true
            manifest = "{self.dir}/accounts.json"
            {line}
            [[catalog.workspace]]
            id = "{WS}"
            reporter_key_file = "{self.key}"
        """).catalog

    def test_execution_observations_is_opt_in(self):
        self.assertFalse(self.observations_cfg().execution_observations)
        self.assertFalse(self.observations_cfg("false").execution_observations)
        self.assertTrue(self.observations_cfg("true").execution_observations)

    def test_execution_observations_needs_the_catalog_controller(self):
        for head in ("[catalog]\nenabled = false", "[catalog]"):
            with self.subTest(head=head), self.assertRaises(ConfigError):
                self.cfg(head + "\nexecution_observations = true\n")

    def test_execution_observations_wrong_type_is_refused_not_enabled(self):
        for bad in ('"true"', '"false"', "1", "0", "[true]"):
            with self.subTest(bad=bad), self.assertRaises(ConfigError):
                self.observations_cfg(bad)

    def test_execution_observations_wrong_type_is_refused_even_when_catalog_is_disabled(self):
        for head in ("[catalog]\nenabled = false", "[catalog]"):
            with self.subTest(head=head), self.assertRaises(ConfigError):
                self.cfg(head + '\nexecution_observations = "true"\n')

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


class ChangeNotice(unittest.TestCase):
    def ctl(self,calls):
        c=WorkspaceController(WS,None,Path('m'),Path('h'),Path('c'),PoolObserver())
        c.on_change=calls.append;return c

    def test_only_semantic_change_notifies_once(self):
        calls=[];c=self.ctl(calls)
        a={'p':{'state':'exhausted','verified':True,'observed_at':'t1'}}
        c._notify_change(a);self.assertEqual(calls,[])                    # baseline
        c._notify_change({'p':{'state':'exhausted','verified':True,'observed_at':'t2'}})
        self.assertEqual(calls,[])                                       # new timestamp, same meaning
        b={'p':{'state':'available','verified':True,'observed_at':'t3'}}
        c._notify_change(b);self.assertEqual(calls,[WS])
        c._notify_change(dict(b));self.assertEqual(calls,[WS])           # not again
        c._notify_change({'p':{'state':'available','verified':False,'observed_at':'t4'}})
        self.assertEqual(calls,[WS,WS])

    def test_consumer_failure_does_not_break_controller(self):
        c=self.ctl([]);c.on_change=lambda w:1/0
        c._notify_change({'p':{'state':'a','verified':True}});c._notify_change({'p':{'state':'b','verified':True}})

if __name__ == "__main__":
    unittest.main()
