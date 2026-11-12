"""The chat runtime snapshot in the controller announcement: what Mesh is told about live chat sessions
and slots, and that a released slot is reported at once, not on the next minute."""
import threading
import time
import unittest

from mesh_runner.chat_announce import Announcer, announce
from mesh_runner.chat_host import runtime_snapshot
from mesh_runner.chat_machine import stop_window
from mesh_runner.chat_delivery import window_key
from mesh_runner.chat_pool import ChatPool
from mesh_runner.chat_protocol import ChatClient
from mesh_runner.chat_service import ChatService
from mesh_runner.mesh import MeshError
from fake_chat_mesh import FakeChatMesh

AGENT_ID, NAME = "agent-1", "builder"
SESSION = "mr-" + NAME


class FakeMesh:
    def __init__(self, refuse_snapshot=None):
        self.calls, self.refuse_snapshot = [], refuse_snapshot
        self.got = threading.Condition()

    def request(self, method, path, body=None, query=None):
        with self.got:
            self.calls.append((method, path, body))
            self.got.notify_all()
        if self.refuse_snapshot and "sessions" in body:
            raise MeshError(self.refuse_snapshot, "unknown field")
        return {}

    def wait_calls(self, n, timeout=3.0):
        with self.got:
            self.got.wait_for(lambda: len(self.calls) >= n, timeout)
        return len(self.calls)


class FakeTmux:
    """A tmux server: (session, window) -> pane alive. Readable unless ``broken``."""

    def __init__(self, panes):
        self.panes, self.broken = dict(panes), False
        self.stop_error = False

    def panes_strict(self):
        return None if self.broken else dict(self.panes)

    def windows(self, session):
        return {w: alive for (s, w), alive in self.panes.items() if s == session}

    def kill_window(self, session, window):
        if self.stop_error:  # mock: external boundary — tmux can fail to stop a live window
            raise OSError("tmux stop failed")
        self.panes.pop((session, window), None)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.t = [1000.0]
        self.pool = ChatPool(max_per_agent=3, max_host=6, idle_minutes=10, clock=lambda: self.t[0])
        self.tmux = FakeTmux({(SESSION, "card-0000aaaa"): True})        # a daemon (card) window of the agent
        self.svc = ChatService(None, [AGENT_ID], self.pool, lambda a: None,
                               lambda a, th: stop_window(self.tmux, None, NAME, th), lambda a, th: True)
        self.mesh = FakeMesh()
        self.announcer = Announcer({NAME: self.mesh}, "rid-1", every=3600.0,
                                   snapshot=lambda: runtime_snapshot(self.pool, self.tmux, {AGENT_ID: NAME}))
        self.svc.on_release = self.announcer.kick

    def open_session(self, thread, idle):
        self.assertTrue(self.pool.admit(AGENT_ID, thread).ok)
        self.tmux.panes[(SESSION, window_key(thread))] = True
        if idle:
            self.pool.end_turn(AGENT_ID, thread)

    @staticmethod
    def row(body, thread):
        return next(s for s in body["sessions"] if s["thread_id"] == thread)

    def run_loop(self):
        stop = threading.Event()
        th = threading.Thread(target=self.announcer.loop, args=(stop,), daemon=True)
        th.start()
        self.addCleanup(lambda: (stop.set(), th.join(3)))
        self.assertEqual(self.mesh.wait_calls(1), 1)                    # the round at start


class ReapReportsAtOnce(Fixture):
    def test_reap_of_an_idle_session_announces_it_stopped_and_the_slot_freed(self):
        self.open_session("thread-idle", idle=True)
        self.open_session("thread-live", idle=False)
        self.run_loop()
        before = self.mesh.calls[0][2]
        self.assertTrue(self.row(before, "thread-idle")["process_alive"])
        self.assertEqual(before["slots"], {"max_per_agent": 3, "used": 2})

        self.t[0] += 11 * 60                                            # past session_idle_minutes
        self.assertEqual(self.svc.reap_idle(), ["thread-idle"])
        # the loop waits an hour for its next round: only the reap's report can arrive now
        self.assertEqual(self.mesh.wait_calls(2, timeout=3.0), 2, "reap sent no announcement")
        method, path, after = self.mesh.calls[1]
        self.assertEqual((method, path), ("POST", "/agents/me/controller"))
        idle = self.row(after, "thread-idle")
        self.assertEqual((idle["process_alive"], idle["slot_held"], idle["busy"]), (False, False, False))
        self.assertEqual(after["slots"]["used"], 1)
        self.assertLess(after["slots"]["used"], before["slots"]["used"])
        self.assertTrue(self.row(after, "thread-live")["process_alive"])

    def test_stop_session_command_announces_at_once(self):
        acks = []
        self.svc.client = type("C", (), {"ack": lambda s, cid, res, *a: acks.append((cid, res))})()
        self.open_session("thread-x", idle=True)
        self.run_loop()
        self.svc._command({"kind": "stop_session", "command_id": "c1", "agent_id": AGENT_ID, "thread_id": "thread-x"})
        self.assertEqual(acks, [("c1", "done")])
        self.assertEqual(self.mesh.wait_calls(2), 2, "stop_session sent no announcement")
        row = self.row(self.mesh.calls[1][2], "thread-x")
        self.assertEqual((row["process_alive"], row["slot_held"]), (False, False))

    def test_a_crashed_turn_announces_its_freed_slot_at_once(self):
        class Boom:
            def __call__(self, req, timeout=None):  # mock: external boundary — unexpected HTTP transport crash
                raise RuntimeError("transport crashed")
        self.svc.client = ChatClient("https://mesh.test", "secret", opener=Boom())
        self.open_session("thread-crash", idle=False)
        self.run_loop()
        with self.assertLogs("mesh_runner", "WARNING") as logs:
            self.svc._run({"turn_id": "turn-crash-1", "agent_id": AGENT_ID, "thread_id": "thread-crash",
                           "claim_token": "c", "message": {"body": "hi"}})
        self.assertTrue(any("crashed: RuntimeError" in line for line in logs.output), logs.output)
        self.assertEqual(self.mesh.wait_calls(2), 2, "crashed turn sent no announcement")
        row = self.row(self.mesh.calls[1][2], "thread-crash")
        self.assertFalse(row["slot_held"])
        self.assertEqual(self.mesh.calls[1][2]["slots"]["used"], 0)


class StopErrorsReport(Fixture):
    def setUp(self):
        super().setUp()
        self.open_session("thread-x", idle=True)
        self.tmux.stop_error = True
        self.run_loop()

    def assert_release_reported(self):
        self.assertEqual(self.mesh.wait_calls(2), 2, "failed stop sent no release announcement")
        body = self.mesh.calls[1][2]
        row = self.row(body, "thread-x")
        self.assertEqual((row["process_alive"], row["slot_held"]), (True, False))
        self.assertEqual(body["slots"]["used"], 0)

    def test_reap_reports_the_freed_slot_when_tmux_stop_raises(self):
        self.t[0] += 11 * 60
        with self.assertRaisesRegex(OSError, "tmux stop failed"):
            self.svc.reap_idle()
        self.assert_release_reported()

    def command(self, kind):
        wire = FakeChatMesh()
        self.svc.client = ChatClient("https://mesh.test", "secret", opener=wire)
        self.svc._command({"kind": kind, "command_id": "c1", "agent_id": AGENT_ID,
                           "thread_id": "thread-x"})
        self.assertEqual(wire.acks["c1"]["result"], "failed")
        self.assert_release_reported()

    def test_stop_command_reports_the_freed_slot_when_tmux_stop_raises(self):
        self.command("stop_session")

    def test_delete_command_reports_the_freed_slot_when_tmux_stop_raises(self):
        self.command("delete_transcript")


class OrdinaryAnnouncement(Fixture):
    def test_active_session_is_alive_and_card_windows_are_not_reported(self):
        self.open_session("thread-live", idle=False)
        self.announcer.once()
        body = self.mesh.calls[0][2]
        self.assertEqual((body["runner_id"], body["name"]), ("rid-1", "mesh-runner"))
        self.assertEqual([s["thread_id"] for s in body["sessions"]], ["thread-live"])
        live = body["sessions"][0]
        self.assertEqual((live["agent"], live["process_alive"], live["busy"], live["slot_held"]),
                         (AGENT_ID, True, True, True))
        self.assertIsNone(live["last_end_at"])                          # no turn has ended yet
        self.assertEqual(body["slots"], {"max_per_agent": 3, "used": 1})
        self.assertTrue(body["reported_at"].endswith("Z"))

    def test_a_dead_pane_is_not_alive(self):
        self.open_session("thread-idle", idle=True)
        self.tmux.panes[(SESSION, window_key("thread-idle"))] = False
        self.announcer.once()
        row = self.mesh.calls[0][2]["sessions"][0]
        self.assertEqual((row["process_alive"], row["slot_held"]), (False, True))
        self.assertTrue(row["last_end_at"].startswith("1970-01-01T00:16:40"))

    def test_unreadable_tmux_sends_no_snapshot_rather_than_nothing_alive(self):
        self.open_session("thread-live", idle=False)
        self.tmux.broken = True
        self.announcer.once()
        body = self.mesh.calls[0][2]
        self.assertNotIn("sessions", body)
        self.assertEqual(body["runner_id"], "rid-1")

    def test_stopped_session_leaves_the_snapshot_after_the_keep_window(self):
        self.open_session("thread-idle", idle=True)
        self.pool.release(AGENT_ID, "thread-idle")
        self.assertEqual(len(self.pool.sessions()), 1)
        self.t[0] += 601
        self.assertEqual(self.pool.sessions(), [])


class SnapshotRefused(unittest.TestCase):
    SNAP = {"sessions": [], "slots": {"max_per_agent": 3, "used": 0}, "reported_at": "2026-11-10T20:00:00Z"}

    def test_a_server_refusing_the_snapshot_still_gets_the_lease(self):
        m = FakeMesh(refuse_snapshot=422)
        self.assertTrue(announce(m, "rid-1", host="h", snapshot=self.SNAP))
        self.assertEqual(len(m.calls), 2)
        self.assertIn("sessions", m.calls[0][2])
        self.assertNotIn("sessions", m.calls[1][2])
        self.assertEqual(m.calls[1][2]["runner_id"], "rid-1")

    def test_a_conflict_is_not_retried_without_the_snapshot(self):
        m = FakeMesh(refuse_snapshot=409)
        self.assertFalse(announce(m, "rid-1", host="h", snapshot=self.SNAP))
        self.assertEqual(len(m.calls), 1)

    def test_a_failing_snapshot_never_costs_the_announcement(self):
        m = FakeMesh()
        Announcer({"A": m}, "rid", snapshot=lambda: 1 / 0).once()
        self.assertEqual(len(m.calls), 1)
        self.assertNotIn("sessions", m.calls[0][2])


if __name__ == "__main__":
    unittest.main()
