"""External timestamps are evidence contracts, never guesses for dependency retries."""
import datetime as dt
import unittest
from unittest import mock

from mesh_runner.outcome import parse_outcome, contract
from mesh_runner.wake import check
from fakes import FakeGitLab, FakeMesh, calls, task, write_replies
from test_runner import Bench

NOW = dt.datetime(2026, 10, 28, 12, tzinfo=dt.timezone.utc)

def external(at='2026-10-29T12:30:00+03:00', event='backup-20261029', source='https://provider.example/schedule', why='published backup available'):
    return f'⏳ WAIT {at}: external={event} source={source} {why}'

class ExternalTimeGrammar(unittest.TestCase):
    def test_absolute_timestamp_and_evidence_are_preserved_without_clamping(self):
        o = parse_outcome(external(), NOW)
        self.assertEqual(o.kind, 'time')
        self.assertEqual(o.until, dt.datetime(2026, 10, 29, 9, 30, tzinfo=dt.timezone.utc))
        self.assertEqual(o.extra['external_time'], {'event':'backup-20261029', 'source':'https://provider.example/schedule', 'why':'published backup available', 'at':'2026-10-29T12:30:00+03:00'})
        o = parse_outcome(external(at='2026-10-28T12:00:01Z', source='card:#abcdef12', event='quota-reset'), NOW)
        self.assertEqual(o.until - NOW, dt.timedelta(seconds=1))

    def test_relative_bare_poll_and_missing_evidence_cannot_schedule(self):
        invalid = ['⏳ WAIT 6h: retry dependency', '⏳ WAIT 24h', '⏳ WAIT 2026-10-29',
                   '⏳ WAIT 2026-10-29T12:30Z: measurement window',
                   '⏳ WAIT 6h: poll: Telegram answer', '⏳ WAIT poll: inbox',
                   external(at='2026-10-29T12:30'), external(at='2026-10-27T12:30Z'),
                   external(source='https://['), external(source='https://example.test:bad/'), external(source='file:///local'), external(source='https://'),
                   external(source='https://user:password@example.test/'),
                   external(event='mr:group/app!7'), external(event='poll:inbox'),
                   external(why='poll: inbox'), external(why='')]
        for line in invalid:
            with self.subTest(line=line):
                o = parse_outcome(line, NOW)
                self.assertEqual(o.kind, 'unparsed')
                self.assertIsNone(o.until)
                self.assertIn('source=', o.note)
                self.assertIn('WAIT card:', o.note)

    def test_explicit_external_reference_does_not_depend_on_natural_language(self):
        # A cited source may mention a card or person; prose is never the validator.
        self.assertEqual(parse_outcome(external(why='approved by @Pavel on #abcdef12'), NOW).kind, 'time')
        for kind in ('backup', 'export', 'scheduled-job', 'measurement', 'provider-retry', 'quota-reset'):
            self.assertEqual(parse_outcome(external(event=kind), NOW).kind, 'time')

    def test_contract_has_one_schema_and_no_poll_escape(self):
        for merge in (True, False):
            text = contract(merge)
            self.assertIn('external=<stable-reference> source=<https-url|card:#8hex>', text)
            self.assertNotIn('WAIT 2h:', text)
            self.assertIn('Internal retry/backoff never writes', text)

    def test_missing_observer_stays_an_event_across_serialization_and_many_checks(self):
        import json
        for extra in ({}, {'ci_change': {'predicate':'success','sha':'a'*40,'pipeline':7,'status':'running'}}):
            wait = dict(kind='mr', target='g/app!7', started='2026-10-01T12:00:00Z', errors=5, **extra)
            for i in range(10):
                wait = json.loads(json.dumps(wait))
                self.assertEqual(check(None, None, None, {}, wait, 'me', [], NOW + dt.timedelta(days=i)), '')
                self.assertEqual(wait['kind'], 'mr')
                self.assertFalse(wait.get('until'))
                self.assertFalse(wait.get('recheck_due'))
                self.assertEqual(wait['integration_blocked'], 'GitLab observer unavailable')

class TypedTimeSettlement(Bench):
    def test_invalid_repeated_timer_is_bounded_even_when_already_warned_today(self):
        t = task(1); mesh = FakeMesh([t], max_concurrent=1)
        write_replies(self.replies, {t['id'][:8]:'⏳ WAIT 6h: poll: inbox', t['id'][:8]+':resume':'⏳ WAIT 6h: poll: inbox'})
        r = self.runner(mesh)
        r.store.mark('*', t['id'], 'time_wait_told', NOW.date().isoformat())
        r.tick(); self.settle_all(r)
        self.assertFalse(r.store.by_state(self.agent.name, ('waiting','running','ready','starting')))
        self.assertLessEqual(len(calls(self.calls)), self.cfg.max_no_outcome + 1)
        self.assertFalse(mesh.tasks[t['id']].get('start_after'))
        self.assertTrue(any('source=' in c['body'] for c in mesh.comment_log[t['id']]))
        self.assertTrue(r.store.by_state(self.agent.name, ('failed',))[0].note.startswith('wait correction required:'))
        count = len(calls(self.calls))
        self.cfg.failed_cooldown_hours = 0
        with mock.patch('mesh_runner.runner.now', return_value=dt.datetime.now(dt.timezone.utc)+dt.timedelta(days=2)):
            r.tick(); self.settle_all(r)
        self.assertEqual(len(calls(self.calls)), count)
        self.assertFalse(r._task_backoff)
        # The persisted failed attempt, not a process-local timer, owns the hold.
        self.assertIn('wait correction required', r._hold_reason(mesh.tasks[t['id']], 'work'))
        r.agent.name='replacement-lane'
        self.assertNotIn('wait correction required', r._hold_reason(mesh.tasks[t['id']], 'work'))
        r.agent.name='builder'
        mesh.add_comment(t['id'], 'Corrected: wait for card event instead', author='lead')
        mesh.tasks[task(0xabcdef12, assignee_id='other')['id']] = task(0xabcdef12, assignee_id='other')
        write_replies(self.replies, {t['id'][:8]:'⏳ WAIT card:#abcdef12', t['id'][:8]+':resume':'⏳ WAIT card:#abcdef12'})
        r.tick(); self.settle_all(r)
        self.assertGreater(len(calls(self.calls)), count)

    def test_same_identity_explicit_next_rearms_but_old_next_and_notices_do_not(self):
        t=task(1);mesh=FakeMesh([t],max_concurrent=1);r=self.runner(mesh);r.refresh_me()
        a=r.store.create(self.agent.name,t['id'],'claude')
        r._correction_hold(a,t,'invalid external time wait; cite the schedule')
        at=dt.datetime.fromisoformat(a.updated_at)
        mesh.add_comment(t['id'],'↻ NEXT: old correction',author=r.agent_id,at=at-dt.timedelta(seconds=1))
        mesh.add_comment(t['id'],'ordinary own update',author=r.agent_id,at=at+dt.timedelta(seconds=1))
        mesh.add_comment(t['id'],'[runner] ↻ NEXT: auto notice',author=r.agent_id,at=at+dt.timedelta(seconds=2))
        self.assertFalse(r._correction_rearmed(t['id'],at))
        restarted=self.runner(mesh);restarted.refresh_me()
        self.assertIn('wait correction required',restarted._hold_reason(t,'work'))
        mesh.add_comment(t['id'],'↻ NEXT',author=r.agent_id,at=at+dt.timedelta(seconds=3))
        self.assertFalse(restarted._correction_rearmed(t['id'],at))
        mesh.add_comment(t['id'],'↻ NEXT: replaced polling with a cited external schedule',author=r.agent_id,at=at+dt.timedelta(seconds=4))
        self.assertTrue(restarted._correction_rearmed(t['id'],at))
        self.assertNotIn('wait correction required',restarted._hold_reason(t,'work'))

    def test_programmatic_time_outcome_cannot_bypass_the_schema_or_queue_contract(self):
        from mesh_runner.outcome import Outcome
        t=task(1); mesh=FakeMesh([t],max_concurrent=1)
        write_replies(self.replies,{t['id'][:8]:'ignored'})
        self.cfg.queue_wait_freshness_v1=True
        r=self.runner(mesh)
        forged=Outcome('time', until=dt.datetime.now(dt.timezone.utc)+dt.timedelta(hours=6),
                       line='⏳ WAIT 6h: poll: queue', extra={'external_time': {'event':'fake'}})
        with mock.patch.object(r,'_read_outcome',return_value=(forged,'')):
            r.tick(); self.settle_all(r)
        self.assertFalse(r.store.by_state(self.agent.name,('waiting',)))
        self.assertFalse(mesh.tasks[t['id']].get('start_after'))

    def test_legacy_persisted_time_wait_is_preserved_until_explicit_audit(self):
        wait={'kind':'time','until':(NOW+dt.timedelta(days=2)).isoformat(), 'line':'⏳ WAIT 24h'}
        self.assertEqual(check(None,None,None,{},wait,'me',[],NOW),'')
        self.assertEqual(wait['kind'],'time')
        self.assertNotIn('external_time',wait)

    def test_valid_external_timer_retains_evidence_and_exact_mesh_schedule(self):
        t = task(1); mesh = FakeMesh([t], max_concurrent=1)
        at = (dt.datetime.now(dt.timezone.utc)+dt.timedelta(minutes=2)).replace(microsecond=0).isoformat()
        write_replies(self.replies,{t['id'][:8]:external(at=at, event='provider-retry', why='provider Retry-After expires')})
        self.cfg.mesh_effects=True
        r=self.runner(mesh);r.tick();self.settle_all(r)
        a=r.store.by_state(self.agent.name,('waiting',))[0]
        self.assertEqual(a.wait['external_time']['at'],at)
        self.assertEqual(dt.datetime.fromisoformat(mesh.tasks[t['id']]['start_after']),dt.datetime.fromisoformat(at))

    def test_repeated_terminal_ci_is_bounded_without_a_time_wait(self):
        t=task(1);mesh=FakeMesh([t],max_concurrent=1);gl=FakeGitLab()
        gl.mr_head['g/app!7']='9:success'
        write_replies(self.replies,{t['id'][:8]:'⏳ WAIT mr:g/app!7', t['id'][:8]+':resume':'⏳ WAIT mr:g/app!7'})
        r=self.runner(mesh,gl);r.tick();self.settle_all(r)
        for _ in range(4):
            r.tick();self.settle_all(r)
        self.assertEqual(len(calls(self.calls)),3)
        self.assertEqual(r.store.by_state(self.agent.name,('failed',))[0].note.split(':')[0], 'wait correction required')
        self.assertFalse(mesh.tasks[t['id']].get('start_after'))

    def test_refused_person_gate_finishes_without_resurrecting_as_waiting(self):
        from mesh_runner.outcome import Outcome
        t=task(1);mesh=FakeMesh([t],max_concurrent=1)
        write_replies(self.replies,{t['id'][:8]:'❓ Blocking @owner: choose?'})
        r=self.runner(mesh)
        r.store.mark(self.agent.name,t['id'],'ask_not_raised','no-marker:blocking')
        r.tick();self.settle_all(r)
        self.assertFalse(r.store.by_state(self.agent.name,('waiting','ready','running')))
        self.assertTrue(r.store.by_state(self.agent.name,('failed',)))
        self.assertFalse(mesh.tasks[t['id']].get('start_after'))
        self.assertTrue(any('Repair owner:' in c['body'] for c in mesh.comment_log[t['id']]))

    def test_no_gitlab_is_visible_event_wait_without_timer(self):
        t=task(1);mesh=FakeMesh([t],max_concurrent=1)
        write_replies(self.replies,{t['id'][:8]:'⏳ WAIT mr:g/app!7'})
        r=self.runner(mesh);r.tick();self.settle_all(r)
        a=r.store.by_state(self.agent.name,('waiting',))[0]
        self.assertEqual(a.wait['kind'],'mr')
        self.assertFalse(a.wait['until'])
        self.assertTrue(any('Integration blocked' in c['body'] for c in mesh.comment_log[t['id']]))
        self.assertEqual(len(calls(self.calls)),1)

if __name__=='__main__':unittest.main()
