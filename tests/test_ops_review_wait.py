"""Board pending source -> real Runner settle/wake/store, with external Mesh transport fake."""
import copy
import datetime as dt
import hashlib
import json
import os
import unittest
from pathlib import Path

from fakes import FakeMesh, task
from test_runner import Bench
from mesh_runner import ops_review_wait as ow
from mesh_runner.config import ConfigError, load
from mesh_runner.outcome import parse_line

NOTIFIER = '00000063-0000-4000-8000-000000000063'


class SyntaxConfig(unittest.TestCase):
    def test_exact_request_syntax(self):
        identity = 'a' * 64
        out = parse_line('⏳ WAIT ops-review:' + identity, dt.datetime.now(dt.timezone.utc))
        self.assertEqual((out.kind, out.target, out.is_wait), ('ops-review', identity, True))
        for value in ('', 'a' * 63, 'a' * 65, '#' + identity, 'A' * 64, '/tmp/request'):
            self.assertEqual(parse_line('⏳ WAIT ops-review:' + value,
                                       dt.datetime.now(dt.timezone.utc)).kind, 'unparsed')


class RunnerPending(Bench):
    def setUp(self):
        super().setUp()
        self.registry = self.root / 'ops-review-pending.json'
        self.cfg.ops_review_wait_registry = self.registry
        self.cfg.ops_review_wait_notifier_id = NOTIFIER
        self.source = task(1, human_gate=False)
        self.mesh = FakeMesh([self.source])
        self.r = self.runner(self.mesh)
        self.r.refresh_me()
        self.a = self.r.store.create(self.agent.name, self.source['id'], 'claude')
        contract = dict(source_task_id=self.source['id'], project_id=self.source['project_id'],
                        author_lane=self.agent.name, package_sha256='b' * 64,
                        manifest_sha256='c' * 64, criteria_sha256='d' * 64)
        binding = dict(contract=contract, owner_id=self.r.agent_id,
                       source_context=ow.source_context(self.source), head_sha='e' * 40)
        self.identity = ow.digest(binding)
        self.row = dict(binding=binding, state='pending', registered_at=1_790_000_000)
        self.write()

    def write(self):
        self.registry.write_text(json.dumps(dict(version=1, requests={self.identity: self.row})))
        self.registry.chmod(0o600)

    def declare(self):
        path = self.root / 'turn.jsonl'
        path.write_text(json.dumps(dict(type='result', subtype='success', is_error=False,
                        result='⏳ WAIT ops-review:' + self.identity, session_id='s')) + '\n')
        self.r.store.update(self.a, state='running', turns=1, log_path=str(path))
        self.r.settle(self.a)

    def waiting(self):
        self.declare()
        self.assertEqual((self.a.state, self.a.wait.get('kind')), ('waiting', 'ops-review'))
        self.assertEqual(self.a.wait['ops_review_request'], self.identity)
        self.assertNotIn('until', {k: v for k, v in self.a.wait.items() if v})
        self.assertFalse(self.mesh.deps)

    def ready(self):
        # Body formatting belongs to the producer; observer consumes only its fenced digest.
        body = '[ops-review-ready:' + self.identity + ']\ncanonical producer body with pins and publication'
        comment = self.mesh.add_comment(self.source['id'], body, author=NOTIFIER)
        self.row.update(state='notified', notification_version=2, reviewer='independent', reason='available',
                        publication=dict(tick_at='2026-11-08T19:00:00Z', controller_ref='mini',
                                         revision=60, digest='f' * 64), comment_id=comment['id'],
                        notification_sha256=hashlib.sha256(body.encode()).hexdigest())
        intent = dict(request_id=self.identity, publication=self.row['publication'], reviewer=self.row['reviewer'],
                      reason=self.row['reason'], notification_version=2)
        self.row['notification_binding_sha256'] = ow.digest(intent)
        self.write()
        return comment

    def test_pending_wait_ignores_mentions_ages_and_reset_notes(self):
        self.waiting()
        self.mesh.add_comment(self.source['id'], '@builder ready? quota reset tomorrow', author=NOTIFIER)
        for _ in range(3):
            self.r._wake_one(self.a)
        self.assertEqual(self.a.state, 'waiting')
        self.assertIsNone(self.r.store.mark(self.a.agent, self.a.task_id, ow.delivered_key(self.identity)))
        self.assertNotIn('start_after', self.mesh.tasks[self.a.task_id])

    def test_receipt_between_registration_and_wait_wakes_once_across_restart(self):
        self.ready()
        self.waiting()
        stale = self.r.store.get(self.a.id)
        self.r._wake_one(self.a)
        self.assertEqual(self.a.state, 'ready')
        self.assertIn('one ordinary --deliver', self.a.wake_text)
        self.assertIn('neither admission nor SHIP', self.a.wake_text)
        self.assertEqual(self.r.store.mark(self.a.agent, self.a.task_id, ow.delivered_key(self.identity))[0],
                         self.row['comment_id'])
        self.r._wake_one(stale)                     # another stale observer cannot CAS waiting again
        r2 = self.runner(self.mesh)
        r2.refresh_me()
        loaded = r2.store.get(self.a.id)
        self.assertEqual(loaded.state, 'ready')      # pending wake text survives admission refusal/restart
        self.assertEqual(loaded.wake_text, self.a.wake_text)
        self.declare()                              # same request cannot be rearmed after consumption
        self.assertNotEqual(self.a.state, 'waiting')
        self.assertIn('already delivered', self.a.wake_text)

    def test_pending_then_notifying_then_notified_is_one_event(self):
        self.waiting()
        self.row['state'] = 'notifying'
        self.write()
        self.r._wake_one(self.a)
        self.assertEqual(self.a.state, 'waiting')
        self.ready()
        self.r._wake_one(self.a)
        self.assertEqual(self.a.state, 'ready')

    def test_marker_explanation_wrong_author_wrong_id_and_changed_body_do_not_wake(self):
        self.waiting()
        comment = self.ready()
        original = copy.deepcopy(comment)
        mutations = [dict(body='Ответ Riker: ' + comment['body']), dict(author_id=self.r.agent_id),
                     dict(id='different-id'), dict(body=comment['body'] + '\nwrong pins')]
        for changes in mutations:
            comment.clear(); comment.update(original); comment.update(changes)
            self.r._wake_one(self.a)
            self.assertEqual(self.a.state, 'waiting', changes)
        comment.clear(); comment.update(original)
        self.r._wake_one(self.a)
        self.assertEqual(self.a.state, 'ready')

    def test_wrong_pins_owner_task_context_and_hold_are_refused(self):
        for field, value in [('head_sha', '1' * 40), ('owner_id', NOTIFIER),
                             ('source_context', '0' * 64)]:
            original = copy.deepcopy(self.row)
            self.row['binding'][field] = value
            self.write()
            with self.assertRaises(ValueError):
                self.r._ops_review_registration(self.a, self.identity, self.source)
            self.row = original
        self.write()
        for changes in (dict(description='changed AC'), dict(human_gate=True),
                        dict(checked_out_by=NOTIFIER), dict(completion_signal=True),
                        dict(id=NOTIFIER), dict(assignee_id=NOTIFIER)):
            with self.assertRaises(ValueError):
                self.r._ops_review_registration(self.a, self.identity, dict(self.source, **changes))
        self.assertEqual(self.r._ops_review_registration(self.a, self.identity, self.source), self.row)

    def test_changed_task_after_wait_cannot_consume_event(self):
        self.waiting()
        self.ready()
        self.mesh.tasks[self.a.task_id]['human_gate'] = True
        self.r._wake_one(self.a)
        self.assertEqual(self.a.state, 'waiting')
        self.assertIsNone(self.r.store.mark(self.a.agent, self.a.task_id, ow.delivered_key(self.identity)))
        self.mesh.tasks[self.a.task_id]['human_gate'] = False
        self.r._wake_one(self.a)
        self.assertEqual(self.a.state, 'ready')

    def test_mutated_intent_legacy_without_hash_and_pre_registration_tick_stay_held(self):
        self.waiting()
        self.ready()
        original = copy.deepcopy(self.row)
        for changes in (dict(notification_sha256='0' * 64), dict(notification_binding_sha256='0' * 64),
                        dict(reviewer='different'), dict(reason='unknown'), dict(notification_version=3),
                        dict(publication=dict(original['publication'], tick_at='2020-01-01T00:00:00Z')),
                        dict(publication=dict(original['publication'], digest='changed'))):
            self.row = dict(original, **changes)
            self.write()
            self.r._wake_one(self.a)
            self.assertEqual(self.a.state, 'waiting', changes)
        self.row = dict(original)
        del self.row['notification_sha256']
        self.write()
        self.r._wake_one(self.a)
        self.assertEqual(self.a.state, 'waiting')
        self.row = original
        self.write()
        self.r._wake_one(self.a)
        self.assertEqual(self.a.state, 'ready')

    def test_registry_absent_disabled_wrong_permissions_symlink_and_fifo_refuse(self):
        self.registry.unlink()
        with self.assertRaises(OSError):
            self.r._ops_review_registration(self.a, self.identity, self.source)
        self.write()
        self.cfg.ops_review_wait_registry = None
        self.declare()
        self.assertNotEqual(self.a.state, 'waiting')
        self.assertIn('observer not configured', self.a.wake_text)
        self.cfg.ops_review_wait_registry = self.registry
        self.registry.chmod(0o644)
        with self.assertRaises(ValueError):
            ow.read(self.registry, self.identity)
        self.registry.unlink()
        other = self.root / 'other'
        other.write_text('{}')
        self.registry.symlink_to(other)
        with self.assertRaises(OSError):
            ow.read(self.registry, self.identity)
        self.registry.unlink()
        os.mkfifo(self.registry, 0o600)
        with self.assertRaises(ValueError):
            ow.read(self.registry, self.identity)

    def test_observer_config_is_opt_in_and_paired(self):
        path = self.root / 'runner.toml'
        path.write_text('[runner]\nmesh_url="http://mesh"\n')
        self.assertIsNone(load(path).ops_review_wait_registry)
        for extra in ('ops_review_wait_registry="/tmp/private"',
                      'ops_review_wait_notifier_id="' + NOTIFIER + '"',
                      'ops_review_wait_registry="relative"\nops_review_wait_notifier_id="' + NOTIFIER + '"',
                      'ops_review_wait_registry="/tmp/private"\nops_review_wait_notifier_id="not-uuid"'):
            path.write_text('[runner]\nmesh_url="http://mesh"\n' + extra + '\n')
            with self.assertRaises(ConfigError):
                load(path)
        path.write_text('[runner]\nmesh_url="http://mesh"\nops_review_wait_registry="/tmp/private"\n'
                        'ops_review_wait_notifier_id="' + NOTIFIER + '"\n')
        self.assertEqual(load(path).ops_review_wait_registry, Path('/tmp/private'))


if __name__ == '__main__':
    unittest.main()
