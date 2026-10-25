"""A suppressed idle wake leaves one bounded receipt, made from the observation already taken."""
import datetime as dt
import json
from unittest import mock

from mesh_runner.store import iso
from mesh_runner.wake import check, window_complete
from fakes import FakeGitLab, FakeMesh, task
from test_runner import Bench

SHA = 'a' * 40
OLD = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=48)    # past any first re-check


def row(pid=14845, status='running', sha=SHA):
    return {'sha': sha, 'state': 'opened', 'head_pipeline': {'sha': sha, 'id': pid, 'status': status}}


class Suppressed(Bench):
    def setup_wait(self, wait, when=OLD):
        t = task(1)
        mesh, gl = FakeMesh([t]), FakeGitLab()
        gl.mr_rows['g/app!56'] = row()
        gl.pipeline_rows['g/app#14845'] = {'sha': SHA, 'id': 14845, 'status': 'running'}
        r = self.runner(mesh, gl)
        r.refresh_me()
        a = r.store.create(self.agent.name, t['id'], 'claude')
        r.store.update(a, state='waiting', wait={**wait, 'since': when.isoformat(), 'started': when.isoformat()})
        return r, a, t, gl

    def ci(self, status='running', target='mr:g/app!56'):
        return {'kind': target.split(':')[0], 'target': target.split(':')[1],
                'ci_change': {'predicate': 'change', 'sha': SHA, 'pipeline': 14845, 'status': status}}

    def receipts(self, r):
        path = self.cfg.state_dir / 'events.jsonl'
        rows = [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []
        return [x for x in rows if x['kind'] == 'wake_suppressed']

    def test_typed_ci_unchanged_gives_one_receipt_per_due_opportunity(self):
        r, a, t, gl = self.setup_wait(self.ci())
        with mock.patch.object(gl, 'get_mr', wraps=gl.get_mr) as spy:
            for _ in range(4):                                   # every 20 s check of the same bucket
                r._wake_one(r.store.get(a.id))
            calls_with = spy.call_count
        got = self.receipts(r)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]['observer'], 'typed_ci')
        self.assertEqual(r.store.get(a.id).state, 'waiting')
        self.assertEqual(calls_with, 4)                          # one provider read per check, as before
        self.assertNotIn('negative_observation', r.store.get(a.id).wait)
        text = json.dumps(got[0])
        for banned in ('g/app', 'http', 'body'):
            self.assertNotIn(banned, text)

    def test_receipt_survives_restart_without_duplicating(self):
        r, a, t, gl = self.setup_wait(self.ci())
        r._wake_one(r.store.get(a.id))
        r2 = self.runner(r.mesh, gl)                              # separate connection, same database
        r2.refresh_me()
        r2._wake_one(r2.store.get(a.id))
        self.assertEqual(len(self.receipts(r2)), 1)

    def test_redeclared_wait_is_a_new_generation(self):
        r, a, t, gl = self.setup_wait(self.ci())
        r._wake_one(r.store.get(a.id))
        w = dict(r.store.get(a.id).wait, started=(OLD - dt.timedelta(hours=1)).isoformat())
        r.store.update(a, wait=w)
        r._wake_one(r.store.get(a.id))
        self.assertEqual(len(self.receipts(r)), 2)

    def test_not_yet_due_gets_no_receipt(self):
        r, a, t, gl = self.setup_wait(self.ci(), when=dt.datetime.now(dt.timezone.utc))
        r._wake_one(r.store.get(a.id))
        self.assertEqual(self.receipts(r), [])

    def test_genuine_change_wakes_without_receipt(self):
        r, a, t, gl = self.setup_wait(self.ci())
        gl.mr_rows['g/app!56'] = row(status='success')
        r._wake_one(r.store.get(a.id))
        self.assertEqual(r.store.get(a.id).state, 'ready')
        self.assertEqual(self.receipts(r), [])

    def test_observer_error_and_unknown_get_no_receipt(self):
        r, a, t, gl = self.setup_wait(self.ci())
        gl.mr_rows['g/app!56'] = {'state': 'opened'}             # unreadable head: raises, UNKNOWN
        r._wake_one(r.store.get(a.id))
        self.assertEqual(self.receipts(r), [])
        r.gitlab = None                                          # observer lost
        r._wake_one(r.store.get(a.id))
        self.assertEqual(self.receipts(r), [])

    def test_foreign_comment_on_decision_wakes_without_receipt(self):
        r, a, t, gl = self.setup_wait({'kind': 'decision', 'handle': 'reviewer'})
        r.mesh.add_comment(t['id'], 'noise', author='bystander')
        r._wake_one(r.store.get(a.id))
        self.assertEqual(r.store.get(a.id).state, 'ready')
        self.assertEqual(self.receipts(r), [])

    def test_decision_complete_empty_window_gets_receipt(self):
        r, a, t, gl = self.setup_wait({'kind': 'decision', 'handle': 'reviewer'})
        r._wake_one(r.store.get(a.id))
        got = self.receipts(r)
        self.assertEqual([x['observer'] for x in got], ['decision_comments'])

    def test_time_wait_gets_no_receipt(self):
        until = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat()
        r, a, t, gl = self.setup_wait({'kind': 'time', 'until': until})
        r._wake_one(r.store.get(a.id))
        self.assertEqual(self.receipts(r), [])

    def test_blocking_wait_gets_no_receipt(self):
        r, a, t, gl = self.setup_wait({'kind': 'blocking', 'target': 'x'})
        r._wake_one(r.store.get(a.id))
        self.assertEqual(self.receipts(r), [])

    def test_stale_state_or_generation_gets_no_receipt(self):
        r, a, t, gl = self.setup_wait(self.ci())
        live = r.store.get(a.id)
        real = r.store.get
        r.store.update(a, state='ready')                         # moved on while we observed
        r._wake_one(live)
        self.assertEqual(self.receipts(r), [])
        r.store.update(a, state='waiting')
        neg = {'observer': 'typed_ci', 'wait_kind': 'mr', 'generation': 'f' * 16, 'contract': 'c' * 16,
               'legacy_due_at': iso(), 'interval_s': 1}
        r._record_suppressed_wake(r.store.get(a.id), neg)        # observed under another generation
        self.assertEqual(self.receipts(r), [])

    def test_stale_observation_cannot_overwrite_a_redeclared_wait(self):
        """The observation resets errors 1->0 (a bookkeeping write): it must not land over a
        wait re-declared meanwhile, and must leave no receipt for the new generation."""
        r, a, t, gl = self.setup_wait({**self.ci(), 'errors': 1})
        live = r.store.get(a.id)
        started = (OLD - dt.timedelta(hours=1)).isoformat()
        fresh = dict(live.wait, started=started, since=started)
        real = check

        def observe_then_redeclare(*args, **kw):
            out = real(*args, **kw)
            r.store.update(a, wait=fresh)                        # the agent re-declared mid-observation
            return out
        with mock.patch('mesh_runner.runner.wake_check', observe_then_redeclare):
            r._wake_one(live)
        stored = r.store.get(a.id)
        self.assertEqual(stored.wait, fresh)                     # new started preserved
        self.assertEqual(self.receipts(r), [])

    def test_telemetry_failure_never_blocks_the_wait(self):
        r, a, t, gl = self.setup_wait(self.ci())
        with mock.patch.object(r.store, 'claim', side_effect=RuntimeError('db')):
            r._wake_one(r.store.get(a.id))
        self.assertEqual(r.store.get(a.id).state, 'waiting')


class WindowComplete(Bench):
    def test_short_read_or_reach_past_since(self):
        since = dt.datetime.now(dt.timezone.utc)
        c = lambda h: {'created_at': (since + dt.timedelta(hours=h)).isoformat()}
        self.assertTrue(window_complete([c(1)] * 3, since))
        self.assertTrue(window_complete([c(-1)] + [c(1)] * 39, since))
        self.assertFalse(window_complete([c(1)] * 40, since))


class StalePersonAsk(Suppressed):
    """A decision wait to a person with no gate and no agent request is re-checked, not parked forever."""

    def test_legacy_person_wait_without_gate_wakes_once_for_a_recheck(self):
        r, a, t, gl = self.setup_wait({'kind': 'decision', 'handle': 'person'})
        r.mesh.tasks[t['id']]['human_gate'] = False
        r._wake_one(r.store.get(a.id))
        got = r.store.get(a.id)
        self.assertEqual(got.state, 'ready')
        self.assertIn('no human gate or agent request', got.wake_text)

    def test_agent_decision_wait_stays_asleep(self):
        r, a, t, gl = self.setup_wait({'kind': 'decision', 'handle': 'lead'})
        r.mesh.tasks[t['id']]['human_gate'] = False
        r._wake_one(r.store.get(a.id))
        self.assertEqual(r.store.get(a.id).state, 'waiting')

    def test_a_standing_gate_keeps_the_person_wait_asleep(self):
        r, a, t, gl = self.setup_wait({'kind': 'decision', 'handle': 'person'})
        now = dt.datetime.now(dt.timezone.utc)
        w = {'kind': 'decision', 'handle': 'person', 'person_ask': True, 'since': OLD.isoformat(), 'started': OLD.isoformat()}
        for gate in (True, None):
            tk = {**t, 'human_gate': gate}
            if gate is None:
                tk.pop('human_gate')
            self.assertEqual(check(r.mesh, r.cats, None, tk, dict(w), r.agent_id, [], now), '')

    def test_unclassified_wait_is_not_a_person_wait(self):
        r, a, t, gl = self.setup_wait({'kind': 'decision', 'handle': 'person'})
        w = {'kind': 'decision', 'handle': 'person', 'since': OLD.isoformat(), 'started': OLD.isoformat()}
        tk = {**t, 'human_gate': False}
        self.assertEqual(check(r.mesh, r.cats, None, tk, w, r.agent_id, [], dt.datetime.now(dt.timezone.utc)), '')
