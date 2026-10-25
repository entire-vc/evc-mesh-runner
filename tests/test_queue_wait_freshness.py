"""Queue demand sleeps without turns; exact fresh proof wakes once durably."""
import copy
import datetime as dt
import json
import os
import threading
from unittest import mock
from uuid import UUID

from mesh_runner import queue_wait as q
from mesh_runner.store import Store, iso
from mesh_runner.mesh import MeshError
from mesh_runner.runner import Runner
from fakes import AGENT_ID, FakeMesh, task
from test_runner import Bench

NOW = 1792500000
A = str(UUID(int=21))
P = str(UUID(int=22))
VALUE = dict(version=1, observed_at=NOW - 600,
             targets=[dict(agent_id=A, project_id=P, lane='Executor')])


def description(value=VALUE):
    return ('[fleet queue-request:' + json.dumps(value, separators=(',', ':')) + '] '
            + '[fiddler starvation-wake:' + str(int(value['observed_at'])) + ']')


def manifest(req):
    return dict(version=1, revision=1, requests=[req], terminal=[])


def proof(req, verified=NOW):
    generation = req['generation_id']
    return dict(version=1, clock_watermark=verified, manifest_revision=1, manifest_digest=q.digest(manifest(req)),
                current={req['task_id']: generation}, requests={generation: dict(
                    request=req, checked_at=verified, clock_watermark=verified,
                    proof=dict(version=1, request=req, verified_at=verified, expires_at=verified + 600))})


class QueueWait(Bench):
    def setUp(self):
        super().setUp()
        self.cfg.queue_wait_freshness_v1 = True
        clock = mock.patch('mesh_runner.runner.time.time', return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)

    def setup_wait(self, **changes):
        t = task(1, labels=['fleet-starvation-wake', 'pin-assignee', 'owner-locked'],
                 human_gate=False, description=description())
        t.update(changes)
        r = self.runner(FakeMesh([t]))
        r.refresh_me()
        a = r.store.create(self.agent.name, t['id'], 'claude')
        r.store.update(a, state='waiting', wait=dict(kind='time', until=iso(dt.datetime(2026,1,1,tzinfo=dt.timezone.utc)),
                       started=iso(dt.datetime(2026,1,1,tzinfo=dt.timezone.utc)), since=iso()))
        return r, a, t

    def mark_delivered(self, r, a):
        """The first-move turn already ran for this source: only availability wakes now."""
        r.store.mark(a.agent, a.task_id, q.delivered_key(r.agent_id, a.wait['queue_request']['contract']), 'delivered')

    def adopted(self):
        r,a,t = self.setup_wait()
        r._wake_one(a)
        self.assertEqual(a.state, 'waiting')
        self.assertEqual(a.wait['kind'], 'queue')
        self.mark_delivered(r, a)
        q.publish(r.store, self.cfg.state_dir/'queue-wait-requests.json',
                  self.cfg.state_dir/'queue-wait-receipts.json')
        return r,a,t

    def receipt(self, a, value=None):
        q.write_json(self.cfg.state_dir / 'queue-wait-receipts.json', value or proof(a.wait['queue_request']))

    def test_fulfilled_source_does_not_rearm_after_restart_but_new_source_does(self):
        r,a,t = self.adopted()
        self.receipt(a)
        r._wake_one(a)
        self.assertEqual(a.state, 'ready')
        r = self.runner(r.mesh)
        r.refresh_me()
        a = r.store.get(a.id)
        path = self.root/'after-proof.jsonl'
        path.write_text(json.dumps(dict(type='result', subtype='success', is_error=False,
            result='⏳ WAIT 6h: owner acceptance required', session_id='s'))+'\n')
        r.store.update(a, state='running', log_path=str(path), turns=1)
        r.settle(a)
        self.assertEqual(a.wait['kind'], 'time')
        original = copy.deepcopy(a.wait)
        # Upgrade compatibility: pre-fix completed intents lack consumed marks.
        r.store.db.execute("DELETE FROM marks WHERE key LIKE 'queue_consumed:%'")
        r._wake_one(a)
        self.assertEqual(a.wait, original)
        newer = copy.deepcopy(VALUE)
        newer['observed_at'] += 1
        r.mesh.tasks[t['id']]['description'] = description(newer)
        r.mesh.tasks[t['id']]['start_after'] = None
        r.store.update(a, wait=dict(a.wait, until=iso(dt.datetime(2026,1,1,tzinfo=dt.timezone.utc))))
        r._wake_one(a)
        self.assertEqual(a.wait['kind'], 'queue')
        self.assertEqual(a.wait['queue_request']['contract'], newer)

    def test_completed_history_does_not_consume_a_genuinely_new_source(self):
        r,a,t = self.adopted()
        for number in range(130):
            old = copy.deepcopy(VALUE)
            old['observed_at'] -= number
            req = q.new_request(a.task_id, old)
            mark = q.intent(a, req, r.agent_id, NOW)
            mark['state'] = 'complete'
            r.store.mark(a.agent, a.task_id, q.key(req), json.dumps(mark))
        self.assertTrue(q.consumed(r.store,a,r.agent_id,VALUE))
        newer = copy.deepcopy(VALUE)
        newer['observed_at'] += 1
        self.assertFalse(q.consumed(r.store,a,r.agent_id,newer))

    def test_explicit_label_withdrawal_cancels_pending_and_continues_without_proof(self):
        r,a,t = self.adopted()
        req = copy.deepcopy(a.wait['queue_request'])
        t['labels'].remove('fleet-starvation-wake')
        horizon = iso(dt.datetime.fromtimestamp(NOW+86400, dt.timezone.utc))
        t['start_after'] = horizon
        r.store.update(a, wait=dict(a.wait, scheduled=horizon))
        r._wake_one(a)
        self.assertEqual(a.state, 'ready')
        self.assertIn('withdrew', a.wake_text)
        self.assertEqual(json.loads(r.store.mark(a.agent,a.task_id,q.key(req))[0])['state'], 'cancelled')
        self.assertEqual(a.turns, 0)
        self.assertFalse(r.mesh.checkouts)

    def test_withdrawal_unknown_owner_human_and_future_gate_fail_closed(self):
        r,a,t = self.adopted()
        original = copy.deepcopy(t)
        for changes in ({'labels':None}, {'labels':[None]}, {'labels':['pin-assignee'], 'human_gate':True},
                        {'labels':['pin-assignee'], 'human_gate':None},
                        {'labels':['pin-assignee'], 'completion_signal':True},
                        {'labels':['pin-assignee'], 'checked_out_by':'foreign'},
                        {'labels':['pin-assignee'], 'assignee_id':'foreign'},
                        {'labels':['pin-assignee'], 'start_after':'not-time'},
                        {'labels':['pin-assignee'], 'start_after':iso(dt.datetime.fromtimestamp(NOW+600,dt.timezone.utc))}):
            with self.subTest(changes=changes):
                t.clear(); t.update(original); t.update(changes)
                r._wake_queue(a,t)
                self.assertEqual(a.state, 'waiting')
        t.clear(); t.update(original)
        r.mesh.broken_tasks.add(t['id'])
        with self.assertRaises(MeshError):
            r._wake_one(a)
        self.assertEqual(a.state, 'waiting')

    def test_existing_machine_time_wait_adopted_without_turn_or_provider_call(self):
        r,a,t = self.adopted()
        self.assertEqual(a.turns,0)
        self.assertFalse(r.mesh.checkouts)
        self.assertIsNotNone(q.pending(r.store,a,a.wait['queue_request'],r.agent_id))
        self.assertNotIn('scheduled',a.wait)

    def test_new_settle_allocates_generation_and_pending_atomically_each_wait(self):
        r,a,t = self.setup_wait()
        path=self.root/'turn.jsonl'
        path.write_text(json.dumps(dict(type='result',subtype='success',is_error=False,
                                        result='⏳ WAIT 2h: queue empty',session_id='s'))+'\n')
        r.store.update(a,state='running',log_path=str(path),turns=1)
        r.settle(a)
        self.assertEqual(a.state,'waiting')
        first=a.wait['queue_request']
        self.assertIsNotNone(q.pending(r.store,a,first,r.agent_id))
        r.store.update(a,state='running')
        r.settle(a)
        self.assertNotEqual(first['generation_id'],a.wait['queue_request']['generation_id'])

    def test_unknown_missing_error_expired_and_24hour_age_do_not_wake(self):
        r,a,t=self.adopted()
        for raw in (None, {'version':0}, proof(a.wait['queue_request'],NOW-600)):
            if raw is not None:
                self.receipt(a,raw)
            r._wake_one(a)
            self.assertEqual(r.store.get(a.id).state,'waiting')
            self.assertIsNotNone(q.pending(r.store,a,a.wait['queue_request'],r.agent_id))
        self.assertEqual(a.turns,0)
        self.assertEqual(r.mesh.comment_log,{})

    def test_exact_fresh_proof_wakes_once_and_restart_is_terminal(self):
        r,a,t=self.adopted()
        self.receipt(a)
        r._wake_one(a)
        self.assertEqual(a.state,'ready')
        self.assertEqual(json.loads(r.store.mark(a.agent,a.task_id,q.key(a.wait['queue_request']))[0])['state'],'complete')
        restarted=self.runner(r.mesh)
        restarted.refresh_me()
        self.assertFalse(restarted.store.by_state(a.agent,('waiting',)))
        a.state='waiting'  # stale callback cannot redeliver terminal generation
        restarted._wake_queue(a,t)
        self.assertEqual(restarted.store.get(a.id).state,'ready')
        self.assertEqual(a.turns,0)

    def test_receipt_manifest_and_read_clock_must_bind_before_ready(self):
        r,a,t=self.adopted()
        req=a.wait['queue_request']
        original=q.intent(a,req,r.agent_id,NOW)
        mutations = [
            ('missing revision', lambda v: v.pop('manifest_revision')),
            ('boolean revision', lambda v: v.update(manifest_revision=True)),
            ('float revision', lambda v: v.update(manifest_revision=1.0)),
            ('older revision', lambda v: v.update(manifest_revision=0)),
            ('newer revision', lambda v: v.update(manifest_revision=2)),
            ('missing digest', lambda v: v.pop('manifest_digest')),
            ('empty digest', lambda v: v.update(manifest_digest='')),
            ('foreign digest', lambda v: v.update(manifest_digest='0'*64)),
            ('missing checked_at', lambda v: v['requests'][req['generation_id']].pop('checked_at')),
        ]
        for invalid in (True, 0, -1, '1792500000', float('nan'), float('inf'), NOW+1, VALUE['observed_at']-1):
            mutations.append((f'checked_at {invalid}', lambda v, x=invalid:
                v['requests'][req['generation_id']].update(checked_at=x)))
        mutations += [
            ('row watermark before proof', lambda v: v['requests'][req['generation_id']].update(clock_watermark=NOW-1)),
            ('global watermark before row', lambda v: v.update(clock_watermark=NOW-1)),
        ]
        for name, mutate in mutations:
            with self.subTest(name=name):
                r.store.update(a,state='waiting')
                r.store.mark(a.agent,a.task_id,q.key(req),json.dumps(original))
                value=proof(req)
                mutate(value)
                self.receipt(a,value)
                r._wake_one(a)
                self.assertEqual(a.state,'waiting')
                self.assertIsNotNone(q.pending(r.store,a,req,r.agent_id))

    def test_receipt_from_previous_manifest_cannot_wake_after_outbox_change(self):
        r,a,t=self.adopted()
        self.receipt(a)
        previous=json.loads(r.store.mark('__runner__','__queue__','manifest')[0])
        req=a.wait['queue_request']
        for revision in (previous['revision'],previous['revision']+1):
            with self.subTest(revision=revision):
                newer=dict(previous, revision=revision, terminal=[str(UUID(int=99))])
                r.store.mark('__runner__','__queue__','manifest',json.dumps(newer))
                r.store.mark(a.agent,a.task_id,q.key(req),json.dumps(q.intent(a,req,r.agent_id,NOW)))
                r._wake_one(a)
                self.assertEqual(a.state,'waiting')
                self.assertIsNotNone(q.pending(r.store,a,req,r.agent_id))

    def test_read_start_precedes_successful_finish_and_global_watermark(self):
        req=q.new_request(task(1)['id'],VALUE)
        value=proof(req, NOW+5)
        value['requests'][req['generation_id']]['checked_at']=NOW
        value['clock_watermark']=NOW+10
        self.assertTrue(q.available(value,req,NOW+10,manifest(req)))
        self.assertFalse(q.available(value,req,NOW+10))  # no owner outbox
        for owner in (dict(manifest(req),requests=[]), dict(manifest(req),version=True),
                      dict(manifest(req),revision=True), dict(manifest(req),requests=[req,req]),
                      dict(manifest(req),terminal=[req['generation_id']])):
            with self.subTest(owner=owner):
                coherent=copy.deepcopy(value)
                coherent['manifest_digest']=q.digest(owner)
                self.assertFalse(q.available(coherent,req,NOW+10,owner))

    def test_missing_corrupt_owner_manifest_and_cas_manifest_change_keep_pending(self):
        r,a,t=self.adopted()
        req=a.wait['queue_request']
        self.receipt(a)
        owner=q.published_manifest(r.store)
        original=q.intent(a,req,r.agent_id,NOW)
        for raw in ('not json', 'null', '{}'):
            with self.subTest(raw=raw):
                r.store.mark('__runner__','__queue__','manifest',raw)
                r.store.mark(a.agent,a.task_id,q.key(req),json.dumps(original))
                r._wake_one(a)
                self.assertEqual(a.state,'waiting')
        r.store.db.execute("DELETE FROM marks WHERE agent='__runner__' AND task_id='__queue__' AND key='manifest'")
        r.store.mark(a.agent,a.task_id,q.key(req),json.dumps(original))
        r._wake_one(a)
        self.assertEqual(a.state,'waiting')
        r.store.mark('__runner__','__queue__','manifest',json.dumps(owner))
        r.store.mark(a.agent,a.task_id,q.key(req),json.dumps(original))
        original_mark=r.store.mark
        def change_owner(agent,task_id,key,value=None):
            result=original_mark(agent,task_id,key,value)
            if value and key==q.key(req) and json.loads(value).get('state')=='complete':
                original_mark('__runner__','__queue__','manifest',json.dumps(dict(owner,revision=2)))
            return result
        with mock.patch.object(r.store,'mark',side_effect=change_owner):
            r._wake_one(a)
        self.assertEqual(a.state,'waiting')
        self.assertIsNotNone(q.pending(r.store,a,req,r.agent_id))
        self.assertEqual(q.published_manifest(r.store),owner)

    def test_crash_before_effect_replays_pending_only_with_fresh_proof(self):
        r,a,t=self.adopted()
        self.receipt(a)
        with mock.patch.object(r.store,'update_with_marks',side_effect=RuntimeError('crash')):
            with self.assertRaises(RuntimeError):
                r._wake_one(a)
        self.assertEqual(r.store.get(a.id).state,'waiting')
        restarted=self.runner(r.mesh)
        restarted.refresh_me()
        saved=restarted.store.get(a.id)
        with mock.patch('mesh_runner.runner.time.time',return_value=NOW+600):
            restarted._wake_one(saved)
        self.assertEqual(saved.state,'waiting')
        self.receipt(saved,proof(saved.wait['queue_request'],NOW+600))
        with mock.patch('mesh_runner.runner.time.time',return_value=NOW+900):
            restarted._wake_one(saved)
        self.assertEqual(saved.state,'ready')

    def test_cas_generation_change_and_mark_failure_cannot_ack(self):
        r,a,t=self.adopted()
        req=a.wait['queue_request']
        original=copy.deepcopy(a.wait)
        newer=q.new_request(a.task_id,VALUE)
        r.store.update(a,wait=dict(a.wait,queue_request=newer))
        self.assertFalse(r.store.update_with_marks(a,{q.key(req):'complete'},expect='waiting',
                        expect_wait=original,state='ready'))
        self.assertEqual(a.state,'waiting')
        self.assertEqual(json.loads(r.store.mark(a.agent,a.task_id,q.key(req))[0])['state'],'pending')
        with mock.patch.object(r.store,'mark',side_effect=RuntimeError('crash')):
            with self.assertRaises(RuntimeError):
                r.store.update_with_marks(a,{q.key(newer):'complete'},expect='waiting',expect_wait=a.wait,state='ready')
        self.assertEqual(r.store.get(a.id).state,'waiting')

    def test_slow_source_read_and_expiry_at_commit_keep_pending(self):
        r,a,t=self.adopted()
        self.receipt(a)
        with mock.patch('mesh_runner.runner.time.time', side_effect=[NOW,NOW+600]):
            r._wake_one(a)
        self.assertEqual(a.state,'waiting')
        original_mark=r.store.mark
        expired=[False]
        def mark(agent,task_id,key,value=None):
            result=original_mark(agent,task_id,key,value)
            if value and key.startswith(q.PREFIX) and json.loads(value).get('state')=='complete':
                expired[0]=True
            return result
        with mock.patch.object(r.store,'mark',side_effect=mark), mock.patch(
                'mesh_runner.runner.time.time',side_effect=lambda: NOW+600 if expired[0] else NOW):
            r._wake_one(a)
        self.assertEqual(r.store.get(a.id).state,'waiting')
        self.assertEqual(a.state,'waiting')
        self.assertIsNotNone(q.pending(r.store,a,a.wait['queue_request'],r.agent_id))

    def test_identity_mismatch_unknown_and_consumer_clock_rollback(self):
        r,a,t=self.adopted()
        req=a.wait['queue_request']
        bad=proof(req)
        bad['requests'][req['generation_id']]['proof']['request']=q.new_request(a.task_id,VALUE)
        self.receipt(a,bad)
        r._wake_one(a)
        self.assertEqual(a.state,'waiting')
        self.receipt(a)
        with mock.patch('mesh_runner.runner.time.time',return_value=NOW-1):
            r._wake_one(a)
        self.assertEqual(a.state,'waiting')
        with mock.patch('mesh_runner.runner.time.time',return_value=NOW+300):
            r._wake_one(a)
        self.assertEqual(a.state,'ready')

    def test_owner_human_checkout_and_park_holds_never_ack(self):
        for change in ({'human_gate':True},{'human_gate':None},{'checked_out_by':'foreign'},
                       {'labels':['fleet-starvation-wake','park:manual']}):
            with self.subTest(change=change):
                r,a,t=self.adopted()
                self.receipt(a)
                t.update(change)
                r._wake_one(a)
                self.assertEqual(a.state,'waiting')
                self.assertIsNotNone(q.pending(r.store,a,a.wait['queue_request'],r.agent_id))
                r.store.update(a,state='completed')
        r,a,t=self.adopted()
        self.receipt(a)
        t['assignee_id']='other'
        r._wake_one(a)
        self.assertEqual(a.state,'completed')
        self.assertEqual(json.loads(r.store.mark(a.agent,a.task_id,q.key(a.wait['queue_request']))[0])['state'],'cancelled')

    def test_comment_read_error_never_reuses_old_description_or_timer(self):
        r,a,t=self.setup_wait()
        with mock.patch.object(r.mesh, 'comments', side_effect=MeshError(429, 'unavailable')):
            r._wake_one(a)
        self.assertEqual(a.state, 'waiting')
        self.assertEqual(a.wait['kind'], 'time')
        r._wake_one(a)
        q.publish(r.store, self.cfg.state_dir/'queue-wait-requests.json',
                  self.cfg.state_dir/'queue-wait-receipts.json')
        self.receipt(a)
        with mock.patch.object(r.mesh, 'comments', side_effect=MeshError(429, 'unavailable')):
            r._wake_one(a)
        self.assertEqual(a.state, 'waiting')
        self.assertIsNotNone(q.pending(r.store,a,a.wait['queue_request'],r.agent_id))
        with mock.patch('mesh_runner.runner.time.time',return_value=NOW+300):
            r._wake_one(a)
        self.assertEqual(a.state, 'ready')

    def test_source_change_supersedes_without_llm_and_old_proof_cannot_wake(self):
        r,a,t=self.adopted()
        old=a.wait['queue_request']
        self.receipt(a)
        changed=dict(VALUE,observed_at=NOW-1)
        r.mesh.comment_log[t['id']]=[dict(body=description(changed),created_at=iso(),author_id='coordinator')]
        r._wake_one(a)
        self.assertEqual(a.state,'waiting')
        self.assertNotEqual(a.wait['queue_request']['generation_id'],old['generation_id'])
        self.assertEqual(a.wait['queue_request']['contract'],changed)
        self.assertEqual(json.loads(r.store.mark(a.agent,a.task_id,q.key(old))[0])['state'],'cancelled')
        r._wake_one(a)
        self.assertEqual(a.state,'waiting')
        current=copy.deepcopy(a.wait['queue_request'])
        r.mesh.comment_log[t['id']]=[]  # bounded read cannot roll back the known request
        with mock.patch('mesh_runner.runner.time.time',return_value=NOW+300):
            r._wake_one(a)
        self.assertEqual(a.wait['queue_request'],current)
        self.assertEqual(a.state,'waiting')

    def test_source_poll_and_watermark_writes_are_bounded_across_ticks_and_restart(self):
        r,a,t=self.adopted()
        with mock.patch.object(r.mesh,'comments',wraps=r.mesh.comments) as read, mock.patch.object(
                r.store,'mark',wraps=r.store.mark) as mark:
            for second in range(300):
                with mock.patch('mesh_runner.runner.time.time',return_value=NOW+second):
                    r._wake_one(a)
            self.assertEqual(read.call_count,1)
            writes=[c for c in mark.call_args_list if len(c.args)==4 and c.args[2].startswith(q.PREFIX)]
            self.assertEqual(len(writes),1)
            restarted=self.runner(r.mesh)
            restarted.refresh_me()
            saved=restarted.store.get(a.id)
            with mock.patch('mesh_runner.runner.time.time',return_value=NOW+299):
                restarted._wake_one(saved)
            self.assertEqual(read.call_count,1)
            with mock.patch('mesh_runner.runner.time.time',return_value=NOW+300):
                restarted._wake_one(saved)
            self.assertEqual(read.call_count,2)
        self.assertEqual(saved.state,'waiting')
        self.assertEqual(saved.turns,0)

    def test_source_429_retry_is_bounded_and_recovery_uses_fresh_source(self):
        r,a,t=self.adopted()
        self.receipt(a)
        with mock.patch.object(r.mesh,'comments',side_effect=MeshError(429,'unavailable')) as failed:
            for second in (0,1,30,100,299):
                with mock.patch('mesh_runner.runner.time.time',return_value=NOW+second):
                    r._wake_one(a)
            self.assertEqual(failed.call_count,1)
        restarted=self.runner(r.mesh)
        restarted.refresh_me()
        saved=restarted.store.get(a.id)
        with mock.patch.object(r.mesh,'comments',wraps=r.mesh.comments) as read:
            with mock.patch('mesh_runner.runner.time.time',return_value=NOW+299):
                restarted._wake_one(saved)
            self.assertEqual(read.call_count,0)
            self.assertEqual(saved.state,'waiting')
            with mock.patch('mesh_runner.runner.time.time',return_value=NOW+300):
                restarted._wake_one(saved)
            self.assertEqual(read.call_count,1)
        self.assertEqual(saved.state,'ready')

    def test_new_source_supersedes_within_bound_without_any_receipt(self):
        r,a,t=self.adopted()
        r._wake_one(a)
        old=copy.deepcopy(a.wait['queue_request'])
        changed=dict(VALUE,observed_at=NOW+1)
        r.mesh.comment_log[t['id']]=[dict(body=description(changed),created_at=iso())]
        with mock.patch('mesh_runner.runner.time.time',return_value=NOW+299):
            r._wake_one(a)
        self.assertEqual(a.wait['queue_request'],old)
        with mock.patch('mesh_runner.runner.time.time',return_value=NOW+300):
            r._wake_one(a)
        self.assertEqual(a.wait['queue_request']['contract'],changed)
        self.assertNotEqual(a.wait['queue_request']['generation_id'],old['generation_id'])
        self.assertEqual(a.state,'waiting')
        self.assertEqual(a.turns,0)

    def test_invalid_source_clock_or_rollback_never_rewrites_later_mark(self):
        r,a,t=self.adopted()
        req=a.wait['queue_request']
        original=q.pending(r.store,a,req,r.agent_id)
        for invalid in (float('nan'),float('inf'),True,-1,NOW+1):
            with self.subTest(invalid=invalid):
                value=dict(original,source_checked_at=invalid)
                r.store.mark(a.agent,a.task_id,q.key(req),json.dumps(value))
                with mock.patch.object(r.mesh,'comments') as read:
                    r._wake_one(a)
                self.assertEqual(read.call_count,0)
                self.assertEqual(r.store.mark(a.agent,a.task_id,q.key(req))[0],json.dumps(value))
        value=dict(original,source_checked_at=NOW,clock_watermark=NOW)
        r.store.mark(a.agent,a.task_id,q.key(req),json.dumps(value))
        with mock.patch('mesh_runner.runner.time.time',return_value=NOW-1):
            r._wake_one(a)
        self.assertEqual(r.store.mark(a.agent,a.task_id,q.key(req))[0],json.dumps(value))

    def test_manifest_revision_restart_terminal_ack_gc_handshake(self):
        r,a,t=self.adopted()
        path=self.cfg.state_dir/'queue-wait-requests.json'
        receipt_path=self.cfg.state_dir/'queue-wait-receipts.json'
        first=q.publish(r.store,path,receipt_path)
        self.assertEqual(first['revision'],1)
        self.assertEqual(q.publish(Store(self.root / 'state' / 'r.sqlite'),path,receipt_path),first)
        self.receipt(a)
        r._wake_one(a)
        terminal=q.publish(r.store,path,receipt_path)
        generation=a.wait['queue_request']['generation_id']
        self.assertEqual(terminal['requests'],[])
        self.assertEqual(terminal['terminal'],[generation])
        q.write_json(receipt_path,dict(version=1,manifest_revision=terminal['revision'],
                                     manifest_digest=q.digest(terminal),terminal_ack=[generation]))
        ack=q.publish(r.store,path,receipt_path)
        self.assertEqual(ack['terminal'],[])
        self.assertGreater(ack['revision'],terminal['revision'])
        self.assertEqual(json.loads(r.store.mark(a.agent,a.task_id,q.key(a.wait['queue_request']))[0])['state'],'complete')
        self.assertEqual(path.stat().st_mode & 0o777,0o600)

    def test_malformed_terminal_ack_is_unavailable_and_status_loop_survives(self):
        r,a,t=self.adopted()
        req=a.wait['queue_request']
        owner=q.published_manifest(r.store)
        path=self.cfg.state_dir/'queue-wait-requests.json'
        receipt_path=self.cfg.state_dir/'queue-wait-receipts.json'
        original=path.read_bytes()
        outer=Runner(self.cfg,lock=False)
        outer.store=r.store
        outer.agents=[]  # run the real status loop without starting agent/provider threads
        self.cfg.poll_seconds=0
        for invalid in (None,1,True,1.5,{},[],"x"):
            with self.subTest(ack=invalid):
                value=proof(req)
                value['terminal_ack']=[invalid]
                q.write_json(receipt_path,value)
                self.assertFalse(q.available(value,req,NOW,owner))
                with self.assertRaises(ValueError):
                    q.publish(r.store,path,receipt_path)
                stop=threading.Event()
                write=outer.write_status
                count=0
                def bounded_status(out):
                    nonlocal count
                    write(out)
                    count+=1
                    if count==2:
                        stop.set()
                with mock.patch.object(outer,'write_status',side_effect=bounded_status), self.assertLogs(
                        'mesh_runner',level='WARNING') as captured:
                    outer.run_forever(stop)
                self.assertEqual(count,3)  # two live iterations plus final status write
                self.assertEqual(len(captured.output),3)
                r._wake_one(a)
                self.assertEqual(r.store.get(a.id).state,'waiting')
                self.assertIsNotNone(q.pending(r.store,a,req,r.agent_id))
                self.assertEqual(q.published_manifest(r.store),owner)
                self.assertEqual(path.read_bytes(),original)
        recovered=NOW+q.SOURCE_INTERVAL
        self.receipt(a,proof(req,recovered))
        with mock.patch('mesh_runner.runner.time.time',return_value=recovered):
            r._wake_one(a)
        self.assertEqual(r.store.get(a.id).state,'ready')
        self.assertEqual(a.turns,0)

    def test_malformed_terminal_ack_never_acknowledges_or_drops_terminal_intent(self):
        r,a,t=self.adopted()
        self.receipt(a)
        r._wake_one(a)
        req=a.wait['queue_request']
        path=self.cfg.state_dir/'queue-wait-requests.json'
        receipt_path=self.cfg.state_dir/'queue-wait-receipts.json'
        terminal=q.publish(r.store,path,receipt_path)
        mark=r.store.mark(a.agent,a.task_id,q.key(req))
        original=path.read_bytes()
        for invalid in (None,1,'x',str(UUID(int=0xabcdef)).upper()):
            with self.subTest(ack=invalid):
                q.write_json(receipt_path,dict(version=1,manifest_revision=terminal['revision'],
                    terminal_ack=[req['generation_id'],invalid]))
                with self.assertRaises(ValueError):
                    q.publish(r.store,path,receipt_path)
                self.assertEqual(r.store.mark(a.agent,a.task_id,q.key(req)),mark)
                self.assertEqual(q.published_manifest(r.store),terminal)
                self.assertEqual(path.read_bytes(),original)
        q.write_json(receipt_path,dict(version=1,manifest_revision=terminal['revision'],
                                      terminal_ack=[req['generation_id']]))
        self.assertEqual(q.publish(r.store,path,receipt_path)['terminal'],[])

    def test_older_terminal_ack_survives_concurrent_manifest_changes(self):
        r,a,t=self.adopted()
        path=self.cfg.state_dir/'queue-wait-requests.json'
        receipt_path=self.cfg.state_dir/'queue-wait-receipts.json'
        q.publish(r.store,path,receipt_path)
        self.receipt(a)
        r._wake_one(a)
        terminal=q.publish(r.store,path,receipt_path)
        generation=a.wait['queue_request']['generation_id']
        b=r.store.create(a.agent,task(2)['id'],'claude')
        req=q.new_request(b.task_id,VALUE)
        r.store.update_with_marks(b,{q.key(req):json.dumps(q.intent(b,req,r.agent_id,NOW))},
                                  state='waiting',wait=dict(kind='queue',queue_request=req))
        newer=q.publish(r.store,path,receipt_path)
        self.assertGreater(newer['revision'],terminal['revision'])
        q.write_json(receipt_path,dict(version=1,manifest_revision=terminal['revision'],terminal_ack=[generation]))
        self.assertEqual(q.publish(r.store,path,receipt_path)['terminal'],[])

    def test_export_failure_retains_revision_and_pending_then_restart_republishes(self):
        r,a,t=self.adopted()
        path=self.cfg.state_dir/'queue-wait-requests.json'
        receipt_path=self.cfg.state_dir/'queue-wait-receipts.json'
        with mock.patch.object(q,'write_json',side_effect=OSError('disk')):
            with self.assertRaises(OSError):
                q.publish(r.store,path,receipt_path)
        persisted=json.loads(r.store.mark('__runner__','__queue__','manifest')[0])
        self.assertEqual(q.publish(r.store,path,receipt_path),persisted)
        self.assertIsNotNone(q.pending(r.store,a,a.wait['queue_request'],r.agent_id))

    def test_default_off_and_ordinary_timer_have_no_queue_intent(self):
        r,a,t=self.setup_wait()
        self.cfg.queue_wait_freshness_v1=False
        r._wake_one(a)
        self.assertNotEqual(a.wait.get('kind'),'queue')
        self.assertEqual(r.store.db.execute('SELECT count(*) FROM marks WHERE key LIKE ?', (q.PREFIX+'%',)).fetchone()[0],0)
        self.cfg.queue_wait_freshness_v1=True
        r.store.update(a,state='completed')
        r,b,t=self.setup_wait(labels=[],description='queue is empty')
        r._wake_one(b)
        self.assertNotEqual(b.wait.get('kind'),'queue')

    def test_private_files_reject_symlink_world_readable_and_oversized(self):
        path=self.root/'state.json'
        q.write_json(path,{'ok':True})
        alias=self.root/'alias.json'
        alias.symlink_to(path)
        with self.assertRaises(OSError): q.read_json(alias)
        with self.assertRaises(ValueError): q.write_json(alias,{'bad':True})
        os.chmod(path,0o644)
        with self.assertRaises(ValueError): q.read_json(path)
        os.chmod(path,0o600)
        with path.open('wb') as stream: stream.truncate(q.MAX_BYTES+1)
        with self.assertRaises(ValueError): q.read_json(path)

    def test_observed_at_requires_exact_integer_in_typed_source_and_request(self):
        machine = VALUE['observed_at']
        t = task(1, human_gate=False, labels=['fleet-starvation-wake'])
        for observed in (machine + 0.5, float(machine), True, str(machine)):
            with self.subTest(observed=observed):
                value = dict(VALUE, observed_at=observed)
                t['description'] = ('[fleet queue-request:' + json.dumps(value) + '] '
                                    + f'[fiddler starvation-wake:{machine}]')
                self.assertIsNone(q.source(t, [], AGENT_ID, NOW))
                with self.assertRaises(ValueError):
                    q.new_request(t['id'], value)
        t['description'] = description(VALUE)
        self.assertEqual(q.source(t, [], AGENT_ID, NOW), VALUE)
        self.assertEqual(q.new_request(t['id'], VALUE)['contract'], VALUE)
        t['description'] = description().replace(str(machine) + ']', str(machine + 1) + ']')
        self.assertIsNone(q.source(t, [], AGENT_ID, NOW))

    def test_fractional_source_cannot_be_adopted_as_machine_queue_wait(self):
        r, a, t = self.setup_wait(description=description(dict(VALUE, observed_at=VALUE['observed_at'] + 0.5)))
        r._wake_one(a)
        self.assertNotEqual(a.wait.get('kind'), 'queue')
        self.assertEqual(r.store.db.execute(
            'SELECT count(*) FROM marks WHERE key LIKE ?', (q.PREFIX + '%',)
        ).fetchone()[0], 0)

    def test_source_strict_machine_contract_not_title_or_malformed_marker(self):
        t=task(1,human_gate=False,labels=['fleet-starvation-wake'],description=description())
        self.assertEqual(q.source(t,[],AGENT_ID,NOW),VALUE)
        for text in ('queue empty',description().replace('fiddler starvation-wake','human request'),
                     description().replace('"version":1','"version":true'),
                     description(dict(VALUE,observed_at=NOW+1))):
            t['description']=text
            self.assertIsNone(q.source(t,[],AGENT_ID,NOW))
        req=q.new_request(t['id'],VALUE)
        self.assertFalse(q.available(proof(req),q.new_request(t['id'],VALUE),NOW,manifest(req)))

    def test_newest_integer_source_supersedes_older_float_without_normalizing_it(self):
        r, a, t = self.setup_wait(description=description(dict(VALUE, observed_at=VALUE['observed_at'] + 0.5)))
        newer = dict(VALUE, observed_at=NOW - 10)
        r.mesh.add_comment(t['id'], description(newer))
        self.assertEqual(q.source(t, r.mesh.comments(t['id']), AGENT_ID, NOW), newer)
        r._wake_one(a)
        self.assertEqual(a.wait['kind'], 'queue')
        self.assertEqual(a.wait['queue_request']['contract'], newer)
        self.assertEqual(a.turns, 0)
        # The superseded source on its own remains invalid.
        self.assertIsNone(q.source(t, [], AGENT_ID, NOW))

    def test_newest_malformed_equal_conflicting_or_unorderable_source_stays_unknown(self):
        t = task(1, human_gate=False, labels=['fleet-starvation-wake'], description=description())
        newer = dict(VALUE, observed_at=NOW - 10)
        invalid = [
            description(dict(newer, observed_at=newer['observed_at'] + 0.5)),
            description(newer).replace(str(newer['observed_at']) + ']', str(NOW + 1) + ']'),
            description(newer).replace('"version":1', '"version":true'),
            '[fleet queue-request:{broken}] [fiddler starvation-wake:' + str(NOW - 10) + ']',
            description(newer).split(' [fiddler')[0],
            '[fleet queue-request:broken] [fiddler starvation-wake:' + str(NOW - 10) + ']',
        ]
        for body in invalid:
            with self.subTest(body=body):
                self.assertIsNone(q.source(t, [{'body': body}], AGENT_ID, NOW))
        conflict = dict(newer, targets=[dict(VALUE['targets'][0], lane='Luke')])
        for values in ((newer, conflict), (newer, dict(newer, observed_at=float(newer['observed_at'])))):
            self.assertIsNone(q.source(t, [{'body': description(x)} for x in values], AGENT_ID, NOW))
        self.assertEqual(q.source(t, [{'body': description(newer)}, {'body': description(newer)}], AGENT_ID, NOW), newer)

    def test_typed_body_requires_complete_unambiguous_machine_stamps(self):
        t = task(1, human_gate=False, labels=['fleet-starvation-wake'], description=description())
        for suffix in ('bad', str(VALUE['observed_at']) + '.5', '',
                       str(VALUE['observed_at'] - 1)):
            with self.subTest(suffix=suffix):
                t['description'] = description() + ' [fiddler starvation-wake:' + suffix + ']'
                self.assertIsNone(q.source(t, [], AGENT_ID, NOW))
        # Repeated identical stamps are unambiguous, including older source bodies.
        t['description'] = description() + ' [fiddler starvation-wake:' + str(VALUE['observed_at']) + ']'
        self.assertEqual(q.source(t, [], AGENT_ID, NOW), VALUE)
        newer = dict(VALUE, observed_at=NOW - 10)
        t['description'] += ' [fiddler starvation-wake:bad]'
        self.assertIsNone(q.source(t, [{'body': description(newer)}], AGENT_ID, NOW))

    def test_future_time_gate_survives_adoption_poll_and_restart_without_turn(self):
        future = iso(dt.datetime.fromtimestamp(NOW + 3600, dt.timezone.utc))
        r, a, t = self.setup_wait(start_after=future)
        r.store.update(a, wait=dict(a.wait, until=future, scheduled=future))
        original_wait = copy.deepcopy(a.wait)
        with mock.patch.object(r.effects, 'on_end', wraps=r.effects.on_end) as on_end:
            for _ in range(3):
                r._wake_one(a)
            self.assertEqual(a.wait, original_wait)
            on_end.assert_not_called()
        self.assertEqual(r.mesh.tasks[t['id']]['start_after'], future)
        mesh = r.mesh
        r.store.db.close()
        r = self.runner(mesh)
        r.refresh_me()
        a = r.store.get(a.id)
        r._wake_one(a)
        self.assertEqual(a.wait, original_wait)
        self.assertEqual(a.turns, 0)
        with mock.patch('mesh_runner.runner.time.time', return_value=NOW + 3601):
            r._wake_one(a)
        self.assertEqual(a.wait['kind'], 'queue')
        self.assertIsNotNone(q.pending(r.store, a, a.wait['queue_request'], r.agent_id))
        self.assertIsNone(mesh.tasks[t['id']]['start_after'])
        self.assertEqual(a.turns, 0)

    def test_adoption_respects_independent_future_until_and_fresh_start_after(self):
        future = iso(dt.datetime.fromtimestamp(NOW + 3600, dt.timezone.utc))
        for field in ('until', 'start_after'):
            with self.subTest(field=field):
                r, a, t = self.setup_wait()
                if field == 'until':
                    r.store.update(a, wait=dict(a.wait, until=future))
                else:
                    r.mesh.tasks[t['id']]['start_after'] = future
                r._wake_one(a)
                self.assertEqual(a.wait['kind'], 'time')
                self.assertEqual(a.state, 'waiting')
                self.assertEqual(a.turns, 0)
                r.store.db.close()
                # Each subcase needs an independent store for its active task.
                (self.root / 'state' / 'r.sqlite').unlink()


class FirstMove(QueueWait):
    """The lead must get the first turn for a new request: no availability can precede it."""

    def parked(self):
        r, a, t = self.setup_wait()
        r._wake_one(a)                     # adoption: no turn
        self.assertEqual((a.state, a.wait['kind'], a.turns), ('waiting', 'queue', 0))
        return r, a, t

    def at(self, r, a, seconds=300):
        with mock.patch('mesh_runner.runner.time.time', return_value=NOW + seconds):
            r._wake_one(a)

    def test_new_source_wakes_owner_once_without_availability_and_stays_pending(self):
        r, a, t = self.parked()
        self.at(r, a)                      # no receipt anywhere: availability cannot be the reason
        self.assertEqual(a.state, 'ready')
        self.assertIn('queue refill', a.wake_text)
        req = a.wait['queue_request']
        self.assertEqual(json.loads(r.store.mark(a.agent, a.task_id, q.key(req))[0])['state'], 'pending')
        # not yet delivered: only a real spawn commits it, a refused gate/crash keeps it owed
        self.assertFalse(q.delivered(r.store, a, r.agent_id, req['contract']))
        self.assertEqual(a.wait['queue_delivery']['key'], q.delivered_key(r.agent_id, req['contract']))
        self.assertFalse(q.consumed(r.store, a, r.agent_id, req['contract']))
        self.assertFalse(r.mesh.checkouts)
        r.store.mark(a.agent, a.task_id, a.wait['queue_delivery']['key'], 'delivered')  # what spawn commits
        self.assertTrue(q.delivered(r.store, a, r.agent_id, req['contract']))

    def test_real_spawn_commits_delivery_and_refused_spawn_does_not(self):
        r, a, t = self.parked()
        self.at(r, a)
        req = a.wait['queue_request']
        with mock.patch.object(r, '_gate', return_value=('capacity', None)):
            r.resume_ready(1)
        self.assertEqual(r.store.get(a.id).state, 'ready')
        self.assertFalse(q.delivered(r.store, a, r.agent_id, req['contract']))
        r._resume_hold.clear()
        r.resume_ready(1)
        a = r.store.get(a.id)
        self.assertEqual(a.state, 'running')
        self.assertTrue(q.delivered(r.store, a, r.agent_id, req['contract']))
        self.assertNotIn('queue_delivery', a.wait)

    def test_delivered_source_is_not_woken_again_by_timer_or_restart(self):
        r, a, t = self.parked()
        self.at(r, a)
        r.store.mark(a.agent, a.task_id, a.wait['queue_delivery']['key'], 'delivered')
        path = self.root / 'turn.jsonl'
        path.write_text(json.dumps(dict(type='result', subtype='success', is_error=False,
                                        result='⏳ WAIT 2h: waiting for the developers', session_id='s')) + '\n')
        r.store.update(a, state='running', log_path=str(path), turns=1)
        r.settle(a)
        self.assertEqual((a.state, a.wait['kind']), ('waiting', 'queue'))   # the lead's WAIT is not acceptance
        mesh = r.mesh
        r.store.db.close()
        r = self.runner(mesh)
        r.refresh_me()
        a = r.store.get(a.id)
        for seconds in (300, 600, 3600, 86400):
            self.at(r, a, seconds)
            self.assertEqual(a.state, 'waiting')
        self.assertEqual(a.turns, 1)

    def test_new_source_after_served_one_gets_its_own_single_turn(self):
        r, a, t = self.parked()
        self.at(r, a)
        r.store.mark(a.agent, a.task_id, a.wait['queue_delivery']['key'], 'delivered')   # the spawn commit
        r.store.update(a, state='waiting')
        changed = dict(VALUE, observed_at=NOW + 1)
        r.mesh.comment_log[t['id']] = [dict(body=description(changed), created_at=iso())]
        self.at(r, a, 600)
        self.assertEqual(a.state, 'waiting')
        self.assertEqual(a.wait['queue_request']['contract'], changed)
        self.at(r, a, 900)
        self.assertEqual(a.state, 'ready')
        r.store.mark(a.agent, a.task_id, a.wait['queue_delivery']['key'], 'delivered')   # the spawn commit
        r.store.update(a, state='waiting')
        self.at(r, a, 1300)
        self.assertEqual(a.state, 'waiting')

    def test_holds_and_source_errors_keep_the_first_move_undelivered(self):
        r, a, t = self.parked()
        req = a.wait['queue_request']
        with mock.patch.object(r.mesh, 'comments', side_effect=MeshError(429, 'unavailable')):
            self.at(r, a)
        self.assertEqual(a.state, 'waiting')
        r.mesh.tasks[t['id']]['human_gate'] = True
        self.at(r, a, 700)
        self.assertEqual(a.state, 'waiting')
        r.mesh.tasks[t['id']]['human_gate'] = False
        future = iso(dt.datetime.fromtimestamp(NOW + 7200, dt.timezone.utc))
        r.mesh.tasks[t['id']]['start_after'] = future
        self.at(r, a, 1100)
        self.assertEqual(a.state, 'waiting')
        self.assertFalse(q.delivered(r.store, a, r.agent_id, req['contract']))
        r.mesh.tasks[t['id']]['start_after'] = None
        self.at(r, a, 1500)
        self.assertEqual(a.state, 'ready')

    def test_cas_loss_does_not_record_delivery(self):
        r, a, t = self.parked()
        req = a.wait['queue_request']
        with mock.patch.object(r.store, 'update_with_marks', return_value=False):
            self.at(r, a)
        self.assertFalse(q.delivered(r.store, a, r.agent_id, req['contract']))
        self.assertEqual(a.state, 'waiting')

    def test_available_work_wakes_with_delivery_recorded_so_no_second_turn(self):
        r, a, t = self.parked()
        q.publish(r.store, self.cfg.state_dir / 'queue-wait-requests.json',
                  self.cfg.state_dir / 'queue-wait-receipts.json')
        self.receipt(a)
        self.at(r, a, 0)
        self.assertEqual(a.state, 'ready')
        self.assertTrue(q.delivered(r.store, a, r.agent_id, a.wait['queue_request']['contract']))

    def settle_wait(self, turn_started):
        r, a, t = self.setup_wait()
        path = self.root / 'turn.jsonl'
        path.write_text(json.dumps(dict(type='result', subtype='success', is_error=False,
                                        result='⏳ WAIT 2h: queue empty', session_id='s')) + '\n')
        r.store.update(a, state='running', log_path=str(path), turns=1,
                       turn_started_at=iso(dt.datetime.fromtimestamp(turn_started, dt.timezone.utc)))
        r.settle(a)
        return r, a

    def test_turn_that_read_the_request_is_the_first_move(self):
        r, a = self.settle_wait(NOW - 10)          # started after the request was observed
        self.assertTrue(q.delivered(r.store, a, r.agent_id, a.wait['queue_request']['contract']))
        self.at(r, a)
        self.assertEqual(a.state, 'waiting')

    def test_turn_older_than_the_request_still_owes_the_first_move(self):
        r, a = self.settle_wait(NOW - 3600)        # the request arrived after this turn began
        self.assertFalse(q.delivered(r.store, a, r.agent_id, a.wait['queue_request']['contract']))
        self.at(r, a)
        self.assertEqual(a.state, 'ready')

    def test_source_arriving_during_turn_with_future_not_before_settles_waiting_and_holds(self):
        future = iso(dt.datetime.fromtimestamp(NOW + 7200, dt.timezone.utc))
        r, a, t = self.setup_wait(start_after=future)
        path = self.root / 'turn.jsonl'
        path.write_text(json.dumps(dict(type='result', subtype='success', is_error=False,
                                        result='⏳ WAIT 2h: queue empty', session_id='s')) + '\n')
        r.store.update(a, state='running', log_path=str(path), turns=1,
                       turn_started_at=iso(dt.datetime.fromtimestamp(NOW - 3600, dt.timezone.utc)))
        r.settle(a)
        self.assertEqual((a.state, a.wait['kind']), ('waiting', 'queue'))   # never left running
        req = a.wait['queue_request']
        self.assertFalse(q.delivered(r.store, a, r.agent_id, req['contract']))
        self.at(r, a, 600)
        self.assertEqual(a.state, 'waiting')                                # held by the future not-before
        with mock.patch('mesh_runner.runner.time.time', return_value=NOW + 7300):
            r.mesh.tasks[t['id']]['start_after'] = None
            r._wake_one(a)
        self.assertEqual(a.state, 'ready')

    def test_older_partial_read_after_new_source_served_never_rearms_or_redelivers_old(self):
        r, a, t = self.parked()
        old = a.wait['queue_request']
        changed = dict(VALUE, observed_at=NOW + 1)
        r.mesh.comment_log[t['id']] = [dict(body=description(changed), created_at=iso())]
        self.at(r, a, 600)                                   # supersession onto the new source
        self.assertEqual(a.wait['queue_request']['contract'], changed)
        self.at(r, a, 900)
        self.assertEqual(a.state, 'ready')
        r.mesh.comment_log[t['id']] = []                     # partial read: only the old description is visible
        r._resume_hold.clear()
        r.resume_ready(1)                                    # real spawn commits delivery + floor
        a = r.store.get(a.id)
        self.assertEqual(a.state, 'running')
        self.assertEqual(q.floor(r.store, a.agent, a.task_id), NOW + 1)
        path = self.root / 'turn.jsonl'
        path.write_text(json.dumps(dict(type='result', subtype='success', is_error=False,
                                        result='⏳ WAIT 2h: waiting for the developers', session_id='s')) + '\n')
        r.store.update(a, log_path=str(path))
        r.settle(a)
        self.assertNotEqual(a.wait.get('queue_request', {}).get('contract'), old['contract'])
        self.assertFalse(q.delivered(r.store, a, r.agent_id, old['contract']))
        for seconds in (1300, 1700, 3000):
            self.at(r, a, seconds)
            self.assertNotEqual(a.state, 'ready')

    def test_fulfilment_raises_floor_so_an_older_partial_read_cannot_rearm(self):
        r, a, t = self.parked()
        q.publish(r.store, self.cfg.state_dir / 'queue-wait-requests.json',
                  self.cfg.state_dir / 'queue-wait-receipts.json')
        self.receipt(a)
        self.at(r, a, 0)
        self.assertEqual(a.state, 'ready')
        req = a.wait['queue_request']
        self.assertEqual(q.floor(r.store, a.agent, a.task_id), req['contract']['observed_at'])
        older = dict(VALUE, observed_at=NOW - 1200)
        r.mesh.comment_log[t['id']] = []
        r.mesh.tasks[t['id']]['description'] = description(older)
        self.assertIsNone(r._queue_source(r.mesh.tasks[t['id']], NOW))
