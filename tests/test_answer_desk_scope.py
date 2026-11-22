"""Desk scope stays with the recipient's workspace key; queue time is not answering time."""
import json
import sys
from types import SimpleNamespace

from mesh_runner import answer_desk
from mesh_runner.adapters.claude import ClaudeAdapter
from mesh_runner.adapters.codex import CodexAdapter
from mesh_runner.answer_desk import AnswerDesk, desk_plan
from mesh_runner.host_admission import count_leases, start_lock
from mesh_runner.interactive import state_root
from mesh_runner.store import Store

from fakes import FakeMesh, task
from test_answer_desk import Desk, QUESTION, REQUEST, lead_lane


class Scope(Desk):
    def test_the_recipient_uses_its_serving_lane_for_the_source_workspace(self):
        t = dict(task(1), workspace_id="ws2")
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.run_turn(mesh, t)
        main = lead_lane(mesh)
        lab = lead_lane(mesh, name="lead-lab", workspace="ws2")
        lab.me.update(name="lead", slug="lead")
        self.cfg.cardless_question_lanes = ["lead", "lead-lab"]
        desk = AnswerDesk(self.cfg, [r, main, lab], Store(self.root / "scope.sqlite"), clock=self.clock,
                          spawn=lambda fn: fn(), run_turn=lambda *args: "end_turn").attach()
        desk.step()
        self.assertIs(desk.recipient({"id": "agent-lead", "name": "lead"}, "ws2"), lab)
        wait = {"decision_request_id": REQUEST, "question": QUESTION}
        self.assertTrue(desk.defer(r.agent.name, SimpleNamespace(task_id=t["id"], task8=t["id"][:8]),
                                  t, wait, {"id": "agent-lead", "name": "lead"}, r.me))
        self.assertEqual(wait["cardless"]["lane"], "lead-lab")
        self.assertEqual(desk.state("lead", REQUEST), "")
        self.assertEqual(desk.state("lead-lab", REQUEST), "queued")

    def test_an_unserved_workspace_does_not_use_another_workspaces_key(self):
        t = dict(task(1), workspace_id="ws2")
        mesh = FakeMesh([t])
        r = self.run_turn(mesh, t)
        lead = lead_lane(mesh)
        desk = self.desk(mesh, r, lead)
        desk.step()
        wait = {"decision_request_id": REQUEST}
        self.assertFalse(desk.defer(r.agent.name, SimpleNamespace(task_id=t["id"], task8=t["id"][:8]),
                                   t, wait, {"id": "agent-lead", "name": "lead"}, r.me))
        self.assertNotIn("cardless", wait)
        self.assertEqual(desk.state("lead", REQUEST), "")

    def test_a_headless_serving_lane_answers_without_a_request_card(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        lead = lead_lane(mesh)
        lead.launch_mode = "headless"
        r, lead, desk, a = self.parked(mesh, t, lead=lead)
        self.assertIn("cardless", a.wait)
        desk.step()
        self.assertEqual((len(self.turns), mesh.created), (1, []))
        self.assertEqual(desk.state("lead", REQUEST), "answered")


class AnsweringClock(Desk):
    def test_a_paused_lane_falls_back_before_the_restarted_desk_is_probed(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r, lead, desk, a = self.parked(mesh, t)
        desk.live.clear()
        lead.is_paused = True
        self.assertEqual(desk.still_waiting(a.wait), "paused")

    def test_twenty_minutes_waiting_for_memory_does_not_file_a_card(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r, lead, desk, a = self.parked(mesh, t)
        lead.memgate.holding = True
        self.clock.advance(25)
        desk.step()
        r.tick()
        self.assertEqual((self.turns, mesh.created), ([], []))
        self.assertEqual(desk.state("lead", REQUEST), "queued")
        lead.memgate.holding = False
        desk.step()
        self.assertEqual(desk.state("lead", REQUEST), "answered")

    def test_host_busy_wait_does_not_spend_the_answering_deadline(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r, lead, desk, a = self.parked(mesh, t)
        desk.run_turn = lambda *args: answer_desk.HOST_BUSY
        desk.step()
        self.clock.advance(25)
        desk.step()
        r.tick()
        self.assertEqual(mesh.created, [])
        self.assertEqual(desk.state("lead", REQUEST), "queued")
        self.assertFalse(desk._get("lead", REQUEST).get("first_turn"))

    def test_twenty_minutes_start_at_the_first_delivered_turn_and_survive_retry(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r, lead, desk, a = self.parked(mesh, t)
        self.clock.advance(25)
        desk._delivered("lead", REQUEST)
        first = desk._get("lead", REQUEST)["first_turn"]
        self.clock.advance(19)
        self.assertEqual(desk.still_waiting(a.wait), "")
        desk._delivered("lead", REQUEST)  # a reminder/recovery cannot restart the clock
        self.assertEqual(desk._get("lead", REQUEST)["first_turn"], first)
        self.clock.advance(1)
        self.assertIn("20 minutes", desk.still_waiting(a.wait))


class NativeTurn(Desk):
    def native(self, harness="claude"):
        # External boundary: a native harness executable, exercised as a real subprocess.
        executable = self.root / "desk-native"
        output = self.root / "native-input.json"
        executable.write_text(f"#!{sys.executable}\n" + """
import json, os, sys
prompt = sys.stdin.read()
with open(os.environ['DESK_TEST_INPUT'], 'w') as out:
    json.dump({'argv': sys.argv, 'cwd': os.getcwd(), 'prompt': prompt}, out)
if os.environ['DESK_TEST_HARNESS'] == 'codex':
    sid = sys.argv[sys.argv.index('resume') + 1] if 'resume' in sys.argv else '872a0352-1db3-4126-8432-235194015a03'
    print(json.dumps({'type': 'thread.started', 'thread_id': sid}), flush=True)
    print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'Answer: ship'}}), flush=True)
    print(json.dumps({'type': 'turn.completed', 'usage': {}}), flush=True)
else:
    key = '--resume' if '--resume' in sys.argv else '--session-id'
    sid = sys.argv[sys.argv.index(key) + 1]
    directory = os.path.join(os.environ['CLAUDE_CONFIG_DIR'], 'projects', 'desk')
    os.makedirs(directory, exist_ok=True)
    open(os.path.join(directory, sid + '.jsonl'), 'a').close()
    print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': sid}), flush=True)
    print(json.dumps({'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'Answer: ship'}]}}), flush=True)
    print(json.dumps({'type': 'result', 'is_error': False}), flush=True)
""")
        executable.chmod(0o700)
        ad = (CodexAdapter(str(executable), ["--dangerously-bypass-approvals-and-sandbox"], profile="desk-native")
              if harness == "codex" else ClaudeAdapter(str(executable)))
        lead = lead_lane(FakeMesh([task(1)]))
        lead.launch_mode = "headless"
        lead.agent.profile_dir = self.root
        lead.agent.harness = harness
        lead.cfg = self.cfg
        lead._read_env = lambda: {"DESK_TEST_INPUT": str(output), "CLAUDE_CONFIG_DIR": str(self.root / "claude"),
                                  "CODEX_HOME": str(self.root / "codex"), "DESK_TEST_HARNESS": harness}
        lead._route_env = lambda raw: (dict(raw), harness, None)
        lead.adapter_for = lambda harness: ad
        return lead, output

    def test_native_turn_preserves_confinement_and_resumes_the_source_session(self):
        lead, output = self.native()
        desk = AnswerDesk(self.cfg, [lead], Store(self.root / "native.sqlite"), clock=self.clock,
                          tmux=SimpleNamespace(all_windows=lambda: [], kill_window=lambda *args: None))
        self.cfg.decision_request_metadata_only_recipients = [lead.me["id"]]
        t = task(1)
        desk._set("lead", REQUEST, {"state": "running", "at": desk._iso(), "task": t["id"]})
        self.assertEqual(desk._drive(lead, REQUEST, t["id"], "first question"), "end_turn")
        delivered = json.loads(output.read_text())
        self.assertEqual((delivered["cwd"], delivered["prompt"]), (str(self.root), "first question"))
        self.assertIn("--disallowedTools", delivered["argv"])
        self.assertNotIn("--add-dir", delivered["argv"])
        self.assertTrue(desk._get("lead", REQUEST).get("first_turn"))
        argv, *_ = desk_plan(lead, t["id"], "second question")({"turn_id": "second"})
        self.assertIn("--resume", argv)
        self.assertNotIn("--key", argv)
        with start_lock(state_root(self.cfg.state_dir)):
            self.assertEqual(count_leases(state_root(self.cfg.state_dir)), 0)

    def test_a_native_turn_obeys_the_same_host_cap_and_stays_undelivered(self):
        from mesh_runner.host_admission import HostAdmission
        import time
        lead, output = self.native()
        self.cfg.interactive_max_sessions = 1
        tmux = SimpleNamespace(all_windows=lambda: [], kill_window=lambda *args: None)
        desk = AnswerDesk(self.cfg, [lead], Store(self.root / "native.sqlite"), clock=self.clock, tmux=tmux)
        desk._set("lead", REQUEST, {"state": "running", "at": desk._iso(), "task": task(1)["id"]})
        with HostAdmission(state_root(self.cfg.state_dir), 1, tmux).acquire(time.monotonic() + 1):
            self.assertEqual(desk._drive(lead, REQUEST, task(1)["id"], "blocked"), answer_desk.HOST_BUSY)
        self.assertFalse(output.exists())
        self.assertFalse(desk._get("lead", REQUEST).get("first_turn"))

    def test_native_codex_records_its_thread_and_confines_the_metadata_only_recipient(self):
        lead, output = self.native("codex")
        desk = AnswerDesk(self.cfg, [lead], Store(self.root / "codex.sqlite"), clock=self.clock,
                          tmux=SimpleNamespace(all_windows=lambda: [], kill_window=lambda *args: None))
        self.cfg.decision_request_metadata_only_recipients = [lead.me["id"]]
        t = task(1)
        desk._set("lead", REQUEST, {"state": "running", "at": desk._iso(), "task": t["id"]})
        self.assertEqual(desk._drive(lead, REQUEST, t["id"], "codex question"), "end_turn")
        first = json.loads(output.read_text())
        argv = first["argv"]
        self.assertEqual(argv[argv.index("--sandbox") + 1], "read-only")
        self.assertLess(argv.index("--sandbox"), argv.index("exec"))
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)
        self.assertEqual(first["cwd"], str(self.root))
        self.assertEqual(first["prompt"], "codex question")
        self.assertTrue(desk._get("lead", REQUEST).get("first_turn"))
        self.assertEqual(desk._drive(lead, REQUEST, t["id"], "codex reminder"), "end_turn")
        second = json.loads(output.read_text())
        argv = second["argv"]
        self.assertEqual(argv[argv.index("resume") + 1], "872a0352-1db3-4126-8432-235194015a03")
        self.assertEqual(second["prompt"], "codex reminder")
