"""The runner's own notes say they are automation.

Mesh opens a follow-up card for a comment written on an already closed card by anyone but the
card's assignee, unless the comment reads as automation: a known driver prefix or a
``metadata.source`` other than ``api``, ``ui`` or ``mcp``. A reviewer lane closes a card it does
not own, and the runner's completion line for that session lands on the closed card seconds
later; without the label every such line became an urgent follow-up card for the owner.

Only the runner's own notes (body opening with ``[runner]``) are labelled: a body it relays for
someone else (a mail, an agent's question) stays unlabelled, so a real remark on a closed card
still reaches its owner. The label is ``source``, never ``informational``: readers that decide
whether a comment needs attention key on ``informational`` and must keep seeing runner notes."""
import io
import json
import unittest

from mesh_runner.mesh import MeshClient, RUNNER_NOTE_SOURCE


class Recorder:
    def __init__(self):
        self.payloads = []

    def __call__(self, req, timeout=None):
        self.payloads.append(json.loads(req.data or b"{}"))
        return io.BytesIO(b'{"id": "c1"}')


def client(rec):
    c = MeshClient("https://m", "k", opener=rec)
    c.idempotency_keys = False
    return c


class RunnerNoteSource(unittest.TestCase):
    def test_completion_line_is_labelled_automation(self):
        rec = Recorder()
        client(rec).add_comment("t1", "[runner] ✅ completed in ~2m · session=01a1dae8 · 1 turns · codex",
                                key="completion|a1")
        self.assertEqual(rec.payloads[-1]["metadata"], {"source": RUNNER_NOTE_SOURCE})

    def test_label_names_automation_not_a_client(self):
        self.assertNotIn(RUNNER_NOTE_SOURCE, ("", "api", "ui", "mcp"))

    def test_every_runner_note_is_labelled(self):
        for body in ("[runner] closed: awaited answer received",
                     "  [runner] NO OUTCOME at 10:00",
                     "[runner] Workspace snapshot keeps failing"):
            rec = Recorder()
            client(rec).add_comment("t1", body)
            self.assertEqual(rec.payloads[-1].get("metadata"), {"source": RUNNER_NOTE_SOURCE}, body)

    def test_relayed_text_is_not_labelled(self):
        for body in ("agent text", "From: someone\nSubject: reply", "see [runner] above"):
            rec = Recorder()
            client(rec).add_comment("t1", body)
            self.assertNotIn("metadata", rec.payloads[-1], body)

    def test_label_never_claims_informational(self):
        rec = Recorder()
        client(rec).add_comment("t1", "[runner] ✅ completed in ~1m")
        self.assertNotIn("informational", rec.payloads[-1]["metadata"])

    def test_runner_comment_helper_goes_through_the_label(self):
        from mesh_runner.runner import AgentRunner
        rec = Recorder()
        r = AgentRunner.__new__(AgentRunner)
        r.agent = type("A", (), {"name": "lane"})()
        r.mesh = client(rec)
        self.assertTrue(r._comment("t1", "✅ completed in ~3m", key="completion|a1"))
        self.assertEqual(rec.payloads[-1], {"body": "[runner] ✅ completed in ~3m",
                                            "metadata": {"source": RUNNER_NOTE_SOURCE}})


if __name__ == "__main__":
    unittest.main()
