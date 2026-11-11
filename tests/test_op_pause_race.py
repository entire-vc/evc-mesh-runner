"""An operation pause token that lands while a tick is already walking the queue (slow admission,
launch probe, resume preparation) stops every later native start, through the one existing pause
mechanism (op_pause). Real temporary Store/SQLite/files; the native child is a recording script."""
from fakes import FakeMesh, task
from mesh_runner import launch_probe, op_pause
from test_launch_probe_runner import ProbeWiring


class PauseRace(ProbeWiring):
    def take(self, name='op-test'):
        return op_pause.take(self.cfg.state_dir, 'builder', name)

    def reservation(self):
        calls = []
        def before_spawn():
            calls.append('reserved')
        before_spawn.undo = lambda: calls.append('returned')
        return calls, before_spawn

    def fresh(self, n):
        self.probe('ok')
        self.agent.start_gate_command = []
        t = task(n, cat='in_progress')
        r = self.runner(FakeMesh([t]))
        a = self.prepare(r, t)
        r.store.update(a, wait={}, wake_text='', turns=0, session_id='')
        return r, r.store.get(a.id), t

    def test_token_landing_in_slow_probe_stops_native_and_returns_nothing_consumed(self):
        r, a, t = self.fresh(2001)
        calls, before_spawn = self.reservation()
        real = launch_probe.observe
        def slow(*args, **kwargs):
            out = real(*args, **kwargs)
            self.take()                              # lands while the probe runs
            return out
        launch_probe.observe = slow
        try:
            self.assertFalse(r._spawn(a, 'first', resume=False, expect=('ready',), before_spawn=before_spawn))
        finally:
            launch_probe.observe = real
        held = r.store.get(a.id)
        self.assertEqual((held.state, held.pid, held.turns), ('ready', 0, 0))
        self.assertEqual(calls, [])                  # one-time reservation never consumed
        self.assertEqual(self.records('starts'), [])
        self.assertEqual(r.procs, {})
        self.assertNotIn('interactive_prompt_intent', held.wait)
        self.assertNotIn(a.id, r._resume_hold)       # no timer: only the token holds it
        self.assertEqual([e for e in r.store.events(a.id) if e[1] == 'launch_probe_denied'][-1][2], 'op_pause')

    def test_token_landing_before_native_exec_denies_and_release_delivers_once(self):
        r, a, t = self.fresh(2002)
        original = r._launch_ready
        r._launch_ready = lambda *args, **kwargs: (self.take(), original(*args, **kwargs))[1]
        self.assertFalse(r._spawn(a, 'first', resume=False, expect=('ready',)))
        self.assertEqual(self.records('starts'), [])
        r._launch_ready = original
        other = op_pause.take(self.cfg.state_dir, 'builder', 'foreign')
        self.assertTrue(op_pause.release(self.cfg.state_dir, 'builder', 'op-test'))
        self.assertTrue(r.paused())                  # releasing one token does not release another
        self.assertFalse(r._spawn(r.store.get(a.id), 'first', resume=False, expect=('ready',)))
        other.unlink()
        self.assertFalse(r.paused())
        self.assertTrue(r._spawn(r.store.get(a.id), 'first', resume=False, expect=('ready',)))
        self.wait_for(lambda: len(self.records('starts')) == 1)

    def test_token_between_candidates_stops_next_start(self):
        self.probe('ok')
        ts = [task(2003, cat='todo'), task(2004, cat='todo')]
        r = self.runner(FakeMesh(ts))
        r.candidates = lambda: [dict(x, _mode='work') for x in ts]
        seen = []
        def one(tk):
            seen.append(tk['id'])
            self.take()
            return True
        r._start_one = one
        r.start_new(5)
        self.assertEqual(len(seen), 1)

    def test_token_between_resumes_stops_next_resume(self):
        self.probe('ok')
        ts = [task(2005, cat='in_progress'), task(2006, cat='in_progress')]
        r = self.runner(FakeMesh(ts))
        attempts = [self.prepare(r, x) for x in ts]
        seen = []
        def one(a):
            seen.append(a.id)
            self.take()
        r._resume_one = one
        r.resume_ready(5)
        self.assertEqual(len(seen), 1)

    def test_cold_start_denied_by_pause_keeps_a_ready_attempt_and_delivers_once_after_release(self):
        self.probe('ok')
        self.agent.start_gate_command = []
        t = task(2007, cat='todo')
        mesh = FakeMesh([t])
        r = self.runner(mesh)
        r.me = {**r.me, 'id': t['assignee_id']}
        original = r._launch_ready
        r._launch_ready = lambda *args, **kwargs: (self.take(), original(*args, **kwargs))[1]
        r.start_new(5)
        attempts = r.store.by_state('builder', ('ready',))
        self.assertEqual(len(attempts), 1)           # not finalized as aborted
        self.assertEqual(self.records('starts'), [])
        self.assertEqual(r.procs, {})
        r._launch_ready = original
        op_pause.release(self.cfg.state_dir, 'builder', 'op-test')
        r.resume_ready(5)
        self.wait_for(lambda: len(self.records('starts')) == 1)
