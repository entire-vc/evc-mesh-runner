"""One chat runner identity on a state directory shared by the runner and its service processes."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class SharedIdentity(unittest.TestCase):
    """The runner and a chat process start together on one state directory: the first identity written wins."""

    def test_a_late_writer_keeps_the_first_runner_id(self):
        from mesh_runner import chat_protocol as cp
        state = Path(tempfile.mkdtemp())
        real_link = os.link

        def racing(src, dst):                    # the other process creates it between our read and our write
            Path(dst).write_text("11111111-1111-4111-8111-111111111111\n")
            return real_link(src, dst)
        with mock.patch("os.link", racing):
            rid = cp.load_runner_id(state)
        self.assertEqual(rid, "11111111-1111-4111-8111-111111111111")
        self.assertEqual(cp.load_runner_id(state), rid)
        self.assertEqual([p.name for p in state.iterdir()], ["chat-runner-id"])     # no temp file left

    def test_an_unreadable_id_is_still_replaced(self):
        from mesh_runner import chat_protocol as cp
        state = Path(tempfile.mkdtemp())
        (state / "chat-runner-id").write_text("garbage")
        rid = cp.load_runner_id(state)
        self.assertEqual((state / "chat-runner-id").read_text().strip(), rid)

    def test_a_late_credential_writer_uses_the_first_credential(self):
        from mesh_runner import chat_protocol as cp
        state = Path(tempfile.mkdtemp())
        path = state / "chat-runner-a.json"

        def register():                          # the other process registered and wrote meanwhile
            path.write_text(json.dumps({"runner_id": "22222222-2222-4222-8222-222222222222", "token": "first"}))
            return {"runner_id": "33333333-3333-4333-8333-333333333333", "token": "second"}
        self.assertEqual(cp.load_runner_credential(state, register, "a"),
                         ("22222222-2222-4222-8222-222222222222", "first"))


if __name__ == "__main__":
    unittest.main()
