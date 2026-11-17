"""Headless lanes switch accounts on the provider's typed limit verdict.

``claude -p --output-format stream-json`` reports the account window in ``rate_limit_event``.
A spent window ends the turn with ``status: rejected``; the closing text ("You've hit your weekly
limit · resets …") is wording, and the runner's text hint does not know it. The journal below is a
recorded turn shape (fields as the CLI writes them; window numbers of a spent weekly limit)."""
import json
import os
import sys
import unittest
from pathlib import Path

from fakes import FakeMesh, task, wait_time, write_replies
from mesh_runner.adapters.claude import ClaudeAdapter
from test_runner import Bench

JOURNAL = Path(__file__).parent / "fixtures" / "claude_headless_rate_limit_rejected.jsonl"


class ParseRateLimitEvent(unittest.TestCase):
    def test_rejected_event_is_typed_evidence(self):
        r = ClaudeAdapter().parse(JOURNAL)
        self.assertTrue(r.is_error)
        self.assertTrue(r.rate_limit_rejected)
        self.assertEqual(r.rate_limit["status"], "rejected")
        self.assertEqual(r.rate_limit["rate_limit_type"], "seven_day")
        self.assertEqual(r.rate_limit["resets_at"], 1795269600)
        self.assertFalse(r.rate_limit["is_using_overage"])
        self.assertEqual(r.rate_limit["windows"], {"five_hour": {"utilization": 0.31, "resets_at": 1795201200},
                                                   "seven_day": {"utilization": 1.0, "resets_at": 1795269600}})

    def test_allowed_event_is_not_a_limit(self):
        from mesh_runner.adapters.claude import rate_limit_info
        info = rate_limit_info({"status": "allowed", "resetsAt": 1792498200, "rateLimitType": "five_hour",
                                "isUsingOverage": False,
                                "unifiedWindows": {"five_hour": {"utilization": 0.22, "resetsAt": 1792498200}}})
        self.assertEqual(info["windows"], {"five_hour": {"utilization": 0.22, "resets_at": 1792498200}})
        self.assertEqual(info["status"], "allowed")

    def test_unknown_status_or_garbage_is_no_evidence(self):
        from mesh_runner.adapters.claude import rate_limit_info
        for raw in (None, "x", {}, {"status": "maybe"}, {"status": 1}):
            self.assertEqual(rate_limit_info(raw), {})


class HeadlessRejectedSwitches(Bench):
    def test_rejected_journal_switches_account_with_reset_time(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        seen = self.root / "switch-env.json"
        self.agent.on_limit_command = [sys.executable, "-c", (
            "import json,os; json.dump({k: os.environ.get(k, '') for k in ('MESH_RUNNER_LIMIT_SOURCE', "
            "'MESH_RUNNER_LIMIT_WINDOW', 'MESH_RUNNER_LIMIT_RESETS_AT', 'MESH_RUNNER_TASK')}, "
            f"open({str(seen)!r}, 'w')); print('to acct-2')")]
        self.agent.start_gate_command = [sys.executable, "-c", "print('ACCOUNT_CAPACITY_READY=1')"]
        write_replies(self.replies, {t["id"][:8]: f"JOURNAL:{JOURNAL}",
                                     t["id"][:8] + ":resume": wait_time(why="soak window")})
        r = self.runner(mesh)
        r.tick()
        self.settle_all(r)
        self.assertTrue(seen.exists(), "a rejected rate_limit_event must call the on-limit command")
        env = json.loads(seen.read_text())
        self.assertEqual(env, {"MESH_RUNNER_LIMIT_SOURCE": "rate_limit_event", "MESH_RUNNER_LIMIT_WINDOW": "seven_day",
                               "MESH_RUNNER_LIMIT_RESETS_AT": "1795269600", "MESH_RUNNER_TASK": t["id"]})
        self.assertEqual(r._starts_paused_until, 0.0)
        self.assertEqual(self.states(r)[t["id"][:8]], "waiting")       # resumed on the new account
        lane = json.loads((self.cfg.state_dir / "rate-limits" / "builder.json").read_text())
        self.assertEqual({k: lane[k] for k in ("schema", "agent", "status", "rate_limit_type", "is_using_overage")},
                         {"schema": "lane-rate-limit/v1", "agent": "builder", "status": "rejected",
                          "rate_limit_type": "seven_day", "is_using_overage": False})
        self.assertEqual(lane["windows"]["seven_day"], {"utilization": 1.0, "resets_at": 1795269600})
        self.assertTrue(lane["account"].startswith("claude "))


class StatusNamesLaunchMode(Bench):
    def test_status_names_launch_mode_and_active_pid(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        self.agent.launch_mode = "headless"
        write_replies(self.replies, {t["id"][:8]: "SLEEP:3:" + "x"})
        r = self.runner(mesh)
        r.tick()
        st = r.status()
        self.assertEqual(st["launch_mode"], "headless")
        running = [x for x in st["attempts"] if x["state"] in ("starting", "running")]
        self.assertTrue(running and all(isinstance(x.get("pid"), int) and x["pid"] > 0 for x in running), st["attempts"])
        launches = [json.loads(d) for (d,) in r.store.db.execute("SELECT detail FROM events WHERE kind='launch'")]
        self.assertTrue(launches and all(x["launch_mode"] == "headless" for x in launches), launches)
        self.settle_all(r)
        self.assertFalse(any("pid" in x for x in r.status()["attempts"]))


if __name__ == "__main__":
    unittest.main()
