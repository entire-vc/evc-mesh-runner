"""External timestamps are evidence contracts, never guesses for dependency retries."""
import datetime as dt
import unittest

from mesh_runner.outcome import TIME_WAIT_GUIDANCE, contract
from mesh_runner.wake import check
from fakes import FakeGitLab, FakeMesh, calls, task, write_replies, wait_mr, wait_time, next_turn, turn_rows, typed
from test_runner import Bench

NOW = dt.datetime(2026, 10, 28, 12, tzinfo=dt.timezone.utc)

def external(at='2026-10-29T12:30:00+03:00', event='backup-20261029', source='https://provider.example/schedule', why='published backup available'):
    """The wait_for on=time turn of a fake agent (the typed replacement for the marker line)."""
    return wait_time(until_at=at, event_ref=event, source=source, why=why)

class ExternalTimeGrammar(unittest.TestCase):
    def test_absolute_timestamp_and_evidence_are_preserved_without_clamping(self):
        o = typed(external(), now=NOW)
        self.assertEqual(o.kind, 'time')
        self.assertEqual(o.until, dt.datetime(2026, 10, 29, 9, 30, tzinfo=dt.timezone.utc))
        self.assertEqual(o.extra['external_time'], {'event':'backup-20261029', 'source':'https://provider.example/schedule', 'why':'published backup available', 'at':'2026-10-29T12:30:00+03:00'})
        o = typed(external(at='2026-10-28T12:00:01Z', source='card:#abcdef12', event='export-window'), now=NOW)
        self.assertEqual(o.until - NOW, dt.timedelta(seconds=1))

    def test_relative_bare_poll_and_missing_evidence_cannot_schedule(self):
        # (the marker's "no seconds" and "already past" refusals do not exist for typed waits:
        # a time that passed while the settle waited is a due time wait, not an invalid one)
        invalid = [external(at='6h'), external(at='24h'), external(at='2026-10-29'),
                   external(at='2026-10-29T12:30'),
                   external(why='poll: Telegram answer'),
                   external(source='https://['), external(source='https://example.test:bad/'), external(source='file:///local'), external(source='https://'),
                   external(source='https://user:password@example.test/'),
                   external(event='mr:group/app!7'), external(event='poll:inbox'),
                   external(why='poll: inbox'), external(why=''), external(source='')]
        for turn in invalid:
            with self.subTest(turn=turn):
                o = typed(turn, now=NOW)
                self.assertEqual(o.kind, 'unparsed')
                self.assertIsNone(o.until)
                self.assertIn('cannot act on it', o.note)
        for turn in (external(event='mr:group/app!7'), external(event='poll:inbox'), external(why='poll: inbox')):
            self.assertIn(TIME_WAIT_GUIDANCE, typed(turn, now=NOW).note)

    def test_explicit_external_reference_does_not_depend_on_natural_language(self):
        # A cited source may mention a card or person; prose is never the validator.
        self.assertEqual(typed(external(why='approved by @Morgan on #abcdef12'), now=NOW).kind, 'time')
        for kind in ('backup', 'export', 'scheduled-job', 'measurement'):
            self.assertEqual(typed(external(event=kind), now=NOW).kind, 'time')

    def test_contract_has_one_schema_and_no_poll_escape(self):
        for merge in (True, False):
            text = contract(merge)
            self.assertIn('wait_for on=time until_at=<RFC3339 with Z or offset> event_ref=<stable reference>', text)
            self.assertIn('source=<https-url|comment:<uuid>|artifact:<uuid>>', text)
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

    def test_same_identity_explicit_next_rearms_but_old_next_and_notices_do_not(self):
        t=task(1);mesh=FakeMesh([t],max_concurrent=1);r=self.runner(mesh);r.refresh_me()
        a=r.store.create(self.agent.name,t['id'],'claude')
        r._correction_hold(a,t,'invalid external time wait; cite the schedule')
        at=dt.datetime.fromisoformat(a.updated_at)
        def nxt(note, secs):
            row, _ = turn_rows(next_turn(note), at=(at+dt.timedelta(seconds=secs)).isoformat())
            mesh.outcomes.setdefault(t['id'], []).append(row)
        mesh.outcomes = {}
        nxt('old correction', -1)
        mesh.add_comment(t['id'],'ordinary own update',author=r.agent_id,at=at+dt.timedelta(seconds=1))
        mesh.add_comment(t['id'],'[runner] ↻ NEXT: auto notice',author=r.agent_id,at=at+dt.timedelta(seconds=2))
        # marker text in the owner's own comment is not a continue_next
        mesh.add_comment(t['id'],'↻ NEXT: replaced polling with a cited external schedule',author=r.agent_id,at=at+dt.timedelta(seconds=2))
        self.assertFalse(r._correction_rearmed(t['id'],at))
        restarted=self.runner(mesh);restarted.refresh_me()
        self.assertIn('wait correction required',restarted._hold_reason(t,'work'))
        nxt('', 3)                                    # a continue_next without a note repairs nothing
        self.assertFalse(restarted._correction_rearmed(t['id'],at))
        nxt('replaced polling with a cited external schedule', 4)
        self.assertTrue(restarted._correction_rearmed(t['id'],at))
        self.assertNotIn('wait correction required',restarted._hold_reason(t,'work'))

    def test_a_typed_poll_time_wait_cannot_bypass_the_schema_or_queue_contract(self):
        # (replaces the forged-programmatic-outcome test: the runner no longer re-parses a non-typed
        # outcome at settle, since none can exist; the schema is checked where the typed outcome is built)
        t=task(1); mesh=FakeMesh([t],max_concurrent=1)
        write_replies(self.replies,{t['id'][:8]:external(event='poll:queue', why='poll: queue')})
        r=self.runner(mesh)
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
        write_replies(self.replies,{t['id'][:8]:external(at=at, event='backup-window', why='published backup becomes available')})
        self.cfg.mesh_effects=True
        r=self.runner(mesh);r.tick();self.settle_all(r)
        a=r.store.by_state(self.agent.name,('waiting',))[0]
        self.assertEqual(a.wait['external_time']['at'],at)
        self.assertEqual(dt.datetime.fromisoformat(mesh.tasks[t['id']]['start_after']),dt.datetime.fromisoformat(at))

    def test_repeated_terminal_ci_is_bounded_without_a_time_wait(self):
        t=task(1);mesh=FakeMesh([t],max_concurrent=1);gl=FakeGitLab()
        gl.mr_head['g/app!7']='9:success'
        write_replies(self.replies,{t['id'][:8]:wait_mr("g/app", 7), t['id'][:8]+':resume':wait_mr("g/app", 7)})
        r=self.runner(mesh,gl);r.tick();self.settle_all(r)
        for _ in range(4):
            r.tick();self.settle_all(r)
        self.assertEqual(len(calls(self.calls)),3)
        self.assertEqual(r.store.by_state(self.agent.name,('failed',))[0].note.split(':')[0], 'wait correction required')
        self.assertFalse(mesh.tasks[t['id']].get('start_after'))

    def test_no_gitlab_is_visible_event_wait_without_timer(self):
        t=task(1);mesh=FakeMesh([t],max_concurrent=1)
        write_replies(self.replies,{t['id'][:8]:wait_mr("g/app", 7)})
        r=self.runner(mesh);r.tick();self.settle_all(r)
        a=r.store.by_state(self.agent.name,('waiting',))[0]
        self.assertEqual(a.wait['kind'],'mr')
        self.assertFalse(a.wait['until'])
        self.assertTrue(any('Integration blocked' in c['body'] for c in mesh.comment_log[t['id']]))
        self.assertEqual(len(calls(self.calls)),1)

if __name__=='__main__':unittest.main()
