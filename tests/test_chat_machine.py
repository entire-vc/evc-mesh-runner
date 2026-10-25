import tempfile
import unittest
from pathlib import Path

from fake_chat_mesh import FakeChatMesh
from mesh_runner.chat_delivery import window_key
from mesh_runner.chat_machine import build_prompt, chat_dir, delete_chat, fetch_attachments
from mesh_runner.chat_protocol import ChatClient
from mesh_runner.interactive import agent_dir, write_meta


class NoTmux:
    def windows(self, session):
        return {}

    def kill_window(self, *a):
        pass


class Machine(unittest.TestCase):
    def setUp(self):
        self.state = Path(tempfile.mkdtemp())

    def test_delete_removes_transcript_files_and_metadata(self):
        home = Path(tempfile.mkdtemp())
        sid = "11111111-2222-3333-4444-555555555555"
        tr = home / ".claude" / "projects" / "p" / f"{sid}.jsonl"
        tr.parent.mkdir(parents=True)
        tr.write_text("x")
        rollout = home / ".codex" / "sessions" / "2026" / "10" / "24" / f"rollout-t-{sid}.jsonl"
        rollout.parent.mkdir(parents=True)
        rollout.write_text("x")
        side = home / ".claude" / "projects" / "p" / sid / "subagents"
        side.mkdir(parents=True)
        (side / "a.jsonl").write_text("x")
        d = agent_dir(self.state, "a")
        write_meta(d, window_key("th1"), {"session_id": sid})
        (chat_dir(self.state, "th1") / "files").mkdir(parents=True)
        self.assertTrue(delete_chat(NoTmux(), self.state, "a", "th1", {"HOME": str(home)}))
        self.assertFalse(tr.exists() or rollout.exists() or side.parent.exists() or chat_dir(self.state, "th1").exists())
        self.assertFalse((d / f"{window_key('th1')}.json").exists())
        self.assertFalse(delete_chat(NoTmux(), self.state, "a", "th1", {"HOME": str(home)}))   # now: not_found

    def test_delete_after_account_switch_clears_side_data_next_to_the_transcript(self):
        home = Path(tempfile.mkdtemp())
        sid = "11111111-2222-3333-4444-555555555555"
        tr = home / "account-a" / "projects" / "p" / f"{sid}.jsonl"
        side = tr.parent / sid / "subagents" / "a.jsonl"
        side.parent.mkdir(parents=True)
        tr.write_text("x")
        side.write_text("x")
        write_meta(agent_dir(self.state, "a"), window_key("th1"),
                   {"session_id": sid, "transcript": str(tr)})
        env = {"HOME": str(home), "CLAUDE_CONFIG_DIR": str(home / "account-b")}
        self.assertTrue(delete_chat(NoTmux(), self.state, "a", "th1", env))
        self.assertFalse(tr.exists())
        self.assertFalse(side.parent.parent.exists())

    def test_delete_removes_logs_a_crash_left_behind(self):
        logs = self.state / "chat" / "logs" / window_key("th1")
        logs.mkdir(parents=True)
        (logs / "t.jsonl").write_text("answer")
        self.assertTrue(delete_chat(NoTmux(), self.state, "a", "th1", {"HOME": tempfile.mkdtemp()}))
        self.assertFalse(logs.exists())

    def test_download_refuses_redirects(self):
        import http.server
        import threading
        from mesh_runner.chat_machine import _NO_REDIRECT

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:1/x")
                self.end_headers()

            def log_message(self, *a):
                pass
        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            with self.assertRaises(OSError):
                _NO_REDIRECT.open(f"http://127.0.0.1:{srv.server_port}/f", timeout=5)
        finally:
            srv.shutdown()

    def test_delete_never_touches_other_chats(self):
        home = Path(tempfile.mkdtemp())
        other = home / ".claude" / "projects" / "p" / "99999999-0000-0000-0000-000000000000.jsonl"
        other.parent.mkdir(parents=True)
        other.write_text("x")
        write_meta(agent_dir(self.state, "a"), window_key("th1"), {"session_id": "11111111-2222-3333-4444-555555555555"})
        delete_chat(NoTmux(), self.state, "a", "th1", {"HOME": str(home)})
        self.assertTrue(other.exists())

    def test_attachments_sanitized_and_failures_marked(self):
        mesh = FakeChatMesh()
        client = ChatClient("https://mesh.test", "secret", opener=mesh)
        turn = {"turn_id": "t", "thread_id": "th", "settings": {}, "message": {"body": "see", "attachments": [
            {"attachment_id": "1", "file_name": "../../etc/pa ss.png", "download_url": "u1"},
            {"attachment_id": "2", "file_name": "b.txt", "download_url": "u2"}]}}

        def dl(url, dest, limit):
            if url == "u2":
                raise ValueError("too big")
            dest.write_text("img")
        files = fetch_attachments(client, turn, self.state, dl)
        self.assertEqual(files[0][1].parent, chat_dir(self.state, "th") / "files")
        self.assertNotIn("..", files[0][1].name)
        self.assertIsNone(files[1][1])
        text = build_prompt(turn, files)
        self.assertIn("could not be downloaded", text)

    def test_prompt_with_history_keeps_order_and_budget(self):
        hist = [{"role": "user", "body": "q1"}, {"role": "agent", "body": "a1"}]
        text = build_prompt({"message": {"body": "q2"}, "requested_by": {"display_name": "Ann"}}, [], hist)
        self.assertLess(text.index("q1"), text.index("a1"))
        self.assertTrue(text.endswith("Ann: q2"))


if __name__ == "__main__":
    unittest.main()
