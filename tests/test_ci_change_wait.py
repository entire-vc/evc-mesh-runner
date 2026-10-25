"""Explicit CI baselines survive alias, attempt and observer restarts."""
import datetime as dt
import unittest
from unittest import mock

from mesh_runner.outcome import parse_line
from mesh_runner.store import Store, iso
from mesh_runner.wake import check, ci_change_event, ci_delivery_key
from fakes import FakeGitLab, FakeMesh, task, write_replies, calls
from test_runner import Bench

NOW = dt.datetime.now(dt.timezone.utc)
SHA = 'a' * 40
OTHER = 'b' * 40


def marker(target='mr:g/app!56', predicate='change', status='failed'):
    return f'⏳ WAIT {target} until={predicate} after={SHA}:14845:{status}'


def row(pid=14845, status='failed', sha=SHA):
    return {'sha': sha, 'state': 'opened', 'head_pipeline': {'sha': sha, 'id': pid, 'status': status}}


class TypedCI(Bench):
    def setup_wait(self, text=None):
        t = task(1)
        mesh, gl = FakeMesh([t]), FakeGitLab()
        gl.mr_rows['g/app!56'] = row()
        gl.pipeline_rows['g/app#14845'] = {'sha': SHA, 'id': 14845, 'status': 'failed'}
        r = self.runner(mesh, gl)
        r.refresh_me()
        a = r.store.create(self.agent.name, t['id'], 'claude')
        o = parse_line(text or marker(), NOW)
        r.store.update(a, state='waiting', wait={'kind': o.kind, 'target': o.target,
                       **o.extra, 'since': iso(), 'started': iso()})
        return r, a, t, gl

    def test_strict_parser_and_pipeline_identity(self):
        self.assertEqual(parse_line(marker(), NOW).extra['ci_change']['pipeline'], 14845)
        for text in (marker().replace(SHA, 'abc'), marker().replace('until=change', 'until=anything'),
                     marker('pipeline:g/app#9'), marker().replace('failed', 'unknown')):
            with self.subTest(text=text):
                self.assertEqual(parse_line(text, NOW).kind, 'unparsed')
        self.assertFalse(parse_line('⏳ WAIT mr:g/app!56: wait for new success', NOW).extra)

    def test_known_failed_baseline_does_not_wake_in_either_spelling(self):
        for target in ('pipeline:g/app#14845', 'mr:g/app!56'):
            with self.subTest(target=target):
                r, a, t, gl = self.setup_wait(marker(target))
                r._wake_one(a)
                self.assertEqual(r.store.get(a.id).state, 'waiting')
                self.assertIsNone(r.store.mark(self.agent.name, t['id'], ci_delivery_key(a.wait['target'],14845)))
                r.store.update(a, state='completed')

    def test_stale_baseline_delivery_is_durable_across_alias_restart_new_attempt(self):
        r, a, t, gl = self.setup_wait(marker('pipeline:g/app#14845', status='running'))
        r._wake_one(a)
        self.assertEqual(r.store.get(a.id).state, 'ready')
        self.assertIn('failed', r.store.get(a.id).wake_text)
        r.store.update(a, state='completed')
        r2 = self.runner(r.mesh, gl)  # separate SQLite connection, same durable database
        b = r2.store.create(self.agent.name, t['id'], 'claude')
        o = parse_line(marker(status='running'), NOW)
        r2.refresh_me()
        r2.store.update(b, state='waiting', wait={'kind':o.kind, 'target':o.target, **o.extra,
                                                'since':iso(), 'started':iso()})
        r2._wake_one(b)
        self.assertEqual(r2.store.get(b.id).state, 'waiting')
        gl.mr_rows['g/app!56'] = row(pid=14846, status='pending', sha=OTHER)
        r2._wake_one(b)
        self.assertEqual(r2.store.get(b.id).state, 'ready')

    def test_success_predicate_pending_failure_unknown_are_not_completion(self):
        r, a, t, gl = self.setup_wait(marker(predicate='success'))
        for value in (row(pid=14846,status='pending'), {}, row(pid=14846,status='unrecognized')):
            gl.mr_rows['g/app!56'] = value
            r._wake_one(a)
            self.assertEqual(r.store.get(a.id).state, 'waiting')
            self.assertIsNone(r.store.mark(self.agent.name,t['id'],ci_delivery_key(a.wait['target'],14845)))
        self.assertEqual(r.status()['attempts'][0]['wait_observation'],'unknown')
        gl.mr_rows['g/app!56'] = row(pid=14846,status='success')
        r._wake_one(a)
        self.assertEqual(r.store.get(a.id).state, 'ready')

    def test_mr_success_wait_on_failed_head_is_an_owner_notice_not_completion(self):
        r, a, t, gl = self.setup_wait(marker(predicate='success'))
        gl.mr_rows['g/app!56'] = row(pid=14846)                    # the new head failed
        r._wake_one(a)
        got = r.store.get(a.id)
        self.assertEqual(got.state, 'ready')
        self.assertIn('action required', got.wake_text)
        self.assertIsNone(r.store.mark(self.agent.name, t['id'], 'met_wake'))
        self.assertIsNone(r.store.mark(self.agent.name, t['id'], ci_delivery_key(a.wait['target'], 14846)))

    def test_failed_cas_cannot_ack_and_rollback_restores_state(self):
        r, a, t, gl = self.setup_wait()
        key = ci_delivery_key(a.wait['target'],14845)
        self.assertFalse(r.store.update_with_marks(a,{key:'x'},expect='running',state='ready'))
        self.assertIsNone(r.store.mark(self.agent.name,t['id'],key))
        with mock.patch.object(r.store,'mark',side_effect=RuntimeError('injected')):
            with self.assertRaises(RuntimeError):
                r.store.update_with_marks(a,{key:'x'},expect='waiting',state='ready')
        self.assertEqual(a.state,'waiting')
        self.assertEqual(r.store.get(a.id).state,'waiting')

    def test_review_uses_same_predicate_ignores_foreign_pipeline_and_does_not_ack(self):
        r, a, t, gl = self.setup_wait(marker(predicate='success'))
        last={'body':marker(predicate='success'),'created_at':iso(NOW),'author_id':r.agent_id}
        r.mesh.vcs_links = lambda tid: [{'url':'https://git.example/g/other/-/pipelines/999',
                                        'status':'failed','updated_at':iso(NOW+dt.timedelta(minutes=1))}]
        self.assertEqual(r._review_event(t,NOW,last,[]),'')
        gl.mr_rows['g/app!56'] = row(pid=14846,status='success')
        self.assertIn('success',r._review_event(t,NOW,last,[]))
        key=ci_delivery_key(a.wait['target'],14846)
        self.assertIsNone(r.store.mark(self.agent.name,t['id'],key))
        delivery=r._review_ci_delivery[t['id']]
        r.store.update_with_marks(a,{key:delivery['value']},expect='waiting',state='ready')
        self.assertEqual(r._review_event(t,NOW,last,[]),'')
        self.assertTrue(r._own_wait_pending(t,last,NOW-dt.timedelta(days=3)))

    def test_old_head_is_unknown_and_ordinary_failed_wake_is_preserved(self):
        r,a,t,gl=self.setup_wait()
        gl.mr_rows['g/app!56']=row(pid=14846,status='success',sha=OTHER)
        gl.mr_rows['g/app!56']['head_pipeline']['sha']=SHA
        r._wake_one(a)
        self.assertEqual(r.store.get(a.id).state,'waiting')
        wait={'kind':'pipeline','target':'g/app#14845','settled':'already failed','since':iso()}
        self.assertEqual(check(r.mesh,r.cats,gl,t,wait,r.agent_id,[],NOW),'already failed')
        self.assertTrue(wait['met'])

    def test_owner_drift_never_acknowledges(self):
        r,a,t,gl=self.setup_wait(marker(status='running'))
        with mock.patch.object(r,'_still_ours',return_value=False):
            r._wake_one(a)
        self.assertEqual(r.store.get(a.id).state,'completed')
        self.assertIsNone(r.store.mark(self.agent.name,t['id'],ci_delivery_key(a.wait['target'],14845)))

    def test_same_attempt_pipeline_to_mr_alias_does_not_redeliver(self):
        r,a,t,gl=self.setup_wait(marker('pipeline:g/app#14845',status='running'))
        r._wake_one(a)
        self.assertEqual(a.state,'ready')
        o=parse_line(marker(status='running'),NOW)
        r.store.update(a,state='waiting',wait={'kind':o.kind,'target':o.target,**o.extra,
                                             'since':iso(),'started':iso()})
        r._wake_one(a)
        self.assertEqual(a.state,'waiting')
        gl.mr_rows['g/app!56']=row(pid=14846,status='failed')
        r._wake_one(a)
        self.assertEqual(a.state,'ready')

    def test_review_delivery_ack_only_after_successful_spawn(self):
        from types import SimpleNamespace
        r,a,t,gl=self.setup_wait()
        key=ci_delivery_key(a.wait['target'],14845)
        r.store.update(a,wait={**a.wait,'ci_delivery':{'key':key,'value':'fresh'}})
        with mock.patch.object(r.adapter,'spawn',side_effect=OSError('spawn failed')):
            with self.assertRaises(OSError):
                r._spawn(a,'prompt',False,('waiting',))
        self.assertIsNone(r.store.mark(self.agent.name,t['id'],key))
        with mock.patch.object(r.adapter,'spawn',return_value=SimpleNamespace(pid=99999999)):
            self.assertTrue(r._spawn(a,'prompt',False,('waiting',)))
        self.assertEqual(r.store.mark(self.agent.name,t['id'],key)[0],'fresh')
        self.assertNotIn('ci_delivery',a.wait)
        self.assertEqual(a.state,'running')

    def test_receipt_failure_after_spawn_stops_untracked_writer(self):
        from types import SimpleNamespace
        r,a,t,gl=self.setup_wait()
        key=ci_delivery_key(a.wait['target'],14845)
        r.store.update(a,wait={**a.wait,'ci_delivery':{'key':key,'value':'fresh'}})
        with mock.patch.object(r.adapter,'spawn',return_value=SimpleNamespace(pid=99999999)), \
             mock.patch.object(r.store,'mark',side_effect=RuntimeError('receipt failure')), \
             mock.patch('mesh_runner.runner.interrupt_group') as stop:
            # auth lookup also reads marks; fail only the transactional receipt write.
            original=Store.mark.__get__(r.store)
            r.store.mark.side_effect=lambda *args: (_ for _ in ()).throw(RuntimeError('receipt failure')) if len(args)==4 and args[2]==key else original(*args)
            with self.assertRaises(RuntimeError):
                r._spawn(a,'prompt',False,('waiting',))
            stop.assert_called_once()
        self.assertEqual(r.store.get(a.id).state,'waiting')
        self.assertNotIn(a.id,r.procs)
        self.assertIsNone(r.store.mark(self.agent.name,t['id'],key))

    def test_other_pipeline_delivery_does_not_reenable_an_old_alias(self):
        r,a,t,gl=self.setup_wait(marker(status='running'))
        r._wake_one(a)
        r.store.mark(self.agent.name,t['id'],ci_delivery_key('g/app#999',999),'different')
        o=parse_line(marker('pipeline:g/app#14845',status='running'),NOW)
        r.store.update(a,state='waiting',wait={'kind':o.kind,'target':o.target,**o.extra,
                                             'since':iso(),'started':iso()})
        r._wake_one(a)
        self.assertEqual(a.state,'waiting')

    def test_review_refeed_age_cannot_bypass_baseline_after_prior_feed(self):
        r,a,t,gl=self.setup_wait()
        old=NOW-dt.timedelta(days=3)
        last={'body':marker(),'created_at':iso(old),'author_id':r.agent_id}
        t['updated_at']=iso(old)
        r.store.mark(self.agent.name,t['id'],'review_fed','previous')
        with mock.patch.object(r,'_own_work',return_value=(a,old,last)), \
             mock.patch.object(r,'_comments',return_value=[last]):
            self.assertIn('declared wait',r._review_hold(t))
            gl.mr_rows['g/app!56']=row(pid=14846,status='pending')
            self.assertEqual(r._review_hold(t),'')
            self.assertIn('ci_delivery',r._review_why[t['id']])

    def test_settle_persists_explicit_baseline_without_legacy_seed(self):
        t=task(1)
        mesh,gl=FakeMesh([t]),FakeGitLab()
        gl.mr_rows['g/app!56']=row()
        gl.mr_head['g/app!56']='14845:failed'
        write_replies(self.replies,{t['id'][:8]:marker()})
        r=self.runner(mesh,gl)
        r.tick()
        self.settle_all(r)
        a=r.store.by_state(self.agent.name,('waiting',))[0]
        self.assertEqual(a.wait['ci_change']['status'],'failed')
        self.assertNotIn('settled',a.wait)
        r._wake_one(a)
        self.assertEqual(a.state,'waiting')

    def _stale(self, status, delivered):
        r,a,t,gl=self.setup_wait(marker('pipeline:g/app#14845',status=status))
        if delivered:
            r.store.mark(self.agent.name,t['id'],ci_delivery_key('g/app!56',14845),f'{SHA}:14845:failed')
        w=dict(a.wait)
        r.store.update(a,state='completed')
        return r._stale_ci_baseline(a,w)

    def test_stale_baseline_on_already_delivered_result_is_sent_back_not_slept_on(self):
        text=self._stale('running',True)
        self.assertIn('stale',text)
        self.assertIn(f'after={SHA}:14845:failed',text)

    def test_stale_baseline_on_undelivered_result_is_left_to_wake(self):
        self.assertEqual(self._stale('running',False),'')

    def test_current_failed_baseline_is_a_valid_wait_for_a_new_observation(self):
        self.assertEqual(self._stale('failed',True),'')

    def _stale_flow(self, status='failed', predicate='change', target='mr:g/app!56'):
        """Real settle + SQLite store: a turn declares a wait whose baseline is stale."""
        t = task(1)
        gl = FakeGitLab()
        gl.mr_rows['g/app!56'] = row(status=status)
        gl.pipeline_rows['g/app#14845'] = {'sha': SHA, 'id': 14845, 'status': status}
        r = self.runner(FakeMesh([t], max_concurrent=1), gl)
        self.addCleanup(r.store.db.close)
        r.refresh_me()
        r.store.mark(self.agent.name, t['id'], ci_delivery_key('g/app!56', 14845), f'{SHA}:14845:{status}')
        return r, t, gl, f'⏳ WAIT {target} until={predicate} after={SHA}:14845:running'

    def _turn(self, r, t, line, first=False):
        write_replies(self.replies, {t['id'][:8]: line, t['id'][:8] + ':resume': line})
        self.assertEqual(r.start_new(1) if first else r.resume_ready(1), 0)
        return self.finish_turn(r)

    def test_stale_wait_through_settle_goes_ready_without_a_wait_and_counts_as_idle(self):
        r, t, gl, line = self._stale_flow()
        a = self._turn(r, t, line, first=True)
        self.assertEqual(a.state, 'ready')
        self.assertIn('stale', a.wake_text)
        self.assertEqual(r.store.outcomes_today(self.agent.name, t['id']), {'wait': 1})
        self.assertEqual(r.store.idle_waits_today(self.agent.name, t['id']), 1)

    def test_repeated_stale_wait_cannot_loop_past_the_wait_budget(self):
        self.cfg.daily_wait_budget = 1
        r, t, gl, line = self._stale_flow()
        a = self._turn(r, t, line, first=True)
        self.assertEqual(a.state, 'ready')
        for _ in range(3):
            if a.state != 'ready':
                break
            a = self._turn(r, t, line)
        self.assertNotEqual(a.state, 'ready')
        used = r.store.outcomes_today(self.agent.name, t['id'])
        self.assertLessEqual(used.get('wait', 0), 3)
        self.assertNotIn('recheck', used)

    def test_stale_success_predicate_on_failed_result_is_not_bounced(self):
        r, t, gl, line = self._stale_flow(predicate='success')
        probe = r.store.create(self.agent.name, t['id'], 'claude')
        self.assertEqual(r._stale_ci_baseline(
            probe, {'kind': 'mr', 'target': 'g/app!56', 'ci_change': parse_line(line, NOW).extra['ci_change']}), '')
        r.store.update(probe, state='completed')
        a = self._turn(r, t, line, first=True)
        self.assertEqual(a.state, 'waiting')

    def test_stale_success_predicate_on_delivered_success_is_bounced(self):
        r, t, gl, line = self._stale_flow(status='success', predicate='success')
        a = self._turn(r, t, line, first=True)
        self.assertEqual(a.state, 'ready')
        self.assertIn('stale', a.wake_text)

    def test_stale_check_applies_the_normal_validation(self):
        bad = {
            'old_head': lambda gl: gl.mr_rows.update({'g/app!56': {'sha': SHA, 'head_pipeline': {
                'sha': OTHER, 'id': 14845, 'status': 'failed'}}}),
            'bad_sha': lambda gl: gl.mr_rows.update({'g/app!56': row(sha='nothex')}),
            'string_pid': lambda gl: gl.mr_rows.update({'g/app!56': row(pid='14845')}),
            'wrong_pipeline': lambda gl: gl.pipeline_rows.update({'g/app#14845': {
                'sha': SHA, 'id': 99, 'status': 'failed'}}),
        }
        for name, mutate in bad.items():
            with self.subTest(name=name):
                target = 'pipeline:g/app#14845' if name == 'wrong_pipeline' else 'mr:g/app!56'
                r, t, gl, line = self._stale_flow(target=target)
                mutate(gl)
                a = r.store.create(self.agent.name, t['id'], 'claude')
                o = parse_line(line, NOW)
                self.assertEqual(r._stale_ci_baseline(a, {'kind': o.kind, 'target': o.target, **o.extra}), '')
                r.store.update(a, state='completed')

    def test_stale_check_failure_is_logged_and_baseline_retained(self):
        r, t, gl, line = self._stale_flow()
        a = r.store.create(self.agent.name, t['id'], 'claude')
        o = parse_line(line, NOW)
        gl.mr_rows['g/app!56'] = row(sha='nothex')
        with self.assertLogs('mesh_runner', level='WARNING') as logs:
            self.assertEqual(r._stale_ci_baseline(a, {'kind': o.kind, 'target': o.target, **o.extra}), '')
        self.assertIn(a.task8, '\n'.join(logs.output))
        self.assertIn('CI head unavailable', '\n'.join(logs.output))

    def test_cap_observation_does_not_ack_before_admitted_writer(self):
        r,a,t,gl=self.setup_wait(marker(predicate='success'))
        r.store.update(a,state='failed',note='turn budget')
        r.store.mark(self.agent.name,t['id'],'cap_observation',a.id)
        gl.mr_rows['g/app!56']=row(pid=14846,status='pending')
        r._check_cap_event(a)
        self.assertFalse(a.wait.get('cap_wake'))
        gl.mr_rows['g/app!56']=row(pid=14846,status='success')
        r._check_cap_event(a)
        self.assertIn('success',a.wait['cap_wake'])
        self.assertIn('ci_delivery',a.wait)
        self.assertIsNone(r.store.mark(self.agent.name,t['id'],a.wait['ci_delivery']['key']))

    def test_review_already_delivered_comment_does_not_bypass_typed_hold(self):
        r,a,t,gl=self.setup_wait()
        old=NOW-dt.timedelta(days=3)
        last={'body':marker(),'created_at':iso(old),'author_id':r.agent_id}
        noise={'body':'earlier answer','created_at':iso(old+dt.timedelta(hours=1)),
               'author_id':'someone-else'}
        t['updated_at']=iso(old)
        r.store.mark(self.agent.name,t['id'],'review_fed','previous')
        with mock.patch.object(r,'_own_work',return_value=(a,old,last)), \
             mock.patch.object(r,'_comments',return_value=[last,noise]):
            self.assertIn('declared wait',r._review_hold(t))

    def test_known_failed_pipeline_redeclared_as_typed_mr_is_not_a_new_event(self):
        t=task(1)
        gl=FakeGitLab()
        gl.pipelines['g/app#14845']='failed'
        gl.mr_head['g/app!56']='14845:failed'
        sha='a'*40
        gl.mr_rows['g/app!56']={'sha':sha,'state':'opened',
            'head_pipeline':{'id':14845,'sha':sha,'status':'failed'}}
        write_replies(self.replies,{t['id'][:8]:'⏳ WAIT pipeline:g/app#14845',
            t['id'][:8]+':resume':f'⏳ WAIT mr:g/app!56 until=change after={sha}:14845:failed'})
        r=self.runner(FakeMesh([t]),gl)
        r.tick()
        # Settle each real child separately: tick() can settle the legacy wait,
        # wake it and launch its replacement in one call. A shared wall-clock
        # settling deadline also counts slow workspace/tick work on loaded CI.
        first=self.finish_turn(r)
        self.assertEqual(first.state,'waiting')
        r.tick()
        resumed=self.finish_turn(r)
        self.assertEqual(resumed.state,'waiting')
        for _ in range(3):
            r.tick()
            self.assertFalse(r.store.by_state(self.agent.name,('starting','running')),
                             'unchanged typed baseline must not launch another writer')
        self.assertEqual(len(calls(self.calls)),2,'same failed CI must not start another LLM turn')
        self.assertEqual(self.states(r)[t['id'][:8]],'waiting')

    def test_missing_observer_retains_typed_wait_without_age_wake_or_receipt(self):
        for target in ('mr:g/app!56', 'pipeline:g/app#14845'):
            with self.subTest(target=target):
                r, a, t, gl = self.setup_wait(marker(target))
                try:
                    old = iso(NOW - dt.timedelta(days=30))
                    r.store.update(a, wait={**a.wait, 'since': old, 'started': old})
                    r.gitlab = None
                    for _ in range(7):
                        r._wake_one(a)
                        self.assertEqual(a.state, 'waiting')
                        self.assertFalse(a.wait.get('recheck_due'))
                        self.assertFalse(a.wait.get('met'))
                        self.assertEqual(r.status()['attempts'][0]['wait_observation'], 'unknown')
                        self.assertIsNone(r.store.mark(self.agent.name, t['id'],
                            ci_delivery_key(a.wait['target'], 14845)))
                finally:
                    r.store.update(a, state='completed')

    def test_missing_observer_legacy_wait_keeps_age_recheck(self):
        for kind, target in (('mr', 'g/app!56'), ('pipeline', 'g/app#14845')):
            with self.subTest(kind=kind):
                wait = {'kind': kind, 'target': target,
                        'since': iso(NOW - dt.timedelta(days=30)),
                        'started': iso(NOW - dt.timedelta(days=30))}
                event = check(None, None, None, {}, wait, 'me', [], NOW)
                self.assertIn('GitLab observer unavailable', event)
                self.assertTrue(wait['recheck_due'])
                self.assertFalse(wait.get('met'))


class OwnPlay(TypedCI):
    """The owner's own play of a manual job: result contract is silent on running, explicit change is not."""

    def play(self, text, later):
        r, a, t, gl = self.setup_wait(text)
        gl.mr_rows['g/app!56'] = row(status='running')
        gl.pipeline_rows['g/app#14845']['status'] = 'running'
        r._wake_one(a)
        first = r.store.get(a.id).state
        gl.mr_rows['g/app!56'] = row(status=later)
        gl.pipeline_rows['g/app#14845']['status'] = later
        r._wake_one(a)
        last = r.store.get(a.id).state
        r.store.update(a, state='completed')
        return first, last

    # One target per test: the delivery receipt is shared by the MR and pipeline spellings.
    def own_play_result(self, target):
        failed = 'ready'   # exact pipeline or MR head: one owner-action notice, never success
        for later, final in (('running', 'waiting'), ('failed', failed), ('success', 'ready')):
            with self.subTest(target=target, later=later):
                self.tearDown(), self.setUp()
                self.assertEqual(self.play(marker(target, 'success', 'manual'), later), ('waiting', final))

    def test_own_play_result_contract_pipeline(self):
        self.own_play_result('pipeline:g/app#14845')

    def test_own_play_result_contract_mr(self):
        self.own_play_result('mr:g/app!56')

    def test_own_play_failed_mr_head_wakes_with_an_owner_notice(self):
        r, a, t, gl = self.setup_wait(marker('mr:g/app!56', 'success', 'manual'))
        gl.mr_rows['g/app!56'] = row(status='running')
        r._wake_one(a)
        self.assertEqual(r.store.get(a.id).state, 'waiting')
        gl.mr_rows['g/app!56'] = row(status='failed')
        r._wake_one(a)
        got = r.store.get(a.id)
        self.assertEqual(got.state, 'ready')
        self.assertIn('action required', got.wake_text)
        self.assertIn('success predicate is not met', got.wake_text)
        self.assertIsNone(r.store.mark(self.agent.name, t['id'], 'met_wake'))

    def test_explicit_change_on_same_manual_to_running_is_still_delivered_pipeline(self):
        self.assertEqual(self.play(marker('pipeline:g/app#14845', 'change', 'manual'), 'running')[0], 'ready')

    def test_explicit_change_on_same_manual_to_running_is_still_delivered_mr(self):
        self.assertEqual(self.play(marker('mr:g/app!56', 'change', 'manual'), 'running')[0], 'ready')

    def test_agent_contract_and_manual_notice_tell_owner_to_wait_for_result(self):
        from mesh_runner.outcome import CONTRACT
        self.assertIn('until=success on that pipeline', CONTRACT)


class ExplicitWaitDiagnosisTests(unittest.TestCase):
    SHA = 'a' * 40

    def note(self, tail):
        o = parse_line(f'⏳ WAIT mr:g/app!5 {tail}', NOW)
        self.assertEqual(o.kind, 'unparsed')
        return o.note

    def test_none_baseline_is_named(self):
        self.assertIn('no pipeline yet', self.note(f'until=change after={self.SHA}:none:none'))

    def test_opened_baseline_with_change_is_named(self):
        self.assertIn('until=merged', self.note(f'until=change after={self.SHA}:opened'))

    def test_other_malformation_gets_the_forms(self):
        self.assertIn('expected', self.note('until=change'))
