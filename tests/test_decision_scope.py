"""A known answer or a matching working card must not become another decision card."""
import datetime as dt

from fakes import AGENT_ID, FakeMesh, task
from mesh_runner.answer_desk import AnswerDesk
from mesh_runner.mesh import MeshError
from mesh_runner.store import iso, now
from mesh_runner.wake import check
from test_runner import Bench
from test_answer_desk import lead_lane


LEAD = "agent-lead"
RID = "acefa3ca-7db9-43d8-8625-b983a3462349"
CURRENT = "bc982967-1ff4-4c7e-8a11-38fa22114a2c"
MR = "https://git.example/g/app/-/merge_requests/730"


class ScopeMesh(FakeMesh):
    def request(self, method, path, body=None, query=None):
        if method == "GET" and path == "/projects/p1/tasks":
            self.owner_queries = getattr(self, "owner_queries", []) + [dict(query)]
            rows = [dict(t) for t in self.tasks.values() if t.get("project_id") == "p1"
                    and t.get("assignee_id") == query["assignee_id"]]
            size, page = query["page_size"], query["page"]
            return {"items": rows[(page - 1) * size:page * size], "total_pages": (len(rows) + size - 1) // size}
        return super().request(method, path, body, query)


class DecisionScope(Bench):
    def setUp(self):
        super().setUp()
        self.cfg.mesh_effects = True
        self.source = task(1, workspace_id="ws1")
        self.card = task(2, cat="in_progress", assignee_id=LEAD, workspace_id="ws1")
        self.mesh = ScopeMesh([self.source, self.card])
        self.r = self.runner(self.mesh)
        self.r.refresh_me()
        self.a = self.r.store.create(self.agent.name, self.source["id"], "claude")

    def ask(self, question, **extra):
        wait = {"kind": "decision", "handle": "lead", "note": question, "question": question,
                "since": iso(now() - dt.timedelta(minutes=1)), "started": iso(), **extra}
        self.r.effects.on_wait(self.a, self.source, wait, self.r.me)
        return wait

    def link(self, card, url=MR, kind="pr"):
        self.mesh.links = getattr(self.mesh, "links", {})
        self.mesh.links.setdefault(card["id"], []).append({"link_type": kind, "url": url, "provider": "gitlab"})

    def prior_answer(self, card=None, question="approve this plan?", answer="Answer: the plan is approved.",
                     author=LEAD, recipient="lead", metadata=None, rid=RID):
        card = card or self.source
        asked = self.mesh.add_comment(card["id"],
            f"❔ **Question to {recipient}** · request `{rid}`\n\n{question}", author=AGENT_ID)
        asked["metadata"] = {"command": "ask_agent", "decision_request_id": rid, "wakes": False}
        reply = self.mesh.add_comment(card["id"], answer, author=author)
        reply["metadata"] = {"answers": rid} if metadata is None else metadata
        return reply

    def typed_request(self, question="approve this plan?"):
        self.cfg.cardless_question_lanes = ["lead"]
        lead = lead_lane(self.mesh)
        AnswerDesk(self.cfg, [self.r, lead], self.r.store).attach()
        asked = self.mesh.add_comment(self.source["id"],
            f"❔ **Question to lead** · request `{CURRENT}`\n\n{question}", author=AGENT_ID)
        asked["metadata"] = {"command": "ask_agent", "decision_request_id": CURRENT, "wakes": False}
        self.mesh.open_decision(CURRENT, self.source["id"], question)

    def server_answer(self, answer, rid=RID):
        return f'💬 **Answer to question** "{rid}"\n\n> approve this plan?\n\n{answer}'

    def test_mr_url_delivers_to_the_matching_live_card(self):
        self.link(self.card)
        wait = self.ask(f"Fix the failed release job on {MR}")
        self.assertEqual(self.mesh.created, [])
        self.assertEqual(wait["delivered"]["card"], self.card["id"])
        self.assertIn(MR, self.mesh.comment_log[self.card["id"]][0]["body"])
        self.r.effects.on_wait(self.a, self.source, wait, self.r.me)
        self.assertEqual(len(self.mesh.comment_log[self.card["id"]]), 1)

    def test_bare_mr_uses_the_sources_repository_scope(self):
        self.link(self.source)
        self.link(self.card)
        other = task(3, cat="in_progress", assignee_id=LEAD, workspace_id="ws1")
        self.mesh.tasks[other["id"]] = other
        self.link(other, "https://git.example/g/other/-/merge_requests/730")
        wait = self.ask("Fix the failed release job on !730")
        self.assertEqual(self.mesh.created, [])
        self.assertEqual(wait["delivered"]["card"], self.card["id"])
        self.assertEqual(self.mesh.comment_log.get(other["id"], []), [])

    def test_a_bare_mr_with_two_repository_matches_does_not_guess(self):
        self.link(self.card)
        other = task(3, cat="in_progress", assignee_id=LEAD, workspace_id="ws1")
        self.mesh.tasks[other["id"]] = other
        self.link(other, "https://git.example/g/other/-/merge_requests/730")
        wait = self.ask("Fix the failed release job on !730")
        self.assertNotIn("delivered", wait)
        self.assertEqual(self.mesh.comment_log.get(self.card["id"], []), [])
        self.assertEqual(self.mesh.comment_log.get(other["id"], []), [])

    def test_a_bare_mr_does_not_switch_the_sources_repository_for_another_iid(self):
        self.link(self.source, "https://git.example/g/app/-/merge_requests/1")
        self.link(self.card, "https://git.example/g/other/-/merge_requests/730")
        wait = self.ask("Fix the failed release job on !730")
        self.assertNotIn("delivered", wait)
        self.assertEqual(self.mesh.comment_log.get(self.card["id"], []), [])

    def test_multiple_source_repositories_leave_a_bare_mr_ambiguous(self):
        self.link(self.source, "https://git.example/g/app/-/merge_requests/1")
        self.link(self.source, "https://git.example/g/other/-/tree/main", "branch")
        self.link(self.card)
        wait = self.ask("Fix the failed release job on !730")
        self.assertNotIn("delivered", wait)

    def test_recipient_filter_paginates_before_selecting_a_unique_card(self):
        self.mesh.tasks.pop(self.card["id"])
        for n in range(3, 54):
            t = task(n, cat="done", assignee_id=LEAD, workspace_id="ws1")
            self.mesh.tasks[t["id"]] = t
        self.mesh.tasks[self.card["id"]] = self.card
        self.link(self.card)
        wait = self.ask(f"Fix the failed release job on {MR}")
        self.assertEqual(wait["delivered"]["card"], self.card["id"])
        self.assertEqual([q["page"] for q in self.mesh.owner_queries], [1, 2])
        self.assertTrue(all(q["assignee_id"] == LEAD for q in self.mesh.owner_queries))

    def test_unknown_or_excessive_pagination_cannot_choose_a_card(self):
        plain = self.mesh.request
        for pages in (None, "unknown", 101):
            with self.subTest(pages=pages):
                def request(method, path, body=None, query=None):
                    out = plain(method, path, body, query)
                    if method == "GET" and path == "/projects/p1/tasks":
                        out["total_pages"] = pages
                    return out
                self.mesh.request = request
                self.link(self.card)
                wait = self.ask(f"Fix the failed release job on {MR}")
                self.assertFalse(wait["fx"])
                self.assertNotIn("delivered", wait)
                self.assertEqual(self.mesh.created, [])

    def test_explicit_branch_delivers_to_its_linked_card(self):
        self.link(self.card, "https://git.example/g/app/-/tree/fix/release-job", "branch")
        wait = self.ask("Fix the release job on branch `fix/release-job`")
        self.assertEqual(self.mesh.created, [])
        self.assertEqual(wait["delivered"]["card"], self.card["id"])

    def test_matching_vcs_of_a_wrong_recipient_or_workspace_is_not_delivered(self):
        self.link(self.card)
        for field, value in (("assignee_id", "someone-else"), ("workspace_id", "ws2")):
            with self.subTest(field=field):
                original = self.card[field]
                self.card[field] = value
                wait = self.ask(f"Fix the failed release job on {MR}")
                self.assertNotIn("delivered", wait)
                self.card[field] = original
        self.assertEqual(self.mesh.comment_log.get(self.card["id"], []), [])

    def test_reference_link_does_not_claim_the_referenced_mr(self):
        self.link(self.card, kind="reference")
        wait = self.ask(f"Fix the failed release job on {MR}")
        self.assertNotIn("delivered", wait)

    def test_transient_vcs_read_keeps_the_route_open_then_delivers(self):
        self.link(self.card)
        plain = self.mesh.vcs_links
        calls = []

        def vcs_links(tid):
            if tid == self.card["id"] and not calls:
                calls.append(tid)
                raise MeshError(503, "unavailable")
            return plain(tid)

        self.mesh.vcs_links = vcs_links
        wait = self.ask(f"Fix the failed release job on {MR}")
        self.assertFalse(wait["fx"])
        self.assertEqual(self.mesh.created, [])
        self.r.effects.adopt(self.a, self.source, wait, self.r.me)
        self.assertEqual(wait["delivered"]["card"], self.card["id"])
        self.assertEqual(self.mesh.created, [])

    def test_an_identical_answer_on_the_source_is_used_without_a_card(self):
        reply = self.prior_answer()
        wait = self.ask("APPROVE this plan")
        self.assertEqual(self.mesh.created, [])
        self.assertIn(reply["id"], wait["settled"])
        self.assertIn("approved", wait["settled"])
        event = check(self.mesh, self.r.cats, None, self.source, wait, AGENT_ID, [], now())
        self.assertIn(reply["id"], event)
        self.assertTrue(wait["met"])

    def test_exact_plain_reply_after_the_matching_ask_is_used(self):
        reply = self.prior_answer(metadata={})
        wait = self.ask("approve this plan?")
        self.assertEqual(self.mesh.created, [])
        self.assertIn(reply["id"], wait["settled"])

    def test_real_server_answer_command_is_reused(self):
        reply = self.prior_answer(answer=self.server_answer("The plan is approved."),
                                  metadata={"command": "answer_decision", "answers": RID})
        wait = self.ask("approve this plan?")
        self.assertIn(reply["id"], wait["settled"])
        self.assertEqual(self.mesh.created, [])

    def test_server_wrapper_cannot_hide_progress_or_an_unbound_answer(self):
        cases = [("Needs work: a new task is needed.", RID), ("FYI: reviewed yesterday.", RID),
                 ("[runner] Answer: approved.", RID), ("checking the plan", RID),
                 ("Approved", ""), ("Approved", CURRENT)]
        for answer, rid in cases:
            with self.subTest(answer=answer, rid=rid):
                self.mesh.comment_log.clear()
                self.prior_answer(answer=self.server_answer(answer),
                                  metadata={"command": "answer_decision", "answers": rid})
                self.assertNotIn("settled", self.ask("approve this plan?"))

    def test_reused_answer_completes_the_typed_request_through_its_recipient(self):
        reply = self.prior_answer()
        self.typed_request()
        wait = self.ask("approve this plan?", decision_request_id=CURRENT)
        self.assertIn(reply["id"], wait["settled"])
        self.assertEqual(self.mesh.decision_rows[0]["state"], "answered")
        self.assertEqual(self.mesh.comment_log[self.source["id"]][-1]["author_id"], LEAD)
        self.assertEqual(self.mesh.created, [])

    def test_a_cancelled_request_conflict_does_not_prove_an_answer(self):
        self.prior_answer()
        self.typed_request()
        self.mesh.decision_rows[0]["state"] = "cancelled"
        wait = self.ask("approve this plan?", decision_request_id=CURRENT)
        self.assertNotIn("settled", wait)
        self.assertEqual(self.mesh.decision_rows[0]["state"], "cancelled")

    def test_a_conflict_is_confirmed_only_by_the_exact_current_server_answer(self):
        self.prior_answer()
        self.typed_request()
        reply = self.mesh.add_comment(self.source["id"], self.server_answer("Approved", CURRENT), author=LEAD)
        reply["metadata"] = {"command": "answer_decision", "answers": CURRENT}
        self.mesh.decision_rows[0]["state"] = "answered"
        wait = self.ask("approve this plan?", decision_request_id=CURRENT)
        self.assertIn(reply["id"], wait["settled"])
        self.assertEqual(self.mesh.created, [])

    def test_answer_on_a_named_open_incident_is_used(self):
        self.card["labels"] = ["auto-incident", "alert-id:abc123"]
        question = f"approve this plan on #{self.card['id'][:8]}?"
        reply = self.prior_answer(self.card, question)
        wait = self.ask(question)
        self.assertEqual(self.mesh.created, [])
        self.assertNotIn("delivered", wait)
        self.assertIn(reply["id"], wait["settled"])

    def test_a_different_question_recipient_or_nonanswer_is_not_reused(self):
        cases = [
            {"question": "approve another plan?"},
            {"recipient": "reviewer"},
            {"author": "agent-reviewer"},
            {"answer": "Needs work: create a task for this."},
            {"answer": "FYI: the plan was discussed."},
            {"answer": "[runner] Answer: approved."},
            {"answer": "Answer: approved.", "metadata": {"answers": RID, "wakes": False}},
        ]
        for case in cases:
            with self.subTest(case=case):
                self.mesh.comment_log.clear()
                self.prior_answer(**case)
                wait = self.ask("approve this plan?")
                self.assertNotIn("settled", wait)

    def test_plain_answer_after_a_different_ask_cannot_answer_the_old_question(self):
        self.prior_answer(answer="checking", metadata={})
        self.prior_answer(question="approve another plan?", answer="Answer: approved.", metadata={})
        wait = self.ask("approve this plan?")
        self.assertNotIn("settled", wait)

    def test_answer_on_another_workspace_is_not_reused(self):
        self.card["workspace_id"] = "ws2"
        question = f"approve this plan on #{self.card['id'][:8]}?"
        self.prior_answer(self.card, question)
        wait = self.ask(question)
        self.assertNotIn("settled", wait)
