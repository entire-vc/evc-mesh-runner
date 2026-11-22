"""A print-mode headless turn that the provider's account limit ended before it bound a session.

Kimi's print mode names its session only after a completed turn (``session.resume_hint``); a turn the
provider refuses on the account's usage window ends with ``error: failed to run prompt: provider.api_error:
You've reached your 5-hour usage limit…`` on stderr and no session id in the stream, whether it was refused
at once on a resume or after it had already worked for a while on a fresh session. Recovery used to read that
as ``headless-native-session-mismatch``: the card was held in ``unknown`` with an operator alert saying the
session did not confirm it took the prompt, and the lane never asked for another account.

Such a turn is a quota failure, not an unknown delivery: the exact pending prompt is kept (nothing binds it to a
session the Runner could resume), the on-limit command runs, and the next start needs fresh account capacity,
on another harness when routing moved the lane there. Any other failure without a session, an authentication
refusal, or limit wording that only appears in the output of the turn's own commands stays a mismatch.
"""
import hashlib
import json
import subprocess
import sys
import uuid
from pathlib import Path

from fakes import FakeMesh, calls, task
from mesh_runner import interactive
from mesh_runner.adapters.base import proc_stamp
from mesh_runner.adapters.kimi import KimiAdapter
from mesh_runner.adapters.zcode import ZCodeAdapter
from test_runner import Bench

VERSION = {"role": "meta", "type": "system.version", "version": "2.6.1"}
LIMIT = ("error: failed to run prompt: provider.api_error: You've reached your 5-hour usage limit. Your quota will "
         "reset when the current 5-hour window ends. To continue now, purchase extra usage or upgrade your plan: "
         "https://www.kimi.com/settings/subscription?tab=quota\nSee log: /var/lib/kimi-code/logs/kimi-code.log\n")
SERVER = "error: failed to run prompt: provider.api_error: internal server error\n"
AUTH = "error: failed to run prompt: provider.auth_error: 401 unauthorized, run /login\n"
# what a fresh session did before the limit: one step and the output of one of its commands
WORK = [{"role": "assistant", "content": "Reading the merge request first.",
         "tool_calls": [{"type": "function", "id": "tool_1", "function": {"name": "Shell", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "tool_1", "content": "ok"}]
# the turn's own commands write to the same stderr as the CLI
COMMAND_NOISE = "## HEAD (no branch)\nquota.py: usage limit tests 12 passed\n"


def dead_pid():
    p = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    stamp = proc_stamp(p.pid)
    p.wait(timeout=10)
    return p.pid, stamp


class HeadlessKimiQuota(Bench):
    """A kimi alt-harness turn on a claude lane whose process group is gone (journal ``native-exited``), with a
    pending CI wake riding on its prompt. The lane's on-limit command records that it ran."""

    PROMPT = "EXACT PENDING REQUEST: review the merge request once."

    def setUp(self):
        super().setUp()
        self.switched = self.root / "switched"
        self.agent.on_limit_command = [sys.executable, "-c",
            "import os;from pathlib import Path;"
            f"Path({str(self.switched)!r}).write_text(os.environ['MESH_RUNNER_AGENT'])"]
        self.agent.alt = {"kimi": {}, "zcode": {}}
        self.t = task(1, cat="in_progress")
        self.mesh = FakeMesh([self.t])
        self.r = self.runner(self.mesh)
        self.r.me = self.mesh.me()
        self.r.alt_adapters = {"kimi": KimiAdapter(binary=str(self.root / "no-kimi")),
                               "zcode": ZCodeAdapter(binary=str(self.root / "no-zcode"))}
        self.directory = interactive.agent_dir(self.cfg.state_dir, self.agent.name)
        self.directory.mkdir(parents=True, exist_ok=True)

    def turn(self, stream, stderr, session="", harness="kimi"):
        """Turn 2 (``session``: a resume of that harness session, else a fresh one) ended with ``stream`` and
        ``stderr``. Returns the attempt."""
        a = self.r.store.create(self.agent.name, self.t["id"], harness, self.t["title"])
        self.workspaces = [str(self.r.pool.lease(a.id, "app"))]
        log = self.root / f"{a.id}.turn2.{harness}.jsonl"
        log.write_text("".join(json.dumps(e) + "\n" for e in stream))
        log.with_suffix(".stderr").write_text(stderr)
        self.intent = {"version": 1, "token": uuid.uuid4().hex, "turn": 2, "log_path": str(log),
                       "sha256": hashlib.sha256(self.PROMPT.encode()).hexdigest(), "prompt": self.PROMPT,
                       "kind": "headless", "harness": harness, "session_id": session, "resume": bool(session),
                       "workspaces": self.workspaces, "source_read_at": "pending-source",
                       "prior": {"turns": 1, "session_id": session, "log_path": "", "turn_started_at": "",
                                 "source_read_at": "previous-source"}}
        interactive.write_delivery(self.directory, a.id, {"version": 1, "token": self.intent["token"],
                                                          "sha256": self.intent["sha256"], "phase": "native-exited"})
        self.pending = {"ci_delivery": {"key": "fixture-ci", "value": "failed:1"}}
        pid, stamp = dead_pid()
        self.assertTrue(self.r.store.update(a, state="running", pid=pid, stamp=stamp, turns=2, session_id=session,
                                            log_path=str(log), workspaces=self.workspaces, wake_text="pending wake",
                                            wait={**self.pending, "interactive_prompt_intent": self.intent}))
        self.a = self.r.store.get(a.id)
        return self.a

    def reconcile(self):
        return self.r._reconcile_interactive_prompt(self.r.store.get(self.a.id))

    def details(self, kind):
        return [d for _, k, d in self.r.store.events(self.a.id) if k == kind]

    def alerts(self):
        path = self.cfg.state_dir / "events.jsonl"
        rows = [json.loads(x) for x in path.read_text().splitlines() if x.strip()] if path.exists() else []
        return [r for r in rows if r["kind"] == "operator_alert"]

    def held(self):
        return [c["body"] for c in self.mesh.comment_log.get(self.t["id"], [])
                if "MANDATORY DELIVERY PENDING" in c["body"]]

    def assert_kept_for_fresh_account(self):
        self.assertFalse(self.reconcile())
        a = self.r.store.get(self.a.id)
        self.assertEqual(self.details("prompt_delivery_unknown"), [])
        self.assertEqual(self.alerts(), [])
        self.assertEqual(self.held(), [])
        self.assertEqual(a.state, "ready")
        # the exact prompt is kept, not acknowledged: the pending wake stays undelivered
        self.assertEqual(a.wait["interactive_prompt_intent"], dict(self.intent, retryable=True))
        self.assertEqual(a.wait["ci_delivery"], self.pending["ci_delivery"])
        self.assertIsNone(self.r.store.mark(self.agent.name, self.t["id"], "fixture-ci"))
        self.assertNotIn("mesh_runner.prompt_ack", Path(self.intent["log_path"]).read_text())
        self.assertEqual((a.turns, a.log_path, a.source_read_at), (1, "", "previous-source"))
        # the provider limit: account routing ran and the next start needs fresh account capacity
        self.assertEqual(self.switched.read_text(), self.agent.name)
        self.assertEqual(self.details("limit_switch"), ["rc=0 "])
        self.assertTrue(self.r._quota_admission_required(a))
        self.assertIn("requires a capable start gate", self.r._gate("reserve", a.task_id, a.mode, a)[0])
        return a

    def assert_mismatch(self):
        self.assertFalse(self.reconcile())
        a = self.r.store.get(self.a.id)
        self.assertEqual(a.state, "unknown")
        self.assertEqual(self.details("prompt_delivery_unknown"), ["headless-native-session-mismatch"])
        self.assertIn("interactive_prompt_intent", a.wait)
        self.assertFalse(self.switched.exists())
        self.assertFalse(self.r._quota_admission_required(a))

    def test_fresh_session_that_worked_then_hit_the_limit_waits_for_a_fresh_account(self):
        self.turn([VERSION, *WORK], COMMAND_NOISE + LIMIT)
        self.assert_kept_for_fresh_account()

    def test_resume_refused_at_once_by_the_limit_waits_for_a_fresh_account(self):
        self.turn([VERSION], LIMIT, session="session_" + str(uuid.uuid4()))
        self.assert_kept_for_fresh_account()

    def test_other_failure_without_a_session_stays_a_mismatch(self):
        # control: the same unbound end, not the account's limit
        self.turn([VERSION, *WORK], SERVER)
        self.assert_mismatch()

    def test_structured_failure_beside_a_limit_line_stays_a_mismatch(self):
        # control: the stream's own typed failure is not the limit, the stderr line is; contradictory evidence
        self.turn([VERSION, *WORK, {"type": "error", "code": "provider.api_error",
                                    "message": "internal server error"}], LIMIT)
        self.assert_mismatch()

    def test_earlier_other_failure_line_beside_a_limit_line_stays_a_mismatch(self):
        # control: the last failure line is the limit, an earlier one is not
        self.turn([VERSION, *WORK], SERVER + LIMIT)
        self.assert_mismatch()

    def test_structured_limit_agreeing_with_the_limit_line_waits_for_a_fresh_account(self):
        # positive control for the two above: both sources name the limit
        self.turn([VERSION, *WORK, {"type": "error", "code": "provider.api_error",
                                    "message": "You've reached your 5-hour usage limit."}], LIMIT)
        self.assert_kept_for_fresh_account()

    def test_authentication_refusal_stays_a_mismatch(self):
        self.turn([VERSION], AUTH)
        self.assert_mismatch()

    def test_zcode_limit_without_a_session_stays_a_mismatch(self):
        # Recovery is proved only for Kimi; another adapter must not inherit it through an empty source list.
        self.turn([{"type": "turn.failed", "payload": {"error": {
            "message": "usage limit", "attribution": {"statusCode": 429}}}}], "", harness="zcode")
        self.assert_mismatch()

    def test_zcode_limit_beside_another_stderr_failure_stays_a_mismatch(self):
        self.turn([{"type": "turn.failed", "payload": {"error": {
            "message": "usage limit", "attribution": {"statusCode": 429}}}}],
            "Error: internal server error\n", harness="zcode")
        self.assert_mismatch()

    def test_zcode_conflicting_structured_failures_stay_a_mismatch(self):
        self.turn([{"type": "turn.failed", "payload": {"error": {
            "message": message, "attribution": {"statusCode": status}}}}
            for status, message in [(500, "internal server error"), (429, "usage limit")]],
            "", harness="zcode")
        self.assert_mismatch()

    def test_limit_words_in_command_output_are_not_the_failure(self):
        # the CLI's own failure line decides, not what the turn's commands printed
        self.turn([VERSION, *WORK], COMMAND_NOISE + SERVER)
        self.assert_mismatch()

    def test_changed_pending_prompt_is_not_quota_proof(self):
        self.turn([VERSION], LIMIT)
        self.r.store.update(self.a, wait={**self.pending, "interactive_prompt_intent": dict(
            self.intent, prompt=self.PROMPT + " altered")})
        self.assertFalse(self.reconcile())
        self.assertEqual(self.r.store.get(self.a.id).state, "unknown")
        self.assertFalse(self.switched.exists())

    def test_routed_to_another_harness_carries_the_exact_prompt_into_a_new_session(self):
        # the on-limit command moved the lane off kimi (back to its claude harness, fake CLI)
        self.turn([VERSION, *WORK], COMMAND_NOISE + LIMIT)
        a = self.assert_kept_for_fresh_account()
        self.agent.start_gate_command = [sys.executable, "-c",
            "import os;assert os.environ['MESH_RUNNER_REQUIRE_ACCOUNT_CAPACITY']=='1';print('ACCOUNT_CAPACITY_READY=1')"]
        self.r._resume_one(a)
        current = self.r.store.get(a.id)
        self.assertEqual(current.state, "running")
        self.assertEqual(current.harness, "claude")
        self.assertEqual(self.details("prompt_retry_scope_changed"), [])
        pending = current.wait["interactive_prompt_intent"]
        self.assertNotEqual(pending["token"], self.intent["token"])
        self.assertEqual(pending["prompt"].count(self.PROMPT), 1)
        self.assertIn("provider quota limit", pending["prompt"])
        self.assertEqual(current.wait["ci_delivery"], self.pending["ci_delivery"])
        self.assertEqual(self.r.procs[a.id].wait(timeout=10), 0)
        seen = calls(self.calls)
        self.assertEqual(len(seen), 1)
        self.assertFalse(seen[0]["resume"])
        self.assertIn(self.PROMPT, seen[0]["prompt"])

    def same_harness_account(self, sessions_directory=True, capacity=True):
        """Route to a fresh Kimi account. Only the native CLI is substituted; argv, session lookup,
        durable prompt delivery and child launch use the real adapter/Runner path."""
        home = self.root / "new-kimi-account"
        home.mkdir()
        if sessions_directory:
            (home / "sessions").mkdir()
        binary = self.root / "kimi-cli"
        binary.write_text(f"#!{sys.executable}\n" + '''import json, os, sys
from pathlib import Path
resume = any(a.startswith("--session=") for a in sys.argv)
prompt = next(a.split("=", 1)[1] for a in sys.argv if a.startswith("--prompt="))
with Path(os.environ["FAKE_CALLS"]).open("a") as output:
    output.write(json.dumps({"resume": resume, "prompt": prompt,
                             "home": os.environ["KIMI_CODE_HOME"]}) + "\\n")
if resume:
    sys.exit("old session does not exist on this account")
print(json.dumps({"role": "meta", "type": "session.resume_hint", "session_id": "fresh-kimi-session"}))
''')
        binary.chmod(0o700)
        self.r.alt_adapters["kimi"] = KimiAdapter(binary=str(binary))
        self.agent.env_command = [sys.executable, "-c",
            f"print('MESH_RUNNER_HARNESS=kimi');print('KIMI_CODE_HOME=' + {str(home)!r})"]
        self.agent.start_gate_command = [sys.executable, "-c",
            "import os;assert os.environ['MESH_RUNNER_REQUIRE_ACCOUNT_CAPACITY']=='1';"
            f"print('ACCOUNT_CAPACITY_READY={int(capacity)}')"]
        return home

    def assert_same_harness_restart(self, sessions_directory=True):
        self.turn([VERSION], LIMIT, session="old-kimi-session")
        a = self.assert_kept_for_fresh_account()
        home = self.same_harness_account(sessions_directory=sessions_directory)
        self.r._resume_one(a)
        current = self.r.store.get(a.id)
        self.assertEqual(current.state, "running")
        rc = self.r.procs[a.id].wait(timeout=10)
        seen = calls(self.calls)
        self.assertEqual(len(seen), 1)
        self.assertFalse(seen[0]["resume"])
        self.assertEqual(rc, 0)
        self.assertEqual(seen[0]["home"], str(home))
        self.assertEqual(seen[0]["prompt"].count(self.PROMPT), 1)
        self.assertEqual(seen[0]["prompt"].count("pending wake"), 1)
        pending = current.wait["interactive_prompt_intent"]
        self.assertNotEqual(pending["token"], self.intent["token"])
        self.assertFalse(pending["resume"])
        self.assertEqual(pending["session_id"], "")
        self.assertEqual(pending["prompt"], seen[0]["prompt"])
        self.assertEqual(current.harness, "kimi")
        self.assertEqual(current.wait["ci_delivery"], self.pending["ci_delivery"])
        self.assertIsNone(self.r.store.mark(self.agent.name, self.t["id"], "fixture-ci"))
        self.assertEqual(self.details("prompt_retry_scope_changed"), [])

    def test_same_harness_resume_on_a_fresh_account_starts_a_new_session(self):
        self.assert_same_harness_restart()

    def test_same_harness_resume_with_unknown_session_inventory_starts_a_new_session(self):
        # Kimi reports session_known=True when the new account has no sessions directory yet.
        self.assert_same_harness_restart(sessions_directory=False)

    def test_same_harness_without_account_capacity_preserves_the_pending_session(self):
        self.turn([VERSION], LIMIT, session="old-kimi-session")
        a = self.assert_kept_for_fresh_account()
        self.same_harness_account(capacity=False)
        self.r._resume_one(a)
        current = self.r.store.get(a.id)
        self.assertEqual(current.state, "ready")
        self.assertEqual(current.session_id, "old-kimi-session")
        self.assertEqual(current.wait, a.wait)
        self.assertTrue(self.r._quota_admission_required(current))
        self.assertFalse(calls(self.calls))

    def test_routed_elsewhere_without_quota_proof_is_not_carried(self):
        # control: an unbound end that is not the limit never moves the pending prompt to another harness
        self.turn([VERSION], LIMIT)
        a = self.assert_kept_for_fresh_account()
        Path(self.intent["log_path"]).with_suffix(".stderr").write_text(SERVER)
        self.agent.start_gate_command = [sys.executable, "-c", "print('ACCOUNT_CAPACITY_READY=1')"]
        self.r._resume_one(a)
        self.assertEqual(self.r.store.get(a.id).state, "unknown")
        self.assertEqual(self.details("prompt_retry_scope_changed"), ["workspaces-or-harness"])
        self.assertFalse(calls(self.calls))
