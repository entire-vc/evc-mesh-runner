"""A provider refusal of the harness version and an expired login are lane faults: the lane is
held, the card keeps its counters, the operator gets one signal naming the account."""
import datetime as dt
import json
import os
import tempfile
import unittest
from pathlib import Path

from mesh_runner import auth, harness_hold, operator_alert
from mesh_runner.adapters.codex import CodexAdapter
from fakes import FakeMesh, task
from test_runner import Bench
from mesh_runner.runner import Runner

# verbatim from provider turn logs (Codex CLI 0.159.3)
REFUSAL = ('{"type":"error","status":400,"error":{"type":"invalid_request_error","message":'
           '"The \'gpt-6.1-sol\' model requires a newer version of Codex. Please upgrade to the latest '
           'app or CLI and try again."}}')
NO_REPLY = "no model output; other: " + REFUSAL


def write(path, *events):
    path.write_text("".join(json.dumps(e) + "\n" for e in events))
    return path


def tui_turn(path, why=NO_REPLY):
    """What the interactive driver writes for a Codex turn that ended without a reply."""
    return write(path, {"type": "thread.started", "thread_id": "t1"},
                 {"type": "item.completed", "item": {"type": "agent_message", "text": ""}},
                 {"type": "turn.completed", "usage": {}, "no_reply": why})


class Parse(unittest.TestCase):
    def parse(self, *events):
        with tempfile.TemporaryDirectory() as root:
            return CodexAdapter().parse(write(Path(root) / "t.jsonl", *events))

    def test_interactive_no_reply_refusal_is_upgrade_required(self):
        for why in (NO_REPLY, "after model output; other: " + REFUSAL):
            with self.subTest(why=why[:30]):
                r = self.parse({"type": "turn.completed", "usage": {}, "no_reply": why})
                self.assertTrue(r.upgrade_required)
                self.assertFalse(r.is_error)

    def test_headless_typed_refusal_is_upgrade_required(self):
        r = self.parse({"type": "turn.failed", "error": {"message": REFUSAL}})
        self.assertTrue(r.upgrade_required and r.is_error)

    def test_other_empty_turns_and_prose_are_not(self):
        for ev in ({"type": "turn.completed", "usage": {}, "no_reply": "no model output"},
                   {"type": "turn.completed", "usage": {}, "no_reply": "no model output; other: stream disconnected"},
                   {"type": "turn.failed", "error": {"message": "stream disconnected"}}):
            with self.subTest(ev=ev):
                self.assertFalse(self.parse(ev).upgrade_required)
        prose = {"type": "item.completed", "item": {"type": "agent_message",
                                                    "text": "the log said: requires a newer version of Codex"}}
        self.assertFalse(self.parse(prose, {"type": "turn.completed", "usage": {}}).upgrade_required)


class Alerts(Bench):
    def alerts(self, r):
        path = r.cfg.state_dir / "events.jsonl"
        rows = [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []
        return [e for e in rows if e["kind"] == "operator_alert"]

    def codex_runner(self, mesh):
        r = self.runner(mesh)
        r.refresh_me()
        r.alt_adapters = {"codex": CodexAdapter(profile="base")}
        return r

    def settle_tui_refusal(self, r, t, name="a"):
        a = r.store.create(self.agent.name, t["id"], "codex")
        path = tui_turn(self.root / f"{name}.jsonl")
        r.store.update(a, state="running", turns=3, log_path=str(path))
        r.settle(r.store.get(a.id))
        return r.store.get(a.id)

    # ---------------------------------------------------------------- 1. outdated CLI
    def test_tui_refusal_holds_the_lane_not_a_no_outcome(self):
        t = task(81)
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.codex_runner(mesh)
        a = self.settle_tui_refusal(r, t)
        self.assertEqual((a.state, a.no_outcome), ("ready", 0))
        kinds = [k for _, k, _ in r.store.events(a.id)]
        self.assertNotIn("no_outcome", kinds)
        self.assertIn("harness_upgrade_required", kinds)
        self.assertEqual(r.store.auth_turns(a.id), 1)            # the turn budget ignores it
        self.assertEqual(mesh.comment_log, {})                   # nothing on the card
        self.assertTrue(harness_hold.active(r.store, "codex", "codex"))
        alerts = self.alerts(r)
        self.assertEqual(len(alerts), 1)
        self.assertIn("outdated", alerts[0]["text"])
        self.assertEqual((alerts[0]["incident"], alerts[0]["harness"]), ("harness_upgrade", "codex"))
        self.assertIn("requires a newer version of Codex", alerts[0]["reason"])

    def test_many_lanes_refused_signal_once(self):
        t1, t2 = task(82), task(83)
        mesh = FakeMesh([t1, t2], max_concurrent=2)
        r = self.codex_runner(mesh)
        self.settle_tui_refusal(r, t1, "a")
        self.settle_tui_refusal(r, t2, "b")
        self.assertEqual(len(self.alerts(r)), 1)

    def test_headless_refusal_is_not_a_failed_turn(self):
        t = task(84)
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.codex_runner(mesh)
        a = r.store.create(self.agent.name, t["id"], "codex")
        path = write(self.root / "h.jsonl", {"type": "thread.started", "thread_id": "t1"},
                     {"type": "turn.failed", "error": {"message": REFUSAL}})
        r.store.update(a, state="running", turns=1, no_outcome=self.cfg.max_failures, log_path=str(path))
        r.settle(r.store.get(a.id))
        a = r.store.get(a.id)
        self.assertEqual((a.state, a.no_outcome), ("ready", self.cfg.max_failures))   # no give-up
        self.assertEqual(mesh.comment_log, {})

    def test_hold_stops_launches_until_the_binary_changes(self):
        binary = self.root / "codex"
        binary.write_text("v0.159.3")
        held, new = harness_hold.hold(self.agent_store(), "codex", str(binary), "refused")
        self.assertTrue(new)
        store = self.agent_store()
        self.assertTrue(harness_hold.active(store, "codex", str(binary)))
        self.assertFalse(harness_hold.hold(store, "codex", str(binary), "again")[1])   # one generation
        binary.write_text("v0.167.1 upgraded")
        os.utime(binary, ns=(1, 1))
        self.assertEqual(harness_hold.active(store, "codex", str(binary)), {})
        later = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=harness_hold.RETRY_HOURS)
        binary.write_text("v0.159.3")
        self.assertEqual(harness_hold.active(store, "codex", str(binary), now=later), {})

    def test_held_harness_refuses_the_spawn_and_keeps_the_card(self):
        t = task(85)
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.codex_runner(mesh)
        harness_hold.hold(r.store, "codex", "codex", "refused")
        a = r.store.create(self.agent.name, t["id"], "codex")
        self.assertTrue(r._harness_held(a, "codex"))
        self.assertEqual(r.store.get(a.id).state, "ready")
        self.assertFalse(r._harness_held(a, "claude"))

    # ---------------------------------------------------------------- 2. expired login
    def test_auth_hold_signals_once_with_the_account_name(self):
        t = task(86)
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        r.refresh_me()
        a = r.store.create(self.agent.name, t["id"], "claude")
        env = {"HOME": "/u/x", "CLAUDE_CONFIG_DIR": "/u/x/.claude-accounts/team-a"}
        profile = auth.profile_id(env, self.root)
        r.store.mark(self.agent.name, t["id"], "auth_launch", json.dumps(
            {"attempt": a.id, "turn": 1, "profile": profile, "account": auth.account_label("claude", env)}))
        r.store.update(a, state="running", turns=1)
        r._record_authentication_failure(r.store.get(a.id), auth_only=True)
        for _ in range(5):                                       # every admission tick meets the hold
            r._authentication_failed(r.store.get(a.id), profile)
        alerts = self.alerts(r)
        self.assertEqual(len(alerts), 1)
        self.assertIn("claude team-a", alerts[0]["text"])
        self.assertEqual((alerts[0]["incident"], alerts[0]["account"], alerts[0]["lane"]),
                         ("auth_hold", "claude team-a", self.agent.name))
        self.assertNotIn(profile + ".", alerts[0]["text"].split("--profile")[0])   # named, not hashed
        self.assertEqual(auth.status(r.store)[0]["account"], "claude team-a")

    def test_auth_hold_repeats_after_the_interval(self):
        store = self.agent_store()
        since = "2026-11-11T01:33:23.355+00:00"
        t0 = dt.datetime(2026, 11, 11, 1, 34, tzinfo=dt.timezone.utc)
        self.assertTrue(operator_alert.signal(store, "auth:p", since, "x", now=t0))
        self.assertFalse(operator_alert.signal(store, "auth:p", since, "x", now=t0 + dt.timedelta(hours=1)))
        self.assertTrue(operator_alert.signal(store, "auth:p", since,
                                              "x", now=t0 + dt.timedelta(hours=operator_alert.REPEAT_HOURS)))
        self.assertTrue(operator_alert.signal(store, "auth:p", "2026-11-12T00:00:00+00:00", "x",
                                              now=t0 + dt.timedelta(days=1)))   # a new hold signals again

    def test_account_labels(self):
        home = {"HOME": "/u/x"}
        self.assertEqual(auth.account_label("claude", home), "claude default login ~/.claude (no CLAUDE_CONFIG_DIR)")
        self.assertEqual(auth.account_label("claude", {**home, "CLAUDE_CONFIG_DIR": "/a/team-b"}), "claude team-b")
        self.assertEqual(auth.account_label("codex", {**home, "CODEX_HOME": "/c/codex-2"}), "codex codex-2")

    def test_api_account_label_never_exposes_provider_url(self):
        for base_url in ("", "https://provider.example/v1",
                         "https://synthetic-user@provider.example/private?token=synthetic-token#secret",
                         "malformed provider url with synthetic-token"):
            with self.subTest(base_url=base_url):
                label = auth.account_label("claude", {"ANTHROPIC_API_KEY": "synthetic-api-key",
                                                       "ANTHROPIC_BASE_URL": base_url})
                self.assertEqual(label, "claude API key")

    # ---------------------------------------------------------------- 3. stale hold on live lanes
    def test_lane_status_hides_a_hold_of_a_login_it_no_longer_uses(self):
        t = task(87)
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        r.refresh_me()
        stale, live = "5b37a622dd826c2437b39528e4f2d5e1", "c311f056c7fa60485963258e3c0aa840"
        auth.hold(r.store, stale)
        auth.hold(r.store, live)
        old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=12)).isoformat()
        r.store.db.execute("INSERT INTO marks VALUES (?,?,?,?,?)", (self.agent.name, "old-task", "auth_launch",
                           json.dumps({"attempt": "x", "turn": 1, "profile": stale}), old))
        r.store.mark(self.agent.name, t["id"], "auth_launch", json.dumps({"attempt": "y", "turn": 1, "profile": live}))
        shown = [h["profile"] for h in r.status()["auth_holds"]]
        self.assertEqual(shown, [live])
        self.assertEqual(sorted(h["profile"] for h in auth.status(r.store)), sorted([stale, live]))
        outer = Runner(self.cfg, lock=False)
        outer.agents, outer.store = [r], r.store                      # the runner-wide view of the same holds
        outer.write_status([])
        status = json.loads((self.cfg.state_dir / "status.json").read_text())
        self.assertEqual([h["profile"] for h in status["auth_holds"]], [live])
        self.assertNotIn(stale, json.dumps(status))

    def agent_store(self):
        from mesh_runner.store import Store
        self.cfg.state_dir.mkdir(parents=True, exist_ok=True)
        if not hasattr(self, "_store"):
            self._store = Store(self.cfg.state_dir / "alerts.sqlite")
        return self._store


if __name__ == "__main__":
    unittest.main()
