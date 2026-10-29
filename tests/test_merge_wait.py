"""`WAIT merge:` — the runner runs the configured merge gate instead of waking a model for it."""
import datetime as dt
import sys
import tracemalloc
import tempfile
from pathlib import Path

from mesh_runner import config
from mesh_runner.effects import runtime_state
from mesh_runner.outcome import CONTRACT, MERGE_WAIT_GUIDANCE, contract, parse_line
from mesh_runner.prompt import no_outcome_prompt, start_prompt
from mesh_runner.store import iso
from fakes import FakeGitLab, FakeMesh, task, write_replies, FUTURE_AT
from test_runner import Bench

NOW = dt.datetime.now(dt.timezone.utc)      # parser tests only


def fresh():
    return dt.datetime.now(dt.timezone.utc)
SHA = "a" * 40
OTHER = "b" * 40
PROJECT = "g/app"
TARGET = f"{PROJECT}!56"
QUEUE = "g/app#77"
LINE = f"⏳ WAIT merge:{TARGET} after={SHA}"

GATE = """\
import os, sys, pathlib
here = pathlib.Path(__file__).parent
with open(here / "runs.log", "a") as f:
    f.write(" ".join(sys.argv[1:]) + "\\n")
with open(here / "seen.log", "a") as f:
    f.write(f"{os.getcwd()}|{os.environ.get('LANE_ID', '')}|{os.environ.get('RUNNER_SECRET', '')}\\n")
out = here / "out.txt"
print(out.read_text() if out.exists() else "", end="")
rc = here / "rc.txt"
sys.exit(int(rc.read_text()) if rc.exists() else 0)
"""


def mr_row(state="opened", sha=SHA, head=(10, "manual"), **extra):
    row = {"sha": sha, "state": state, "head_pipeline": {"id": head[0], "sha": sha, "status": head[1]}}
    row.update(extra)
    return row


class MergeBench(Bench):
    def setUp(self):
        super().setUp()
        self.gate_dir = Path(tempfile.mkdtemp(dir=self.root))
        self.gate = self.gate_dir / "gate.py"
        self.gate.write_text(GATE)
        self.cfg.merge_command = (sys.executable, str(self.gate), "{iid}", "{project}", "{sha}")
        self.cfg.merge_command_cwd = self.gate_dir

    def say(self, out="", rc=0):
        (self.gate_dir / "out.txt").write_text(out)
        (self.gate_dir / "rc.txt").write_text(str(rc))

    def runs(self):
        p = self.gate_dir / "runs.log"
        return p.read_text().splitlines() if p.exists() else []

    def waiting(self, **merge):
        t = task(1)
        gl = FakeGitLab()
        gl.mr_rows[TARGET] = mr_row()
        mesh = FakeMesh([t])
        r = self.runner(mesh, gl)
        r.refresh_me()
        a = r.store.create(self.agent.name, t["id"], "claude")
        r.store.update(a, state="waiting", wait={
            "kind": "merge", "target": TARGET, "merge": {"sha": SHA, "runs": 0, **merge},
            "since": iso(), "started": iso()})
        return r, a, t, gl

    def check(self, r, a):
        """One wake check; waits for a gate run it started."""
        r._wake_one(a)
        job = r._merge_jobs.get(a.id)
        if job:
            job["thread"].join(15)
        return r.store.get(a.id)


class Parser(MergeBench):
    def test_accepts_the_declaration_only_when_enabled(self):
        o = parse_line(LINE + ": SHIP on this head", NOW, merge=True)
        self.assertEqual((o.kind, o.target, o.extra), ("merge", TARGET, {"merge": {"sha": SHA}}))
        self.assertEqual(o.note, "SHIP on this head")
        self.assertTrue(o.is_wait)
        off = parse_line(LINE, NOW)
        self.assertEqual(off.kind, "unparsed")
        self.assertIn("not enabled", off.note)

    def test_rejects_malformed_declarations_with_a_reason(self):
        for text in (f"⏳ WAIT merge:{TARGET}", f"⏳ WAIT merge:{TARGET} after=abc",
                     f"⏳ WAIT merge:{TARGET} until=merged after={SHA}",
                     f"⏳ WAIT merge:g/app after={SHA}", f"⏳ WAIT merge:g/app!0 after={SHA}",
                     f"⏳ WAIT merge:app!56 after={SHA}", f"⏳ WAIT merge:-x/app!56 after={SHA}",
                     f"⏳ WAIT merge:{TARGET} after={SHA.upper()}"):
            with self.subTest(text=text):
                o = parse_line(text, NOW, merge=True)
                self.assertEqual(o.kind, "unparsed")
                self.assertIn("merge:<group>/<project>!<iid> after=<full-head-sha>", o.note)

    def test_contract_names_the_wait_only_when_enabled(self):
        guidance = "⏳ WAIT merge:<group>/<project>!<iid> after=<full-head-sha>"
        self.assertIn(guidance, contract(True))
        self.assertIn("Do not re-run the merge gate in a loop yourself", contract(True))
        for text in (CONTRACT, contract(), contract(False)):
            self.assertNotIn("WAIT merge:", text)
        self.assertEqual(contract(True).replace(MERGE_WAIT_GUIDANCE, ""), CONTRACT)
        t = task(1)
        for merge in (True, False):
            for text in (start_prompt(t, "builder", [], [], merge=merge),
                         no_outcome_prompt(t, "x", merge=merge)):
                self.assertEqual(guidance in text, merge)


class RenderedPrompts(MergeBench):
    def test_runner_renders_the_line_by_feature_state(self):
        t = task(1)
        for command, gitlab in ((self.cfg.merge_command, FakeGitLab()), ((), FakeGitLab()),
                                (self.cfg.merge_command, None)):
            with self.subTest(command=bool(command), gitlab=bool(gitlab)):
                self.cfg.merge_command = command
                r = self.runner(FakeMesh([t]), gitlab)
                self.assertEqual("WAIT merge:" in r._first_prompt(t, "work", []), r.merge_enabled)


class Config(MergeBench):
    def load(self, extra):
        p = self.root / "runner.toml"
        p.write_text(f'[runner]\nmesh_url = "http://mesh"\n{extra}\n')
        return config.load(p)

    def test_off_by_default_with_documented_defaults(self):
        c = self.load("")
        self.assertEqual((c.merge_command, c.merge_command_cwd, c.merge_timeout_minutes,
                          c.merge_rerun_minutes, c.merge_queue_hours), ((), None, 15.0, 5.0, 24.0))

    def test_reads_and_validates_the_keys(self):
        c = self.load('merge_command = ["gate", "{iid}", "{project}", "{sha}", "{target}"]\n'
                      'merge_command_cwd = "/srv/tools"\nmerge_timeout_minutes = 3\n'
                      'merge_rerun_minutes = 2.5\nmerge_queue_hours = 7')
        self.assertEqual(c.merge_command, ("gate", "{iid}", "{project}", "{sha}", "{target}"))
        self.assertEqual((str(c.merge_command_cwd), c.merge_timeout_minutes, c.merge_rerun_minutes,
                          c.merge_queue_hours), ("/srv/tools", 3.0, 2.5, 7.0))
        for bad in ('merge_command = "gate"', 'merge_command = ["gate", ""]',
                    'merge_command = ["gate", "{branch}"]', 'merge_max_runs = 0', 'merge_max_runs = 1.5', 'merge_queue_hours = 0',
                    'merge_timeout_minutes = -1', 'merge_rerun_minutes = "5"'):
            with self.subTest(bad=bad), self.assertRaises(config.ConfigError):
                self.load(bad)


class Declaration(MergeBench):
    def test_declaration_parks_a_merge_wait(self):
        t = task(1)
        gl = FakeGitLab()
        gl.mr_rows[TARGET] = mr_row()
        write_replies(self.replies, {t["id"][:8]: LINE})
        r = self.runner(FakeMesh([t]), gl)
        r.tick()
        self.settle_all(r)
        a = r.store.by_state(self.agent.name, ("waiting",))[0]
        self.assertEqual((a.wait["kind"], a.wait["target"], a.wait["merge"]["sha"]), ("merge", TARGET, SHA))
        r._merge_jobs[a.id]["thread"].join(15)
        self.assertEqual(self.runs(), [f"56 {PROJECT} {SHA}"])    # the gate ran at once, no model turn
        self.assertEqual(runtime_state(a, "builder"), {"kind": "waiting", "ref": f"mr:{TARGET}"})

    def test_feature_off_never_parks_or_runs_anything(self):
        for command, gitlab in ((), FakeGitLab()), (self.cfg.merge_command, None):
            with self.subTest(command=bool(command), gitlab=bool(gitlab)):
                self.cfg.merge_command = command
                r = self.runner(FakeMesh([task(1)]), gitlab)
                self.assertFalse(r.merge_enabled)
                o = r._parse(LINE, NOW)
                self.assertEqual(o.kind, "unparsed")
                self.assertIn("not enabled", o.note)

    def test_feature_off_sends_the_declaration_back(self):
        self.cfg.merge_command = ()
        t = task(1)
        write_replies(self.replies, {t["id"][:8]: LINE})
        r = self.runner(FakeMesh([t]), FakeGitLab())
        r.tick()
        self.settle_all(r)
        self.assertEqual(r.store.by_state(self.agent.name, ("waiting",)), [])
        self.assertEqual(self.runs(), [])


class BoundedOutput(MergeBench):
    FLOOD = ("import sys\n"
             "sys.stdout.write(('x' * 60 + '\\n') * 400000)\n"          # ~24 MB in 400k lines
             "sys.stdout.write('y' * 6000000 + '\\n')\n"                  # one line far over the cap
             "sys.stderr.write('z' * 3000000)\n"                          # and unterminated stderr
             "sys.stderr.write('\\n⏳ WAIT pipeline:g/app#77 — behind it\\n')\n"
             "sys.exit(3)\n")

    def test_huge_output_keeps_a_bounded_tail(self):
        r = self.runner(FakeMesh([task(1)]), FakeGitLab())
        argv = [sys.executable, "-c", self.FLOOD]
        tracemalloc.start()
        try:
            rc, tail = r._run_merge_command(argv, {"PATH": "/usr/bin:/bin"})
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        lines = tail.split("\n")
        self.assertEqual(rc, 3)
        self.assertLessEqual(len(lines), 40)
        self.assertTrue(all(len(ln) <= 2000 for ln in lines))
        self.assertEqual(lines[-1], "⏳ WAIT pipeline:g/app#77 — behind it")
        self.assertLess(peak, 8_000_000)           # the child wrote ~33 MB

    def test_huge_output_is_still_classified_by_its_last_line(self):
        self.cfg.merge_command = (sys.executable, "-c", self.FLOOD)
        r, a, t, gl = self.waiting()
        gl.pipeline_rows[QUEUE] = {"status": "running"}
        a = self.check(r, a)
        a = self.check(r, a)
        self.assertEqual(a.state, "waiting")
        self.assertEqual(a.wait["merge"]["pipeline"], QUEUE)

    def test_huge_failure_wakes_with_a_bounded_tail(self):
        self.cfg.merge_command = (sys.executable, "-c", self.FLOOD.replace("WAIT pipeline", "REFUSED"))
        r, a, t, gl = self.waiting()
        a = self.check(r, a)
        a = self.check(r, a)
        self.assertEqual(a.state, "ready")
        self.assertIn("REFUSED", a.wake_text)
        self.assertLess(len(a.wake_text), 10000)

    def test_timeout_with_endless_output_still_kills_and_returns(self):
        self.cfg.merge_timeout_minutes = 0.03
        r = self.runner(FakeMesh([task(1)]), FakeGitLab())
        rc, tail = r._run_merge_command(
            [sys.executable, "-c", "import sys\nwhile True: sys.stdout.write('spam\\n' * 1000)"], {})
        self.assertEqual(rc, -1)
        self.assertIn("killed after", tail)
        self.assertLessEqual(len(tail.split("\n")), 41)


class Gate(MergeBench):
    def test_runs_with_the_lane_environment_in_its_cwd(self):
        self.agent.env_command = [sys.executable, "-c", "print('LANE_ID=lane-7')"]
        import os
        os.environ["RUNNER_SECRET"] = "must-not-leak"
        self.addCleanup(os.environ.pop, "RUNNER_SECRET", None)
        r, a, t, gl = self.waiting()
        self.say("⏳ WAIT pipeline:g/app#77 — queued\n", rc=3)
        gl.pipeline_rows[QUEUE] = {"status": "running"}
        self.check(r, a)
        self.assertEqual(self.runs(), [f"56 {PROJECT} {SHA}"])
        cwd, lane, secret = (self.gate_dir / "seen.log").read_text().strip().split("|")
        self.assertEqual((Path(cwd).resolve(), lane, secret), (self.gate_dir.resolve(), "lane-7", ""))

    def test_merged_state_wakes_once_with_the_merge_commit(self):
        r, a, t, gl = self.waiting()
        self.say("merged ok\n", rc=0)
        a = self.check(r, a)
        self.assertEqual(a.state, "waiting")             # the command ran; the MR has not merged yet
        gl.mr_rows[TARGET] = mr_row("merged", merge_commit_sha="c" * 40)
        a = self.check(r, a)
        self.assertEqual(a.state, "ready")
        self.assertIn(f"MR !56 ({PROJECT}) merged at {'c' * 40}: deploy / verify", a.wake_text)
        self.assertEqual(len(self.runs()), 1)
        r.check_wakes()                                   # no longer waiting: nothing more happens
        self.assertEqual(len(self.runs()), 1)
        self.assertEqual(r.store.get(a.id).wake_text, a.wake_text)

    def test_squash_merge_reports_the_squash_commit(self):
        r, a, t, gl = self.waiting()
        gl.mr_rows[TARGET] = mr_row("merged", merge_commit_sha=None, squash_commit_sha="d" * 40)
        a = self.check(r, a)
        self.assertIn("merged at " + "d" * 40, a.wake_text)
        self.assertEqual(self.runs(), [])                 # already merged: no gate run needed

    def test_text_alone_is_not_a_merge(self):
        r, a, t, gl = self.waiting()
        self.say("  ✓ merged, sha=" + "c" * 40 + "\n", rc=0)
        a = self.check(r, a)                              # run
        a = self.check(r, a)                              # classify against the fresh MR read
        self.assertEqual(a.state, "ready")
        self.assertNotIn("merged at", a.wake_text)
        self.assertIn("did not merge", a.wake_text)

    def test_queued_gate_keeps_the_agent_asleep(self):
        r, a, t, gl = self.waiting()
        gl.pipeline_rows[QUEUE] = {"status": "running"}
        self.say("post-review jobs started\n⏳ WAIT pipeline:g/app#77 — behind it\n", rc=3)
        for _ in range(4):
            a = self.check(r, a)
            self.assertEqual(a.state, "waiting")
        self.assertEqual(len(self.runs()), 1)             # nothing changed: no second run
        self.assertEqual(a.wait["merge"]["pipeline"], QUEUE)
        self.assertEqual(len(a.wait["merge"]["history"]), 1)

    def test_reruns_when_the_queued_pipeline_settles(self):
        r, a, t, gl = self.waiting()
        gl.pipeline_rows[QUEUE] = {"status": "running"}
        self.say("⏳ WAIT pipeline:g/app#77 — behind it\n", rc=3)
        a = self.check(r, a)
        a = self.check(r, a)
        self.assertEqual(len(self.runs()), 1)
        gl.pipeline_rows[QUEUE] = {"status": "success"}
        a = self.check(r, a)
        self.assertEqual((len(self.runs()), a.state), (2, "waiting"))
        a = self.check(r, a)                              # classify, still queued, obs updated
        self.assertEqual(len(self.runs()), 2)

    def test_reruns_when_the_head_pipeline_settles(self):
        r, a, t, gl = self.waiting()
        self.say("⏳ WAIT pipeline:g/app#10 — own wave running\n", rc=3)
        gl.pipeline_rows["g/app#10"] = {"status": "running"}
        gl.mr_rows[TARGET] = mr_row(head=(10, "running"))
        a = self.check(r, a)
        a = self.check(r, a)
        self.assertEqual(len(self.runs()), 1)
        gl.pipeline_rows["g/app#10"] = {"status": "success"}
        gl.mr_rows[TARGET] = mr_row(head=(10, "success"))
        a = self.check(r, a)
        self.assertEqual(len(self.runs()), 2)

    def test_fallback_rerun_is_rate_limited(self):
        r, a, t, gl = self.waiting()
        gl.pipeline_rows[QUEUE] = {"status": "running"}
        self.say("⏳ WAIT pipeline:g/app#77 — behind it\n", rc=3)
        a = self.check(r, a)
        a = self.check(r, a)
        self.assertEqual(len(self.runs()), 1)
        wait = dict(a.wait)
        wait["merge"] = dict(wait["merge"], last_run=iso(fresh() - dt.timedelta(minutes=4)))
        r.store.update(a, wait=wait)
        a = self.check(r, a)
        self.assertEqual(len(self.runs()), 1)             # 4 min < merge_rerun_minutes (5)
        wait = dict(a.wait)
        wait["merge"] = dict(wait["merge"], last_run=iso(fresh() - dt.timedelta(minutes=6)))
        r.store.update(a, wait=wait)
        a = self.check(r, a)
        self.assertEqual((len(self.runs()), a.state), (2, "waiting"))

    def test_queue_polls_do_not_wake_before_the_queue_age_limit(self):
        # 40 settled-pipeline reruns used to wake the agent at merge_max_runs (30); it then re-armed
        # as a plain MR wait that never runs the gate, and the MRs queued behind it stalled.
        r, a, t, gl = self.waiting()
        self.say("⏳ WAIT pipeline:g/app#77 — behind it\n", rc=3)
        for n in range(40):
            gl.pipeline_rows[QUEUE] = {"status": "running" if n % 2 else "success"}
            a = self.check(r, a)                          # run
            a = self.check(r, a)                          # classify
            self.assertEqual(a.state, "waiting")
        self.assertEqual(len(self.runs()), 40)

    def test_queue_age_limit_wakes_with_the_history(self):
        self.cfg.merge_queue_hours = 2
        r, a, t, gl = self.waiting()
        gl.pipeline_rows[QUEUE] = {"status": "running"}
        self.say("⏳ WAIT pipeline:g/app#77 — behind it\n", rc=3)
        a = self.check(r, a)
        a = self.check(r, a)
        self.assertEqual((len(self.runs()), a.state), (1, "waiting"))
        wait = dict(a.wait)
        wait["merge"] = dict(wait["merge"], first_run=iso(fresh() - dt.timedelta(hours=3)),
                             last_run=iso(fresh() - dt.timedelta(minutes=6)))
        r.store.update(a, wait=wait)
        a = self.check(r, a)
        self.assertEqual((len(self.runs()), a.state), (1, "ready"))
        self.assertIn("still queued after 2 h", a.wake_text)
        self.assertIn("⏳ WAIT merge:", a.wake_text)
        self.assertIn("exit 3: ⏳ WAIT pipeline:g/app#77", a.wake_text)

    def test_moved_head_wakes_without_running_the_gate(self):
        r, a, t, gl = self.waiting()
        gl.mr_rows[TARGET] = mr_row(sha=OTHER)
        a = self.check(r, a)
        self.assertEqual(a.state, "ready")
        self.assertIn("head moved to " + OTHER[:12], a.wake_text)
        self.assertIn("new review is needed", a.wake_text)
        self.assertEqual(self.runs(), [])

    def test_head_moving_while_queued_wakes(self):
        r, a, t, gl = self.waiting()
        gl.pipeline_rows[QUEUE] = {"status": "running"}
        self.say("⏳ WAIT pipeline:g/app#77 — behind it\n", rc=3)
        a = self.check(r, a)
        a = self.check(r, a)
        gl.mr_rows[TARGET] = mr_row(sha=OTHER)
        a = self.check(r, a)
        self.assertEqual(a.state, "ready")
        self.assertIn("head moved", a.wake_text)

    def test_refusal_wakes_with_the_tail(self):
        r, a, t, gl = self.waiting()
        lines = [f"line {i}" for i in range(60)] + ["HOLD: label hold is set"]
        self.say("\n".join(lines) + "\n", rc=2)
        a = self.check(r, a)
        a = self.check(r, a)
        self.assertEqual(a.state, "ready")
        self.assertIn("failed (exit 2)", a.wake_text)
        self.assertIn("HOLD: label hold is set", a.wake_text)
        self.assertIn("line 21", a.wake_text)
        self.assertNotIn("line 20\n", a.wake_text)        # only the last 40 lines
        self.assertEqual(len(self.runs()), 1)

    def test_other_wait_lines_are_not_a_queue(self):
        for last in ("⏳ WAIT mr:g/app!56 — no pipeline yet", "⏳ WAIT 2h: pending jobs cannot be read",
                     "⏳ WAIT pipeline:g/app!56 — wrong kind"):
            with self.subTest(last=last):
                r, a, t, gl = self.waiting()
                self.say(last + "\n", rc=3)
                a = self.check(r, a)
                a = self.check(r, a)
                self.assertEqual(a.state, "ready")
                self.assertIn(last, a.wake_text)
                r.store.update(a, state="completed")

    def test_closed_mr_wakes(self):
        r, a, t, gl = self.waiting()
        gl.mr_rows[TARGET] = mr_row("closed")
        a = self.check(r, a)
        self.assertEqual(a.state, "ready")
        self.assertIn("closed without merge", a.wake_text)

    def test_missing_command_wakes(self):
        self.cfg.merge_command = ("/nonexistent/gate", "{iid}")
        r, a, t, gl = self.waiting()
        a = self.check(r, a)
        a = self.check(r, a)
        self.assertEqual(a.state, "ready")
        self.assertIn("could not run the merge command", a.wake_text)

    def test_timeout_kills_the_command_and_wakes(self):
        self.cfg.merge_command = (sys.executable, "-c", "import time; print('start', flush=True); time.sleep(30)")
        self.cfg.merge_timeout_minutes = 0.03
        r, a, t, gl = self.waiting()
        a = self.check(r, a)
        a = self.check(r, a)
        self.assertEqual(a.state, "ready")
        self.assertIn("killed after", a.wake_text)
        self.assertIn("start", a.wake_text)

    def test_unreadable_mr_keeps_waiting_then_tells(self):
        r, a, t, gl = self.waiting()
        gl.mr_rows[TARGET] = {"state": "weird"}
        for _ in range(4):
            a = self.check(r, a)
            self.assertEqual(a.state, "waiting")
        a = self.check(r, a)
        self.assertEqual(a.state, "ready")
        self.assertIn("cannot read MR", a.wake_text)
        self.assertEqual(self.runs(), [])

    def test_someone_elses_comment_wakes(self):
        r, a, t, gl = self.waiting()
        r.mesh.add_comment(t["id"], "hold on, do not merge", author="agent-lead",
                           at=fresh() + dt.timedelta(seconds=5))
        a = self.check(r, a)
        self.assertEqual(a.state, "ready")
        self.assertIn("commented: hold on", a.wake_text)
        self.assertEqual(self.runs(), [])

    def foreign_agent_comment(self, r, t, body, author_type="agent"):
        c = r.mesh.add_comment(t["id"], body, author="agent-lead", at=fresh() + dt.timedelta(seconds=5))
        c["author_type"] = author_type

    def test_addressed_mode_holds_a_foreign_agent_comment_on_a_merge_wait(self):
        self.cfg.comment_wake = "addressed"
        r, a, t, gl = self.waiting()
        self.foreign_agent_comment(r, t, "Combined handback (FYI, same grant).")
        a = self.check(r, a)
        self.assertEqual(a.state, "waiting")          # the held note ends nothing; the gate goes on running
        self.assertEqual(len(self.runs()), 1)

    def test_addressed_mode_still_wakes_a_merge_wait_on_a_mention_or_a_person(self):
        self.cfg.comment_wake = "addressed"
        r, a, t, gl = self.waiting()
        self.foreign_agent_comment(r, t, "@builder hold on, do not merge")
        a = self.check(r, a)
        self.assertEqual(a.state, "ready")
        self.assertIn("commented: @builder hold on", a.wake_text)
        self.assertEqual(self.runs(), [])

    def test_addressed_mode_wakes_a_merge_wait_on_a_persons_comment(self):
        self.cfg.comment_wake = "addressed"
        r, a, t, gl = self.waiting()
        self.foreign_agent_comment(r, t, "hold on", author_type="user")
        a = self.check(r, a)
        self.assertEqual(a.state, "ready")

    def test_addressed_mode_wakes_a_merge_wait_when_the_mr_merges_despite_a_held_comment(self):
        self.cfg.comment_wake = "addressed"
        r, a, t, gl = self.waiting()
        self.foreign_agent_comment(r, t, "Combined handback (FYI, same grant).")
        gl.mr_rows[TARGET] = mr_row(state="merged")
        a = self.check(r, a)
        self.assertEqual(a.state, "ready")
        self.assertIn("merged at", a.wake_text)

    def test_env_failure_does_not_run_the_gate(self):
        self.agent.env_command = [sys.executable, "-c", "import sys; sys.exit(3)"]
        r, a, t, gl = self.waiting()
        for _ in range(2):
            a = self.check(r, a)
            self.assertEqual(a.state, "waiting")
        a = self.check(r, a)
        self.assertEqual(a.state, "ready")
        self.assertIn("cannot run the merge gate", a.wake_text)
        self.assertEqual(self.runs(), [])

    def test_merge_is_progress_for_the_wait_budget_and_a_failure_is_not(self):
        r, a, t, gl = self.waiting()
        gl.mr_rows[TARGET] = mr_row("merged", merge_commit_sha="c" * 40)
        a = self.check(r, a)
        self.assertEqual(r.store.mark(self.agent.name, t["id"], "met_wake")[0], a.id)

    def test_closed_is_not_progress(self):
        r, a, t, gl = self.waiting()
        gl.mr_rows[TARGET] = mr_row("closed")
        self.check(r, a)
        self.assertFalse(r.store.mark(self.agent.name, t["id"], "met_wake"))


class ReleasedWorkspace(MergeBench):
    """merge / mr(merged) waits return the worktree after the threshold; pipeline waits keep it."""

    serial = 100

    def parked(self, wait, repo_text="work in progress"):
        self.agent.workspace_wait_release_minutes = 30
        ReleasedWorkspace.serial += 1
        t = task(self.serial)
        gl = FakeGitLab()
        gl.mr_rows[TARGET] = mr_row()
        r = self.runner(FakeMesh([t]), gl)
        r.refresh_me()
        a = r.store.create(self.agent.name, t["id"], "claude")
        path = r.pool.lease(a.id, "app")
        Path(path).joinpath("README").write_text(repo_text)
        old = (fresh() - dt.timedelta(hours=2)).isoformat()
        r.store.update(a, state="waiting", workspaces=[str(path)],
                       wait=dict(wait, target=TARGET, since=iso(), started=old))
        return r, r.store.get(a.id), t, gl

    def test_merge_and_merged_mr_waits_release_after_the_threshold(self):
        merge = {"kind": "merge", "merge": {"sha": SHA, "runs": 0}}
        mr = {"kind": "mr", "mr_state": {"predicate": "merged", "sha": SHA, "state": "opened"}}
        for wait in (merge, mr):
            with self.subTest(kind=wait["kind"]):
                r, a, t, gl = self.parked(wait)
                r._suspend_waiting(a)
                self.assertEqual(r.pool.leased_to(a.id), [])
                rows = r.pool._checkpoint_rows(a.id)
                self.assertTrue(rows)
                self.assertTrue(all(row["phase"] == "released" for row in rows))
                self.assertIn("workspace_suspended", [e[1] for e in r.store.events(a.id)])

    def test_not_before_the_threshold(self):
        r, a, t, gl = self.parked({"kind": "merge", "merge": {"sha": SHA, "runs": 0}})
        r.store.update(a, wait=dict(a.wait, started=iso()))
        r._suspend_waiting(r.store.get(a.id))
        self.assertTrue(r.pool.leased_to(a.id))

    def test_pipeline_and_ci_watching_mr_waits_keep_the_worktree(self):
        for wait in ({"kind": "pipeline"},
                     {"kind": "mr", "ci_change": {"predicate": "change", "sha": SHA}},
                     {"kind": "mr"}):
            with self.subTest(wait=wait):
                r, a, t, gl = self.parked(wait)
                r._suspend_waiting(a)
                self.assertTrue(r.pool.leased_to(a.id))
                self.assertEqual(r.pool._checkpoint_rows(a.id), [])

    def test_mr_waits_without_the_merged_contract_keep_the_worktree(self):
        for wait in ({"kind": "mr", "mr_state": "merged"},                         # not a contract
                     {"kind": "mr", "mr_state": {"predicate": "change", "state": "opened"}},
                     {"kind": "mr", "mr_state": {"predicate": "merged", "state": "merged"}}):
            with self.subTest(wait=wait):
                r, a, t, gl = self.parked(wait)
                r._suspend_waiting(a)
                self.assertTrue(r.pool.leased_to(a.id))
                self.assertEqual(r.pool._checkpoint_rows(a.id), [])

    def test_released_merge_wait_runs_the_gate_wakes_and_resumes_with_the_files(self):
        r, a, t, gl = self.parked({"kind": "merge", "merge": {"sha": SHA, "runs": 0}}, "unsaved edit")
        r._suspend_waiting(a)
        self.assertEqual(r.pool.leased_to(a.id), [])
        rows = r.pool._checkpoint_rows(a.id)
        checkpoint = rows[0]["checkpoint"]
        self.assertTrue(Path(checkpoint).exists())
        self.say("gate ran\n", rc=0)
        a = self.check(r, r.store.get(a.id))
        self.assertEqual(a.state, "waiting")
        self.assertEqual(self.runs(), [f"56 {PROJECT} {SHA}"])       # the gate ran with the tree released
        cwd = (self.gate_dir / "seen.log").read_text().split("|")[0]
        self.assertEqual(Path(cwd).resolve(), self.gate_dir.resolve())
        self.assertEqual(r.pool.leased_to(a.id), [])
        gl.mr_rows[TARGET] = mr_row("merged", merge_commit_sha="c" * 40)
        a = self.check(r, a)
        self.assertEqual(a.state, "ready")
        self.assertIn("merged at " + "c" * 40, a.wake_text)
        write_replies(self.replies, {t["id"][:8]: f"⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 verifying"})
        r._resume_one(a)
        resumed = r.store.get(a.id)
        self.assertEqual(resumed.state, "running")
        self.assertEqual(Path(resumed.workspaces[0]).joinpath("README").read_text(), "unsaved edit")
        self.settle_all(r)


class IdleBudget(MergeBench):
    """A merge wait hands the next step to the runner's own gate; the idle-wait cap must not
    pre-empt it (a capped task waits for new input, and nothing can change an MR whose remaining
    jobs only the gate starts)."""

    def setUp(self):
        super().setUp()
        self.cfg.requeue_on_cap = True
        self.cfg.daily_wait_budget = 2

    def run_turn(self, line, waits=3, nexts=0, steps=0):
        """One turn ending in ``line`` on a task that already spent some of today's budgets."""
        t = task(1)
        gl = FakeGitLab()
        gl.mr_rows[TARGET] = mr_row()
        write_replies(self.replies, {t["id"][:8]: line})
        r = self.runner(FakeMesh([t], max_concurrent=1), gl)
        r.refresh_me()
        prev = r.store.create(self.agent.name, t["id"], "claude")
        for kind, count in (("wait", waits), ("next", nexts), ("step", steps)):
            for _ in range(count):
                r.store.outcome(prev, kind)
        r.store.update(prev, state="completed", turns=1, note="earlier turns")
        r.tick()
        self.finish_turn(r)
        return r, t

    def capped(self, r, t):
        notes = [a.note for a in r.store.by_state(self.agent.name, ("completed", "failed"))
                 if a.task_id == t["id"] and a.note.startswith("cap:")]
        return bool(notes) and r._cap_wait(t["id"]) is not None

    def test_merge_wait_over_the_idle_budget_still_parks_and_runs_the_gate(self):
        r, t = self.run_turn(LINE)
        self.assertFalse(self.capped(r, t))
        waiting = r.store.by_state(self.agent.name, ("waiting",))
        self.assertEqual(len(waiting), 1)
        self.assertEqual((waiting[0].wait["kind"], waiting[0].wait["merge"]["sha"]), ("merge", SHA))
        r._wake_one(waiting[0])
        r._merge_jobs[waiting[0].id]["thread"].join(15)
        self.assertEqual(self.runs(), [f"56 {PROJECT} {SHA}"])       # the gate the cap used to starve

    def test_the_merge_wait_is_exempt_from_the_stop_not_from_the_count(self):
        r, t = self.run_turn(LINE)
        self.assertEqual(r.store.idle_waits_today(self.agent.name, t["id"]), 4)    # 3 earlier + this one

    def test_time_wait_over_the_idle_budget_is_still_capped(self):
        r, t = self.run_turn(f"⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 soak window")
        self.assertTrue(self.capped(r, t))
        self.assertEqual(r.store.by_state(self.agent.name, ("waiting",)), [])

    def test_pipeline_wait_over_the_idle_budget_is_still_capped(self):
        r, t = self.run_turn("⏳ WAIT pipeline:g/app#77: build")
        self.assertTrue(self.capped(r, t))
        self.assertEqual(r.store.by_state(self.agent.name, ("waiting",)), [])

    def test_merge_wait_does_not_forgive_the_next_budget(self):
        self.cfg.daily_next_budget = 1
        r, t = self.run_turn(LINE, waits=0, nexts=2)
        self.assertTrue(self.capped(r, t))
        self.assertEqual(r.store.by_state(self.agent.name, ("waiting",)), [])

    def test_merge_wait_does_not_forgive_the_step_budget(self):
        self.cfg.daily_step_budget = 1
        r, t = self.run_turn(LINE, waits=0, steps=2)
        self.assertTrue(self.capped(r, t))
        self.assertEqual(r.store.by_state(self.agent.name, ("waiting",)), [])

    def test_a_merge_wait_the_runner_refuses_is_not_parked_and_the_cap_is_unaffected(self):
        self.cfg.merge_command = ()                  # feature off: the declaration is sent back unparsed
        r, t = self.run_turn(LINE)
        self.assertEqual(r.store.by_state(self.agent.name, ("waiting",)), [])
        self.assertEqual(self.runs(), [])
