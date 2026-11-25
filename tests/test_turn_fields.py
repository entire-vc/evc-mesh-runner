"""The turn fields of a spend report: turn id, times, provider, account, billing mode, cache split."""
import datetime as dt
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from mesh_runner import turn_usage
from mesh_runner.adapters.agy import AgyAdapter
from mesh_runner.adapters.base import TurnResult
from mesh_runner.adapters.claude import ClaudeAdapter
from mesh_runner.catalog import PoolObserver, iso as epoch_iso
from mesh_runner.config import AgentConfig, ConfigError, RunnerConfig, load
from mesh_runner.interactive import Tmux
from mesh_runner.mesh import MeshClient
from mesh_runner.runner import AgentRunner
from mesh_runner.store import Attempt, Store, iso
from mesh_runner.tui_driver import Driver
from fakes import FakeMesh, task, wait_time, write_replies
from test_runner import Bench

# What Mesh accepts in a report (evc-mesh ReportSession): a name in ``unknown`` outside this set, a
# figure for a name it lists, or a 1 h cache part without its total refuses the whole report.
UNKNOWN_NAMES = {"tokens_in", "tokens_out", "estimated_cost", "cache_read", "cache_write"}
TURN_FIELDS = {"turn_id", "started_at", "ended_at", "provider", "account_ref", "billing_mode",
               "cache_read", "cache_write", "cache_write_1h"}


def parse_time(value):
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


class Accepted(unittest.TestCase):
    def assert_mesh_accepts(self, body):
        unknown = body.get("unknown", [])
        self.assertLessEqual(set(unknown), UNKNOWN_NAMES)
        for name in unknown:
            self.assertNotIn(name, body)
        if "cache_write_1h" in body:
            self.assertIn("cache_write", body)
            self.assertLessEqual(body["cache_write_1h"], body["cache_write"])
        if "started_at" in body and "ended_at" in body:
            self.assertLessEqual(parse_time(body["started_at"]), parse_time(body["ended_at"]))
        for name in ("started_at", "ended_at"):
            if name in body:
                self.assertTrue(body[name].endswith("Z"), body[name])
        if "account_ref" in body:
            self.assertNotRegex(body["account_ref"], r"[@\s]")
        if "billing_mode" in body:
            self.assertIn(body["billing_mode"], ("subscription", "api"))


class Body(Accepted):
    """The encoded request Mesh receives, from settle through the real HTTP client."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.sent = []
        self.agent = AgentConfig("builder", "test-key", "claude", self.root, [])
        self.cfg = RunnerConfig(mesh_url="https://mesh.example", state_dir=self.root, agents=[self.agent],
                                spend_report_turn_fields=True)
        self.attempt = Attempt("a1", "builder", "t1", "claude", "waiting", turns=1)
        self.store = Store(self.root / "state.sqlite")
        self.addCleanup(self.store.db.close)

    def opener(self, req, timeout=None):
        # mock: external boundary — Mesh HTTP, the encoded body is what Mesh receives
        self.sent.append(req)
        return io.BytesIO(b"{}")

    def login(self, *parts):
        path = self.root.joinpath(*parts)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def launch(self, harness, env, turn=None, attempt="a1", api_account=""):
        """The launch record the runner writes when it starts the turn (runner._spawn)."""
        self.store.mark("builder", "t1", "turn_launch", json.dumps(
            {"attempt": attempt, "turn": self.attempt.turns + 1 if turn is None else turn, "started_at": iso(),
             **turn_usage.launch_facts(harness, env, api_account, self.cfg.account_refs, self.root)}))

    def send(self, result, harness="claude", log=None):
        runner = AgentRunner(self.cfg, self.agent, self.store,
                             mesh=MeshClient(self.cfg.mesh_url, "test-key", opener=self.opener))
        self.attempt.turns += 1
        self.attempt.harness = harness
        if log is None:
            log = self.root / f"turn{self.attempt.turns}.jsonl"
            log.write_text("{}\n")
        self.attempt.log_path = str(log)
        runner._report_spend(self.attempt, result)
        return json.loads(self.sent[-1].data)

    def interactive_claude(self):
        """An interactive Claude turn of two API calls, 1 h cache writes, as the driver emits it."""
        def message(mid, usage):
            return {"type": "assistant", "message": {"id": mid, "model": "claude-opus-5-5", "usage": usage,
                                                     "content": [{"type": "text", "text": "ok"}]}}
        first = {"input_tokens": 100, "output_tokens": 40000, "cache_read_input_tokens": 6000000,
                 "cache_creation_input_tokens": 165000,
                 "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 165000}}
        second = {"input_tokens": 28, "output_tokens": 3935, "cache_read_input_tokens": 789009,
                  "cache_creation_input_tokens": 280,
                  "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 280}}
        out = io.StringIO()
        d = Driver("claude", "builder", self.root, Tmux("tmux"), 1, self.root, "s1", "start", "k", [],
                   out=out, env={})
        transcript = self.root / "transcript.jsonl"
        transcript.write_text(json.dumps(message("m1", first)) + "\n" + json.dumps(message("m2", second)) + "\n")
        d.emit_claude({"event": {"transcript_path": str(transcript)}, "offset": 0})
        log = self.root / "turn.jsonl"
        log.write_text(out.getvalue())
        return ClaudeAdapter().parse(log), log

    # AC1: a Claude turn
    def test_claude_turn_carries_its_id_times_provider_account_and_cache(self):
        self.launch("claude", {"HOME": str(self.root), "CLAUDE_CONFIG_DIR": str(self.login("accounts", "ops-team"))})
        result, log = self.interactive_claude()
        body = self.send(result, log=log)
        self.assert_mesh_accepts(body)
        self.assertEqual(body["turn_id"], "a1:2")
        self.assertLessEqual(parse_time(body["started_at"]), parse_time(body["ended_at"]))
        self.assertEqual((body["provider"], body["account_ref"], body["billing_mode"]),
                         ("anthropic", "claude/ops-team", "subscription"))
        self.assertEqual((body["cache_read"], body["cache_write"], body["cache_write_1h"]),
                         (6789009, 165280, 165280))
        # tokens_in keeps its meaning: the whole input, cache included
        self.assertEqual(body["tokens_in"], 128 + 6789009 + 165280)
        self.assertNotIn("unknown", body)

    # AC1: a Codex turn
    def test_codex_turn_reads_its_cache_from_cached_input_and_keeps_tokens_in(self):
        self.launch("codex", {"HOME": str(self.root), "CODEX_HOME": str(self.login("codex-accounts", "ops"))})
        usage = {"input_tokens": 1489062, "cached_input_tokens": 1300000, "output_tokens": 8385}
        body = self.send(TurnResult(model="gpt-6.1-sol", usage=usage), harness="codex")
        self.assert_mesh_accepts(body)
        self.assertEqual(body["cache_read"], 1300000)
        self.assertEqual(body["tokens_in"], 1489062)            # not reduced by the cache part
        # OpenAI bills no cache write of its own: the input outside the hits is plain input (Doc §2.5)
        self.assertEqual(body["cache_write"], 0)
        self.assertNotIn("cache_write_1h", body)
        self.assertEqual((body["provider"], body["account_ref"], body["billing_mode"]),
                         ("openai", "codex/ops", "subscription"))

    def test_agy_turn_reports_its_implicit_cache_with_no_write(self):
        # Gemini caches implicitly only: no explicit cache, no write price, so the write part is 0
        log = self.root / "turn.agy.jsonl"
        log.write_text("\n".join(json.dumps(e) for e in (
            {"event": "init", "conversation_id": "c1", "init": {"model": "gemini-4-neon-high"}},
            {"event": "step_update", "step_update": {"state": "DONE", "step_type": "agent_response", "usage": {
                "input_tokens": 9388, "output_tokens": 852, "thinking_tokens": 278, "cache_read_tokens": 220200,
                "total_tokens": 10240}}},
            {"event": "result", "result": {"conversation_id": "c1", "status": "SUCCESS", "response": "ok"}})) + "\n")
        self.launch("agy", {"HOME": str(self.root)})
        body = self.send(AgyAdapter().parse(log), harness="agy", log=log)
        self.assert_mesh_accepts(body)
        self.assertEqual((body["provider"], body["cache_read"], body["cache_write"]), ("google", 220200, 0))
        self.assertFalse({"cache_read", "cache_write"} & set(body.get("unknown", [])))
        self.assertEqual((body["tokens_in"], body["tokens_out"]), (9388 + 220200, 852))   # thinking inside output

    # AC1: a turn without usage
    def test_turn_without_usage_lists_its_cache_as_unknown_never_zero(self):
        self.launch("claude", {"HOME": str(self.root), "CLAUDE_CONFIG_DIR": str(self.login("accounts", "ops-team"))})
        body = self.send(TurnResult(model="claude-opus-5-5", usage={}))
        self.assert_mesh_accepts(body)
        self.assertEqual(body["unknown"], ["tokens_in", "tokens_out", "estimated_cost", "cache_read", "cache_write"])
        for name in ("cache_read", "cache_write", "cache_write_1h", "tokens_in", "tokens_out"):
            self.assertNotIn(name, body)
        self.assertEqual(body["turn_id"], "a1:2")               # what is known still goes
        # a Codex turn without its cached counter: the same
        self.launch("codex", {"HOME": str(self.root), "CODEX_HOME": str(self.login("codex-accounts", "ops"))})
        body = self.send(TurnResult(model="gpt-6.1-sol", usage={"input_tokens": 5, "output_tokens": 1}),
                         harness="codex")
        self.assert_mesh_accepts(body)
        self.assertEqual(body["unknown"][-2:], ["cache_read", "cache_write"])

    def test_cache_write_of_unknown_lifetime_sends_no_1h_part(self):
        usage = {"input_tokens": 1, "output_tokens": 1, "cache_read_input_tokens": 5,
                 "cache_creation_input_tokens": 7, "cache_creation": None}
        body = self.send(TurnResult(model="claude-opus-5-5", usage=usage))
        self.assert_mesh_accepts(body)
        self.assertEqual((body["cache_read"], body["cache_write"]), (5, 7))
        self.assertNotIn("cache_write_1h", body)

    def test_off_by_default_the_body_is_unchanged(self):
        self.cfg = RunnerConfig(mesh_url="https://mesh.example", state_dir=self.root, agents=[self.agent])
        self.launch("claude", {"HOME": str(self.root), "CLAUDE_CONFIG_DIR": str(self.login("accounts", "ops-team"))})
        usage = {"input_tokens": 1489062, "cached_input_tokens": 1300000, "output_tokens": 8385}
        body = self.send(TurnResult(model="gpt-6.1-sol", usage=usage), harness="codex")
        self.assertEqual(set(body), {"task_id", "model", "tokens_in", "tokens_out", "estimated_cost"})
        self.assertFalse(set(body) & TURN_FIELDS)

    def test_another_turns_launch_record_says_nothing_about_this_turn(self):
        env = {"HOME": str(self.root), "CLAUDE_CONFIG_DIR": str(self.login("accounts", "ops-team"))}
        for kw in ({"turn": 7}, {"attempt": "other"}):
            with self.subTest(**kw):
                self.launch("claude", env, **kw)
                body = self.send(TurnResult(model="claude-opus-5-5", usage={}))
                self.assert_mesh_accepts(body)
                for name in ("started_at", "account_ref", "billing_mode"):
                    self.assertNotIn(name, body)
                self.assertIn("ended_at", body)

    def test_a_resend_of_the_report_carries_the_same_fields(self):
        self.launch("claude", {"HOME": str(self.root), "CLAUDE_CONFIG_DIR": str(self.login("accounts", "ops-team"))})
        result, log = self.interactive_claude()
        first = self.send(result, log=log)
        self.attempt.turns -= 1                                  # the same turn settles again (restart)
        os.utime(log, (time.time() + 60, time.time() + 60))     # its log was touched meanwhile
        self.store.mark("builder", "t1", "spend", "")            # the first answer was lost
        self.store.mark("builder", "t1", "turn_launch", "")      # and the launch record is gone
        again = self.send(result, log=log)
        self.assertEqual(self.sent[-1].data, self.sent[-2].data)
        self.assertEqual(first, again)


class Launch(unittest.TestCase):
    """What the launch of a turn says about its account and billing mode."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        (self.home / ".claude").mkdir()
        self.team = self.home / ".claude-accounts" / "ops-team"
        self.team.mkdir(parents=True)

    def facts(self, harness="claude", aliases=None, api_account="", **env):
        return turn_usage.launch_facts(harness, {"HOME": str(self.home), **env}, api_account, aliases, self.home)

    def test_a_login_directory_names_its_account(self):
        self.assertEqual(self.facts(CLAUDE_CONFIG_DIR=str(self.team)),
                         {"billing_mode": "subscription", "account_ref": "claude/ops-team"})
        # a relative directory is read from the lane's directory
        self.assertEqual(self.facts(CLAUDE_CONFIG_DIR=".claude-accounts/ops-team")["account_ref"], "claude/ops-team")

    def test_the_default_directory_names_an_account_only_through_an_alias(self):
        self.assertEqual(self.facts(), {"billing_mode": "subscription"})
        codex = self.home / ".codex"
        codex.mkdir()
        self.assertEqual(self.facts("codex"), {"billing_mode": "subscription"})
        self.assertEqual(self.facts("codex", aliases={str(codex): "codex/ops"}),
                         {"billing_mode": "subscription", "account_ref": "codex/ops"})

    def test_a_key_is_api_billing_without_an_account(self):
        self.assertEqual(self.facts(CLAUDE_CONFIG_DIR=str(self.team), ANTHROPIC_API_KEY="x"),  # pragma: allowlist secret
                         {"billing_mode": "api"})
        self.assertEqual(self.facts("codex", OPENAI_API_KEY="x"), {"billing_mode": "api"})  # pragma: allowlist secret
        # the runner's own API route names its account and provider
        self.assertEqual(self.facts(api_account="anthropic-api/ops"),
                         {"billing_mode": "api", "provider": "anthropic", "account_ref": "anthropic-api/ops"})

    def test_another_endpoint_names_neither_account_nor_billing(self):
        for env in ({"ANTHROPIC_BASE_URL": "https://api.example.ai/anthropic"}, {"ANTHROPIC_AUTH_TOKEN": "x"},
                    {"CLAUDE_CODE_USE_BEDROCK": "1"},
                    {"ANTHROPIC_BASE_URL": "https://api.example.ai/anthropic", "ANTHROPIC_API_KEY": "x"}):
            with self.subTest(env=sorted(env)):
                self.assertEqual(self.facts(CLAUDE_CONFIG_DIR=str(self.team), **env), {})
        self.assertEqual(self.facts(CLAUDE_CONFIG_DIR=str(self.team), ANTHROPIC_BASE_URL="https://api.anthropic.com"),
                         {"billing_mode": "subscription", "account_ref": "claude/ops-team"})
        # a login token is a subscription, of an account the directory does not name
        self.assertEqual(self.facts(CLAUDE_CONFIG_DIR=str(self.team), CLAUDE_CODE_OAUTH_TOKEN="x"),
                         {"billing_mode": "subscription"})

    def test_a_harness_with_its_own_login_is_not_guessed(self):
        self.assertEqual(self.facts("kimi"), {})
        self.assertEqual(self.facts("zcode"), {})

    def test_provider_from_the_model_else_from_a_single_provider_harness(self):
        cases = [("claude-opus-5-5", "claude", "anthropic"), ("gpt-6.1-sol", "codex", "openai"),
                 ("glm-5.3", "claude", "zai"), ("kimi-k3", "claude", "moonshot"), ("", "codex", "openai"),
                 ("", "kimi", "moonshot"), ("", "zcode", "zai"), ("fake-model", "claude", "")]
        for model, harness, want in cases:
            with self.subTest(model=model, harness=harness):
                self.assertEqual(turn_usage.provider(model, harness), want)

    def test_a_negative_duration_sends_no_times(self):
        fields = turn_usage.turn_fields("a:1", {}, "claude-opus-5-5", "claude",
                                        {"started_at": "2026-11-19T10:00:00.000+00:00"},
                                        dt.datetime(2026, 11, 19, 9, tzinfo=dt.timezone.utc).timestamp())
        self.assertNotIn("started_at", fields)
        self.assertNotIn("ended_at", fields)
        # a log written within the file clock's tick of the launch: the turn ended when it started
        fields = turn_usage.turn_fields("a:1", {}, "claude-opus-5-5", "claude",
                                        {"started_at": "2026-11-19T10:00:00.004+00:00"},
                                        dt.datetime(2026, 11, 19, 10, tzinfo=dt.timezone.utc).timestamp())
        self.assertEqual((fields["started_at"], fields["ended_at"]),
                         ("2026-11-19T10:00:00.004Z", "2026-11-19T10:00:00.004Z"))
        # a start without a zone is no time
        fields = turn_usage.turn_fields("a:1", {}, "m", "claude", {"started_at": "2026-11-19T10:00:00"}, None)
        self.assertNotIn("started_at", fields)


class PoolJoin(unittest.TestCase):
    """AC2: a turn's account_ref is the account of its quota pools, the pool ref without its window."""

    NOW = 1_800_000_000.0

    def test_account_ref_is_the_pool_ref_of_the_same_accounts_quota_observation(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            team, codex = home / ".claude-accounts" / "ops-team", home / ".codex"
            team.mkdir(parents=True)
            codex.mkdir()

            def record(account, window):
                pool = f"{account}/{window}"
                return {"account_ref": account, "catalog_pool": pool, "pool_id": pool, "status": "available",
                        "used_fraction": 0.4, "observed_at": epoch_iso(self.NOW - 60), "max_age": 1800,
                        "source": "claude_ratelimit_headers", "window": window}
            observations = home / "quota-observations.json"
            observations.write_text(json.dumps({"schema": "quota-observations/v1",
                                                "records": [record("claude/ops-team", "5h"),
                                                            record("claude/ops-team", "7d"),
                                                            record("codex/ops", "7d")]}))
            aliases = {str(codex): "codex/ops"}
            launches = {
                "claude/ops-team": turn_usage.launch_facts("claude", {"HOME": tmp, "CLAUDE_CONFIG_DIR": str(team)},
                                                           "", aliases, home),
                "codex/ops": turn_usage.launch_facts("codex", {"HOME": tmp}, "", aliases, home),
            }
            pools = {r["pool_id"]: r for r in json.loads(observations.read_text())["records"]}
            seen = PoolObserver(observations=observations).observe(set(pools), self.NOW)
            for pool, rec in pools.items():
                with self.subTest(pool=pool):
                    self.assertEqual(seen[pool]["state"], "available")      # the runner reads this pool
                    ref = turn_usage.turn_fields("a:1", {}, "", "claude", launches[rec["account_ref"]], None)
                    self.assertEqual(ref["account_ref"], pool.rsplit("/", 1)[0])
                    self.assertEqual(ref["account_ref"], rec["account_ref"])


class Config(unittest.TestCase):
    def load(self, text):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runner.toml"
            path.write_text(f'[runner]\nmesh_url = "https://mesh.example"\nstate_dir = "{tmp}"\n' + text)
            return load(path)

    def test_off_unless_configured(self):
        cfg = self.load("")
        self.assertEqual((cfg.spend_report_turn_fields, cfg.account_refs), (False, {}))
        cfg = self.load('[spend_report]\nturn_fields = true\n[spend_report.account_refs]\n"/h/.codex" = "codex/ops"\n')
        self.assertEqual((cfg.spend_report_turn_fields, cfg.account_refs), (True, {"/h/.codex": "codex/ops"}))

    def test_refuses_what_mesh_would_refuse(self):
        for text in ('[spend_report]\nturn_fields = "yes"\n', '[spend_report]\nfields = true\n',
                     '[spend_report.account_refs]\n"/h/.codex" = "ops@example.com"\n',
                     '[spend_report.account_refs]\n"/h/.codex" = "codex ops"\n',
                     '[spend_report.account_refs]\n"relative" = "codex/ops"\n'):
            with self.subTest(text=text):
                with self.assertRaises(ConfigError):
                    self.load(text)


class Launched(Bench, Accepted):
    """The launch record written by a real spawn reaches the report of that turn."""

    def test_a_launched_turn_reports_its_fields_and_a_resend_repeats_them(self):
        team = self.root / "accounts" / "ops-team"
        team.mkdir(parents=True)
        self.agent.inline_env["CLAUDE_CONFIG_DIR"] = str(team)     # the lane's login directory
        self.cfg.spend_report_turn_fields = True
        self.cfg.idempotency_keys = True
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        mesh.lose_report_answer = 1
        write_replies(self.replies, {t["id"][:8]: wait_time(why="soak window")})
        r = self.runner(mesh)
        before = dt.datetime.now(dt.timezone.utc)
        r.tick()
        self.settle_all(r)
        a = r.store.by_state("builder", ("waiting",))[0]
        fields = mesh.report_turns[0]
        self.assertEqual(fields["turn_id"], f"{a.id}:{a.turns}")
        self.assertLessEqual(before - dt.timedelta(seconds=1), parse_time(fields["started_at"]))
        self.assertLessEqual(parse_time(fields["started_at"]), parse_time(fields["ended_at"]))
        self.assertEqual((fields["account_ref"], fields["billing_mode"]), ("claude/ops-team", "subscription"))
        self.assertEqual((fields["cache_read"], fields["unknown"]), (90, ["cache_write"]))
        self.assertNotIn("provider", fields)                       # "fake-model" names no provider
        body = {k: v for k, v in fields.items() if k != "unknown"}
        self.assert_mesh_accepts(body)
        # the answer was lost: a new runner on the same state resends the same report
        r2 = self.runner(mesh)
        a = r2.store.by_state("builder", ("waiting",))[0]
        r2._report_spend(a, r2.adapter_for(a.harness).parse(Path(a.log_path)))
        self.assertEqual(mesh.report_ids[0], mesh.report_ids[1])
        self.assertEqual(mesh.report_turns[0], mesh.report_turns[1])

    def test_off_the_runner_sends_no_turn_fields(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        write_replies(self.replies, {t["id"][:8]: wait_time(why="soak window")})
        r = self.runner(mesh)
        r.tick()
        self.settle_all(r)
        self.assertEqual(mesh.report_turns, [None])


if __name__ == "__main__":
    unittest.main()
