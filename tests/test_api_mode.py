"""API mode: a lane turn under an API key, within the budget of its spend pool (api_mode.py).

The turn path is the ordinary one (Bench: a headless Claude-shaped CLI). The fake CLI reports
``apiKeySource`` in its ``system/init`` event as the real one does in print mode: the environment key
when one is set, ``none`` for the subscription login."""
import json
import os
import secrets
import sys
import unittest
from unittest import mock
from pathlib import Path

from fakes import FakeMesh, calls, next_turn, task, wait_time, write_replies
from mesh_runner import api_mode, turn_cost
from mesh_runner.adapters.claude import ClaudeAdapter
from test_runner import Bench

PROFILE = "anthropic-api/ops/api"
POOL = "anthropic-api/ops/spend"
CRED = "cred:anthropic-api/ops"


def manifest(path: Path, enabled: bool = True) -> Path:
    path.write_text(json.dumps({"schema_version": 1, "accounts": {
        "anthropic-api/ops": {"provider": "anthropic", "credential_ref": CRED, "harness": "claude_code",
                              "enabled": enabled, "modes": {"api": {"pools": [POOL]}},
                              "pools": {POOL: {"window": "budget", "max_concurrency": 1}}},
        "claude/ops-team": {"provider": "anthropic", "credential_ref": "cred:claude/ops-team",
                            "harness": "claude_code", "enabled": True,
                            "modes": {"subscription": {"pools": ["claude/ops-team/5h"]}},
                            "pools": {"claude/ops-team/5h": {"window": "5h"}}}}}))
    return path


def fake_key() -> str:
    return "sk-ant-api03-" + secrets.token_urlsafe(48)


def carrier(path: Path, key: str, mode: int = 0o600) -> Path:
    path.write_text(f"ANTHROPIC_API_KEY={key}\n")
    os.chmod(path, mode)
    return path


class ApiBench(Bench):
    def setUp(self):
        super().setUp()
        self.key = fake_key()
        self.cfg.api = api_mode.ApiConfig(
            manifest=manifest(self.root / "accounts.json"), ledger=self.root / "state" / "api-spend.sqlite",
            pools={POOL: api_mode.PoolBudget(cap=api_mode.usd_micros("10.00"), run=api_mode.usd_micros("2.00"))},
            credentials={CRED: api_mode.Carrier(carrier(self.root / "api.env", self.key))})
        self.agent.launch_mode = "headless"
        self.agent.inline_env["FAKE_COST"] = "0.37"

    def run_one(self, reply=None):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        write_replies(self.replies, {t["id"][:8]: reply or wait_time(why="soak window")})
        r = self.runner(mesh)
        r.tick()
        self.settle_all(r)
        return r, t

    def events(self, r, kind):
        return [d for (d,) in r.store.db.execute("SELECT detail FROM events WHERE kind=?", (kind,))]

    def ledger(self):
        return api_mode.Ledger(self.cfg.api.ledger, self.cfg.api.pools)

    def turn_logs(self, r):
        d = self.cfg.state_dir / "logs" / self.agent.name
        return sorted(d.glob("*.turn*.jsonl"))


class ApiTurn(ApiBench):
    def test_without_consent_the_turn_runs_on_the_subscription(self):
        self.agent.api_primary_profile = PROFILE          # the route names API, the lane never agreed
        r, t = self.run_one()
        got = calls(self.calls)
        self.assertEqual(len(got), 1, "the card still runs")
        self.assertFalse(got[0]["api_key"], "no key without the lane's consent")
        self.assertEqual(got[0]["max_budget"], "")
        self.assertEqual(self.events(r, "api_route_ignored"), ["api_profile_not_opted_in"])
        self.assertEqual(ClaudeAdapter().parse(self.turn_logs(r)[0]).api_key_source, "none")
        self.assertFalse(self.cfg.api.ledger.exists() and self.ledger().status(POOL)["open_reservations"])

    def test_consent_and_budget_run_under_the_key_and_charge_the_turn_cost(self):
        self.agent.api_optin = True
        self.agent.api_primary_profile = PROFILE
        r, t = self.run_one()
        got = calls(self.calls)
        self.assertEqual(len(got), 1)
        self.assertTrue(got[0]["api_key"], "the turn's process holds the key")
        self.assertEqual(got[0]["max_budget"], "2.00", "the CLI is bounded by the reservation")
        logs = self.turn_logs(r)
        self.assertEqual(len(logs), 1)
        source = ClaudeAdapter().parse(logs[0]).api_key_source
        self.assertTrue(source and source != "none", source)
        st = self.ledger().status(POOL)
        self.assertEqual((st["spent_usd"], st["committed_usd"], st["open_reservations"]), ("0.37", "0.37", 0))
        spend = [json.loads(d) for d in self.events(r, "api_spend")]
        self.assertEqual([(x["charged_usd"], x["total_cost_usd"], x["api_key_source"]) for x in spend],
                         [("0.37", 0.37, "ANTHROPIC_API_KEY")])
        admitted = [json.loads(d) for d in self.events(r, "api_admitted")]
        self.assertEqual([(x["profile"], x["role"]) for x in admitted], [(PROFILE, "primary")])

    def test_the_spend_report_names_the_route_account_its_pools_are_kept_under(self):
        self.cfg.spend_report_turn_fields = True
        self.agent.api_optin = True
        self.agent.api_primary_profile = PROFILE
        r, t = self.run_one()
        self.assertEqual(len(calls(self.calls)), 1, "the turn ran under the key")
        turns = [x for x in r.mesh.report_turns if x]
        self.assertEqual(len(turns), 1, r.mesh.report_turns)
        self.assertEqual((turns[0]["account_ref"], turns[0]["billing_mode"], turns[0]["provider"]),
                         (POOL.rsplit("/", 1)[0], "api", "anthropic"))
        self.assertLessEqual(turns[0]["started_at"], turns[0]["ended_at"])

    def test_the_key_value_is_in_no_journal_log_or_record(self):
        self.agent.api_optin = True
        self.agent.api_primary_profile = PROFILE
        with self.assertLogs("mesh_runner", level="DEBUG") as captured:
            r, t = self.run_one()
        self.assertTrue(calls(self.calls)[0]["api_key"])
        haystack = [p for p in self.cfg.state_dir.rglob("*") if p.is_file()]
        self.assertTrue(any(p.suffix == ".jsonl" for p in haystack))
        r.store.db.commit()
        leaked = [str(p) for p in haystack if self.key.encode() in p.read_bytes()]
        self.assertEqual(leaked, [], "the key value reached a file under the state directory")
        self.assertNotIn(self.key, "\n".join(captured.output))
        # positive control: the same search finds a value that IS written (the session id)
        sid = r.store.by_state(self.agent.name, ("ready", "waiting", "done", "failed"))[0].session_id
        self.assertTrue(any(sid.encode() in p.read_bytes() for p in self.turn_logs(r)))

    def test_exhausted_budget_refuses_the_start_with_its_reason(self):
        self.agent.api_optin = True
        self.agent.api_primary_profile = PROFILE
        self.cfg.api.pools[POOL] = api_mode.PoolBudget(cap=api_mode.usd_micros("3.00"), run=api_mode.usd_micros("2.00"))
        led = self.ledger()
        led.reserve("earlier:turn1", [POOL])
        led.settle("earlier:turn1", "1.50", "total_cost_usd")      # 1.50 + 2.00 > 3.00
        r, t = self.run_one()
        self.assertEqual(calls(self.calls), [], "no process starts without budget")
        self.assertIn("budget_exhausted", self.events(r, "api_refused"))
        notes = [n for (n,) in r.store.db.execute("SELECT note FROM attempts")]
        self.assertTrue(notes and all("budget_exhausted" in n for n in notes), notes)
        st = self.ledger().status(POOL)
        self.assertEqual((st["spent_usd"], st["open_reservations"]), ("1.50", 0))

    def test_a_spawn_failing_before_any_process_returns_the_reservation(self):
        self.agent.api_optin = True
        self.agent.api_primary_profile = PROFILE
        with mock.patch("mesh_runner.adapters.base.Adapter.spawn", side_effect=OSError(28, "No space left")):
            try:
                self.run_one()
            except OSError:
                pass
        self.assertEqual(calls(self.calls), [], "no process started")
        st = self.ledger().status(POOL)
        self.assertEqual((st["spent_usd"], st["open_reservations"]), ("0.00", 0))

    def test_a_failing_spend_record_before_spawn_returns_the_reservation(self):
        self.agent.api_optin = True
        self.agent.api_primary_profile = PROFILE
        from mesh_runner.store import Store
        real = Store.event

        def event(store, attempt, kind, *rest, **kw):
            if kind == "api_admitted":
                raise OSError(28, "No space left")
            return real(store, attempt, kind, *rest, **kw)
        with mock.patch.object(Store, "event", event):
            try:
                self.run_one()
            except OSError:
                pass
        self.assertEqual(calls(self.calls), [], "no process started")
        st = self.ledger().status(POOL)
        self.assertEqual((st["spent_usd"], st["open_reservations"]), ("0.00", 0))

    def test_a_one_time_launch_failure_recovers_with_one_start_and_one_charge(self):
        self.agent.api_optin = True
        self.agent.api_primary_profile = PROFILE
        from mesh_runner.store import Store
        real, faults = Store.event, []

        def event(store, attempt, kind, *rest, **kw):
            if kind == "api_admitted" and not faults:
                faults.append(kind)
                raise OSError(28, "No space left")
            return real(store, attempt, kind, *rest, **kw)
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        write_replies(self.replies, {t["id"][:8]: wait_time(why="soak window")})
        r = self.runner(mesh)
        with mock.patch.object(Store, "event", event):
            try:
                r.tick()
            except OSError:
                pass
            self.assertEqual((faults, calls(self.calls)), (["api_admitted"], []))
            r._resume_hold.clear()                          # the local hold has passed
            r.tick()
            self.settle_all(r)
        self.assertEqual(len(calls(self.calls)), 1, self.events(r, "api_refused"))
        self.assertEqual(self.events(r, "api_refused"), [])
        st = self.ledger().status(POOL)
        self.assertEqual((st["spent_usd"], st["open_reservations"]), ("0.37", 0))

    def test_interactive_launch_never_gets_the_key(self):
        self.agent.api_optin = True
        self.agent.api_primary_profile = PROFILE
        self.agent.launch_mode = "interactive"
        r, t = self.run_one()
        self.assertEqual(calls(self.calls), [])
        self.assertEqual(self.events(r, "api_refused"), ["api_requires_headless"])

    def test_reserve_route_needs_the_provider_in_the_lane_policy(self):
        self.agent.api_optin = True
        self.agent.env_command = [sys.executable, "-c", f"print('MESH_RUNNER_API_PROFILE={PROFILE}')"]
        r, t = self.run_one()
        self.assertFalse(calls(self.calls)[0]["api_key"])
        self.assertEqual(self.events(r, "api_route_ignored"), ["api_profile_not_permitted"])

    def test_reserve_route_with_the_provider_listed_runs_under_the_key(self):
        self.agent.api_optin = True
        self.agent.api_reserve_providers = ["anthropic"]
        self.agent.env_command = [sys.executable, "-c", f"print('MESH_RUNNER_API_PROFILE={PROFILE}')"]
        r, t = self.run_one()
        got = calls(self.calls)
        self.assertTrue(got[0]["api_key"])
        self.assertEqual([json.loads(d)["role"] for d in self.events(r, "api_admitted")], ["reserve"])

    def test_route_marker_never_reaches_the_session_env(self):
        self.agent.env_command = [sys.executable, "-c", f"print('MESH_RUNNER_API_PROFILE={PROFILE}')"]
        r, t = self.run_one()
        self.assertNotIn(api_mode.PROFILE_VAR, r.env)

    def test_withdrawn_consent_returns_the_lane_to_the_subscription(self):
        self.agent.api_optin = True
        self.agent.api_primary_profile = PROFILE
        r, t = self.run_one()
        self.assertTrue(calls(self.calls)[0]["api_key"])
        self.agent.api_optin = False
        self.calls.unlink()
        t2 = task(2)
        mesh = FakeMesh([t2], max_concurrent=1)
        write_replies(self.replies, {t2["id"][:8]: wait_time(why="soak window")})
        r2 = self.runner(mesh)
        r2.tick()
        self.settle_all(r2)
        self.assertFalse(calls(self.calls)[0]["api_key"])

    def test_unreadable_key_holds_the_start(self):
        self.agent.api_optin = True
        self.agent.api_primary_profile = PROFILE
        os.chmod(self.root / "api.env", 0o644)
        r, t = self.run_one()
        self.assertEqual(calls(self.calls), [])
        self.assertEqual(self.events(r, "api_refused"), ["credential_carrier_permissions"])
        self.assertEqual(self.ledger().status(POOL)["open_reservations"], 0)


class ResumedTurns(ApiBench):
    """A resumed Claude session reports its running total as ``total_cost_usd``: the pool is charged
    each turn's own part of it (the live case: two turns of 0.272918 -> 0.4071958)."""

    def setUp(self):
        super().setUp()
        self.agent.api_optin = True
        self.agent.api_primary_profile = PROFILE
        self.agent.inline_env["FAKE_COST"] = "0.272918"           # turn 1: the session so far
        self.agent.inline_env["FAKE_COST_RESUME"] = "0.4071958"   # turn 2 (--resume): the session so far

    def two_turns(self, second, restart=False):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        write_replies(self.replies, {t["id"][:8]: next_turn("part one"), t["id"][:8] + ":resume": second})
        r = self.runner(mesh)
        r.tick()
        self.settle_all(r)
        if restart:
            r = self.runner(mesh)                                   # a runner restart between the turns
        for _ in range(3):
            if len(calls(self.calls)) >= 2:
                break
            r.tick()
            self.settle_all(r)
        self.assertEqual([c["resume"] for c in calls(self.calls)], [False, True])
        return r

    def charged(self):
        import sqlite3
        db = sqlite3.connect(self.cfg.api.ledger)
        try:
            return db.execute("SELECT spent, source FROM spend WHERE pool=? ORDER BY created", (POOL,)).fetchall()
        finally:
            db.close()

    def test_the_pool_is_charged_the_session_total_once(self):
        r = self.two_turns(wait_time(why="soak window"))
        self.assertEqual(self.charged(), [(272918, "total_cost_usd_delta"), (134278, "total_cost_usd_delta")])
        self.assertEqual(self.ledger().status(POOL)["spent_micros"], 407196)
        spend = [json.loads(d) for d in self.events(r, "api_spend")]
        self.assertEqual([(x["total_cost_usd"], x["session_base_usd"]) for x in spend],
                         [(0.272918, 0.0), (0.4071958, 0.272918)])
        self.assertEqual([x["cost"] for x in r.mesh.reports], [0.2729, 0.1343],
                         "Mesh and the API pool settle against the same session base")

    def test_a_runner_restart_between_the_turns_keeps_the_base(self):
        self.two_turns(wait_time(why="soak window"), restart=True)
        self.assertEqual(self.ledger().status(POOL)["spent_micros"], 407196)

    def test_a_resumed_turn_without_a_cost_charges_its_whole_reservation(self):
        self.agent.inline_env["FAKE_COST_RESUME"] = "none"
        self.two_turns(wait_time(why="soak window"))
        self.assertEqual(self.charged(), [(272918, "total_cost_usd_delta"),
                                          (2_000_000, "unknown_charged_reservation")])


class _Marks:
    def __init__(self):
        self.d = {}

    def mark(self, agent, task_id, key, value=None):
        if value is not None:
            self.d[(agent, task_id, key)] = value
            return None
        v = self.d.get((agent, task_id, key))
        return (v, "at") if v is not None else None


class SessionBase(unittest.TestCase):
    def base(self, store, turn, sid, total):
        return turn_cost.session_base(store, "owner", "t1", turn, sid, total)

    def test_the_base_is_the_previous_total_of_the_same_session(self):
        m = _Marks()
        self.assertEqual(self.base(m, "a:1", "s1", 0.272918), 0.0)
        self.assertEqual(self.base(m, "a:2", "s1", 0.4071958), 0.272918)
        self.assertEqual(self.base(m, "a:2", "s1", 0.4071958), 0.272918, "a turn settled again: same base")
        self.assertEqual(self.base(m, "b:1", "s1", 0.5), 0.4071958, "a new attempt resuming the session")

    def test_a_new_session_or_a_counter_that_started_over_counts_whole(self):
        m = _Marks()
        self.base(m, "a:1", "s1", 0.4)
        self.assertEqual(self.base(m, "a:2", "s2", 0.1), 0.0, "another session")
        self.assertEqual(self.base(m, "a:3", "s2", 0.05), 0.0, "a total below the base")
        self.assertEqual(self.base(m, "a:4", "", 0.3), 0.0, "no session id")

    def test_a_turn_without_a_total_leaves_the_base(self):
        m = _Marks()
        self.base(m, "a:1", "s1", 0.27)
        self.assertIsNone(self.base(m, "a:2", "s1", None))
        self.assertEqual(self.base(m, "a:3", "s1", 0.40), 0.27)

    def test_turn_spend_is_the_increment_in_whole_micros(self):
        self.assertEqual(api_mode.usd_micros(api_mode.turn_spend(0.4071958, 0.272918)), 134278)
        self.assertEqual(api_mode.usd_micros(api_mode.turn_spend(0.272918, None)), 272918)
        self.assertEqual(api_mode.usd_micros(api_mode.turn_spend(0.1, 0.4)), 100000, "base above total")
        self.assertIsNone(api_mode.turn_spend(None, 0.27), "unknown: the reservation is charged")
        self.assertIsNone(api_mode.turn_spend(-1, 0.27))


class Carrier(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def refused(self, text, mode=0o600, pin=""):
        p = self.root / "k.env"
        p.write_text(text)
        os.chmod(p, mode)
        with self.assertRaises(api_mode.Refused) as cm:
            api_mode.read_key(api_mode.Carrier(p, sha256_8=pin))
        return cm.exception.reason

    def test_refusals_name_the_cause_and_never_the_value(self):
        key = fake_key()
        self.assertEqual(self.refused(f"ANTHROPIC_API_KEY={key}\n", 0o640), "credential_carrier_permissions")
        oauth_shaped = "-".join(("sk", "ant", "oat01", "x" * 40))           # an OAuth token is not an API key
        self.assertEqual(self.refused(f"ANTHROPIC_API_KEY={oauth_shaped}"), "credential_not_api_key")
        self.assertEqual(self.refused(f"ANTHROPIC_API_KEY={key}\nOTHER=1"), "credential_carrier_extra_field")
        self.assertEqual(self.refused(f"ANTHROPIC_API_KEY={key}", pin="00000000"), "credential_fingerprint_mismatch")
        self.assertEqual(self.refused(""), "credential_carrier_field_missing")

    def test_pinned_key_is_read(self):
        import hashlib
        key = fake_key()
        p = carrier(self.root / "k.env", key)
        pin = hashlib.sha256(key.encode()).hexdigest()[:8]
        self.assertEqual(api_mode.read_key(api_mode.Carrier(p, sha256_8=pin)), key)

    def test_turn_env_drops_other_logins(self):
        env = api_mode.turn_env({"CLAUDE_CODE_OAUTH_TOKEN": "t", "ANTHROPIC_BASE_URL": "u", "HOME": "/h",
                                 api_mode.PROFILE_VAR: PROFILE}, "k")
        self.assertEqual(env, {"HOME": "/h", "ANTHROPIC_API_KEY": "k"})


class LedgerRules(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.led = api_mode.Ledger(Path(self.tmp.name) / "l.sqlite",
                                   {POOL: api_mode.PoolBudget(api_mode.usd_micros("5"), api_mode.usd_micros("2"))})

    def test_unknown_cost_charges_the_reservation_and_settle_is_once(self):
        self.led.reserve("a:turn1", [POOL])
        self.assertEqual(self.led.reserve("a:turn1", [POOL]), 2_000_000)   # the same turn: the same hold
        self.assertEqual(self.led.settle("a:turn1", None, "total_cost_usd"), 2_000_000)
        self.assertIsNone(self.led.settle("a:turn1", "0.10", "total_cost_usd"))
        self.assertEqual(self.led.status(POOL)["spent_usd"], "2.00")

    def test_open_reservations_count_against_the_cap(self):
        self.led.reserve("a:turn1", [POOL])
        self.led.reserve("b:turn1", [POOL])
        with self.assertRaises(api_mode.Refused) as cm:
            self.led.reserve("c:turn1", [POOL])
        self.assertEqual(cm.exception.reason, "budget_exhausted")
        self.led.settle("a:turn1", "0.25", "total_cost_usd")
        self.led.reserve("c:turn1", [POOL])                               # 0.25 + 2 + 2 <= 5

    def test_a_released_turn_reserves_again_and_a_settled_one_never(self):
        self.led.reserve("a:turn1", [POOL])
        self.assertTrue(self.led.release("a:turn1"))                       # no process ran
        self.assertEqual(self.led.status(POOL)["open_reservations"], 0)
        self.assertEqual(self.led.reserve("a:turn1", [POOL]), 2_000_000)  # the same turn tries again
        self.led.settle("a:turn1", "0.30", "total_cost_usd")
        self.assertFalse(self.led.release("a:turn1"), "a turn that ran is never returned")
        with self.assertRaises(api_mode.Refused) as cm:
            self.led.reserve("a:turn1", [POOL])
        self.assertEqual(cm.exception.reason, "budget_attempt_settled")
        self.assertEqual(self.led.status(POOL)["spent_usd"], "0.30")

    def test_a_pool_without_a_cap_is_never_spent(self):
        with self.assertRaises(api_mode.Refused) as cm:
            self.led.reserve("a:turn1", ["other/pool"])
        self.assertEqual(cm.exception.reason, "api_budget_unconfigured")


class ConfigParse(unittest.TestCase):
    def test_api_table_and_lane_fields(self):
        import tempfile
        from mesh_runner.config import ConfigError, load
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "k").write_text("k")
            os.chmod(root / "k", 0o600)
            (root / "p").mkdir()
            cfg_path = root / "runner.toml"
            cfg_path.write_text(f'''
[runner]
mesh_url = "http://mesh"
state_dir = "{root}/state"
[api]
manifest = "{root}/accounts.json"
[api.pools."{POOL}"]
cap_usd = "10.00"
run_usd = "2.00"
[api.credentials."{CRED}"]
path = "{root}/api.env"
sha256_8 = "93c4f618"
[[agents]]
name = "t"
key_file = "{root}/k"
profile_dir = "{root}/p"
launch_mode = "headless"
api_optin = true
api_primary_profile = "{PROFILE}"
api_reserve_providers = ["anthropic"]
''')
            cfg = load(cfg_path)
            self.assertEqual(cfg.api.pools[POOL], api_mode.PoolBudget(10_000_000, 2_000_000))
            self.assertEqual(cfg.api.credentials[CRED].sha256_8, "93c4f618")
            self.assertEqual(cfg.api.ledger, root / "state" / "api-spend.sqlite")
            a = cfg.agents[0]
            self.assertEqual((a.api_optin, a.api_primary_profile, a.api_reserve_providers),
                             (True, PROFILE, ["anthropic"]))
            cfg_path.write_text(cfg_path.read_text().replace('run_usd = "2.00"', 'run_usd = "20.00"'))
            with self.assertRaises(ConfigError):
                load(cfg_path)


if __name__ == "__main__":
    unittest.main()
