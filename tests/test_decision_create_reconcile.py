"""A decision create whose outcome the runner never saw settles itself."""
import datetime as dt
from test_runner import Bench
from fakes import FakeMesh, task
from mesh_runner.mesh import MeshError
from mesh_runner.store import iso, now


class DecisionCreateReconcile(Bench):
    def question(self, note='approve the plan?', handle='lead'):
        return {'kind': 'decision', 'handle': handle, 'note': note,
                'line': f'⏳ WAIT decision:@{handle}: {note}',
                'since': iso(now() - dt.timedelta(minutes=1)), 'started': iso()}

    def lost_create(self, *, committed):
        source = task(1)
        mesh = FakeMesh([source])
        if committed:
            mesh.lose_create_response = 1
        else:
            mesh.fail_create = 1
        r = self.runner(mesh)
        r.refresh_me()
        attempt = r.store.create(self.agent.name, source['id'], 'claude')
        wait = self.question()
        r.effects.on_wait(attempt, source, wait, r.me)
        r.store.update(attempt, state='waiting', wait=wait)
        record = r.effects._decision(source['id'])
        self.assertEqual(record['state'], 'creating')
        return r, mesh, source, record

    def pass_(self, r, *, age_minutes=0):
        """One reconcile pass; ``age_minutes`` moves the create into the past."""
        for task_id, record in r.store.marked('_decision', 'decision_request').items():
            record = r.effects._decision(task_id)
            if age_minutes and record.get('started_at'):
                record['started_at'] = iso(now() - dt.timedelta(minutes=age_minutes))
                r.effects._save_decision(task_id, record)
        r.effects._decision_checked.clear()
        r.effects.reconcile_decisions()

    def test_timeout_after_commit_adopts_the_card_without_a_second_create(self):
        r, mesh, source, record = self.lost_create(committed=True)
        self.assertEqual(len(mesh.created), 1)
        self.pass_(r)
        after = r.effects._decision(source['id'])
        self.assertEqual(after['state'], 'open')
        self.assertEqual(after['request'], {'id': mesh.created[0]['id'], 'project_id': 'p1'})
        for _ in range(3):
            self.pass_(r, age_minutes=30)
        self.assertEqual(len(mesh.created), 1)

    def test_adopted_card_is_closed_when_the_source_ends(self):
        r, mesh, source, _ = self.lost_create(committed=True)
        self.pass_(r)
        mesh.set_category(source['id'], 'done')
        r.effects.reconcile_decisions()
        r.effects._decision_checked.clear()
        r.effects.reconcile_decisions()
        self.assertEqual(mesh.category(mesh.tasks[mesh.created[0]['id']]), 'done')

    def test_timeout_without_commit_retries_once_after_the_window(self):
        r, mesh, source, record = self.lost_create(committed=False)
        self.assertEqual(mesh.created, [])
        self.pass_(r)                                    # inside the window: only a search
        self.pass_(r)
        self.assertEqual(mesh.created, [])
        self.pass_(r, age_minutes=11)                    # window over, second empty search: one retry
        self.assertEqual(len(mesh.created), 1)
        self.assertGreaterEqual(mesh.finds, 3)
        after = r.effects._decision(source['id'])
        self.assertEqual(after['state'], 'open')
        self.assertEqual(after['request']['id'], mesh.created[0]['id'])
        self.assertIn(f"[runner decision-create:{record['create_id']}]", mesh.created[0]['description'])
        for _ in range(3):
            self.pass_(r, age_minutes=60)
        self.assertEqual(len(mesh.created), 1)

    def test_a_retry_whose_response_is_lost_is_adopted_not_repeated(self):
        r, mesh, source, _ = self.lost_create(committed=False)
        mesh.lose_create_response = 1
        self.pass_(r)
        self.pass_(r, age_minutes=11)
        self.assertEqual(len(mesh.created), 1)
        self.assertEqual(r.effects._decision(source['id'])['state'], 'creating')
        for _ in range(3):
            self.pass_(r, age_minutes=60)
        self.assertEqual(len(mesh.created), 1)
        self.assertEqual(r.effects._decision(source['id'])['state'], 'open')

    def test_no_retry_while_a_search_cannot_be_answered(self):
        r, mesh, source, _ = self.lost_create(committed=False)
        mesh.fail_find = 5
        for _ in range(4):
            self.pass_(r, age_minutes=60)
        self.assertEqual(mesh.created, [])
        self.assertEqual(r.effects._decision(source['id'])['state'], 'creating')

    def test_a_lane_that_moved_on_does_not_get_the_old_question_back(self):
        r, mesh, source, _ = self.lost_create(committed=False)
        attempt = r.store.open_for_task(self.agent.name, source['id'])
        r.store.update(attempt, wait=self.question(note='another question'))
        self.pass_(r)
        self.pass_(r, age_minutes=11)
        self.assertEqual(mesh.created, [])
        self.assertFalse(r.effects._decision(source['id']))

    def test_terminal_source_without_a_card_drops_the_record(self):
        r, mesh, source, _ = self.lost_create(committed=False)
        mesh.set_category(source['id'], 'done')
        self.pass_(r)
        self.assertEqual(mesh.created, [])
        self.assertFalse(r.effects._decision(source['id']))

    def test_legacy_record_adopts_an_open_card_by_key_but_not_a_closed_one(self):
        r, mesh, source, record = self.lost_create(committed=True)
        for stale in ('create_id', 'project_id', 'started_at'):
            record.pop(stale)
        mesh.tasks[mesh.created[0]['id']]['description'] = f"[runner decision-key:{record['key']}]\nold"
        mesh.set_category(mesh.created[0]['id'], 'done')  # an earlier cycle of the same question
        r.effects._save_decision(source['id'], record)
        self.pass_(r)
        self.assertEqual(r.effects._decision(source['id'])['state'], 'creating')
        mesh.set_category(mesh.created[0]['id'], 'todo')  # an orphan that is still open
        self.pass_(r)
        self.assertEqual(r.effects._decision(source['id'])['state'], 'open')
        self.assertEqual(len(mesh.created), 1)

    def test_found_card_is_closed_not_adopted_when_the_lane_moved_on(self):
        r, mesh, source, _ = self.lost_create(committed=True)
        attempt = r.store.open_for_task(self.agent.name, source['id'])
        r.store.update(attempt, wait=self.question(note='another question'))
        self.pass_(r)
        card = mesh.tasks[mesh.created[0]['id']]
        self.assertEqual(mesh.category(card), 'done')
        self.assertFalse(r.effects._decision(source['id']))
        self.assertEqual(len(mesh.created), 1)

    def test_found_card_is_closed_when_the_lane_no_longer_waits_at_all(self):
        r, mesh, source, _ = self.lost_create(committed=True)
        attempt = r.store.open_for_task(self.agent.name, source['id'])
        r.store.update(attempt, state='completed')
        self.pass_(r)
        self.assertEqual(mesh.category(mesh.tasks[mesh.created[0]['id']]), 'done')
        self.assertFalse(r.effects._decision(source['id']))

    def test_found_card_is_closed_when_the_recipient_changed(self):
        r, mesh, source, record = self.lost_create(committed=True)
        record['recipient'] = 'someone-else'
        r.effects._save_decision(source['id'], record)
        self.pass_(r)
        self.assertEqual(mesh.category(mesh.tasks[mesh.created[0]['id']]), 'done')
        self.assertFalse(r.effects._decision(source['id']))

    def test_found_card_is_adopted_while_the_lane_still_waits(self):
        r, mesh, source, _ = self.lost_create(committed=True)
        self.pass_(r)
        self.assertEqual(r.effects._decision(source['id'])['state'], 'open')
        self.assertNotEqual(mesh.category(mesh.tasks[mesh.created[0]['id']]), 'done')

    def test_preflight_failure_does_not_consume_the_only_retry(self):
        r, mesh, source, _ = self.lost_create(committed=False)
        self.pass_(r)
        calls = []
        real = mesh.statuses

        def flaky(pid):
            calls.append(pid)
            if len(calls) == 1:
                raise MeshError(503, 'unavailable')
            return real(pid)
        mesh.statuses = flaky
        self.pass_(r, age_minutes=11)                    # statuses fails: nothing posted, nothing consumed
        self.assertEqual(mesh.created, [])
        self.assertFalse(r.effects._decision(source['id']).get('retried'))
        self.pass_(r, age_minutes=11)                    # next pass retries and creates once
        self.assertEqual(len(mesh.created), 1)
        self.assertEqual(r.effects._decision(source['id'])['state'], 'open')
        self.pass_(r, age_minutes=60)
        self.assertEqual(len(mesh.created), 1)

    def test_a_rejected_retry_is_the_only_post_and_the_record_freezes(self):
        r, mesh, source, _ = self.lost_create(committed=False)
        self.pass_(r)
        posts = []
        def rejected(pid, fields):
            posts.append(pid)
            raise MeshError(422, 'invalid')
        mesh.create_task = rejected
        for _ in range(5):
            self.pass_(r, age_minutes=60)
        self.assertEqual(len(posts), 1)
        frozen = r.effects._decision(source['id'])
        self.assertEqual(frozen['state'], 'creating')
        self.assertTrue(frozen['retried'])
        attempt = r.store.open_for_task(self.agent.name, source['id'])
        r.store.update(attempt, wait=self.question(note='another question'))
        self.pass_(r, age_minutes=60)                    # lane moved on: record dropped, still one POST
        self.assertFalse(r.effects._decision(source['id']))
        self.assertEqual(len(posts), 1)
