"""A producer card's turn names the open evidence edges that wait on it.

An edge ``wait_for on=card target_condition=artifact_published|access_ready|contract_ready`` is released
only by an attestation on the producer's side (or by the producer's closure). The producer's session
used not to know the edge existed: the awaited result was published, never attested, and the waiting
card slept for hours. Its turn prompt now lists every open incoming evidence edge with the waiting card,
the condition, the exact criterion, the dependency id and the one call that releases it."""
import logging
import unittest
from unittest import mock

from mesh_runner import prompt as prompts
from mesh_runner.mesh import MeshError

from fakes import FakeMesh, ask_agent, calls, next_turn, task, write_replies
from test_runner import Bench

PRODUCER = "0000aaaa-8c5c-4d02-81ef-d3df80f17dcc"
WAITER = "0000bbbb-dfa3-4967-970d-e5e09ee3d6f7"
EDGE = "0000cccc-f7d2-4096-bd0b-3008d937ff61"
CRITERION = ("Independent pre-change Verify: SHIP @<full sha> on the new head of the consumer-uid branch "
             "after DO-NOT-SHIP; daemon_fix merged and installed (check ok:true) — «готово»")
HEADER = "[runner] Cards waiting on a result of this card"


def incoming(**over) -> dict:
    """An incoming edge in the shape GET /tasks/{id}/dependencies returns it (fields copied from a live
    response; ids and texts replaced)."""
    row = {"id": EDGE, "task_id": WAITER, "depends_on_task_id": PRODUCER, "dependency_type": "blocks",
           "condition": "artifact_published", "criterion": CRITERION, "condition_satisfied": False,
           "evidence_status": "none", "generation": 1, "created_at": "2026-11-18T23:50:10.55574Z",
           "owner_agent_id": "aaaa1111-a9e0-4583-a014-cdf2a1fd1159",
           "owner_wait_id": "bbbb2222-c89d-4aab-b71e-73604abd9f80",
           "related_task_status_id": "55556666-ff34-497e-8a2b-cdff6ac5957b",
           "related_task_title": "[access] Keys for the consumer: carriers first"}
    row.update(over)
    return row


def block(*rows, task_id=PRODUCER, closed=None) -> str:
    return prompts.evidence_edges_block(task_id, {"outgoing": [], "incoming": list(rows)}, closed=closed)


class EvidenceEdgesBlock(unittest.TestCase):
    def test_an_open_artifact_published_edge_names_waiter_criterion_edge_and_the_releasing_call(self):
        text = block(incoming())
        self.assertIn(HEADER, text)
        self.assertIn("#0000bbbb", text)
        self.assertIn("artifact_published", text)
        self.assertIn(CRITERION, text)
        self.assertIn(f"dependency {EDGE}", text)
        call = (f'update_dependency task_id={WAITER} depends_on_task_id={PRODUCER} '
                f'evidence_criterion="{CRITERION}" evidence_url=<https link to the result> attest=true')
        self.assertIn(call, text)
        # the edge id and attest=true travel on the same entry: one edge, one releasing call
        entry = text.split("  - #0000bbbb", 1)[1].split("\n  - ", 1)[0]
        self.assertIn(EDGE, entry)
        self.assertIn("attest=true", entry)

    def test_access_ready_and_contract_ready_are_evidence_edges_too(self):
        for cond in ("access_ready", "contract_ready"):
            text = block(incoming(condition=cond, criterion=""))
            self.assertIn(f"waits for {cond}", text)
            # no criterion on the edge: Mesh compares the evidence with the condition name
            self.assertIn(f'evidence_criterion="{cond}"', text)

    def test_the_criterion_is_quoted_exactly_never_cut_or_collapsed(self):
        crit = "line one\nline  two \"quoted\" " + "x" * 600
        text = block(incoming(criterion=crit))
        self.assertIn('evidence_criterion="line one\\nline  two \\"quoted\\" ' + "x" * 600 + '"', text)

    def test_no_block_for_a_closed_attested_or_non_evidence_edge(self):
        for row in (incoming(condition="task_done", criterion=""),           # wait_for on=card (done)
                    incoming(condition="shipped"), incoming(condition="deployed"), incoming(condition="accepted"),
                    incoming(condition_satisfied=True, released_by="evidence", evidence_status="trusted"),
                    incoming(condition_satisfied=True, released_by="closure_without_evidence"),
                    incoming(condition_satisfied=None),                       # no server verdict: not claimed open
                    incoming(dependency_type="relates_to"),
                    incoming(depends_on_task_id=WAITER),                      # not this card's edge
                    incoming(task_id=PRODUCER)):                              # a self-edge
            self.assertEqual(block(row), "", row)

    def test_no_edges_no_block_and_the_start_prompt_is_unchanged(self):
        t = {"id": PRODUCER, "title": "t", "priority": "high", "labels": []}
        self.assertEqual(block(), "")
        self.assertEqual(prompts.evidence_edges_block(PRODUCER, {"outgoing": [], "incoming": []}), "")
        self.assertEqual(prompts.evidence_edges_block(PRODUCER, []), "")             # legacy bare list
        plain = prompts.start_prompt(t, "Lane", ["/w"], [])
        self.assertEqual(prompts.start_prompt(t, "Lane", ["/w"], [], evidence=""), plain)
        self.assertIn(HEADER, prompts.start_prompt(t, "Lane", ["/w"], [], evidence=block(incoming())))

    def test_a_closed_waiter_is_left_out_an_unknown_one_is_shown(self):
        self.assertEqual(block(incoming(), closed=lambda status_id: True), "")
        self.assertIn("#0000bbbb", block(incoming(related_task_status_id=None), closed=lambda status_id: False))
        self.assertIn("#0000bbbb", block(incoming(related_task_title=None)))          # cross-project: no title

    def test_several_edges_each_get_their_call_and_a_long_list_is_counted(self):
        rows = [incoming(id=f"{n:08x}-0000-4000-8000-000000000000", task_id=f"{n:08x}-1111-4000-8000-000000000000")
                for n in range(1, 15)]
        text = block(*rows)
        self.assertEqual(text.count("attest=true"), prompts.EVIDENCE_EDGES_SHOWN)
        self.assertIn(f"+{14 - prompts.EVIDENCE_EDGES_SHOWN} more", text)


class EvidenceEdgesInTurns(Bench):
    """The block reaches the session: on the first prompt and on a resumed turn; a read error drops it."""

    def producer(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        mesh.incoming = {t["id"]: [incoming(depends_on_task_id=t["id"])]}
        return t, mesh

    def test_the_first_prompt_carries_the_block(self):
        t, mesh = self.producer()
        write_replies(self.replies, {t["id"][:8]: next_turn("more", text="Step done.")})
        r = self.runner(mesh)
        r.tick()
        self.finish_turn(r)
        prompt = calls(self.calls)[0]["prompt"]
        self.assertIn(HEADER, prompt)
        self.assertIn(f"update_dependency task_id={WAITER} depends_on_task_id={t['id']} ", prompt)

    def test_a_resumed_turn_carries_the_block_read_at_turn_start(self):
        t, mesh = self.producer()
        mesh.incoming = {}
        write_replies(self.replies, {t["id"][:8]: ask_agent("lead", "ready?"),
                                     t["id"][:8] + ":resume": next_turn("more", text="Step done.")})
        r = self.runner(mesh)
        r.tick()
        self.settle_all(r)
        self.assertNotIn(HEADER, calls(self.calls)[0]["prompt"])
        mesh.incoming = {t["id"]: [incoming(depends_on_task_id=t["id"])]}   # the edge appears while it waits
        mesh.add_comment(t["id"], "yes, go", author="lead")
        r.tick()
        self.settle_all(r)
        c = calls(self.calls)
        self.assertEqual([x["resume"] for x in c][:2], [False, True])
        self.assertIn(HEADER, c[1]["prompt"])
        self.assertIn(f"dependency {EDGE}", c[1]["prompt"])

    def test_an_unreadable_edge_set_drops_the_block_not_the_turn(self):
        t, mesh = self.producer()
        for fault in (MeshError(500, "boom"), ValueError("bad payload")):
            def broken(tid, fault=fault):
                raise fault
            mesh.dependencies = broken
            write_replies(self.replies, {t["id"][:8]: next_turn("more", text="Step done.")})
            r = self.runner(mesh)
            with self.assertLogs("mesh_runner", logging.WARNING) as logs:
                text = r._evidence_edges(t["id"], t["id"][:8])
            self.assertEqual(text, "")
            self.assertTrue(any("evidence edges unreadable" in line and t["id"][:8] in line for line in logs.output),
                            logs.output)
            r.store.db.close()
        # end to end: a renderer that raises on what Mesh sent costs the block, the turn still starts
        del mesh.dependencies                                  # back to the fake's own method
        r = self.runner(mesh)
        with mock.patch.object(prompts, "evidence_edges_block", side_effect=TypeError("unexpected row")), \
                self.assertLogs("mesh_runner", logging.WARNING) as logs:
            r.tick()
            self.finish_turn(r)
        prompt = calls(self.calls)[-1]["prompt"]
        self.assertIn("[runner] Task #00000001", prompt)
        self.assertNotIn(HEADER, prompt)
        self.assertTrue(any("evidence edges unreadable, block omitted: unexpected row" in line for line in logs.output))


if __name__ == "__main__":
    unittest.main()
