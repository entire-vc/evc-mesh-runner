"""Explicit operations for a confined decision recipient enter ordinary work admission."""
from dataclasses import replace
import unittest

from mesh_runner.effects import ACCESS_REQUEST_LABEL, is_ops_request
from mesh_runner.workspace_scope import metadata_only

from fakes import FakeMesh, task, wait_time, write_replies
from test_answer_desk import Desk, REQUEST, lead_lane
from test_decision_order_delivery import FakeMeshWith


LEAD = "agent-lead"
OPERATION = "Please deploy the reviewed runner head and report its health probe."


class ExplicitOperations(unittest.TestCase):
    def test_only_explicit_operational_requests_match(self):
        for text in (OPERATION, "Can you install the approved build?", "Restart runner.service",
                     "Merge MR !52", "Grant builder access to the deployment", "give lead read access",
                     "Run the migration on the authorised host", "@operator, выкати проверенный HEAD",
                     "Прошу установить сборку", "Нужно перезапустить runner", "выдай lead доступ",
                     "Предоставь доступ к проекту", "можешь влить MR !12?", "Примени миграцию 7"):
            with self.subTest(text=text):
                self.assertTrue(is_ops_request(text))
        for text in ("Should we restart the runner?", "Is it safe to deploy?", "Approve the merge?",
                     "Какой доступ нужен?", "Нужно ли перезапустить runner?", "Можно ли выкатить HEAD?",
                     "Не перезапускай runner", "The deployment is blocked", "Deploy or hold?", ""):
            with self.subTest(text=text):
                self.assertFalse(is_ops_request(text))


class OpsRequestDelivery(Desk):
    def setUp(self):
        super().setUp()
        self.cfg.decision_request_metadata_only_recipients = [LEAD]

    def park(self, mesh, source, question=OPERATION):
        runner = self.run_turn(mesh, source)
        desk = self.desk(mesh, runner, lead_lane(mesh))
        desk.step()
        self.ask(mesh, source, question=question)
        return runner, desk, self.finish_turn(runner)

    def assert_access(self, request):
        self.assertTrue(request["title"].startswith("[access] "))
        self.assertIn(ACCESS_REQUEST_LABEL, request["labels"])
        self.assertIn("runner:workspace-required", request["labels"])
        self.assertNotIn("decision-request", request["labels"])
        self.assertNotIn("runner:metadata-only", request["labels"])
        self.assertFalse(metadata_only(request))
        self.assertEqual(request["assignee_id"], LEAD)
        self.assertIn("normal permissions and approval requirements", request["description"])

    def test_operation_bypasses_a_live_desk_and_creates_one_executable_request(self):
        source = task(1)
        mesh = FakeMesh([source])
        runner, desk, attempt = self.park(mesh, source)
        self.assertEqual((attempt.state, attempt.wait["kind"]), ("waiting", "decision"))
        self.assertEqual(len(mesh.created), 1)
        self.assert_access(mesh.created[0])
        self.assertEqual(attempt.wait["request"]["id"], mesh.created[0]["id"])
        self.assertNotIn("cardless", attempt.wait)
        self.assertEqual(desk.state("lead", REQUEST), "")
        desk.step()
        runner.tick()
        self.assertEqual((len(mesh.created), self.turns), (1, []))
        self.assertEqual(self.cfg.decision_request_metadata_only_recipients, [LEAD])

    def test_an_ordinary_question_still_goes_to_the_answer_desk(self):
        source = task(1)
        mesh = FakeMesh([source])
        _, desk, attempt = self.park(mesh, source, "Should we restart the runner?")
        self.assertTrue(attempt.wait.get("cardless"))
        self.assertEqual(mesh.created, [])
        self.assertEqual(desk.state("lead", REQUEST), "queued")

    def test_operations_for_other_recipients_keep_the_existing_route(self):
        self.cfg.decision_request_metadata_only_recipients = ["someone-else"]
        source = task(1)
        mesh = FakeMesh([source])
        _, desk, attempt = self.park(mesh, source)
        self.assertTrue(attempt.wait.get("cardless"))
        self.assertEqual(mesh.created, [])
        self.assertEqual(desk.state("lead", REQUEST), "queued")

    def test_a_named_owner_card_receives_the_operation_without_a_new_card(self):
        source = task(1)
        owned = task(2, cat="in_progress", assignee_id=LEAD)
        mesh = FakeMeshWith([source, owned])
        _, desk, attempt = self.park(mesh, source, f"Restart the runner on #{owned['id'][:8]}")
        self.assertEqual(mesh.created, [])
        self.assertEqual(len(mesh.comment_log.get(owned["id"], [])), 1)
        self.assertEqual(attempt.wait["delivered"]["card"], owned["id"])
        self.assertEqual(desk.state("lead", REQUEST), "")

    def test_an_unknown_create_response_is_adopted_as_the_same_access_request(self):
        source = task(1)
        mesh = FakeMesh([source])
        mesh.lose_create_response = 1
        with self.assertLogs("mesh_runner", "WARNING"):
            runner, desk, attempt = self.park(mesh, source)
        self.assertEqual(len(mesh.created), 1)
        self.assert_access(mesh.created[0])
        self.assertEqual(runner.effects._decision(source["id"])["state"], "creating")
        runner.effects._decision_checked.clear()
        runner.effects.reconcile_decisions()
        runner.tick()
        attempt = runner.store.get(attempt.id)
        self.assertEqual(len(mesh.created), 1)
        self.assertEqual(attempt.wait["request"]["id"], mesh.created[0]["id"])
        self.assertEqual(desk.state("lead", REQUEST), "")

    def test_access_request_needs_and_receives_an_ordinary_workspace(self):
        source = task(1)
        mesh = FakeMesh([source])
        _, _, _ = self.park(mesh, source)
        request = mesh.created[0]

        class RecipientMesh(FakeMesh):
            def me(inner):
                return dict(super().me(), id=LEAD, name="lead")

            def my_tasks(inner, categories):
                return [dict(card) for card in inner.tasks.values()
                        if card.get("assignee_id") == LEAD and inner.category(card) in categories.split(",")]

        self.agent.name = "lead"
        self.agent.repos[0] = replace(self.agent.repos[0], max_slots=1)
        recipient = self.runner(RecipientMesh([request], max_concurrent=1))
        recipient.pool.lease("other-attempt", "app")
        write_replies(self.replies, {request["id"][:8]: wait_time(why="checking installed health")})
        recipient.tick()
        self.assertEqual(recipient.store.by_state("lead", ("running",)), [])
        self.assertEqual(recipient.mesh.checkouts, {})
        recipient.pool.release("other-attempt")
        recipient.tick()
        attempt, = recipient.store.by_state("lead", ("running",))
        self.assertEqual(attempt.task_id, request["id"])
        self.assertTrue(attempt.workspaces)
        self.assertEqual(self.finish_turn(recipient).state, "waiting")

    def retained_question(self):
        self.cfg.cardless_question_lanes = []
        source = task(1)
        mesh = FakeMesh([source])
        runner = self.run_turn(mesh, source)
        self.ask(mesh, source, question="Which rollout target is approved?")
        attempt = self.finish_turn(runner)
        original = mesh.created[0]
        self.assertTrue(metadata_only(original))
        wait = {"kind": "decision", "handle": "lead", "note": OPERATION}
        return source, mesh, runner, attempt, original, wait

    def test_a_retained_metadata_question_is_superseded_without_changing_its_scope(self):
        source, mesh, runner, attempt, original, wait = self.retained_question()
        runner.effects.on_wait(attempt, source, wait, runner.me)
        self.assertEqual(original["status_id"], "s-done")
        self.assertTrue(metadata_only(original))
        self.assertEqual(len(mesh.created), 2)
        self.assert_access(mesh.created[1])
        runner.effects.on_wait(attempt, source, wait, runner.me)
        self.assertEqual(len(mesh.created), 2)

    def test_an_identical_operation_retained_by_the_old_route_is_replaced(self):
        source, mesh, runner, attempt, original, wait = self.retained_question()
        # External Mesh/store state from the previous runner version: the same
        # operation was filed as a metadata-only decision without an access key.
        record = runner.effects._decision(source["id"])
        record.update(key=runner.effects._decision_key(source["id"], LEAD, wait), question=OPERATION)
        runner.effects._save_decision(source["id"], record)
        runner.effects.on_wait(attempt, source, wait, runner.me)
        self.assertEqual(len(mesh.created), 2)
        self.assertTrue(metadata_only(original))
        self.assertEqual(original["status_id"], "s-done")
        self.assert_access(mesh.created[1])

    def test_a_running_retained_question_keeps_its_lease_and_confinement(self):
        source, mesh, runner, attempt, original, wait = self.retained_question()
        original["checked_out_by"] = LEAD
        runner.effects.on_wait(attempt, source, wait, runner.me)
        self.assertFalse(wait["fx"])
        self.assertEqual(len(mesh.created), 1)
        self.assertTrue(metadata_only(original))
        self.assertNotIn("runner:workspace-required", original["labels"])
        self.assertNotEqual(original["status_id"], "s-done")


if __name__ == "__main__":
    unittest.main()
