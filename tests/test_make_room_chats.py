"""Card #face4a7a: chat windows count against interactive_max_sessions but are never closed by make_room."""
import tempfile
import unittest
from pathlib import Path

from mesh_runner import tui_driver
from mesh_runner.interactive import write_meta


class FakeTmux:
    def __init__(self, windows):
        self.windows, self.killed = windows, []

    def all_windows(self):
        return list(self.windows)

    def kill_window(self, session, window):
        self.killed.append((session, window))


def room(windows, cap, root, idle=()):
    for session, window in windows:
        d = root / session[len("mr-"):]
        d.mkdir(parents=True, exist_ok=True)
        write_meta(d, window, {"last_end": 5.0 if window in idle else 1.0, "busy_pid": 0 if window in idle else 99999999})
    d = tui_driver.Driver.__new__(tui_driver.Driver)
    d.tmux, d.max_sessions, d.key, d.dir = FakeTmux(windows), cap, "new", root / "builder"
    orig = tui_driver.pid_alive
    tui_driver.pid_alive = lambda pid: pid == 99999999
    try:
        return d.make_room(), d.tmux.killed
    finally:
        tui_driver.pid_alive = orig


class MakeRoomChats(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_a_chat_window_takes_a_slot_of_the_cap(self):
        ok, killed = room([("mr-builder", "card-1"), ("mr-builder", "chat-a")], 2, self.root, idle=("card-1",))
        self.assertTrue(ok)
        self.assertEqual(killed, [("mr-builder", "card-1")])        # with the chat counted the cap is full

    def test_chats_are_never_closed_and_busy_cards_refuse_the_start(self):
        ok, killed = room([("mr-builder", "card-1"), ("mr-builder", "chat-a")], 2, self.root)
        self.assertFalse(ok)
        self.assertEqual(killed, [])

    def test_a_window_without_meta_under_our_root_is_never_closed(self):
        # a foreign runner's window: no meta file under this state root
        windows = [("mr-builder", "card-1"), ("mr-other", "card-9")]
        (self.root / "builder").mkdir(parents=True)
        write_meta(self.root / "builder", "card-1", {"last_end": 1.0, "busy_pid": 99999999})
        d = tui_driver.Driver.__new__(tui_driver.Driver)
        d.tmux, d.max_sessions, d.key, d.dir = FakeTmux(windows), 2, "new", self.root / "builder"
        orig = tui_driver.pid_alive
        tui_driver.pid_alive = lambda pid: pid == 99999999
        try:
            self.assertFalse(d.make_room())                          # wait, do not kill
        finally:
            tui_driver.pid_alive = orig
        self.assertEqual(d.tmux.killed, [])

    def test_below_the_cap_nothing_is_closed(self):
        ok, killed = room([("mr-builder", "card-1"), ("mr-builder", "chat-a")], 3, self.root, idle=("card-1",))
        self.assertTrue(ok)
        self.assertEqual(killed, [])


if __name__ == "__main__":
    unittest.main()


class WindowGauges(unittest.TestCase):
    def test_write_status_exports_card_and_chat_windows(self):
        from types import SimpleNamespace
        from mesh_runner import interactive, runner as rn
        with tempfile.TemporaryDirectory() as tmp:
            metrics = Path(tmp) / "m.prom"
            r = rn.Runner.__new__(rn.Runner)
            r.store = None
            r.cfg = SimpleNamespace(state_dir=Path(tmp), metrics_file=metrics, queue_wait_freshness_v1=False,
                                    pool_root=Path(tmp), checkpoint_budget_gb=1, tmux_binary="tmux",
                                    interactive_max_sessions=22)
            import mesh_runner.pool_ops as po
            real_summary, po.summary = po.summary, lambda store, root: {}
            orig = interactive.Tmux.all_windows
            interactive.Tmux.all_windows = lambda self: [("mr-a", "card-1"), ("mr-a", "chat-x"), ("mr-b", "card-2")]
            try:
                r.write_status([])
            finally:
                interactive.Tmux.all_windows = orig
                po.summary = real_summary
            text = metrics.read_text()
        self.assertIn('mesh_runner_interactive_windows{kind="card"} 2', text)
        self.assertIn('mesh_runner_interactive_windows{kind="chat"} 1', text)
        self.assertIn("mesh_runner_interactive_max_sessions 22", text)
