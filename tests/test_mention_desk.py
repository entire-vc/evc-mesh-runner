"""Mention provenance and delivery, using the real runner/desk state machines."""
import datetime as dt

from mesh_runner.answer_desk import AnswerDesk
from mesh_runner.store import Store
from fakes import FakeMesh, task
from test_answer_desk import FALLBACK_MIN, Desk, gated, run_for


class Mentions(Desk):
    def setup_mention(self, body="@builder: which commit should ship?", source=None):
        t = task(9, assignee_id="agent-lead")
        mesh = FakeMesh([t])
        c = mesh.add_comment(t["id"], body, author="agent-lead")
        if source:
            c["metadata"] = {"source": source}
        mesh.mention_rows = [{"comment_id": c["id"], "task_id": t["id"], "author_id": "agent-lead",
                              "author_name": "lead", "comment_body": body, "mentioned_slug": "builder",
                              "extracted_at": dt.datetime.now(dt.timezone.utc).isoformat()}]
        r = self.runner(mesh)
        r.refresh_me()
        self.cfg.cardless_question_lanes = ["builder"]
        desk = AnswerDesk(self.cfg, [r], Store(self.root / "mention-desk.sqlite"), clock=self.clock,
                          spawn=lambda fn: fn(), run_turn=lambda *args: "end_turn")
        desk.attach()
        return mesh, r, desk, c

    def test_addressed_question_is_answered_through_desk_without_a_card(self):
        mesh, r, desk, c = self.setup_mention()
        def comment(ar, rid, tid, text):
            self.turns.append(text)
            mesh.add_comment(tid, "Answer: ship the verified main commit", author=ar.me["id"])
            return "end_turn"
        desk.run_turn = comment
        r.deliver_mentions()
        self.assertEqual(mesh.created, [])
        desk.step()
        self.assertEqual(desk.state("builder", "mention:" + c["id"]), "answered")
        self.assertEqual(len(self.turns), 1)
        self.assertIn("There is no decision request", self.turns[0])
        desk.step()
        self.assertEqual(mesh.created, [])

    def test_notification_and_census_create_neither_queue_nor_card(self):
        for body, source in (("@builder delivery complete", None),
                             ("@builder: merge complete", None),
                             ("@builder: please check the daily census", "fleet-census")):
            with self.subTest(body=body):
                mesh, r, desk, c = self.setup_mention(body, source)
                r.deliver_mentions()
                desk.step()
                self.assertEqual(mesh.created, [])
                self.assertEqual(desk.state("builder", "mention:" + c["id"]), "")

    def test_unanswered_request_falls_back_once_and_survives_seen_ack(self):
        mesh, r, desk, c = self.setup_mention()
        r.deliver_mentions()
        self.assertTrue(mesh.mention_rows[0].get("seen"))
        with self.assertLogs("mesh_runner", "WARNING"):
            desk.step()
        self.assertEqual(desk.state("builder", "mention:" + c["id"]), "failed")
        desk.step()
        self.assertEqual(len(mesh.created), 1)
        self.assertIn("mention-request", mesh.created[0]["labels"])
        desk.step()
        self.assertEqual(len(mesh.created), 1)

    def test_a_mention_its_lane_gate_holds_goes_to_its_card(self):
        mesh, r, desk, c = self.setup_mention()
        gated(self.clock, r)
        r.deliver_mentions()
        self.assertEqual(desk.state("builder", "mention:" + c["id"]), "queued")
        run_for(self.clock, desk, FALLBACK_MIN - 1)
        self.assertEqual(mesh.created, [])
        run_for(self.clock, desk, 2)
        self.assertEqual(desk.state("builder", "mention:" + c["id"]), "carded")
        self.assertEqual(len(mesh.created), 1)
        self.assertEqual(self.turns, [])

    def test_acknowledged_question_remains_deliverable_after_a_restart(self):
        mesh, r, desk, c = self.setup_mention()
        r.deliver_mentions()
        self.assertTrue(mesh.mention_rows[0].get("seen"))
        def comment(ar, rid, tid, text):
            mesh.add_comment(tid, "Answer: hold for the reviewed commit", author=ar.me["id"])
            return "end_turn"
        restarted = AnswerDesk(self.cfg, [r], desk.store, clock=self.clock,
                               spawn=lambda fn: fn(), run_turn=comment).attach()
        restarted.step()
        self.assertEqual(restarted.state("builder", "mention:" + c["id"]), "answered")
        self.assertEqual(mesh.created, [])

    def test_metadata_only_ops_mention_gets_workspace_required_access_card_immediately(self):
        mesh, r, desk, c = self.setup_mention("@builder: please restart the runner unit")
        self.cfg.decision_request_metadata_only_recipients = [r.agent_id]
        r.deliver_mentions()
        self.assertEqual(len(mesh.created), 1)
        made = mesh.created[0]
        self.assertTrue(made["title"].startswith("[access]"))
        self.assertIn("runner:workspace-required", made["labels"])
        self.assertNotIn("decision-request", made["labels"])
        self.assertEqual(desk.state("builder", "mention:" + c["id"]), "")
