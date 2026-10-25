"""Explicit merge waits observe MR state, never pipeline completion."""
import datetime as dt
from types import SimpleNamespace
from unittest import mock

from mesh_runner.outcome import parse_line
from mesh_runner.store import iso
from mesh_runner.wake import typed_event
from fakes import FakeGitLab, FakeMesh, task, write_replies
from test_runner import Bench

NOW = dt.datetime.now(dt.timezone.utc)
SHA = 'a' * 40
TARGET = 'g/app!56'
KEY = 'mr-state:' + TARGET


def marker():
    return f'⏳ WAIT mr:{TARGET} until=merged after={SHA}:opened'


def row(state='opened', at=None):
    out = {'sha': SHA, 'state': state,
           'head_pipeline': {'id': 10, 'sha': SHA, 'status': 'success'}}
    if at:
        out['merged_at' if state == 'merged' else 'closed_at'] = at
    return out


class MRStateWait(Bench):
    def setup_wait(self):
        t = task(1)
        gl = FakeGitLab()
        gl.mr_rows[TARGET] = row()
        r = self.runner(FakeMesh([t]), gl)
        r.refresh_me()
        a = r.store.create(self.agent.name, t['id'], 'claude')
        o = parse_line(marker(), NOW)
        r.store.update(a, state='waiting', wait={'kind': o.kind, 'target': o.target,
                       **o.extra, 'since': iso(), 'started': iso()})
        return r, a, t, gl

    def test_strict_parser_does_not_fall_back_to_legacy(self):
        self.assertEqual(parse_line(marker(), NOW).extra['mr_state']['state'], 'opened')
        for text in (marker().replace(SHA, 'abc'), marker().replace(':opened', ':closed'),
                     marker().replace('until=merged', 'until=merge'),
                     marker().replace('mr:', 'pipeline:'), marker().replace(' after=', ' after =')):
            with self.subTest(text=text):
                self.assertEqual(parse_line(text, NOW).kind, 'unparsed')

    def test_settle_skips_terminal_ci_seed_and_new_heads_do_not_wake(self):
        t = task(1)
        gl = FakeGitLab()
        gl.mr_rows[TARGET] = row()
        gl.mr_head[TARGET] = '10:success'
        write_replies(self.replies, {t['id'][:8]: marker()})
        r = self.runner(FakeMesh([t]), gl)
        r.tick()
        self.settle_all(r)
        a = r.store.by_state(self.agent.name, ('waiting',))[0]
        self.assertIn('mr_state', a.wait)
        self.assertNotIn('settled', a.wait)
        for status in ('success', 'manual', 'pending', 'failed'):
            gl.mr_rows[TARGET] = row()
            gl.mr_rows[TARGET].update(sha='b' * 40)
            gl.mr_rows[TARGET]['head_pipeline'].update(id=11, status=status)
            r._wake_one(a)
            self.assertEqual(a.state, 'waiting')

    def test_terminal_delivery_survives_restart_new_attempt_and_reopen(self):
        for state in ('merged', 'closed'):
            with self.subTest(state=state):
                r, a, t, gl = self.setup_wait()
                gl.mr_rows[TARGET] = row(state, iso(NOW))
                gl.mr_rows[TARGET].pop('head_pipeline')  # MR state needs no CI
                r._wake_one(a)
                self.assertEqual(a.state, 'ready')
                self.assertIn('merged' if state == 'merged' else 'closed without merge', a.wake_text)
                self.assertIsNotNone(r.store.mark(self.agent.name, t['id'], KEY))
                r.store.update(a, state='completed')
                r2 = self.runner(r.mesh, gl)
                r2.refresh_me()
                b = r2.store.create(self.agent.name, t['id'], 'claude')
                o = parse_line(marker(), NOW)
                r2.store.update(b, state='waiting', wait={'kind': o.kind, 'target': o.target,
                    **o.extra, 'since': iso(), 'started': iso()})
                r2._wake_one(b)
                self.assertEqual(b.state, 'waiting')
                if state == 'closed':
                    gl.mr_rows[TARGET] = row()
                    r2._wake_one(b)
                    gl.mr_rows[TARGET] = row('closed', iso(NOW + dt.timedelta(hours=1)))
                    r2._wake_one(b)
                    self.assertEqual(b.state, 'ready')
                r2.store.update(b, state='completed')

    def test_unknown_never_acknowledges_or_becomes_timer(self):
        r, a, t, gl = self.setup_wait()
        for value in ({}, row('closed'), row('merged', 'bad'), row('locked'), []):
            gl.mr_rows[TARGET] = value
            r._wake_one(a)
            self.assertEqual(a.state, 'waiting')
            self.assertEqual(r.status()['attempts'][0]['wait_observation'], 'unknown')
            self.assertIsNone(r.store.mark(self.agent.name, t['id'], KEY))
        r.gitlab = None
        r._wake_one(a)
        self.assertEqual(a.wait['kind'], 'mr')
        self.assertEqual(a.state, 'waiting')

    def test_failed_state_cas_does_not_acknowledge_terminal(self):
        r, a, t, gl = self.setup_wait()
        gl.mr_rows[TARGET] = row('merged', iso(NOW))
        with mock.patch.object(r.store, 'update_with_marks', return_value=False):
            r._wake_one(a)
        self.assertEqual(r.store.get(a.id).state, 'waiting')
        self.assertIsNone(r.store.mark(self.agent.name, t['id'], KEY))
        r._wake_one(a)
        self.assertEqual(a.state, 'ready')

    def test_terminal_timestamp_requires_rfc3339_datetime_with_timezone(self):
        r, a, t, gl = self.setup_wait()
        for state in ('merged', 'closed'):
            for stamp in ('2026-10-20', '20261020', '2026-W43-2',
                          '2026-10-20T10:00:00', '2026-10-20T10:00Z',
                          '2026-10-20 10:00:00Z', '2026-10-20T10:00:00+0300',
                          '2026-10-20T10:00:00+03:60', '2026-10-20T10:60:00Z',
                          '2026-02-30T10:00:00Z', 'bad', '', None, 1792490400):
                with self.subTest(state=state, stamp=stamp):
                    gl.mr_rows[TARGET] = row(state)
                    gl.mr_rows[TARGET]['merged_at' if state == 'merged' else 'closed_at'] = stamp
                    with self.assertRaises(ValueError):
                        typed_event(gl, 'mr', TARGET, a.wait)
                    r._wake_one(a)
                    self.assertEqual(a.state, 'waiting')
                    self.assertEqual(a.wait['observation_state'], 'unknown')
                    self.assertIsNone(r.store.mark(self.agent.name, t['id'], KEY))
        gl.mr_rows[TARGET] = row('merged', '2026-10-20T10:00:00.123456Z')
        r._wake_one(a)
        self.assertEqual(a.state, 'ready')

    def test_equivalent_terminal_timestamp_spelling_is_deduped(self):
        r, a, t, gl = self.setup_wait()
        gl.mr_rows[TARGET] = row('closed', '2026-10-20T10:00:00.123Z')
        r._wake_one(a)
        self.assertEqual(a.state, 'ready')
        for stamp in ('2026-10-20T13:00:00.123000+03:00',
                      '2026-10-20t10:00:00.123z'):
            r.store.update(a, state='waiting')
            gl.mr_rows[TARGET] = row('closed', stamp)
            r._wake_one(a)
            self.assertEqual(a.state, 'waiting')
            self.assertEqual(a.wait['observation_state'], 'unchanged')
        gl.mr_rows[TARGET] = row('closed', '2026-10-20T10:00:01Z')
        r._wake_one(a)
        self.assertEqual(a.state, 'ready')

    def test_review_uses_same_terminal_predicate_without_ack_before_spawn(self):
        r, a, t, gl = self.setup_wait()
        old = NOW - dt.timedelta(days=3)
        last = {'body': marker(), 'created_at': iso(old), 'author_id': r.agent_id}
        self.assertEqual(r._review_event(t, old, last, []), '')
        self.assertTrue(r._own_wait_pending(t, last, old))
        gl.mr_rows[TARGET] = row('closed', iso(NOW))
        self.assertIn('closed without merge', r._review_event(t, old, last, []))
        delivery = r._review_ci_delivery[t['id']]
        self.assertIsNone(r.store.mark(self.agent.name, t['id'], KEY))
        r.store.update(a, wait={**a.wait, 'ci_delivery': delivery})
        with mock.patch.object(r.adapter, 'spawn', side_effect=OSError('injected')):
            with self.assertRaises(OSError):
                r._spawn(a, 'prompt', False, ('waiting',))
        self.assertIsNone(r.store.mark(self.agent.name, t['id'], KEY))
        with mock.patch.object(r.adapter, 'spawn', return_value=SimpleNamespace(pid=99999999)):
            self.assertTrue(r._spawn(a, 'prompt', False, ('waiting',)))
        self.assertEqual(r._review_event(t, old, last, []), '')

    def test_owner_drift_does_not_deliver(self):
        r, a, t, gl = self.setup_wait()
        gl.mr_rows[TARGET] = row('merged', iso(NOW))
        with mock.patch.object(r, '_still_ours', return_value=False):
            r._wake_one(a)
        self.assertEqual(a.state, 'completed')
        self.assertIsNone(r.store.mark(self.agent.name, t['id'], KEY))

    def test_terminal_event_does_not_bypass_human_admission_gate(self):
        r, a, t, gl = self.setup_wait()
        gl.mr_rows[TARGET] = row('merged', iso(NOW))
        r._wake_one(a)
        self.assertEqual(a.state, 'ready')
        with mock.patch.object(r, '_gate', return_value=('human_gate', {})), \
                mock.patch.object(r.adapter, 'spawn') as spawn:
            r._resume_one(a)
        self.assertEqual(a.state, 'ready')
        spawn.assert_not_called()
