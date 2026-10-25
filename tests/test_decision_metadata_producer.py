"""Real Runner decision producer: opt-in is the resolved recipient identity."""
from dataclasses import replace
from unittest.mock import patch
from test_runner import Bench
from fakes import AGENT_ID, FakeMesh, task, write_replies
from mesh_runner.config import ConfigError, RunnerConfig, load
from mesh_runner.effects import DECISION_LABELS
from mesh_runner.mesh import MeshError
from mesh_runner.workspace_scope import metadata_only

RECIPIENT = '11111111-1111-4111-8111-111111111111'
OTHER = '22222222-2222-4222-8222-222222222222'


class DecisionMetadataProducer(Bench):
    def prepare(self, recipients=(), *, enabled=True):
        self.cfg.decision_request_metadata_only_recipients = list(recipients)
        self.cfg.mesh_effects = enabled
        source = task(1, labels=['alert', 'runner:metadata-only'])
        mesh = FakeMesh([source])
        mesh.agents = [{'id': AGENT_ID, 'name': 'builder'},
                       {'id': RECIPIENT, 'name': 'coordinator'}, {'id': OTHER, 'name': 'other'}]
        r = self.runner(mesh)
        r.refresh_me()
        attempt = r.store.create(self.agent.name, source['id'], 'claude')
        return r, mesh, source, attempt

    def question(self, handle='coordinator'):
        return {'kind': 'decision', 'handle': handle, 'note': 'approve?',
                'line': f'WAIT decision:@{handle}: approve?'}

    def test_optin_labels_fresh_real_producer_and_preserves_source(self):
        r, mesh, source, a = self.prepare([RECIPIENT])
        original = dict(source, labels=list(source['labels']))
        r.effects.on_wait(a, source, self.question('cOoRdInAtOr'), r.me)
        self.assertEqual(len(mesh.created), 1)
        self.assertEqual(mesh.created[0]['assignee_id'], RECIPIENT)
        self.assertEqual(mesh.created[0]['labels'], DECISION_LABELS + ['runner:metadata-only'])
        self.assertEqual(source['labels'], original['labels'])
        self.assertTrue(metadata_only(mesh.created[0]))

    def test_created_request_starts_for_recipient_without_git_in_full_pool(self):
        r, mesh, source, a = self.prepare([RECIPIENT])
        r.effects.on_wait(a, source, self.question(), r.me)
        request = mesh.created[0]
        r.store.update(a, state='completed')

        class RecipientMesh(FakeMesh):
            def me(inner):
                return dict(super().me(), id=RECIPIENT, name='coordinator')

            def my_tasks(inner, cat):
                return [dict(t) for t in inner.tasks.values()
                        if t.get('assignee_id') == RECIPIENT and inner.category(t) == cat]

        self.agent.name = 'coordinator'
        self.agent.repos[0] = replace(self.agent.repos[0], max_slots=1)
        recipient = self.runner(RecipientMesh([request], max_concurrent=1))
        held = recipient.pool.lease('other-attempt', 'app')
        write_replies(self.replies, {request['id'][:8]: '⏳ WAIT 20m: reviewing evidence'})
        recipient.tick()
        attempt, = recipient.store.by_state('coordinator', ('running',))
        self.assertEqual(attempt.task_id, request['id'])
        self.assertEqual(attempt.workspaces, [])
        self.assertTrue(attempt.checkout.get('token'))
        self.assertEqual([(row['attempt'], row['path']) for row in recipient.pool.rows()],
                         [('other-attempt', str(held))])
        self.assertEqual(self.finish_turn(recipient).state, 'waiting')

    def test_default_and_other_recipient_keep_original_labels(self):
        for recipients, handle in (([], 'coordinator'), ([RECIPIENT], 'other'), (['builder'], 'coordinator')):
            with self.subTest(recipients=recipients, handle=handle):
                r, mesh, source, a = self.prepare(recipients)
                r.effects.on_wait(a, source, self.question(handle), r.me)
                self.assertEqual(mesh.created[0]['labels'], DECISION_LABELS)
                r.store.update(a, state='completed')

    def test_parent_disabled_and_unresolved_self_do_not_create(self):
        for enabled, handle in ((False, 'coordinator'), (True, 'unknown'), (True, 'builder')):
            with self.subTest(enabled=enabled, handle=handle):
                r, mesh, source, a = self.prepare([RECIPIENT], enabled=enabled)
                r.effects.on_wait(a, source, self.question(handle), r.me)
                self.assertEqual(mesh.created, [])
                r.store.update(a, state='completed')

    def test_existing_request_is_not_relabelled_after_restart_optin(self):
        r, mesh, source, a = self.prepare()
        wait = self.question()
        r.effects.on_wait(a, source, wait, r.me)
        self.cfg.decision_request_metadata_only_recipients = [RECIPIENT]
        restarted = self.runner(mesh)
        restarted.effects.on_wait(a, source, self.question(), r.me)
        self.assertEqual(len(mesh.created), 1)
        self.assertEqual(mesh.created[0]['labels'], DECISION_LABELS)

    def test_legacy_wait_request_is_reused_without_relabel(self):
        r, mesh, source, a = self.prepare([RECIPIENT])
        old = mesh.create_task(source['project_id'], {'labels': list(DECISION_LABELS)})
        wait = dict(self.question(), request={'id': old['id'], 'project_id': source['project_id']})
        r.effects.on_wait(a, source, wait, r.me)
        self.assertEqual(len(mesh.created), 1)
        self.assertEqual(mesh.created[0]['labels'], DECISION_LABELS)

    def test_ambiguous_timeout_is_not_reposted_or_relabelled(self):
        r, mesh, source, a = self.prepare()
        create = mesh.create_task
        def timeout(pid, fields):
            create(pid, fields)
            raise MeshError(0, 'timeout')
        with patch.object(mesh, 'create_task', side_effect=timeout):
            r.effects.on_wait(a, source, self.question(), r.me)
        self.cfg.decision_request_metadata_only_recipients = [RECIPIENT]
        restarted = self.runner(mesh)
        restarted.effects.on_wait(a, source, self.question(), r.me)
        self.assertEqual(len(mesh.created), 1)
        self.assertEqual(mesh.created[0]['labels'], DECISION_LABELS)

    def test_nondecision_alert_wait_cannot_create_request(self):
        r, mesh, source, a = self.prepare([RECIPIENT])
        r.effects.on_wait(a, source, {'kind': 'card', 'target': task(2)['id']}, r.me)
        self.assertEqual(mesh.created, [])

    def test_config_default_and_exact_uuid_list(self):
        self.assertEqual(RunnerConfig('x', self.root, []).decision_request_metadata_only_recipients, [])
        path = self.root / 'r.toml'
        base = '[runner]\nmesh_url="http://mesh.invalid"\n'
        path.write_text(base)
        self.assertEqual(load(path).decision_request_metadata_only_recipients, [])
        path.write_text(base + f'decision_request_metadata_only_recipients=["{RECIPIENT}"]\n')
        self.assertEqual(load(path).decision_request_metadata_only_recipients, [RECIPIENT])
        for value in ('true', '"coordinator"', '["coordinator"]', '[1]', '[true]', '["not-a-uuid"]'):
            with self.subTest(value=value):
                path.write_text(base + f'decision_request_metadata_only_recipients={value}\n')
                with self.assertRaises(ConfigError):
                    load(path)
