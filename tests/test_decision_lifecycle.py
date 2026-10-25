"""A retained product question outlives observational wakes and attempts."""
import datetime as dt
from unittest.mock import patch
from test_runner import Bench
from fakes import FakeMesh, task
from mesh_runner.mesh import MeshError
from mesh_runner.store import iso, now


class DecisionLifecycle(Bench):
    def setup_question(self):
        source = task(1)
        mesh = FakeMesh([source])
        runner = self.runner(mesh)
        runner.refresh_me()
        attempt = runner.store.create(self.agent.name, source['id'], 'claude')
        wait = self.question()
        runner.effects.on_wait(attempt, source, wait, runner.me)
        runner.store.update(attempt, state='waiting', wait=wait)
        return runner, mesh, source, runner.store.get(attempt.id), wait

    def question(self, note='approve the plan?', handle='lead'):
        return {'kind': 'decision', 'handle': handle, 'note': note,
                'line': f'⏳ WAIT decision:@{handle}: {note}',
                'since': iso(now()-dt.timedelta(minutes=1)), 'started': iso()}

    def test_nested_current_owner_creates_and_reuses_exactly_one_request(self):
        self._assert_current_owner_shape(nested=True, ours=True)

    def test_foreign_flat_current_owner_cannot_create_a_request(self):
        self._assert_current_owner_shape(nested=False, ours=False)

    def test_foreign_nested_current_owner_cannot_create_a_request(self):
        self._assert_current_owner_shape(nested=True, ours=False)

    def _assert_current_owner_shape(self, *, nested, ours):
        source = task(1)
        cached = dict(source)  # stale attempt input still claims this lane owns it
        mesh = FakeMesh([source])
        r = self.runner(mesh)
        r.refresh_me()
        owner = r.me['id'] if ours else 'another-owner'
        if nested:
            source.pop('assignee_id')
            source['assignee'] = {'id': owner}
        else:
            source['assignee_id'] = owner
            source['assignee'] = {'id': r.me['id']}  # flat owner takes precedence
        attempt = r.store.create(self.agent.name, source['id'], 'claude')
        first, again = self.question(), self.question()
        r.effects.on_wait(attempt, cached, first, r.me)
        r.effects.on_wait(attempt, cached, again, r.me)
        self.assertEqual(len(mesh.created), 1 if ours else 0)
        if ours:
            self.assertEqual(first['request']['id'], again['request']['id'])
        else:
            self.assertNotIn('request', first)
            self.assertNotIn('request', again)
            self.assertFalse(r.effects._decision(source['id']))

    def test_observation_wakes_and_unschedules_without_closing_question(self):
        r, mesh, source, attempt, wait = self.setup_question()
        request = wait['request']['id']
        mesh.add_comment(source['id'], 'status observation', author='bystander')
        r._wake_one(attempt)
        current = r.store.get(attempt.id)
        self.assertEqual(current.state, 'ready')
        self.assertFalse(mesh.tasks[source['id']].get('start_after'))
        self.assertEqual(mesh.category(mesh.tasks[request]), 'todo')
        r.effects.on_end(current, dict(current.wait), 'resuming', close_request=False)
        self.assertEqual(mesh.category(mesh.tasks[request]), 'todo')
        self.assertEqual(r.effects._decision(source['id'])['state'], 'open')

    def test_met_unrelated_wait_does_not_answer_a_retained_question(self):
        r, mesh, source, attempt, wait = self.setup_question()
        r.effects.on_end(attempt, {'kind': 'time'}, 'time elapsed', close_request=True)
        self.assertEqual(mesh.category(mesh.tasks[wait['request']['id']]), 'todo')
        self.assertEqual(r.effects._decision(source['id'])['state'], 'open')

    def test_age_alone_preserves_question_without_waking(self):
        r, mesh, source, attempt, wait = self.setup_question()
        wait['started'] = iso(now()-dt.timedelta(days=2))
        r.store.update(attempt, wait=wait)
        r._wake_one(r.store.get(attempt.id))
        self.assertEqual(r.store.get(attempt.id).state, 'waiting')
        self.assertEqual(mesh.category(mesh.tasks[wait['request']['id']]), 'todo')
        self.assertEqual(r.effects._decision(source['id'])['state'], 'open')
        self.assertEqual(r.procs, {})
        # An actual answer still wakes and resolves the retained old question.
        mesh.add_comment(source['id'], 'approved', author='agent-lead')
        r._wake_one(r.store.get(attempt.id))
        self.assertEqual(r.store.get(attempt.id).state, 'ready')
        self.assertEqual(mesh.category(mesh.tasks[wait['request']['id']]), 'done')

    def test_recipient_id_answer_closes_and_denial_is_an_answer_too(self):
        for body in ('approved', 'denied'):
            with self.subTest(body=body):
                # A distinct source lets both decisions share the same real store.
                r, mesh, source, attempt, wait = self.setup_question()
                mesh.add_comment(source['id'], body, author='agent-lead')
                r._wake_one(attempt)
                self.assertEqual(mesh.category(mesh.tasks[wait['request']['id']]), 'done')
                self.assertFalse(r.effects._decision(source['id']))
                r.store.update(r.store.get(attempt.id), state='completed')
                # setup_question creates another attempt on the same source next iteration.

    def test_explicit_answer_marker_preserves_existing_addressed_response_contract(self):
        r, mesh, source, attempt, wait = self.setup_question()
        mesh.add_comment(source['id'], 'Decision: use the second plan', author='bystander')
        r._wake_one(attempt)
        self.assertEqual(mesh.category(mesh.tasks[wait['request']['id']]), 'done')

    def test_unmarked_comment_from_recipient_remains_an_answer_without_language_guessing(self):
        r, mesh, source, attempt, wait = self.setup_question()
        mesh.add_comment(source['id'], 'status observation', author='agent-lead')
        r._wake_one(attempt)
        self.assertEqual(mesh.category(mesh.tasks[wait['request']['id']]), 'done')

    def test_restart_and_new_attempt_reuse_source_recipient_normalized_question(self):
        r, mesh, source, old, wait = self.setup_question()
        request = wait['request']['id']
        r._finish(old, 'aborted', 'transient infrastructure problem')
        self.assertEqual(mesh.category(mesh.tasks[request]), 'todo')
        restarted = self.runner(mesh)
        restarted.refresh_me()
        new = restarted.store.create(self.agent.name, source['id'], 'claude')
        again = self.question('approve  the\nplan?')
        restarted.effects.on_wait(new, source, again, restarted.me)
        self.assertEqual(again['request']['id'], request)
        self.assertEqual(len(mesh.created), 1)

    def test_changed_question_and_recipient_supersede_explicitly(self):
        # with request reuse off (decision_reuse_hours = 0); reuse: test_decision_request_reuse
        self.cfg.decision_reuse_hours = 0
        r, mesh, source, attempt, wait = self.setup_question()
        old = wait['request']['id']
        changed = self.question('approve another plan?')
        r.effects.on_wait(attempt, source, changed, r.me)
        self.assertEqual(mesh.category(mesh.tasks[old]), 'done')
        self.assertEqual(len(mesh.created), 2)
        reassigned = self.question('approve another plan?', 'reviewer')
        r.effects.on_wait(attempt, source, reassigned, r.me)
        self.assertEqual(mesh.category(mesh.tasks[changed['request']['id']]), 'done')
        self.assertEqual(len(mesh.created), 3)

    def test_new_attempt_without_wait_closes_retained_request_only_when_source_terminal(self):
        r, mesh, source, old, wait = self.setup_question()
        r._finish(old, 'aborted', 'transient')
        new = r.store.create(self.agent.name, source['id'], 'claude')
        mesh.set_category(source['id'], 'done')
        r._finish(new, 'completed')
        self.assertEqual(mesh.category(mesh.tasks[wait['request']['id']]), 'done')

    def test_source_reassignment_does_not_close_or_supersede_question(self):
        r, mesh, source, attempt, wait = self.setup_question()
        mesh.tasks[source['id']]['assignee_id'] = 'another-owner'
        r.effects.on_wait(attempt, source, self.question('a changed question'), r.me)
        r._finish(attempt, 'completed', 'source reassigned')
        self.assertEqual(len(mesh.created), 1)
        self.assertEqual(mesh.category(mesh.tasks[wait['request']['id']]), 'todo')

    def test_close_failure_is_durable_and_retried_without_an_active_wait(self):
        r, mesh, source, attempt, wait = self.setup_question()
        with patch.object(mesh, 'move', side_effect=MeshError(503, 'unavailable')):
            r.effects.on_end(attempt, wait, 'answered', close_request=True)
        self.assertEqual(r.effects._decision(source['id'])['state'], 'closing')
        restarted = self.runner(mesh)
        restarted.effects.reconcile_decisions()
        self.assertEqual(mesh.category(mesh.tasks[wait['request']['id']]), 'done')
        self.assertFalse(restarted.effects._decision(source['id']))

    def test_terminal_source_cleanup_is_bounded_and_fails_closed_on_unknown_source(self):
        r, mesh, source, attempt, wait = self.setup_question()
        mesh.broken_tasks.add(source['id'])
        r.effects.close_terminal_decision(attempt)
        self.assertEqual(mesh.category(mesh.tasks[wait['request']['id']]), 'todo')
        mesh.broken_tasks.clear()
        mesh.set_category(source['id'], 'done')
        r.effects.reconcile_decisions()
        self.assertEqual(mesh.category(mesh.tasks[wait['request']['id']]), 'done')

    def test_reconciliation_limits_source_checks_per_pass_and_heartbeat(self):
        r, mesh, source, attempt, wait = self.setup_question()
        record = r.effects._decision(source['id'])
        for index in range(2, 14):
            other = task(index)
            mesh.tasks[other['id']] = other
            r.effects._save_decision(other['id'], dict(record))
        with patch.object(mesh, 'task', wraps=mesh.task) as lookup:
            r.effects.reconcile_decisions()
            self.assertEqual(lookup.call_count, 10)
            r.effects.reconcile_decisions()
            self.assertEqual(lookup.call_count, 13)
            r.effects.reconcile_decisions()
            self.assertEqual(lookup.call_count, 13)

    def test_ambiguous_create_and_crash_marker_never_repeat_post(self):
        source = task(1); mesh = FakeMesh([source]); r = self.runner(mesh); r.refresh_me()
        attempt = r.store.create(self.agent.name, source['id'], 'claude')
        create = mesh.create_task
        def committed_but_response_lost(*args):
            create(*args)
            raise MeshError(0, 'response lost')
        with patch.object(mesh, 'create_task', side_effect=committed_but_response_lost) as post:
            r.effects.on_wait(attempt, source, self.question(), r.me)
            r.effects.on_wait(attempt, source, self.question(), r.me)
            self.assertEqual(post.call_count, 1)
        restarted = self.runner(mesh); restarted.refresh_me()
        restarted.effects.on_wait(attempt, source, self.question(), restarted.me)
        self.assertEqual(len(mesh.created), 1)
        key = restarted.effects._decision(source['id'])['key']
        self.assertIn(f'[runner decision-key:{key}]', mesh.created[0]['description'])
        self.assertEqual(restarted.effects._decision(source['id'])['state'], 'creating')

    def test_definitive_validation_rejection_can_retry(self):
        source = task(1); mesh = FakeMesh([source]); r = self.runner(mesh); r.refresh_me()
        attempt = r.store.create(self.agent.name, source['id'], 'claude')
        wait = self.question()
        with patch.object(mesh, 'create_task', side_effect=MeshError(422, 'invalid')):
            r.effects.on_wait(attempt, source, wait, r.me)
        self.assertFalse(wait['fx'])
        r.effects.on_wait(attempt, source, wait, r.me)
        self.assertEqual(len(mesh.created), 1)

    def test_legacy_wait_receipt_is_retained_before_observational_cleanup(self):
        r, mesh, source, attempt, wait = self.setup_question()
        from mesh_runner.effects import DECISION_OWNER, DECISION_MARK
        r.store.mark(DECISION_OWNER, source['id'], DECISION_MARK, '')
        wait.pop('recipient_id', None)
        r.effects.on_end(attempt, wait, 'observation', close_request=False)
        self.assertTrue(r.effects._decision(source['id']))
        again = self.question()
        r.effects.on_wait(attempt, source, again, r.me)
        self.assertEqual(again['request'], wait['request'])
        self.assertEqual(len(mesh.created), 1)

    def test_repeated_move_failure_posts_one_close_comment_across_restarts(self):
        r, mesh, source, attempt, wait = self.setup_question()
        request = wait['request']['id']
        with patch.object(mesh, 'move', side_effect=MeshError(500, 'unavailable')):
            r.effects.on_end(attempt, wait, 'answered', close_request=True)
            for _ in range(3):
                self.runner(mesh).effects.reconcile_decisions()
        self.assertEqual(len(mesh.comments(request)), 1)
        self.assertEqual(r.effects._decision(source['id'])['state'], 'closing')
        self.runner(mesh).effects.reconcile_decisions()
        self.assertEqual(mesh.category(mesh.tasks[request]), 'done')
        self.assertEqual(len(mesh.comments(request)), 1)

    def test_ambiguous_close_comment_post_is_not_repeated(self):
        r, mesh, source, attempt, wait = self.setup_question()
        request = wait['request']['id']
        add = mesh.add_comment
        def committed_but_response_lost(*args):
            add(*args)
            raise MeshError(0, 'response lost')
        with patch.object(mesh, 'add_comment', side_effect=committed_but_response_lost) as post:
            r.effects.on_end(attempt, wait, 'answered', close_request=True)
            self.runner(mesh).effects.reconcile_decisions()
            self.assertEqual(post.call_count, 1)
        self.assertEqual(mesh.category(mesh.tasks[request]), 'done')
        self.assertEqual(len(mesh.comments(request)), 1)

    def test_status_failure_posts_nothing_until_close_can_proceed(self):
        r, mesh, source, attempt, wait = self.setup_question()
        request = wait['request']['id']
        with patch.object(mesh, 'statuses', side_effect=MeshError(500, 'unavailable')):
            r.effects.on_end(attempt, wait, 'answered', close_request=True)
            self.runner(mesh).effects.reconcile_decisions()
        self.assertEqual(mesh.comments(request), [])
        self.runner(mesh).effects.reconcile_decisions()
        self.assertEqual(mesh.category(mesh.tasks[request]), 'done')
        self.assertEqual(len(mesh.comments(request)), 1)

    def test_legacy_close_without_durable_identity_never_posts_comments(self):
        r, mesh, source, attempt, wait = self.setup_question()
        request = wait['request']['id']
        # Legacy identity adoption is unavailable: status closure remains useful,
        # but an optional comment has no durable idempotency receipt.
        with patch.object(mesh, 'move', side_effect=MeshError(500, 'unavailable')):
            for _ in range(3):
                r.effects._close_decision(wait, 'answered')
        self.assertEqual(mesh.comments(request), [])
        r.effects._close_decision(wait, 'answered')
        self.assertEqual(mesh.category(mesh.tasks[request]), 'done')
        self.assertNotIn('request', wait)
        self.assertEqual(mesh.comments(request), [])
