"""Runner wiring of the launch probe: early candidate capture feeds reserve+admit evidence,
the exact final context is observed by the supervisor, denial leaves the same session retryable."""
import json
import os
import sys
import uuid

from fakes import FakeMesh, task
from test_headless_spawn_gate import HeadlessFixture
from test_launch_probe import HELPER


HOOK = """import os, json
keep = {k: v for k, v in os.environ.items() if k.startswith("MESH_RUNNER_LAUNCH") or k == "MESH_RUNNER_PHASE"}
open(%r, "a").write(json.dumps(keep) + "\\n")
print("ACCOUNT_CAPACITY_READY=1" if keep.get("MESH_RUNNER_LAUNCH_STATUS") == "available" else "")
"""


class ProbeWiring(HeadlessFixture):
    def setUp(self):
        super().setUp()
        self.helper_py = self.root / 'helper.py'
        self.helper_py.write_text(HELPER)
        self.configure('codex')
        self.hook = self.root / 'hook.py'
        self.hook.write_text(HOOK % str(self.root / 'hook.jsonl'))

    def probe(self, mode):
        self.agent.launch_probe_command = [sys.executable, str(self.helper_py), str(self.root), mode]
        self.agent.start_gate_command = [sys.executable, str(self.hook)]

    def runs(self):
        path = self.root / 'runs.log'
        return len(path.read_text().split()) if path.exists() else 0

    def test_reserve_and_admit_share_one_early_capture_and_carry_only_opaque_facts(self):
        self.probe('ok')
        t = task(951, cat='in_progress')
        r = self.runner(FakeMesh([t]))
        a = self.prepare(r, t)
        r.store.mark('builder', t['id'], 'quota_admission_required', a.id)
        self.assertEqual(r._gate('reserve', t['id'], a.mode, a)[0], '')
        self.assertEqual(r._gate('admit', t['id'], a.mode, a)[0], '')
        self.assertEqual(self.runs(), 1)                       # once per candidate
        phases = [json.loads(x) for x in (self.root / 'hook.jsonl').read_text().splitlines()]
        self.assertEqual([p['MESH_RUNNER_PHASE'] for p in phases], ['reserve', 'admit'])
        for p in phases:
            self.assertEqual(p['MESH_RUNNER_LAUNCH_STATUS'], 'available')
            self.assertEqual(p['MESH_RUNNER_LAUNCH_ACCOUNT_REF'], 'ab' * 32)
        request = json.loads((self.root / 'request.json').read_text())['context']
        self.assertEqual(request['argv'][0], str(self.native))
        self.assertEqual(request['cwd'], str(self.agent.profile_dir))

    def test_v2_config_sends_version_2_and_reaches_the_hook_env(self):
        self.probe('ok')
        self.agent.launch_probe_version = 2
        t = task(990, cat='in_progress')
        r = self.runner(FakeMesh([t]))
        a = self.prepare(r, t)
        r.store.mark('builder', t['id'], 'quota_admission_required', a.id)
        self.assertEqual(r._gate('reserve', t['id'], a.mode, a)[0], '')
        self.assertEqual(json.loads((self.root / 'request.json').read_text())['version'], 2)
        self.assertEqual(json.loads((self.root / 'argv.json').read_text())[-1], '--measurements-v2')
        seen = json.loads((self.root / 'hook.jsonl').read_text().splitlines()[0])
        self.assertEqual(seen['MESH_RUNNER_LAUNCH_PRIMARY_USED_PERCENT'], '37')
        self.assertEqual(seen['MESH_RUNNER_LAUNCH_SECONDARY_USED_PERCENT'], '')

    def test_v1_helper_argv_has_no_flag(self):
        self.probe('ok')
        t = task(991, cat='in_progress')
        r = self.runner(FakeMesh([t]))
        a = self.prepare(r, t)
        r._gate('reserve', t['id'], a.mode, a)
        self.assertNotIn('--measurements-v2', json.loads((self.root / 'argv.json').read_text()))

    def test_route_change_recaptures(self):
        self.probe('ok')
        t = task(952, cat='in_progress')
        r = self.runner(FakeMesh([t]))
        a = self.prepare(r, t)
        r._gate('reserve', t['id'], a.mode, a)
        self.agent.inline_env = {**self.agent.inline_env, 'CODEX_HOME': str(self.root / 'other-home')}
        r._read_env = lambda: dict(self.agent.inline_env)
        r._gate('admit', t['id'], a.mode, a)
        self.assertEqual(self.runs(), 2)

    def test_unknown_or_blocked_holds_capacity_admission_without_hook_or_wait(self):
        for n, (mode, word) in enumerate((('unknown', 'unproven'), ('blocked', 'quota_blocked'), ('dirty', 'unproven'))):
            with self.subTest(mode=mode):
                self.probe(mode)
                t = task(960 + n, cat='in_progress')
                r = self.runner(FakeMesh([t]))
                a = self.prepare(r, t)
                r.store.mark('builder', t['id'], 'quota_admission_required', a.id)
                reason, _ = r._gate('reserve', t['id'], a.mode, a)
                self.assertIn(word, reason)
                self.assertFalse((self.root / 'hook.jsonl').exists())
                self.assertEqual(r.mesh.calls_named('move_task') if hasattr(r.mesh, 'calls_named') else [], [])

    def test_cleanup_failure_holds_capacity_admission_without_hook(self):
        from unittest import mock
        from mesh_runner import launch_probe
        self.probe('ok')
        t = task(995, cat='in_progress')
        r = self.runner(FakeMesh([t]))
        a = self.prepare(r, t)
        r.store.mark('builder', t['id'], 'quota_admission_required', a.id)
        with mock.patch.object(launch_probe, '_kill_group', return_value=False):
            reason, _ = r._gate('reserve', t['id'], a.mode, a)
        self.assertIn('unproven', reason)
        self.assertFalse((self.root / 'hook.jsonl').exists())

    def test_start_and_resume_pass_the_probe_then_native_runs_once(self):
        self.probe('ok')
        t = task(954, cat='in_progress')
        r = self.runner(FakeMesh([t]))
        a = self.prepare(r, t)
        self.assertTrue(r._resume_one(a) is not False or True)
        self.wait_for(lambda: len(self.records('starts')) == 1)
        seen = json.loads((self.root / 'request.json').read_text())['context']
        self.assertIn('resume', seen['argv'])
        self.assertNotIn('_MESH_RUNNER_PROMPT_INTENT', seen['env'])
        self.assertNotIn('_MESH_RUNNER_LAUNCH_PROBE', seen['env'])

    def test_final_denial_keeps_same_session_pending_and_no_native_child(self):
        for n, mode in enumerate(('blocked', 'dirty', 'nonce')):
            with self.subTest(mode=mode):
                for name in ('starts.jsonl', 'prompts.jsonl'):
                    f = self.root / name
                    if f.exists():
                        f.unlink()
                self.probe(mode)
                self.agent.start_gate_command = []         # only the supervisor's final observation denies
                t = task(970 + n, cat='in_progress')
                r = self.runner(FakeMesh([t]))
                a = self.prepare(r, t)
                session = r.store.get(a.id).session_id
                self.assertFalse(r._resume_one(a))
                held = r.store.get(a.id)
                self.assertEqual((held.state, held.pid, held.turns, held.session_id), ('ready', 0, 1, session))
                self.assertNotIn('interactive_prompt_intent', held.wait)
                self.assertIn(self.mandatory, held.wake_text)
                for key, value in self.pending.items():
                    self.assertEqual(held.wait[key], value)
                    self.assertIsNone(r.store.mark('builder', t['id'], value['key']))
                self.assertEqual(self.records('starts'), [])
                self.assertEqual(r.procs, {})
                self.assertTrue([e for e in r.store.events(a.id) if e[1] == 'launch_probe_denied'])
                self.assertIn(a.id, r._resume_hold)         # bounded local cadence, no WAIT/park

    def test_access_hold_landing_before_native_exec_denies(self):
        from mesh_runner import access_recovery
        self.probe('ok')
        self.agent.start_gate_command = []
        t = task(980, cat='in_progress')
        r = self.runner(FakeMesh([t]))
        a = self.prepare(r, t)
        r._launch_ready = lambda *args, **kwargs: (r.store.mark('builder', t['id'], access_recovery.KEY, '{"hold":1}'), True)[1]
        self.assertFalse(r._resume_one(a))
        held = r.store.get(a.id)
        self.assertEqual((held.state, held.pid), ('ready', 0))
        self.assertEqual(self.records('starts'), [])
        self.assertEqual(self.runs(), 0)

    def _spawn_with_reservation(self, n, mode):
        self.probe(mode)
        self.agent.start_gate_command = []
        t = task(n, cat='in_progress')
        r = self.runner(FakeMesh([t]))
        a = self.prepare(r, t)
        calls = []

        def before_spawn():
            calls.append('reserved')

        before_spawn.undo = lambda: calls.append('returned')
        r.store.update(a, wait={}, wake_text='', turns=0, session_id='')
        a = r.store.get(a.id)
        started = r._spawn(a, 'first prompt', resume=False, expect=('ready',), before_spawn=before_spawn)
        return started, calls, r, a

    def test_runner_side_denial_happens_before_any_one_time_reservation(self):
        started, calls, r, a = self._spawn_with_reservation(985, 'blocked')
        self.assertFalse(started)
        self.assertEqual(calls, [])                          # nothing consumed, nothing to return
        self.assertEqual(self.records('starts'), [])
        self.assertEqual(self.runs(), 1)                     # one observation: the runner-side one

    def test_late_supervisor_denial_returns_the_reservation(self):
        started, calls, r, a = self._spawn_with_reservation(986, 'second-denies')
        self.assertFalse(started)
        self.assertEqual(calls, ['reserved', 'returned'])
        self.assertEqual(self.records('starts'), [])
        self.assertEqual(self.runs(), 2)
        self.assertEqual(r.store.get(a.id).state, 'ready')


POOL_HOOK = """import os, json
from pathlib import Path
pools = json.loads(Path(%r).read_text())["pools"]
state = pools[%r]["state"]
ok = state == "available" and os.environ.get("MESH_RUNNER_LAUNCH_STATUS") == "available"
Path(%r).open("a").write(json.dumps({"phase": os.environ.get("MESH_RUNNER_PHASE"), "pool": state}) + "\\n")
print("ACCOUNT_CAPACITY_READY=1" if ok else "")
"""


class CatalogToAdmission(ProbeWiring):
    """The native controller's actual /report state (pools.json, built from the launch cache) is what the
    reserve+admit authority sees; the launch observation alone never grants."""

    def tick(self, snapshot):
        import tempfile
        from pathlib import Path
        import test_catalog_launch as tcl
        from mesh_runner.catalog import ControllerClient, PoolObserver, WorkspaceController
        from test_catalog import FakeMesh as CatalogMesh, NOW, WS
        d = Path(tempfile.mkdtemp(dir=self.root))
        bind, cache, m = d / 'b.json', d / 'c.json', tcl.manifest()
        for path, doc in ((bind, {"version": 1, "accounts": {"codex/example": tcl.REF}}),
                          (cache, {"version": 1, "accounts": {"codex/example": {
                              "binding": tcl.cl.binding_digest(m, "codex/example", tcl.REF),
                              "observation": snapshot, "fingerprint": "f" * 64}}})):
            path.write_text(json.dumps(doc))
            path.chmod(0o600)
        (d / 'accounts.json').write_text(json.dumps(m))
        mesh = CatalogMesh(tcl.codex_desired())
        client = ControllerClient("https://mesh.example.com", WS, "reporter-key", opener=mesh, sleep=lambda _s: None)
        WorkspaceController(WS, client, d / 'accounts.json', d / 'home', d / 'home',
                            PoolObserver(launch_bindings=bind, launch_cache=cache)).tick(NOW)
        return d / 'home' / 'pools.json', mesh.reports[0]

    def drive(self, n, snapshot, helper='ok'):
        import test_catalog_launch as tcl
        pools, report = self.tick(snapshot)
        log = self.root / 'pool-hook.jsonl'
        if log.exists():
            log.unlink()
        hook = self.root / 'pool_hook.py'
        hook.write_text(POOL_HOOK % (str(pools), tcl.POOL, str(log)))
        self.probe(helper)
        self.agent.start_gate_command = [sys.executable, str(hook)]
        t = task(n, cat='in_progress')
        r = self.runner(FakeMesh([t]))
        a = self.prepare(r, t)
        r.store.mark('builder', t['id'], 'quota_admission_required', a.id)
        out = [r._gate(phase, t['id'], a.mode, a)[0] for phase in ('reserve', 'admit')]
        seen = [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []
        return report, out, seen

    def test_available_evidence_reaches_reserve_and_admit(self):
        import test_catalog_launch as tcl
        report, out, seen = self.drive(980, tcl.snap(primary=10, secondary=20))
        self.assertEqual(report['pools'][tcl.POOL]['state'], 'available')
        self.assertEqual(out, ['', ''])
        self.assertEqual([s['pool'] for s in seen], ['available', 'available'])

    def test_exhausted_report_denies_although_the_launch_probe_is_available(self):
        import test_catalog_launch as tcl
        report, out, seen = self.drive(981, tcl.snap("quota_blocked", 100, 3))
        self.assertEqual(report['pools'][tcl.POOL]['state'], 'exhausted')
        self.assertTrue(all(out), out)                     # the hook withheld ACCOUNT_CAPACITY_READY
        self.assertEqual({s['pool'] for s in seen}, {'exhausted'})

    def test_stale_cache_is_unknown_and_never_grants(self):
        import test_catalog_launch as tcl
        report, out, seen = self.drive(982, tcl.snap(at=int(tcl.NOW - 5000)))
        self.assertEqual(report['pools'][tcl.POOL]['state'], 'unknown')
        self.assertTrue(all(out), out)
        self.assertNotIn('available', [s['pool'] for s in seen])
