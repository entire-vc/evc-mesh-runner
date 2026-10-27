import unittest
from subprocess import CompletedProcess

from mesh_runner.interactive import Tmux


class WindowsStrict(unittest.TestCase):
    def listing(self, code, out="", err=""):
        t = Tmux("tmux")
        t.run = lambda *a, **k: CompletedProcess(a, code, out, err)
        return t.windows_strict()

    def test_answer_is_filtered_to_runner_sessions(self):
        self.assertEqual(self.listing(0, "mr-A\tchat-x\nother\tw\n"), [("mr-A", "chat-x")])

    def test_only_no_server_is_an_empty_list(self):
        self.assertEqual(self.listing(1, err="no server running on /tmp/tmux-501/default"), [])
        self.assertEqual(self.listing(1, err="error connecting to /tmp/tmux-501/default (No such file or directory)"), [])

    def test_an_unusable_socket_or_any_other_failure_is_unreadable(self):
        self.assertIsNone(self.listing(1, err="error connecting to /tmp/tmux-501/default (Permission denied)"))
        self.assertIsNone(self.listing(1, err="error connecting to /tmp/tmux-501/default (Connection refused)"))
        self.assertIsNone(self.listing(1, err="TimeoutExpired: timed out"))


if __name__ == "__main__":
    unittest.main()
