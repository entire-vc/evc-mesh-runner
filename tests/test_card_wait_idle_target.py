"""A ``WAIT card:#X`` is sent back when X itself waits on a timer, a person or a human gate."""
import datetime as dt

from fakes import FakeMesh, task, write_replies, wait_card
from mesh_runner.store import iso
from test_runner import Bench


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
        line = f"⏳ WAIT card:#{x['id'][:8]}"
        write_replies(self.replies, {t['id'][:8]: line, t['id'][:8] + ':resume': line})
        r.start_new(1) if first else r.resume_ready(1)
        return self.finish_turn(r)

    def sent_back(self, a):
        return a.state == 'ready' and 'does not hold development' in a.wake_text

    def test_time_target_is_sent_back(self):
        r, t, x = self.flow(x_wait={'kind': 'time', 'target': '', 'until': iso()})
        self.assertTrue(self.sent_back(self.turn(r, t, x)))

    def test_decision_target_is_sent_back(self):
        r, t, x = self.flow(x_wait={'kind': 'decision', 'target': '', 'handle': 'lead'})
        self.assertTrue(self.sent_back(self.turn(r, t, x)))

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
        r, t, x = self.flow(x_wait={'kind': 'decision', 'target': '', 'handle': 'lead'}, x_extra={'labels': ['umbrella']})
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
        r.store.update(ax, state='waiting', wait={'kind': 'decision', 'target': '', 'handle': 'lead',
                                                 'since': iso(), 'started': iso()})
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

    # 53db6352: what the waiter waits for, and what the target's timer is, decide the send-back
    def turn_line(self, r, t, x, line):
        write_replies(self.replies, {t['id'][:8]: line, t['id'][:8] + ':resume': line})
        r.start_new(1)
        return self.finish_turn(r)

    EXTERNAL = {'event': 'pbs-gc-0400', 'source': 'comment:7c24ab97-0000-4000-8000-000000000000',
                'why': 'GC frees the datastore', 'at': iso()}

    def condition_on_time_target(self, cond):
        r, t, x = self.flow(x_wait={'kind': 'time', 'target': '', 'until': iso()})
        a = self.turn_line(r, t, x, f"⏳ WAIT card:#{x['id'][:8]} until={cond}")
        self.assertEqual(a.state, 'waiting', a.wake_text)

    def test_accepted_condition_on_time_target_is_accepted(self):
        self.condition_on_time_target('accepted')

    def test_deployed_condition_on_time_target_is_accepted(self):
        self.condition_on_time_target('deployed')

    def test_shipped_condition_on_time_target_is_accepted(self):
        self.condition_on_time_target('shipped')

    def test_acceptance_condition_on_decision_target_is_accepted(self):
        r, t, x = self.flow(x_wait={'kind': 'decision', 'target': '', 'handle': 'lead'})
        a = self.turn_line(r, t, x, f"⏳ WAIT card:#{x['id'][:8]} until=accepted")
        self.assertEqual(a.state, 'waiting', a.wake_text)

    def test_target_waiting_for_published_external_event_is_accepted(self):
        r, t, x = self.flow(x_wait={'kind': 'time', 'target': '', 'until': iso(), 'external_time': self.EXTERNAL})
        self.assertEqual(self.turn(r, t, x).state, 'waiting')

    def test_target_time_wait_keeps_its_event_and_source(self):
        # the target's own `on=time` declaration is stored with event_ref and source
        x = task(2)
        r = self.runner(FakeMesh([x], max_concurrent=1))
        self.addCleanup(r.store.db.close)
        r.refresh_me()
        when = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).strftime('%Y-%m-%dT04:20:00Z')
        line = f"⏳ WAIT {when}: external=pbs-gc-0400 source=https://example.org/gc GC frees the datastore"
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

