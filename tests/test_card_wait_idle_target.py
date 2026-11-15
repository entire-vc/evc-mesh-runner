"""A ``wait_for on=card`` on X is sent back when X itself waits on a timer, a person or a human gate.
A question X put to an agent (``ask_agent``), the waiter itself included, is not a person's."""
import datetime as dt

from fakes import FakeMesh, task, write_replies, wait_card, wait_time
from mesh_runner.store import iso
from test_runner import Bench

# a decision wait addressed to a person: the handle is no workspace agent (FakeMesh has builder and lead)
PERSON_ASK = {'kind': 'decision', 'target': '', 'handle': 'owner'}


class CardWaitOnIdleTarget(Bench):
    def flow(self, x_state='waiting', x_wait=None, x_extra=None, x_gate=False, t_extra=None):
        t, x = task(1, **(t_extra or {})), task(2, **(x_extra or {}))
        if x_gate:
            x['human_gate'] = True
        r = self.runner(FakeMesh([t, x], max_concurrent=1))
        self.addCleanup(r.store.db.close)
        r.refresh_me()
        ax = r.store.create(self.agent.name, x['id'], 'claude')
        r.store.update(ax, state=x_state, wait=x_wait and {**x_wait, 'since': iso(), 'started': iso()})
        return r, t, x

    def turn(self, r, t, x, first=True):
        line = wait_card(x['id'])
        write_replies(self.replies, {t['id'][:8]: line, t['id'][:8] + ':resume': line})
        r.start_new(1) if first else r.resume_ready(1)
        return self.finish_turn(r)

    def sent_back(self, a):
        return a.state == 'ready' and 'does not hold development' in a.wake_text

    def test_time_target_is_sent_back(self):
        r, t, x = self.flow(x_wait={'kind': 'time', 'target': '', 'until': iso()})
        self.assertTrue(self.sent_back(self.turn(r, t, x)))

    def test_decision_target_is_sent_back(self):
        r, t, x = self.flow(x_wait=PERSON_ASK)
        a = self.turn(r, t, x)
        self.assertTrue(self.sent_back(a))
        self.assertIn('a person (decision → @owner)', a.wake_text)

    def test_marked_person_ask_target_is_sent_back(self):
        r, t, x = self.flow(x_wait={**PERSON_ASK, 'handle': 'lead', 'person_ask': True})
        self.assertTrue(self.sent_back(self.turn(r, t, x)))

    # a question to an agent is answered by an agent, not a person
    def test_ask_agent_target_is_accepted(self):
        r, t, x = self.flow(x_wait={'kind': 'decision', 'target': '', 'handle': 'lead',
                                    'decision_request_id': 'dr-6b0e2240'})
        a = self.turn(r, t, x)
        self.assertEqual(a.state, 'waiting', a.wake_text)

    def test_agent_handle_decision_target_is_accepted(self):
        # a marker question to an agent (no request id yet): the handle resolves to a workspace agent
        r, t, x = self.flow(x_wait={'kind': 'decision', 'target': '', 'handle': 'lead'})
        a = self.turn(r, t, x)
        self.assertEqual(a.state, 'waiting', a.wake_text)

    def test_filed_agent_recipient_target_is_accepted(self):
        r, t, x = self.flow(x_wait={'kind': 'decision', 'target': '', 'handle': 'Someone-Renamed',
                                    'recipient_id': 'agent-lead'})
        a = self.turn(r, t, x)
        self.assertEqual(a.state, 'waiting', a.wake_text)

    def test_target_waiting_for_the_waiters_own_answer_is_accepted(self):
        # the live case: X asked this very agent; its answer is on the way, X is not a person's
        r, t, x = self.flow(x_wait={'kind': 'decision', 'target': '', 'handle': self.agent.name,
                                    'decision_request_id': 'dr-6b0e2240'})
        a = self.turn(r, t, x)
        self.assertEqual(a.state, 'waiting', a.wake_text)

    def test_target_waiting_for_the_waiters_own_marker_answer_is_accepted(self):
        r, t, x = self.flow(x_wait={'kind': 'decision', 'target': '', 'handle': '@' + self.agent.name.upper()})
        a = self.turn(r, t, x)
        self.assertEqual(a.state, 'waiting', a.wake_text)

    def test_ask_human_target_is_sent_back(self):
        # negative control: `ask_human` (Mesh armed the gate) is still a person's
        r, t, x = self.flow(x_wait={'kind': 'blocking', 'target': '', 'handle': 'owner',
                                    'human_gate_ref': 'gate-1'}, x_gate=True)
        a = self.turn(r, t, x)
        self.assertTrue(self.sent_back(a))
        self.assertIn('a person (ask_human → @owner)', a.wake_text)

    def test_ask_agent_target_under_a_human_gate_is_sent_back(self):
        # negative control: a human gate on X holds it whoever X asked
        r, t, x = self.flow(x_wait={'kind': 'decision', 'target': '', 'handle': 'lead',
                                    'decision_request_id': 'dr-1'}, x_gate=True)
        a = self.turn(r, t, x)
        self.assertTrue(self.sent_back(a))
        self.assertIn('human gate; decision → @lead', a.wake_text)

    def test_roster_failure_keeps_the_wait(self):
        # an unknown addressee is never taken for a person
        r, t, x = self.flow(x_wait=PERSON_ASK)
        def down(ws):
            from mesh_runner.mesh import MeshError
            raise MeshError(503, 'roster down')
        r.mesh.workspace_agents = down
        r.effects._agents_at = None
        r.effects._agents = {}
        self.assertEqual(self.turn(r, t, x).state, 'waiting')

    def test_send_back_log_names_kind_and_addressee(self):
        r, t, x = self.flow(x_wait={'kind': 'blocking', 'target': '', 'handle': 'owner',
                                    'human_gate_ref': 'gate-1'}, x_gate=True)
        with self.assertLogs('mesh_runner', level='INFO') as logs:
            self.assertTrue(self.sent_back(self.turn(r, t, x)))
        line = next(m for m in logs.output if 'sent back' in m and f"#{x['id'][:8]}" in m)
        self.assertIn('waits for a person (ask_human → @owner)', line)
        self.assertNotIn('a timer or a person', line)

    def test_send_back_log_keeps_the_human_gate_addressee(self):
        # the human-gate text holds a ';' of its own; the log line kept only
        # 'waits for a person (human gate' and lost the addressee
        r, t, x = self.flow(x_wait={'kind': 'decision', 'target': '', 'handle': 'lead',
                                    'decision_request_id': 'dr-1'}, x_gate=True)
        with self.assertLogs('mesh_runner', level='INFO') as logs:
            self.assertTrue(self.sent_back(self.turn(r, t, x)))
        line = next(m for m in logs.output if 'sent back' in m and f"#{x['id'][:8]}" in m)
        self.assertIn('waits for a person (human gate; decision → @lead)', line)
        self.assertNotIn('a release gate', line)

    def test_time_target_text_names_the_timer(self):
        r, t, x = self.flow(x_wait={'kind': 'time', 'target': '', 'until': '2026-11-20T04:00:00+00:00'})
        a = self.turn(r, t, x)
        self.assertTrue(self.sent_back(a))
        self.assertIn('time until 2026-11-20T04:00:00+00:00', a.wake_text)

    def test_human_gate_ready_target_is_sent_back(self):
        r, t, x = self.flow(x_state='ready', x_gate=True)
        self.assertTrue(self.sent_back(self.turn(r, t, x)))

    def release_waiter(self, label):
        r, t, x = self.flow(x_wait={'kind': 'decision', 'target': '', 'handle': 'lead'}, x_gate=True,
                            t_extra={'labels': [label]})
        return self.turn(r, t, x)

    def test_release_batch_waiter_on_idle_target_is_accepted(self):
        self.assertEqual(self.release_waiter('release-batch').state, 'waiting')

    def test_kind_release_waiter_on_idle_target_is_accepted(self):
        self.assertEqual(self.release_waiter('kind:release').state, 'waiting')

    def test_release_batch_waiter_on_gated_ready_target_is_accepted(self):
        r, t, x = self.flow(x_state='ready', x_gate=True, t_extra={'labels': ['release-batch']})
        self.assertEqual(self.turn(r, t, x).state, 'waiting')

    def test_pipeline_target_is_accepted(self):
        r, t, x = self.flow(x_wait={'kind': 'pipeline', 'target': 'g/app#1'})
        self.assertEqual(self.turn(r, t, x).state, 'waiting')

    def test_mr_target_is_accepted(self):
        r, t, x = self.flow(x_wait={'kind': 'mr', 'target': 'g/app!1'})
        self.assertEqual(self.turn(r, t, x).state, 'waiting')

    def test_ready_target_without_gate_is_accepted(self):
        r, t, x = self.flow(x_state='ready')
        self.assertEqual(self.turn(r, t, x).state, 'waiting')

    def test_epic_target_is_accepted(self):
        r, t, x = self.flow(x_wait={'kind': 'time', 'target': '', 'until': iso()}, x_extra={'labels': ['epic']})
        self.assertEqual(self.turn(r, t, x).state, 'waiting')

    def test_umbrella_target_is_accepted(self):
        r, t, x = self.flow(x_wait=PERSON_ASK, x_extra={'labels': ['umbrella']})
        self.assertEqual(self.turn(r, t, x).state, 'waiting')

    def test_running_target_is_accepted(self):
        r, t, x = self.flow(x_state='waiting', x_wait={'kind': 'time', 'target': '', 'until': iso()})
        ax = r.store.waiting_on_task(x['id'][:8])[0]
        r.store.db.execute("UPDATE attempts SET agent='other', state='running' WHERE id=?", (ax.id,))   # another lane runs X
        self.assertEqual(self.turn(r, t, x).state, 'waiting')

    def test_target_without_any_attempt_is_accepted(self):
        t = task(1)
        r = self.runner(FakeMesh([t], max_concurrent=1))
        self.addCleanup(r.store.db.close)
        r.refresh_me()
        write_replies(self.replies, {t['id'][:8]: wait_card("deadbeef")})
        r.start_new(1)
        self.assertEqual(self.finish_turn(r).state, 'waiting')

    def test_unknown_target_is_accepted(self):
        t, x = task(1), task(2)
        r = self.runner(FakeMesh([t, x], max_concurrent=1))
        self.addCleanup(r.store.db.close)
        r.refresh_me()
        self.assertEqual(self.turn(r, t, x).state, 'waiting')

    def test_review_card_waiting_on_idle_target_is_accepted(self):
        t, x = task(1, labels=['kind:review']), task(2)
        r = self.runner(FakeMesh([t, x], max_concurrent=1))
        self.addCleanup(r.store.db.close)
        r.refresh_me()
        ax = r.store.create(self.agent.name, x['id'], 'claude')
        r.store.update(ax, state='waiting', wait={**PERSON_ASK, 'since': iso(), 'started': iso()})
        self.assertEqual(self.turn(r, t, x).state, 'waiting')

    def test_repeat_counts_in_the_wait_budget(self):
        self.cfg.daily_wait_budget = 1
        r, t, x = self.flow(x_wait={'kind': 'time', 'target': '', 'until': iso()})
        a = self.turn(r, t, x)
        for _ in range(3):
            if a.state != 'ready':
                break
            a = self.turn(r, t, x, first=False)
        self.assertNotEqual(a.state, 'ready')
        self.assertGreaterEqual(r.store.idle_waits_today(self.agent.name, t['id']), 1)

    def test_same_wait_redeclared_on_unchanged_target_is_accepted(self):
        # told once, the dependent declares the same wait again while X has not moved:
        # waking it again brings no new event, the wait stands
        r, t, x = self.flow(x_wait={'kind': 'time', 'target': '', 'until': iso(), 'mesh_wait_id': 'w1'})
        self.assertTrue(self.sent_back(self.turn(r, t, x)))
        a = self.turn(r, t, x, first=False)
        self.assertEqual(a.state, 'waiting')

    def test_changed_target_state_is_told_once_more(self):
        # a new (dependent, target state) pair is told again: real ungated work is not stuck
        r, t, x = self.flow(x_wait={'kind': 'time', 'target': '', 'until': iso(), 'mesh_wait_id': 'w1'})
        self.assertTrue(self.sent_back(self.turn(r, t, x)))
        ax = r.store.waiting_on_task(x['id'][:8])[0]
        r.store.update(ax, wait={'kind': 'decision', 'target': '', 'handle': 'lead', 'person_ask': True,
                                 'mesh_wait_id': 'w2', 'since': iso(), 'started': iso()})
        self.assertTrue(self.sent_back(self.turn(r, t, x, first=False)))
        self.assertEqual(self.turn(r, t, x, first=False).state, 'waiting')

    def test_new_human_gate_on_same_target_is_told_again(self):
        # a gate cleared and raised again on the same attempt is a new target state
        r, t, x = self.flow(x_state='ready', x_gate=True)
        x['human_gate_info'] = {'gated': True, 'marker_comment_id': 'c1'}
        self.assertTrue(self.sent_back(self.turn(r, t, x)))
        x['human_gate_info'] = {'gated': True, 'marker_comment_id': 'c2'}
        self.assertTrue(self.sent_back(self.turn(r, t, x, first=False)))
        self.assertEqual(self.turn(r, t, x, first=False).state, 'waiting')

    def test_gated_agent_question_target_is_told_once_per_gate(self):
        # X asked an agent and a human gate holds it: told once, again only for a new gate
        r, t, x = self.flow(x_wait={'kind': 'decision', 'target': '', 'handle': 'lead',
                                    'decision_request_id': 'dr-1'}, x_gate=True)
        x['human_gate_info'] = {'gated': True, 'marker_comment_id': 'c1'}
        self.assertTrue(self.sent_back(self.turn(r, t, x)))
        x['human_gate_info'] = {'gated': True, 'marker_comment_id': 'c2'}
        self.assertTrue(self.sent_back(self.turn(r, t, x, first=False)))
        self.assertEqual(self.turn(r, t, x, first=False).state, 'waiting')

    # what the waiter waits for, and what the target's timer is, decide the send-back
    def turn_line(self, r, t, x, line):
        write_replies(self.replies, {t['id'][:8]: line, t['id'][:8] + ':resume': line})
        r.start_new(1)
        return self.finish_turn(r)

    EXTERNAL = {'event': 'pbs-gc-0400', 'source': 'comment:7c24ab97-0000-4000-8000-000000000000',
                'why': 'GC frees the datastore', 'at': iso()}

    def condition_on_time_target(self, cond):
        r, t, x = self.flow(x_wait={'kind': 'time', 'target': '', 'until': iso()})
        a = self.turn_line(r, t, x, wait_card(x['id'], condition=cond))
        self.assertEqual(a.state, 'waiting', a.wake_text)

    def test_accepted_condition_on_time_target_is_accepted(self):
        self.condition_on_time_target('accepted')

    def test_deployed_condition_on_time_target_is_accepted(self):
        self.condition_on_time_target('deployed')

    def test_shipped_condition_on_time_target_is_accepted(self):
        self.condition_on_time_target('shipped')

    def test_acceptance_condition_on_decision_target_is_accepted(self):
        r, t, x = self.flow(x_wait=PERSON_ASK)
        a = self.turn_line(r, t, x, wait_card(x['id'], condition='accepted'))
        self.assertEqual(a.state, 'waiting', a.wake_text)

    def test_target_waiting_for_published_external_event_is_accepted(self):
        r, t, x = self.flow(x_wait={'kind': 'time', 'target': '', 'until': iso(), 'external_time': self.EXTERNAL})
        self.assertEqual(self.turn(r, t, x).state, 'waiting')

    def test_target_time_wait_keeps_its_event_and_source(self):
        # the target's own `wait_for on=time` is stored with event_ref and source
        x = task(2)
        r = self.runner(FakeMesh([x], max_concurrent=1))
        self.addCleanup(r.store.db.close)
        r.refresh_me()
        when = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).strftime('%Y-%m-%dT04:20:00Z')
        line = wait_time(until_at=when, event_ref='pbs-gc-0400', source='https://example.org/gc',
                         why='GC frees the datastore')
        write_replies(self.replies, {x['id'][:8]: line})
        r.start_new(1)
        a = self.finish_turn(r)
        self.assertEqual(a.state, 'waiting', a.wake_text)
        ext = a.wait.get('external_time') or {}
        self.assertEqual((ext.get('event'), ext.get('source')), ('pbs-gc-0400', 'https://example.org/gc'))

    def test_measurement_window_target_still_sent_back(self):
        # control: a timer with no published event is still a release gate for a task_done wait
        r, t, x = self.flow(x_wait={'kind': 'time', 'target': '', 'until': iso(),
                                    'external_time': {'event': '', 'source': '', 'why': 'measure'}})
        self.assertTrue(self.sent_back(self.turn(r, t, x)))

