import json
import unittest

import os
import tempfile

from mesh_runner.execution_observations import (DENY_SECONDS, HEARTBEAT_SECONDS, Identity, Publisher,
                                                ResolvedRoute, Router, RunnerHook, Transport, binding_lookup,
                                                iso, launch_route)

WS = "11111111-2222-3333-4444-555555555555"
PROJECT = "22222222-2222-3333-4444-555555555555"
AGENT = "33333333-2222-3333-4444-555555555555"
TASK = "44444444-2222-3333-4444-555555555555"
NOW = 1_800_000_000.0
OBSERVED_KEYS = {"harness", "model", "effort", "source", "observed_at"}
ATTEMPT_KEYS = {"workspace_id", "project_id", "agent_id", "binding_ref", "task_id", "controller_ref", "instance_id",
                "generation", "attempt_id", "turn_id", "state", "observed", "resolved_route", "provenance"}
FORBIDDEN = ("env", "argv", "output", "path", "credential_ref", "token", "key", "prompt", "email")


class Clock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now


class FakeServer:
    """Fixture server for the two telemetry endpoints; enforces ordering the way the contract says."""

    def __init__(self):
        self.generation = 0
        self.instances = {}
        self.reports = []
        self.last_seq = {}
        self.script = []          # codes forced for the next report posts
        self.instance_posts = 0
        self.refused_projects = set()   # projects without the Runner integration: Mesh answers 403

    def post_instance(self, body):
        self.instance_posts += 1
        if set(body) != {"schema_version", "instance_id"}:
            return 400, None
        if body["instance_id"] not in self.instances:
            self.generation += 1
            self.instances[body["instance_id"]] = self.generation
        return 200, {"generation": self.instances[body["instance_id"]]}

    def post_report(self, body):
        if self.script:
            code = self.script.pop(0)
            if code != 200:
                return code, None
        if body["generation"] != max(self.instances.values()):
            return 409, None                        # a newer boot fenced this instance
        if any(a["project_id"] in self.refused_projects for a in body["attempts"]):
            return 403, None                        # whole batch refused, nothing recorded
        if body["sequence"] <= self.last_seq.get(body["instance_id"], 0):
            return 409, None
        for a in body["attempts"]:       # strict wire shape of the API ExecutionAttemptReport
            if set(a["observed"]) - OBSERVED_KEYS or set(a) - ATTEMPT_KEYS:
                return 400, None
        self.last_seq[body["instance_id"]] = body["sequence"]
        self.reports.append(json.loads(json.dumps(body)))
        return 200, {"recorded": True}


def ident(n=1, task=TASK):
    return Identity(WS, PROJECT, AGENT, "lane-a", task, f"attempt-{n}")


def make(server=None, clock=None, **kw):
    server = server or FakeServer()
    clock = clock or Clock()
    return Publisher(server, "mesh-runner", clock, sleep=lambda _s: None, **kw), server, clock


def rows(server):
    return [a for r in server.reports for a in r["attempts"]]


class NoUsageTest(unittest.TestCase):
    def test_no_observation_publishes_null_unknown_never_the_configured_model(self):
        pub, srv, _ = make()
        route = ResolvedRoute("local_resolver", 4, harness="codex", model="gpt-configured")
        pub.begin_turn(ident(), harness="codex", resolved=route)
        self.assertEqual(pub.flush(), "sent")
        obs = rows(srv)[0]["observed"]
        self.assertEqual((obs["harness"], obs["model"], obs["effort"], obs["source"]), ("codex", None, None, "adapter_launch"))
        self.assertTrue(obs["observed_at"])          # the deployed API rejects a null source/time even for unknown model
        self.assertEqual(rows(srv)[0]["resolved_route"]["model"], "gpt-configured")   # separate field

    def test_real_observation_carries_source_and_time(self):
        pub, srv, clock = make()
        turn = pub.begin_turn(ident(), harness="claude")
        clock.now += 3
        self.assertTrue(pub.observe("attempt-1", turn, model="claude-opus-4", effort="high"))
        pub.flush()
        obs = rows(srv)[0]["observed"]
        self.assertEqual((obs["model"], obs["effort"], obs["source"]), ("claude-opus-4", "high", "adapter_stream"))
        self.assertEqual(obs["observed_at"], iso(NOW + 3))

    def test_model_and_effort_seen_at_different_times_use_attempt_level_provenance(self):
        pub, srv, clock = make()
        turn = pub.begin_turn(ident(), harness="codex")
        clock.now += 1
        pub.observe("attempt-1", turn, model="gpt-6.1-sol", effort="low")
        clock.now += 2
        pub.observe("attempt-1", turn, effort="high")
        self.assertEqual(pub.flush(), "sent")
        row = rows(srv)[0]
        self.assertNotIn("provenance", row["observed"])
        self.assertEqual(row["provenance"]["model"]["observed_at"], iso(NOW + 1))
        self.assertEqual(row["provenance"]["effort"]["observed_at"], iso(NOW + 3))
        self.assertEqual((row["observed"]["model"], row["observed"]["effort"]), ("gpt-6.1-sol", "high"))

    def test_profile_usage_and_env_are_not_sources(self):
        pub, _, _ = make()
        turn = pub.begin_turn(ident(), harness="claude")
        for source in ("agent_profile", "usage", "env", "session_report", "configured"):
            self.assertFalse(pub.observe("attempt-1", turn, model="m", source=source))

    def test_future_observation_time_is_clamped_to_now(self):
        pub, srv, _ = make()
        turn = pub.begin_turn(ident(), harness="claude")
        pub.observe("attempt-1", turn, model="m1", at=NOW + 9999)
        pub.flush()
        self.assertEqual(rows(srv)[0]["observed"]["observed_at"], iso(NOW))


class ResumeTest(unittest.TestCase):
    def test_resume_clears_the_previous_model_until_a_new_observation(self):
        pub, srv, _ = make()
        t1 = pub.begin_turn(ident(), harness="claude")
        pub.observe("attempt-1", t1, model="old-model", effort="low")
        pub.flush()
        t2 = pub.begin_turn(ident(), harness="claude")
        self.assertNotEqual(t1, t2)
        pub.flush()
        obs = rows(srv)[-1]["observed"]
        self.assertEqual((rows(srv)[-1]["turn_id"], obs["model"], obs["effort"]), (t2, None, None))
        self.assertFalse(pub.observe("attempt-1", t1, model="late-old"))      # late report of the old turn
        pub.observe("attempt-1", t2, model="new-model")
        pub.flush()
        self.assertEqual(rows(srv)[-1]["observed"]["model"], "new-model")

    def test_two_attempts_with_different_models_stay_separate(self):
        pub, srv, _ = make()
        ta = pub.begin_turn(ident(1), harness="claude")
        tb = pub.begin_turn(ident(2), harness="codex")
        pub.observe("attempt-1", ta, model="model-a")
        pub.observe("attempt-2", tb, model="model-b")
        pub.flush()
        got = {a["attempt_id"]: (a["observed"]["harness"], a["observed"]["model"]) for a in rows(srv)}
        self.assertEqual(got, {"attempt-1": ("claude", "model-a"), "attempt-2": ("codex", "model-b")})


class IdentityAndLifecycleTest(unittest.TestCase):
    def test_row_identity_sequence_generation_and_instance(self):
        pub, srv, _ = make()
        pub.begin_turn(ident(), harness="claude")
        pub.flush()
        rep = srv.reports[0]
        row = rep["attempts"][0]
        self.assertEqual((rep["schema_version"], rep["generation"], rep["sequence"]), (1, 1, 1))
        for key, want in (("workspace_id", WS), ("project_id", PROJECT), ("agent_id", AGENT),
                          ("binding_ref", "lane-a"), ("task_id", TASK), ("controller_ref", "mesh-runner"),
                          ("instance_id", pub.instance_id), ("generation", 1), ("state", "starting")):
            self.assertEqual(row[key], want, key)

    def test_heartbeat_is_30s_and_idle_between(self):
        pub, srv, clock = make()
        pub.begin_turn(ident(), harness="claude")
        pub.flush()
        self.assertFalse(pub.due())
        clock.now += HEARTBEAT_SECONDS - 1
        self.assertFalse(pub.due())
        clock.now += 1
        self.assertTrue(pub.due())
        pub.heartbeat("attempt-1")
        pub.flush()
        self.assertEqual([r["sequence"] for r in srv.reports], [1, 2])

    def test_completed_is_explicit_delivered_once_then_forgotten(self):
        pub, srv, _ = make()
        pub.begin_turn(ident(), harness="claude")
        pub.finish("attempt-1", "completed")
        pub.flush()
        self.assertEqual(rows(srv)[-1]["state"], "completed")
        self.assertEqual(pub.flush(), "idle")

    def test_invalid_identity_is_not_published(self):
        pub, srv, _ = make()
        bad = Identity("not-a-uuid", PROJECT, AGENT, "lane-a", TASK, "attempt-1")
        self.assertIsNone(pub.begin_turn(bad, harness="claude"))
        self.assertEqual(pub.flush(), "idle")
        self.assertEqual(srv.reports, [])


class OfflineMeshTest(unittest.TestCase):
    def test_bounded_retry_and_no_exception_into_execution(self):
        pub, srv, clock = make()
        pub.begin_turn(ident(), harness="claude")
        srv.script = [None, None, None]                  # transport failure on every attempt
        self.assertEqual(pub.flush(), "transport")
        self.assertEqual(srv.reports, [])
        self.assertEqual(pub.flush(), "backoff")         # not again before the next interval
        clock.now += HEARTBEAT_SECONDS
        self.assertEqual(pub.flush(), "sent")            # the very same pending batch goes again
        self.assertEqual(srv.reports[0]["sequence"], 1)

    def test_replay_after_lost_answer_is_the_same_body_and_sequence(self):
        pub, srv, clock = make()
        pub.begin_turn(ident(), harness="claude")
        srv.script = [500, 500, 500]
        pub.flush()
        clock.now += 50
        pub.flush()
        self.assertEqual(len(srv.reports), 1)
        self.assertEqual(srv.reports[0]["reported_at"], iso(NOW))         # not re-stamped

    def test_offline_traffic_is_one_batch_per_heartbeat_interval_and_old_backlog_expires(self):
        class Down:
            posts = 0

            def post_instance(self, body):
                return 200, {"generation": 1}

            def post_report(self, body):
                Down.posts += 1
                return None, None
        clock = Clock()
        pub = Publisher(Down(), "mesh-runner", clock, sleep=lambda _s: None)
        pub.begin_turn(ident(), harness="claude")
        hook = RunnerHook(pub, WS, lambda _a: "lane-a", lambda _t: PROJECT)
        for _ in range(60):                           # a five second loop for five minutes
            hook.tick()
            clock.now += 5
        self.assertLessEqual(Down.posts, 3 * 11)      # three sends per 30 s interval, not per tick
        pub._retry_at = 0.0
        pub.flush()
        # an expired pending batch is rebuilt from live rows (fresh reported_at, newer sequence)
        self.assertGreater(pub.sequence, 1)

    def test_transport_that_raises_is_swallowed(self):
        class Boom:
            def post_instance(self, body):
                raise OSError("down")

            def post_report(self, body):
                raise OSError("down")
        pub = Publisher(Boom(), "mesh-runner", Clock(), sleep=lambda _s: None)
        pub.begin_turn(ident(), harness="claude")
        self.assertEqual(pub.flush(), "error")


class InFlightRaceTest(unittest.TestCase):
    def test_terminal_update_made_while_a_post_is_in_flight_is_not_lost(self):
        pub, srv, _ = make()
        pub.begin_turn(ident(), harness="claude")
        real = srv.post_report

        def racing(body):
            pub.finish("attempt-1", "completed")      # the attempt ends while this POST is on the wire
            return real(body)
        srv.post_report = racing
        self.assertEqual(pub.flush(), "sent")
        srv.post_report = real
        self.assertEqual([a["state"] for a in rows(srv)], ["starting"])
        self.assertEqual(pub.flush(), "sent")          # the terminal state still goes out
        self.assertEqual([a["state"] for a in rows(srv)], ["starting", "completed"])
        self.assertEqual(pub.flush(), "idle")

    def test_observation_made_in_flight_is_carried_by_the_next_batch(self):
        pub, srv, _ = make()
        turn = pub.begin_turn(ident(), harness="claude")
        real = srv.post_report

        def racing(body):
            pub.observe("attempt-1", turn, model="m-late")
            return real(body)
        srv.post_report = racing
        pub.flush()
        srv.post_report = real
        pub.flush()
        self.assertEqual(rows(srv)[-1]["observed"]["model"], "m-late")


class RestartFencingTest(unittest.TestCase):
    def test_new_boot_gets_a_new_generation_and_inherits_nothing(self):
        srv, clock = FakeServer(), Clock()
        old, _, _ = make(srv, clock)
        t = old.begin_turn(ident(), harness="claude")
        old.observe("attempt-1", t, model="old-model")
        old.flush()
        new, _, _ = make(srv, clock)
        new.begin_turn(ident(2), harness="claude")
        new.flush()
        last = srv.reports[-1]
        self.assertEqual(last["generation"], 2)
        self.assertEqual([a["attempt_id"] for a in last["attempts"]], ["attempt-2"])
        self.assertIsNone(last["attempts"][0]["observed"]["model"])

    def test_stale_publisher_is_fenced_and_never_registers_again_or_replays_backlog(self):
        srv, clock = FakeServer(), Clock()
        old, _, _ = make(srv, clock)
        old.begin_turn(ident(), harness="claude")
        old.flush()
        new, _, _ = make(srv, clock)
        new.boot()
        old.heartbeat("attempt-1")
        old.begin_turn(ident(3), harness="claude")
        posts, seen = srv.instance_posts, len(srv.reports)
        self.assertEqual(old.flush(), "fenced")
        self.assertTrue(old.fenced)
        for _ in range(3):
            clock.now += 60
            self.assertEqual(old.flush(), "fenced")
            self.assertFalse(old.boot())
        self.assertEqual((srv.instance_posts, len(srv.reports)), (posts, seen))

    def test_same_instance_boot_is_idempotent(self):
        pub, srv, _ = make()
        pub.boot()
        again = Publisher(srv, "mesh-runner", Clock(), instance_id=pub.instance_id, sleep=lambda _s: None)
        again.boot()
        self.assertEqual((pub.generation, again.generation), (1, 1))

    def test_forbidden_is_a_scope_answer_not_a_fence(self):
        pub, srv, clock = make()
        pub.begin_turn(ident(), harness="claude")
        srv.refused_projects = {PROJECT}
        self.assertEqual(pub.flush(), "denied")
        self.assertFalse(pub.fenced)
        self.assertIsNone(pub.begin_turn(ident(2), harness="claude"))     # the refused scope is skipped
        clock.now += DENY_SECONDS + 1
        srv.refused_projects = set()                                      # the project got the Runner
        self.assertIsNotNone(pub.begin_turn(ident(3), harness="claude"))
        self.assertEqual(pub.flush(), "sent")
        self.assertEqual([r["attempt_id"] for r in rows(srv)], ["attempt-3"])

    def test_one_refused_project_does_not_silence_the_others(self):
        pub, srv, _ = make()
        other = "55555555-2222-3333-4444-555555555555"
        pub.begin_turn(ident(1), harness="claude")
        pub.begin_turn(Identity(WS, other, AGENT, "lane-a", TASK, "attempt-2"), harness="codex")
        srv.refused_projects = {other}
        self.assertEqual(pub.flush(), "isolated")
        self.assertEqual({r["attempt_id"] for r in rows(srv)}, {"attempt-1"})
        self.assertFalse(pub.fenced)
        pub.heartbeat("attempt-1")
        self.assertEqual(pub.flush(), "sent")                 # sequence stays increasing after the split
        self.assertEqual({r["project_id"] for r in rows(srv)}, {PROJECT})

    def test_conflict_is_still_a_fence(self):
        pub, srv, _ = make()
        pub.begin_turn(ident(), harness="claude")
        srv.script = [409]
        self.assertEqual(pub.flush(), "fenced")
        self.assertEqual(pub.flush(), "fenced")


class WhitelistTest(unittest.TestCase):
    def test_payload_has_no_secret_looking_fields_and_no_unsafe_values(self):
        pub, srv, _ = make()
        route = ResolvedRoute("local_resolver", 2, profile_ref="sol-high", harness="codex",
                              model="/u/me/.codex/secret", effort="high; echo pwned")
        t = pub.begin_turn(ident(), harness="codex", resolved=route)
        pub.observe("attempt-1", t, model="sk-live key=abc def", effort="medium")
        pub.flush()
        text = json.dumps(srv.reports[0])
        for word in ("/u/me", "echo pwned", "sk-live", "abc def"):
            self.assertNotIn(word, text)

        def keys(node):
            if isinstance(node, dict):
                for k, v in node.items():
                    yield k
                    yield from keys(v)
            elif isinstance(node, list):
                for v in node:
                    yield from keys(v)
        for k in keys(srv.reports[0]):
            self.assertFalse(any(f in k for f in FORBIDDEN), k)

    def test_unknown_harness_is_refused(self):
        pub, _, _ = make()
        self.assertIsNone(pub.begin_turn(ident(), harness="env-default"))


class RunnerHookTest(unittest.TestCase):
    def hook(self, binding="lane-a", project=PROJECT):
        pub, srv, clock = make()
        return RunnerHook(pub, WS, lambda _a: binding, lambda _t: project), pub, srv

    def test_stream_model_is_published_and_resume_resets_it(self):
        hook, pub, srv = self.hook()
        hook.turn_started("attempt-1", TASK, AGENT, "claude")
        hook.turn_settled("attempt-1", {"model": "claude-opus-4"})
        pub.flush()
        self.assertEqual(rows(srv)[-1]["observed"]["model"], "claude-opus-4")
        hook.turn_started("attempt-1", TASK, AGENT, "claude")
        pub.flush()
        self.assertIsNone(rows(srv)[-1]["observed"]["model"])

    def test_record_one_second_before_the_turn_is_not_this_turns(self):
        hook, pub, srv = self.hook()
        hook.turn_started("attempt-1", TASK, AGENT, "codex")
        hook.turn_settled("attempt-1", {"model": "gpt-6.1-sol", "at": iso(NOW - 1)}, source="adapter_turn_context")
        pub.flush()
        self.assertIsNone(rows(srv)[-1]["observed"]["model"])

    def test_codex_turn_context_gives_model_and_effort_stale_record_dropped(self):
        hook, pub, srv = self.hook()
        hook.turn_started("attempt-1", TASK, AGENT, "codex")
        hook.turn_settled("attempt-1", {"model": "gpt-6.1-sol", "effort": "high", "at": iso(NOW - 600)},
                          source="adapter_turn_context")
        pub.flush()
        self.assertIsNone(rows(srv)[-1]["observed"]["model"])        # a previous turn's record
        hook.turn_settled("attempt-1", {"model": "gpt-6.1-sol", "effort": "high", "at": iso(NOW)},
                          source="adapter_turn_context")
        pub.flush()
        obs = rows(srv)[-1]["observed"]
        self.assertEqual((obs["harness"], obs["model"], obs["effort"], obs["source"]),
                         ("codex", "gpt-6.1-sol", "high", "adapter_turn_context"))

    def test_attempt_end_is_sent_as_terminal_once_and_the_row_is_forgotten(self):
        for ended, wire in (("completed", "completed"), ("failed", "failed"), ("cancelled", "stopped"),
                            ("handed_back", "stopped")):
            hook, pub, srv = self.hook()
            hook.turn_started("attempt-1", TASK, AGENT, "claude")
            hook.turn_settled("attempt-1", {"model": "m"})
            pub.flush()
            hook.attempt_ended("attempt-1", ended)
            pub.flush()
            self.assertEqual(rows(srv)[-1]["state"], wire)
            self.assertEqual(pub.flush(), "idle")                     # no heartbeat of a finished attempt
            self.assertFalse(pub.observe("attempt-1", "attempt-1:t1", model="late"))

    def test_no_stream_model_stays_null(self):
        hook, pub, srv = self.hook()
        hook.turn_started("attempt-1", TASK, AGENT, "codex")
        hook.turn_settled("attempt-1", {})
        pub.flush()
        self.assertIsNone(rows(srv)[-1]["observed"]["model"])

    def test_unknown_binding_or_project_publishes_nothing(self):
        for hook, pub, srv in (self.hook(binding=None), self.hook(project=None)):
            hook.turn_started("attempt-1", TASK, AGENT, "claude")
            self.assertEqual(pub.flush(), "idle")
            self.assertEqual(srv.reports, [])

    def test_idle_tick_registers_the_boot_so_a_new_boot_fences_the_old_one_at_once(self):
        srv, clock = FakeServer(), Clock()
        old, _, _ = make(srv, clock)
        old.begin_turn(ident(), harness="claude")
        old.flush()
        new, _, _ = make(srv, clock)
        RunnerHook(new, WS, lambda _a: "lane-a", lambda _t: PROJECT).tick()      # no attempt at all
        self.assertEqual(new.generation, 2)
        old.heartbeat("attempt-1")
        self.assertEqual(old.flush(), "fenced")

    def test_failed_boot_is_retried_at_heartbeat_pace_not_every_tick(self):
        srv, clock = FakeServer(), Clock()
        pub, _, _ = make(srv, clock)
        hook = RunnerHook(pub, WS, lambda _a: "lane-a", lambda _t: PROJECT)
        srv.instance_posts = 0
        orig = srv.post_instance
        srv.post_instance = lambda body: (srv.__setattr__("instance_posts", srv.instance_posts + 1), (None, None))[1]
        for _ in range(5):
            hook.tick()
        self.assertEqual(srv.instance_posts, 1)
        clock.now += HEARTBEAT_SECONDS
        hook.tick()
        self.assertEqual(srv.instance_posts, 2)
        srv.post_instance = orig
        clock.now += HEARTBEAT_SECONDS
        hook.tick()
        self.assertEqual(pub.generation, 1)

    def test_a_failing_lookup_never_raises_into_the_runner(self):
        pub, _, _ = make()
        hook = RunnerHook(pub, WS, lambda _a: 1 / 0, lambda _t: PROJECT)
        hook.turn_started("attempt-1", TASK, AGENT, "claude")
        hook.turn_settled("attempt-1", {"model": "m"})
        hook.tick()


class TransportTest(unittest.TestCase):
    def test_paths_and_bodies(self):
        class Client:
            base = "https://mesh.example.com/api/v1/workspaces/" + WS + "/runtime/controllers/"
            calls = []

            def _exchange(self, method, url, body=None, etag=None):
                self.calls.append((method, url, body))
                return 200, None, {"generation": 1}
        c = Client()
        t = Transport(c, "mesh-runner")
        t.post_instance({"schema_version": 1, "instance_id": "i"})
        t.post_report({"x": 1})
        self.assertTrue(c.calls[0][1].endswith("/mesh-runner/execution-instances"))
        self.assertTrue(c.calls[1][1].endswith("/mesh-runner/execution-reports"))
        with self.assertRaises(ValueError):
            Transport(c, "../x")



class SharedRunnerTest(unittest.TestCase):
    """The shared Runner: workspaces from the catalog binding, route from the launch argv."""

    def desired(self, bindings):
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: [os.remove(os.path.join(d, f)) for f in os.listdir(d)] and os.rmdir(d))
        with open(os.path.join(d, "desired.json"), "w") as fh:
            json.dump({"catalog": {"bindings": bindings}}, fh)
        return d

    def test_binding_lookup_takes_enabled_bindings_of_its_workspace_only(self):
        other_ws = "99999999-2222-3333-4444-555555555555"
        d = self.desired({
            "lane-a": {"enabled": True, "binding": {"agent_id": AGENT, "workspace_id": WS}},
            "lane-off": {"enabled": False, "binding": {"agent_id": TASK, "workspace_id": WS}},
            "lane-x": {"enabled": True, "binding": {"agent_id": PROJECT, "workspace_id": other_ws}},
        })
        look = binding_lookup(d, WS)
        self.assertEqual(look(AGENT), "lane-a")
        self.assertIsNone(look(TASK))
        self.assertIsNone(look(PROJECT))
        self.assertIsNone(binding_lookup(d + "-missing", WS)(AGENT))

    def test_launch_route_reads_the_native_command_line(self):
        r = launch_route("claude", ["claude", "--chrome", "--model", "claude-opus-5-5", "--effort", "high", "-p"])
        self.assertEqual((r.source, r.harness, r.model, r.effort), ("runner_launch", "claude", "claude-opus-5-5", "high"))
        r = launch_route("codex", ["codex", "-c", 'model_reasoning_effort="xhigh"', "-p", "sol"], "gpt-6.1-sol")
        self.assertEqual((r.model, r.effort), ("gpt-6.1-sol", "xhigh"))
        r = launch_route("codex", ["codex", "-c", 'model="gpt-x"'], "gpt-profile")
        self.assertEqual(r.model, "gpt-x")
        self.assertIsNone(launch_route("claude", ["claude"]).model)

    def test_router_sends_each_attempt_to_its_bound_workspace(self):
        other_ws = "99999999-2222-3333-4444-555555555555"
        other_agent = "66666666-2222-3333-4444-555555555555"
        stranger = "77777777-2222-3333-4444-555555555555"
        p1, s1, _ = make()
        p2, s2, _ = make()
        h1 = RunnerHook(p1, WS, {AGENT: "lane-a"}.get)
        h2 = RunnerHook(p2, other_ws, {other_agent: "lane-b"}.get)
        router = Router([h1, h2])
        self.assertTrue(router.bound(other_agent))
        self.assertFalse(router.bound(stranger))
        router.turn_started("attempt-1", TASK, AGENT, "claude", launch_route("claude", ["claude", "--model", "m1"]),
                            project_id=PROJECT)
        router.turn_started("attempt-2", TASK, other_agent, "codex", project_id=PROJECT)
        router.turn_started("attempt-3", TASK, stranger, "codex", project_id=PROJECT)
        router.turn_settled("attempt-1", {"model": "claude-opus-5-5"}, "waiting")
        router.tick()
        self.assertEqual([(r["workspace_id"], r["attempt_id"]) for r in rows(s1)], [(WS, "attempt-1")])
        self.assertEqual([(r["workspace_id"], r["attempt_id"]) for r in rows(s2)], [(other_ws, "attempt-2")])
        row = rows(s1)[0]
        self.assertEqual((row["observed"]["model"], row["resolved_route"]["model"]), ("claude-opus-5-5", "m1"))
        router.attempt_ended("attempt-1", "completed")
        router.tick()
        self.assertEqual(rows(s1)[-1]["state"], "completed")


if __name__ == "__main__":
    unittest.main()
