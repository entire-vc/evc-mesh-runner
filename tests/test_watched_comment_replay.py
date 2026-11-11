"""Regression for repeated watched-card declarations in an installed release.

The unchanged decision converts to a watched card after source-clock dedup.
Use the real Runner, SQLite, harness subprocess and repeated declarations;
FakeMesh is the external Mesh HTTP boundary. These controls run only in CI.
"""
import datetime as dt
import json

from fakes import task, write_replies
from mesh_runner.selection import parse_time
from mesh_runner.store import Store
from mesh_runner.wake import check as wake_check
from test_decision_waste import DeclaredBench
from test_runner import calls


class WatchedCommentReplay(DeclaredBench):
    def start(self):
        self.live = task(2, cat="in_progress", assignee_id="agent-lead")
        self.line = f"⏳ WAIT decision:@lead: pass the handback from #{self.live['id'][:8]} when ready"
        a = self.declare(self.line, self.live)
        write_replies(self.replies, {self.source['id'][:8]: self.line,
                                    self.source['id'][:8]+':resume': self.line})
        self.at = parse_time(a.wait['since']) + dt.timedelta(seconds=5)
        return a

    def ticks(self, count=1):
        for _ in range(count):
            self.r._last_wake_check.clear()
            self.r.tick()
            if self.r.store.by_state(self.r.agent.name, ('running',)):
                self.finish_turn(self.r)

    def comment(self, body, author="agent-lead", at=None):
        return self.mesh.add_comment(self.live['id'], body, author=author, at=at or self.at)

    def deliveries(self):
        return list(self.r.store.db.execute(
            "SELECT key,value FROM marks WHERE agent=? AND task_id=? AND key LIKE 'card_comment_delivery:%'",
            (self.r.agent.name, self.source['id'])))

    def test_unchanged_decision_delivers_once_across_redeclarations_and_restart(self):
        a = self.start()
        self.comment('foreign progress', author='agent-reviewer')
        self.ticks()
        self.assertEqual(len(calls(self.calls)), 1)
        self.comment('not ready; same pending contract')
        self.ticks()
        self.assertEqual(len(calls(self.calls)), 2)
        self.ticks(3)
        self.r.store.db.close()
        self.r = self.runner(self.mesh)
        self.ticks(3)
        self.assertEqual(len(calls(self.calls)), 2)
        self.assertEqual(len(self.deliveries()), 1)
        waiting = self.r.store.get(a.id)
        self.assertEqual(waiting.state, 'waiting')
        self.assertEqual(waiting.session_id, a.session_id)
        self.assertEqual(self.mesh.created, [])
        self.comment('new substantive answer', at=self.at+dt.timedelta(seconds=1))
        self.ticks(4)
        self.assertEqual(len(calls(self.calls)), 3)
        self.assertEqual(self.r.store.get(a.id).state, 'waiting')

    def test_equal_timestamp_new_comment_is_not_swallowed(self):
        self.start()
        self.comment('first')['id'] = 'z'
        self.ticks(3)
        self.assertEqual(len(calls(self.calls)), 2)
        self.comment('second, same Mesh timestamp')['id'] = 'a'
        self.ticks(3)
        self.assertEqual(len(calls(self.calls)), 3)
        self.assertEqual(json.loads(self.deliveries()[0][1])['ids'], ['a', 'z'])

    def race(self, a, mutate):
        other = Store(self.root/'state'/'r.sqlite')
        self.addCleanup(other.db.close)
        real = self.mesh.comments
        def comments(tid, limit=20):
            # mock: external Mesh HTTP response interleaved with a real second SQLite writer.
            rows = real(tid, limit)
            if tid == self.live['id']:
                self.mesh.comments = real
                mutate(other)
            return rows
        self.mesh.comments = comments
        self.addCleanup(setattr, self.mesh, 'comments', real)
        self.r._wake_one(self.r.store.get(a.id))

    def test_lost_wait_cas_acknowledges_nothing(self):
        a = self.start()
        self.comment('answer')
        def redeclare(other):
            row = other.get(a.id)
            other.update(row, wait={**row.wait, 'note': 'concurrent redeclaration'})
        self.race(a, redeclare)
        self.assertEqual(self.r.store.get(a.id).state, 'waiting')
        self.assertEqual(self.deliveries(), [])
        self.assertIsNone(self.r.store.mark(a.agent, a.task_id, 'met_wake'))
        self.assertEqual(self.mesh.edges[a.task_id][0]['id'], a.wait['edge'])
        self.r._wake_one(self.r.store.get(a.id))
        self.assertEqual(self.r.store.get(a.id).state, 'ready')
        self.assertEqual(len(self.deliveries()), 1)

    def test_concurrent_delivery_is_not_overwritten_or_replayed(self):
        a = self.start()
        self.comment('answer')
        wait = dict(a.wait)
        wake_check(self.mesh, self.r.cats, None, self.source, wait, self.r.agent_id,
                   self.mesh.comments(self.source['id']), self.at)
        observation = wait['card_comment_observation']
        self.race(a, lambda other: other.mark(a.agent, a.task_id,
                                             observation['key'], observation['value']))
        self.assertEqual(self.r.store.get(a.id).state, 'waiting')
        self.assertEqual(self.deliveries()[0][1], observation['value'])
        self.assertIsNone(self.r.store.mark(a.agent, a.task_id, 'met_wake'))
        self.assertEqual(self.mesh.edges[a.task_id][0]['id'], a.wait['edge'])
        self.r._wake_one(self.r.store.get(a.id))
        self.assertEqual(self.r.store.get(a.id).state, 'waiting')

    def test_delivery_is_scoped_to_full_target_and_holder(self):
        a = self.start()
        self.comment('first answer')
        self.ticks()
        other = task(3, cat='in_progress', assignee_id='agent-lead')
        self.mesh.tasks[other['id']] = other
        self.mesh.add_comment(other['id'], 'another target', author='agent-lead', at=self.at)
        wait = {**a.wait, 'target': other['id'][:8]}
        wait.pop('target_id', None)
        event = wake_check(self.mesh, self.r.cats, None, self.source, wait, self.r.agent_id,
                           [], self.at, delivery_reader=lambda key: self.r._ci_delivered(a.task_id, key))
        self.assertIn('another target', event)
        self.assertNotEqual(wait['card_comment_observation']['key'], self.deliveries()[0][0])
        self.comment('new holder answer', author='agent-reviewer')
        wait = {**a.wait, 'watch_comments_by': {'id': 'agent-reviewer', 'name': 'reviewer'}}
        event = wake_check(self.mesh, self.r.cats, None, self.source, wait, self.r.agent_id,
                           [], self.at, delivery_reader=lambda key: self.r._ci_delivered(a.task_id, key))
        self.assertIn('new holder answer', event)
        self.assertNotEqual(wait['card_comment_observation']['key'], self.deliveries()[0][0])

    def test_ordinary_open_card_wait_does_not_wake_on_target_progress(self):
        a = self.start()
        self.comment('ordinary progress')
        wait = dict(a.wait)
        wait.pop('watch_comments_by')
        event = wake_check(self.mesh, self.r.cats, None, self.source, wait, self.r.agent_id,
                           [], self.at, delivery_reader=lambda key: self.r._ci_delivered(a.task_id, key))
        self.assertEqual(event, '')
        self.assertNotIn('card_comment_observation', wait)
