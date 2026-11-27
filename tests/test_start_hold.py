"""A headless CLI refusing to start on several cards of the host holds those launches, not the cards.

When the provider withdraws the model of the lanes' agy, the cards of several lanes are refused before any
conversation within minutes, often seconds apart. Such a start is unsent,
and a card refused twice in a row goes up with the CLI's words (RUNNER GAVE UP) with no operator
signal: in a mass refusal every card went up to a lead or into its executor's queue, the operator heard
nothing. Two different (lane, card) refusals of one harness within the window now hold that harness's
refused launch configuration: the cards stay with their executors with their unsent prompts, the operator is
told once per harness and reason, and the hold lifts with a changed configuration or a probe that starts.
"""
import datetime as dt
import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

from fakes import FakeMesh, calls, task
from mesh_runner import operator_alert, start_hold
from mesh_runner.config import AgentConfig
from mesh_runner.runner import AgentRunner, START_REFUSED, startup_error_excerpt
from mesh_runner.store import Store
from test_headless_start_refused import (FRESH, REFUSED, REFUSED_STDERR, ZCODE_STDERR, HeadlessStartRefused,
                                         MODEL_ERROR)


class StartHold(HeadlessStartRefused):

    def setUp(self):
        super().setUp()
        self.cards = {}
        self.clock = None
        real = start_hold._now
        patcher = mock.patch.object(start_hold, "_now", lambda now=None: now or self.clock or real(None))
        patcher.start()
        self.addCleanup(patcher.stop)

    def refuse(self, key, stream=REFUSED, stderr=REFUSED_STDERR, harness="agy", returncode=1):
        """The next start of card ``key`` (a new card the first time) ended as ``stream``/``stderr``; the
        runner reads it. Returns the attempt after the read."""
        self.a, self.t = self.cards.get(key, (None, None))
        self.launch(stream, stderr, harness=harness, returncode=returncode)
        # the launch carries the fingerprint of its configuration, as _spawn writes it
        self.intent["start_fp"] = self.r._start_fingerprint(harness, self.t["id"])
        self.assertTrue(self.r.store.update(self.a, wait={**self.pending, "interactive_prompt_intent": self.intent}))
        self.a = self.r.store.get(self.a.id)
        self.cards[key] = (self.a, self.t)
        result = self.reconcile()
        return result, self.r.store.get(self.a.id)

    def all_bodies(self):
        return " ".join(c["body"] for cs in self.mesh.comment_log.values() for c in cs)

    def hold_alerts(self):
        return [e for e in self.alerts() if e.get("incident") == "harness_start_hold"]

    def snapshot(self):
        return {tid: (t.get("assignee_id"), t.get("status_id")) for tid, t in self.mesh.tasks.items()}

    # -- AC5: a mass refusal gives exactly one alert and no GAVE UP ------------------------------------------

    def test_a_mass_refusal_gives_one_alert_and_no_gave_up(self):
        before = None
        for _ in range(3):                                   # more starts per card than max_startup_failures
            before = before or (self.snapshot() if self.cards else None)
            for key in "ABCD":
                ok, a = self.refuse(key)
                self.assertFalse(ok)
                self.assertEqual(a.state, "ready")            # the card stays with its executor
                self.assertTrue(a.wait["interactive_prompt_intent"]["retryable"])
                self.assertEqual(a.wait["interactive_prompt_intent"]["prompt"], self.PROMPT)
        alerts = self.hold_alerts()
        self.assertEqual(len(alerts), 1, alerts)
        self.assertEqual(len(self.alerts()), 1)               # and no other operator alert
        self.assertIn("agy CLI refuses to start", alerts[0]["text"])
        self.assertIn("invalid model selection", alerts[0]["text"])   # the CLI's words, masked excerpt
        bodies = self.all_bodies()
        self.assertNotIn("RUNNER GAVE UP", bodies)
        self.assertEqual(self.emitted("prompt_ended_by_start_refusal"), [])
        self.assertEqual(self.emitted("prompt_delivery_unknown"), [])
        self.assertEqual(self.snapshot(), before)            # no hand-up: assignee and status unchanged
        for a, t in self.cards.values():
            self.assertEqual(self.r.store.get(a.id).state, "ready")
            self.assertFalse((self.r.store.mark(self.agent.name, t["id"], START_REFUSED) or ("",))[0])
        held = start_hold.status(self.r.store)
        self.assertEqual([h["harness"] for h in held], ["agy"])
        self.assertIn("invalid model selection", held[0]["reasons"][0])

    def test_a_then_a_then_b_holds_before_the_first_card_goes_up(self):
        ok, a = self.refuse("A")
        # the first refusal of a card waits a pause before its retry: another card may show the host's fault
        self.assertGreater(self.r._resume_hold.get(a.id, 0), 0)
        self.assertEqual(start_hold.active(self.r.store, "agy"), {})
        self.refuse("B")                                     # another card refused within the window
        self.assertTrue(start_hold.active(self.r.store, "agy"))
        ok, a = self.refuse("A")                             # the card's second refusal in a row
        self.assertEqual(a.state, "ready")
        self.assertNotIn("RUNNER GAVE UP", self.all_bodies())
        self.assertEqual(len(self.hold_alerts()), 1)

    def test_two_lanes_refused_once_each_open_one_hold(self):
        other = AgentConfig(name="second", key="k2", harness="claude", profile_dir=self.agent.profile_dir,
                            repos=self.agent.repos, env=self.agent.env, alt=self.agent.alt)
        t2 = task(40, cat="in_progress")
        mesh2 = FakeMesh([t2])
        r2 = AgentRunner(self.cfg, other, self.r.store, mesh=mesh2, adapter=self.adapter)
        self.__dict__.setdefault("_runners", []).append(r2)
        r2.me = mesh2.me()
        r2.alt_adapters = self.r.alt_adapters
        self.refuse("A")
        self.assertEqual(start_hold.active(self.r.store, "agy"), {})
        # the same refusal on the other lane of the host
        fp = r2._start_fingerprint("agy", t2["id"])
        self.assertEqual(fp, self.r._start_fingerprint("agy", self.t["id"]))   # one configuration, two lanes
        held, added = start_hold.observe(r2.store, "agy", other.name, t2["id"], fp,
                                         startup_error_excerpt(MODEL_ERROR))
        self.assertTrue(held)
        self.assertEqual(sorted(held["cards"]), sorted([f"builder/{self.t['id']}", f"second/{t2['id']}"]))
        self.assertEqual(r2._signal_start_hold("agy", held, added), 1)
        self.assertEqual(self.r._signal_start_hold("agy", held, added), 0)   # one alert for the host

    # -- AC1: one card never holds the host --------------------------------------------------------------------

    def test_one_card_refused_again_and_again_holds_nothing(self):
        self.refuse("A")
        ok, a = self.refuse("A")
        self.assertIn(a.state, ("failed", "completed"))        # the card goes up with the CLI's words
        self.assertIn("RUNNER GAVE UP", self.all_bodies())
        self.assertEqual(start_hold.active(self.r.store, "agy"), {})
        self.assertEqual(self.hold_alerts(), [])
        other = self.r.store.create(self.agent.name, task(50)["id"], "agy", "other")
        self.assertFalse(self.r._start_refusal_held(other, "agy"))   # another card's launch goes ahead

    def test_the_pause_after_a_first_refusal_outlives_a_runner_restart(self):
        ok, a = self.refuse("A")
        self.assertEqual(a.state, "ready")
        self.assertGreater(self.r._resume_hold_until(a), time.monotonic())
        restarted = AgentRunner(self.cfg, self.agent, self.r.store, mesh=self.mesh, adapter=self.adapter)
        self.__dict__.setdefault("_runners", []).append(restarted)
        left = restarted._resume_hold_until(a) - time.monotonic()
        self.assertGreater(left, start_hold.PAUSE_SECONDS - 30)   # the same pause, read back from the store
        self.assertLessEqual(left, start_hold.PAUSE_SECONDS)
        # control: an attempt the pause was not written for is not held by it
        other = self.r.store.create(self.agent.name, task(51)["id"], "agy", "other")
        self.assertEqual(restarted._resume_hold_until(other), 0)

    def test_refusals_outside_the_window_open_no_hold(self):
        t0 = dt.datetime(2026, 11, 26, 17, 50, tzinfo=dt.timezone.utc)
        self.clock = t0
        self.refuse("A")
        self.clock = t0 + dt.timedelta(minutes=start_hold.WINDOW_MINUTES + 1)
        self.refuse("B")
        self.assertEqual(start_hold.active(self.r.store, "agy"), {})
        self.assertEqual(self.hold_alerts(), [])
        self.clock = t0 + dt.timedelta(minutes=start_hold.WINDOW_MINUTES + 2)
        self.refuse("C")                                      # positive control: within the window of B
        self.assertTrue(start_hold.active(self.r.store, "agy"))

    def test_other_harnesses_are_not_held(self):
        self.refuse("A")
        self.refuse("B")
        other = self.r.store.create(self.agent.name, task(51)["id"], "zcode", "other")
        self.assertFalse(self.r._start_refusal_held(other, "zcode"))
        self.assertFalse(self.r._start_refusal_held(other, "codex"))

    # -- AC3: one alert per harness and reason -----------------------------------------------------------------

    def test_a_second_reason_on_the_same_harness_is_one_more_alert(self):
        self.refuse("A")
        self.refuse("B")
        self.refuse("C", stream=[], stderr="error: unknown flag '--x'\n")
        self.refuse("D", stream=[], stderr="error: unknown flag '--x'\n")
        texts = [e["text"] for e in self.hold_alerts()]
        self.assertEqual(len(texts), 2, texts)
        self.assertIn("unknown flag", texts[1])

    def test_volatile_ids_in_the_cli_words_are_one_reason(self):
        self.refuse("A", stream=[], stderr=ZCODE_STDERR, harness="zcode")
        self.refuse("B", stream=[], stderr=ZCODE_STDERR.replace("0000000000b2", "0000000011b2"), harness="zcode")
        self.refuse("C", stream=[], stderr=ZCODE_STDERR.replace("0000000000b2", "0000000033b2"), harness="zcode")
        self.assertEqual(len(self.hold_alerts()), 1)
        self.assertNotIn("RUNNER GAVE UP", self.all_bodies())

    # -- AC4: the hold lifts with a changed configuration or a probe, not with a changed binary --------------

    def held_pair(self):
        self.refuse("A")
        self.refuse("B")
        a = self.r.store.get(self.cards["A"][0].id)
        self.assertTrue(self.r._start_refusal_held(a, "agy"))
        return a

    def test_a_held_launch_is_not_made_and_keeps_the_card(self):
        a = self.held_pair()
        before = self.snapshot()
        current = self.r.store.get(a.id)
        self.assertEqual(current.state, "ready")
        self.assertEqual(current.wait["interactive_prompt_intent"]["prompt"], self.PROMPT)
        self.assertGreater(self.r._resume_hold.get(a.id, 0), 0)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(len(self.hold_alerts()), 1)           # admission meets the hold: no second alert

    def test_a_changed_binary_does_not_lift_the_hold(self):
        a = self.held_pair()
        binary = Path(self.r.alt_adapters["agy"].binary)
        binary.write_text("#!/bin/sh\necho upgraded\n")          # the file at the same path changed
        os.utime(binary, (1, 1))
        self.assertTrue(self.r._start_refusal_held(a, "agy"))

    def test_another_model_is_not_held(self):
        a = self.held_pair()
        self.r.alt_adapters["agy"].extra_args = ["--model", "gemini-4-hydrogen-xhigh"]   # lane config fixed
        self.assertFalse(self.r._start_refusal_held(a, "agy"))
        self.r.alt_adapters["agy"].extra_args = []
        self.assertTrue(self.r._start_refusal_held(a, "agy"))  # control: the refused configuration still is
        self.r.store.mark(self.agent.name, a.task_id, "model", "gemini-3.8-flash-low")
        self.r.store.mark(self.agent.name, a.task_id, "override_harness", "agy")
        self.assertFalse(self.r._start_refusal_held(a, "agy"))  # the card's own model override

    def test_an_effort_the_lane_policy_chooses_anew_is_not_held_under_the_old_one(self):
        # the lane's two-step effort policy: the base level, one step up after a rejected review. The level is
        # chosen further on in the launch and read from a card mark: admission must see the new one.
        self.agent.default_effort, self.agent.escalate_effort = "medium", "high"
        harness = self.agent.harness
        a = self.held_pair()
        self.r._effort_choice(a, [])                                   # the last launch ran the base level
        base = self.r._start_fingerprint(harness, a.task_id)
        for card in ("other-1", "other-2"):
            start_hold.observe(self.r.store, harness, "lane-x", card, base, MODEL_ERROR)
        self.assertTrue(self.r._start_refusal_held(a, harness))      # control: the base level is held
        self.mesh.add_comment(a.task_id, "Verify: DO-NOT-SHIP @" + "a" * 40, author="reviewer")
        self.assertFalse(self.r._start_refusal_held(a, harness))     # this launch runs one step up
        self.assertNotEqual(self.r._start_fingerprint(harness, a.task_id), base)

    def test_one_probe_per_interval_and_its_conversation_lifts_the_hold(self):
        t0 = dt.datetime(2026, 11, 26, 17, 50, tzinfo=dt.timezone.utc)
        self.clock = t0
        a = self.held_pair()
        b = self.r.store.get(self.cards["B"][0].id)
        self.clock = t0 + dt.timedelta(minutes=start_hold.PROBE_MINUTES + 1)
        self.assertFalse(self.r._start_refusal_held(a, "agy"))   # the probe goes through
        self.assertTrue(self.r._start_refusal_held(b, "agy"))    # single flight: the next one is held
        # a refused probe: no new alert, still held, the card not given up
        ok, a = self.refuse("A")
        self.assertEqual(a.state, "ready")
        self.assertTrue(self.r._start_refusal_held(b, "agy"))
        self.assertEqual(len(self.hold_alerts()), 1)
        # the next probe starts a conversation: the hold of that configuration lifts for every card
        self.clock = t0 + dt.timedelta(minutes=2 * start_hold.PROBE_MINUTES + 1)
        self.assertFalse(self.r._start_refusal_held(self.r.store.get(a.id), "agy"))
        ok, a = self.refuse("A", stream=FRESH, stderr="", returncode=0)
        self.assertTrue(ok)
        self.assertEqual(start_hold.active(self.r.store, "agy"), {})
        self.assertEqual(self.details("start_hold_lifted"), ["agy"])
        self.assertFalse(self.r._start_refusal_held(self.r.store.get(b.id), "agy"))
        self.assertEqual(start_hold.status(self.r.store), [])

    def test_no_probe_in_the_first_interval(self):
        t0 = dt.datetime(2026, 11, 26, 17, 50, tzinfo=dt.timezone.utc)
        self.clock = t0
        a = self.held_pair()
        self.clock = t0 + dt.timedelta(minutes=start_hold.PROBE_MINUTES - 1)
        self.assertTrue(self.r._start_refusal_held(a, "agy"))

    def test_a_configuration_nobody_launches_drops_out(self):
        t0 = dt.datetime(2026, 11, 26, 17, 50, tzinfo=dt.timezone.utc)
        self.clock = t0
        self.held_pair()
        self.clock = t0 + dt.timedelta(hours=start_hold.STALE_HOURS, minutes=1)
        self.assertEqual(start_hold.active(self.r.store, "agy"), {})
        self.assertEqual(start_hold.status(self.r.store), [])

    # -- the lane's route moved under a held unsent prompt -----------------------------------------------------

    def test_a_route_change_carries_the_unsent_prompt_to_the_new_harness(self):
        a = self.held_pair()
        delivery = self.r.store.get(a.id).wait["ci_delivery"]
        # the lane routes to its claude harness again (fake CLI): the prompt reached no model on agy
        self.r._resume_one(self.r.store.get(a.id))
        current = self.r.store.get(a.id)
        self.assertEqual(self.details("prompt_retry_scope_changed"), [])
        self.assertEqual(current.state, "running")
        self.assertEqual(current.harness, "claude")
        pending = current.wait["interactive_prompt_intent"]
        self.assertEqual(pending["prompt"].count(self.PROMPT), 1)
        self.assertIn("refused to start before any conversation", pending["prompt"])
        self.assertEqual(current.wait["ci_delivery"], delivery)   # the pending wake rides on, undelivered
        self.assertEqual(self.r.procs[a.id].wait(timeout=10), 0)
        self.assertIn(self.PROMPT, calls(self.calls)[0]["prompt"])

    def test_a_route_change_without_a_refused_start_still_goes_to_the_owner(self):
        # control: a launch that is not a proven refused start never moves to another harness
        a = self.held_pair()
        Path(self.intent["log_path"]).with_suffix(".stderr").write_text("")
        Path(self.intent["log_path"]).write_text(json.dumps(FRESH[0]) + "\n")
        self.r._resume_one(self.r.store.get(self.cards["B"][0].id))
        self.assertEqual(self.r.store.get(self.cards["B"][0].id).state, "unknown")
        self.assertEqual(self.details("prompt_retry_scope_changed"), ["workspaces-or-harness"])
        self.assertFalse(calls(self.calls))


# The lane's account routing as an operator's account-routing script answers it: `env` prints the harness of the lane's
# route and ends a start fallback once the Runner's record of the lane says it returned; `switch-codex` with
# MESH_RUNNER_LIMIT_SOURCE=start_hold follows quota_edges google -> claude (or refuses with ``rc``).
ROUTE_ENV = r'''
import json, sqlite3, sys
from pathlib import Path
db, route, lane = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
now = route.read_text().strip()
if now != "agy":
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as con:
        row = con.execute("SELECT value FROM marks WHERE agent=? AND task_id=? AND key=?",
                          (lane, "_harness_start_agy", "start_route")).fetchone()
    if row and row[0] and json.loads(row[0]).get("state") == "returned":
        route.write_text("agy")
        now = "agy"
print("MESH_RUNNER_HARNESS=" + now)
'''
ROUTE_SWITCH = r'''
import json, os, sys
from pathlib import Path
route, calls, rc = Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3])
with calls.open("a") as f:
    f.write(json.dumps({k: v for k, v in os.environ.items() if k.startswith("MESH_RUNNER_")}) + "\n")
if rc == 0:
    route.write_text("claude")
    print("fallback builder to claude/ops-team/subscription")
else:
    print("no fallback: fallback_route_not_team_chain")
raise SystemExit(rc)
'''


class StartHoldRoute(StartHold):
    """A held harness on a lane whose route has a quota_edges fallback moves the lane there, as at a
    429: one route call per lane and hold generation; the next turn runs on the fallback with the exact unsent
    prompt; the lane comes back once the hold no longer covers its configuration, and a moved lane takes the
    probe in its turn."""

    def setUp(self):
        super().setUp()
        self.route, self.route_calls = self.root / "route", self.root / "route-calls.jsonl"
        self.route.write_text("agy")
        env_py, switch_py = self.root / "route-env.py", self.root / "route-switch.py"
        env_py.write_text(ROUTE_ENV)
        switch_py.write_text(ROUTE_SWITCH)
        self.agent.env_command = [sys.executable, str(env_py), str(self.r.store.path), str(self.route),
                                  self.agent.name]
        self.switch_with(0)
        binary = Path(self.r.alt_adapters["agy"].binary)      # installed: a held launch gets as far as the hold
        binary.write_text("#!/bin/sh\nexit 1\n")
        binary.chmod(0o755)

    def switch_with(self, rc):
        self.agent.on_limit_command = [sys.executable, str(self.root / "route-switch.py"), str(self.route),
                                       str(self.route_calls), str(rc)]

    def calls_made(self):
        return [json.loads(x) for x in self.route_calls.read_text().splitlines()] if self.route_calls.exists() else []

    def resume(self, key):
        a = self.r.store.get(self.cards[key][0].id)
        self.r._resume_hold.pop(a.id, None)
        self.r._resume_one(a)
        return self.r.store.get(a.id)

    def moved(self):
        """A mass refusal opened the hold; card A's next launch met it and moved the lane."""
        for key in "ABCD":
            self.refuse(key)
        self.assertTrue(start_hold.active(self.r.store, "agy"))
        a = self.resume("A")
        self.assertEqual(self.route.read_text(), "claude")
        return a

    def no_limit_path(self):
        for a, t in self.cards.values():
            for key in ("limit_hold_n", "limit_hold_until"):
                self.assertFalse((self.r.store.mark(self.agent.name, t["id"], key) or ("",))[0], key)
        self.assertEqual(self.emitted("limit"), [])

    def lost(self, state, age):
        """The lane's route record as a Runner that died mid-call / a probe that never ended leaves it."""
        for key in "ABCD":
            self.refuse(key)
        since = start_hold.active(self.r.store, "agy")["since"]
        raw, _ = start_hold.route(self.r.store, self.agent.name, "agy")
        at = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=age)).isoformat()
        self.assertTrue(start_hold.set_route(self.r.store, self.agent.name, "agy", raw,
                                             {"since": since, "state": state, "task": "x", "at": at}))

    def test_a_call_the_runner_lost_ages_out_and_the_next_held_launch_moves_the_lane(self):
        self.lost(start_hold.CALLING, start_hold.CALL_STALE_SECONDS + 60)
        self.resume("A")
        self.assertEqual((len(self.calls_made()), self.route.read_text()), (1, "claude"))

    def test_a_probe_that_never_ended_ages_out_after_one_probe_interval(self):
        self.lost(start_hold.PROBING, start_hold.PROBE_MINUTES * 60 + 60)
        self.resume("A")
        self.assertEqual((len(self.calls_made()), self.route.read_text()), (1, "claude"))

    def test_a_call_or_probe_still_out_is_never_doubled(self):
        for state in (start_hold.CALLING, start_hold.PROBING):
            with self.subTest(state=state):
                since = "2026-11-27T09:00:00+00:00"
                fresh = {"since": since, "state": state, "at": dt.datetime.now(dt.timezone.utc).isoformat()}
                self.assertFalse(start_hold.may_move(fresh, since))
                self.assertTrue(start_hold.may_move(fresh, "2026-11-27T10:00:00+00:00"))

    # -- AC1 -----------------------------------------------------------------------------------------------------

    def test_a_mass_refusal_moves_the_lane_once_and_the_next_turn_runs_on_claude(self):
        a = self.moved()
        self.assertEqual(a.state, "ready")                     # the launch that met the hold was not made
        since = start_hold.active(self.r.store, "agy")["since"]
        for key in "BCD":                                     # every other held card: the lane's new route
            b = self.resume(key)
            self.assertEqual(b.state, "running", key)
            self.assertEqual(b.harness, "claude")
            pending = b.wait["interactive_prompt_intent"]
            self.assertEqual(pending["prompt"].count(self.PROMPT), 1)
            self.assertIn("refused to start before any conversation", pending["prompt"])
            self.assertEqual(self.r.procs[b.id].wait(timeout=10), 0)
        a = self.resume("A")                                  # and the card whose launch moved it
        self.assertEqual((a.state, a.harness), ("running", "claude"))
        self.assertEqual(a.wait["interactive_prompt_intent"]["prompt"].count(self.PROMPT), 1)
        self.assertEqual(self.r.procs[a.id].wait(timeout=10), 0)
        made = self.calls_made()
        self.assertEqual(len(made), 1, made)                  # one route call for the lane and generation
        self.assertEqual((made[0]["MESH_RUNNER_LIMIT_SOURCE"], made[0]["MESH_RUNNER_START_HOLD_HARNESS"],
                          made[0]["MESH_RUNNER_START_HOLD_SINCE"], made[0]["MESH_RUNNER_AGENT"]),
                         ("start_hold", "agy", since, "builder"))
        self.assertNotIn("MESH_RUNNER_MODE", made[0])         # a lane's route, not one card's review
        self.assertNotIn("RUNNER GAVE UP", self.all_bodies())
        self.assertEqual(len(self.hold_alerts()), 1)
        self.assertEqual(len(self.alerts()), 1)
        self.no_limit_path()
        self.assertEqual(self.details("start_route_moved"), [])   # events are per attempt: A's own
        moved = [e for e in self.emitted("start_route") if e.get("state") == "moved"]
        self.assertEqual(len(moved), 1, moved)

    def test_a_refused_route_call_keeps_the_hold_without_a_limit_streak(self):
        self.switch_with(78)
        for key in "ABCD":
            self.refuse(key)
        for key in "AB":                                      # two held launches: one call
            a = self.resume(key)
            self.assertEqual(a.state, "ready")
            self.assertGreater(self.r._resume_hold.get(a.id, 0), 0)
        self.assertEqual(len(self.calls_made()), 1)
        self.assertEqual(self.route.read_text(), "agy")
        self.no_limit_path()
        self.assertNotIn("RUNNER GAVE UP", self.all_bodies())

    def test_a_card_on_its_own_route_never_moves_the_lane(self):
        for key in "AB":
            self.refuse(key)
        a = self.r.store.get(self.cards["A"][0].id)
        self.r._card_routed = True                            # label- or refusal-routed launch of this card
        self.assertTrue(self.r._start_refusal_held(a, "agy"))
        self.assertEqual(self.calls_made(), [])

    # -- AC3: the hold lifting brings the lane back ---------------------------------------------------------------

    def test_a_lifted_hold_returns_the_moved_lane_to_its_primary(self):
        self.moved()
        self.r._fresh_env(self.cards["B"][1]["id"])
        self.assertEqual(self.r._routed_harness, "claude")    # still held: the lane stays on its fallback
        # another lane's probe of the same configuration got a conversation: the hold of it lifts
        fp = self.r._start_fingerprint("agy", self.cards["A"][1]["id"])
        self.assertTrue(start_hold.succeeded(self.r.store, "agy", fp))
        self.r._fresh_env(self.cards["B"][1]["id"])
        self.assertEqual(self.route.read_text(), "agy")
        self.assertEqual(self.r._routed_harness, "agy")
        self.assertEqual([e["cause"] for e in self.emitted("start_route") if e.get("state") == "returned"],
                         ["lifted"])

    def test_a_changed_configuration_returns_the_moved_lane(self):
        self.moved()
        self.r.alt_adapters["agy"].extra_args = ["--model", "gemini-4-hydrogen-xhigh"]   # lane config fixed
        self.r._fresh_env(self.cards["B"][1]["id"])
        self.assertEqual(self.r._routed_harness, "agy")
        self.assertFalse(self.r._start_refusal_held(self.r.store.get(self.cards["B"][0].id), "agy"))

    def test_a_stale_hold_returns_the_moved_lane(self):
        t0 = dt.datetime(2026, 11, 26, 17, 50, tzinfo=dt.timezone.utc)
        self.clock = t0
        self.moved()
        self.clock = t0 + dt.timedelta(hours=start_hold.STALE_HOURS, minutes=1)
        self.r._fresh_env(self.cards["B"][1]["id"])
        self.assertEqual(self.r._routed_harness, "agy")

    def test_a_moved_lane_takes_the_probe_and_a_refused_probe_moves_it_again(self):
        t0 = dt.datetime(2026, 11, 26, 17, 50, tzinfo=dt.timezone.utc)
        self.clock = t0
        self.moved()
        self.clock = t0 + dt.timedelta(minutes=start_hold.PROBE_MINUTES + 1)
        b = self.r.store.get(self.cards["B"][0].id)
        self.r._fresh_env(b.task_id)                          # nobody on agy any more: the moved lane probes
        self.assertEqual(self.r._routed_harness, "agy")
        self.assertFalse(self.r._start_refusal_held(b, "agy"))   # its launch is the probe
        self.assertEqual(self.details("start_hold_probe"), [])    # (details read the last refused card's)
        c = self.r.store.get(self.cards["C"][0].id)
        self.assertTrue(self.r._start_refusal_held(c, "agy"))   # single flight: the lane waits for its probe
        self.assertEqual(len(self.calls_made()), 1)
        ok, b = self.refuse("B")                              # the probe is refused too
        self.assertEqual(b.state, "ready")
        self.assertTrue(self.r._start_refusal_held(self.r.store.get(c.id), "agy"))
        self.assertEqual(len(self.calls_made()), 2)           # the next held launch moves the lane again
        self.assertEqual(self.route.read_text(), "claude")
        self.r._fresh_env(b.task_id)                          # same probe interval: no second probe
        self.assertEqual(self.r._routed_harness, "claude")
        self.assertEqual(len(self.hold_alerts()), 1)


class StartHoldModule(HeadlessStartRefused):
    """The record itself: concurrent lanes, reasons, alert slots."""

    def test_every_concurrent_observation_counts(self):
        store = self.r.store
        for i in range(5):
            start_hold.observe(store, "agy", f"lane{i}", f"card{i}", "fp", "error: model gone")
        hold = start_hold.active(store, "agy")
        self.assertEqual(len(hold["cards"]), 2)               # opened by the second, the rest joined it
        raw = json.loads(store.mark(start_hold.OWNER, start_hold.PREFIX + "agy", start_hold.KEY)[0])
        self.assertEqual(len(raw["obs"]), 5)

    def test_the_alert_is_once_per_slot(self):
        store = self.r.store
        self.assertTrue(operator_alert.signal(store, "start:agy:x", "2026-01-01T00:00:00", "t",
                                              repeat_hours=float("inf")))
        self.assertFalse(operator_alert.signal(store, "start:agy:x", "2026-01-01T00:00:00", "t",
                                               repeat_hours=float("inf")))


def load_tests(loader, tests, pattern):
    """Only the tests of this file: the fixture class imported above is collected in its own module."""
    suite = unittest.TestSuite()
    for cls in (StartHold, StartHoldRoute, StartHoldModule):
        for name in sorted(n for n in vars(cls) if n.startswith("test_")):
            suite.addTest(cls(name))
    return suite
