"""A resume the start gate refuses for a person's decision (exit 76, ``HOLD_REASON_CODE=human``)
is not re-tried with a fresh env build (env command: secrets, tool config) and a gate run every GATE_HOLD_SECONDS:
the refusal stands until the card changes, its wake file changes or TASK_HOLD_SECONDS pass. The lane is
a lead lane: ``human_gate_flag_holds = false`` (policy.human_gate off), the operator's gate decides."""
import json
import sys
import time
from unittest.mock import patch

from fakes import FakeMesh, task, wait_time, write_replies
from test_runner import Bench

from mesh_runner import runner as R


class ResumeHumanHold(Bench):
    def gate(self):
        vfile, log_file = self.root / "verdicts.json", self.root / "hooklog"
        vfile.write_text(json.dumps({}))
        gate = ("import os,sys,json,pathlib\n"
                f"v=json.loads(pathlib.Path({str(vfile)!r}).read_text()).get(os.environ['MESH_RUNNER_PHASE'],'go')\n"
                f"pathlib.Path({str(log_file)!r}).open('a').write(os.environ['MESH_RUNNER_PHASE']+'\\n')\n"
                "if v=='human':\n print('HOLD_REASON_CODE=human'); sys.exit(76)\n"
                "print('RES=r1')\n"
                "if os.environ['MESH_RUNNER_PHASE']=='admit': print('ADMITTED=1')\n")
        self.agent.start_gate_command = [sys.executable, "-c", gate]
        self.agent.turn_end_command = [sys.executable, "-c", "pass"]
        return vfile, log_file

    def held_resume(self):
        t = task(1, updated_at="2026-11-27T10:00:00Z", human_gate=True)
        mesh = FakeMesh([t], max_concurrent=1)
        vfile, log_file = self.gate()
        write_replies(self.replies, {t["id"][:8]: wait_time(why="soak window")})
        r = self.runner(mesh)
        self.agent.policy.human_gate = False          # the gate decides, not the server flag
        r.tick()
        self.settle_all(r)
        a = r.store.by_state("builder", ("waiting",))[0]
        r.store.update(a, state="ready", wake_text="go on")
        vfile.write_text(json.dumps({"reserve": "human"}))
        return t, mesh, r, a, log_file

    def retry(self, r, a, n):
        builds = []
        real = r._fresh_env

        def counted(*args, **kw):
            builds.append(kw.get("reason", ""))
            return real(*args, **kw)
        with patch.object(r, "_fresh_env", side_effect=counted):
            for _ in range(n):
                r._resume_hold.pop(a.id, None)             # GATE_HOLD_SECONDS passed
                r._resume_one(r.store.get(a.id))
        return builds

    def test_refused_resume_builds_no_env_until_the_card_changes(self):
        t, mesh, r, a, log_file = self.held_resume()
        self.assertEqual(len(self.retry(r, a, 1)), 1)           # the first refusal: one build, one gate run
        reserves = log_file.read_text().count("reserve")
        self.assertEqual(self.retry(r, a, 5), [])               # held: no env build ...
        self.assertEqual(log_file.read_text().count("reserve"), reserves)   # ... and no gate run
        self.assertEqual(r.store.get(a.id).state, "ready")
        self.assertEqual(r._resume_hold_reason[a.id], R.GATE_PUBLIC_REASONS["human"])
        mesh.tasks[t["id"]]["updated_at"] = "2026-11-27T11:00:00Z"   # an answer moved the card
        self.assertEqual(len(self.retry(r, a, 1)), 1)           # asked again at once

    def test_gate_details_or_the_ttl_lift_the_hold(self):
        t, mesh, r, a, _ = self.held_resume()
        self.retry(r, a, 1)
        mesh.tasks[t["id"]]["human_gate_info"] = {"decision_pending": True}   # updated_at unchanged
        self.assertEqual(len(self.retry(r, a, 1)), 1)
        held = r._resume_gate_hold[a.id]
        r._resume_gate_hold[a.id] = (held[0], time.monotonic() - 1, *held[2:])   # TASK_HOLD_SECONDS passed
        self.assertEqual(len(self.retry(r, a, 1)), 1)

    def test_a_passed_gate_drops_the_hold(self):
        t, mesh, r, a, _ = self.held_resume()
        vfile = self.root / "verdicts.json"
        self.retry(r, a, 1)
        self.assertIn(a.id, r._resume_gate_hold)
        mesh.tasks[t["id"]]["updated_at"] = "2026-11-27T11:00:00Z"
        vfile.write_text(json.dumps({}))
        write_replies(self.replies, {t["id"][:8]: wait_time(why="soak window")})
        builds = self.retry(r, a, 1)
        self.assertEqual(builds[0], "resume")
        self.assertNotIn(a.id, r._resume_gate_hold)
