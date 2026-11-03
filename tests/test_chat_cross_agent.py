"""Two real ChatHosts (one per agent) over one shared cap-1 ChatPool: a session evicted for another
agent's chat is stopped by the host that owns it, and a Stop that fails leaves no busy slot behind."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

from fake_chat_mesh import FakeChatMesh
from mesh_runner import interactive
from mesh_runner.chat_delivery import window_key
from mesh_runner.chat_host import ChatHost
from mesh_runner.chat_protocol import ChatClient

AGP, ADEX = "id-agp", "id-adex"


class Tmux:
    def __init__(self):
        self.live, self.fail = set(), False

    def windows(self, session):
        return {w: True for s, w in self.live if s == session}

    def windows_strict(self):
        return sorted(self.live)

    def all_windows(self):
        return sorted(self.live)

    def kill_window(self, session, window):
        if self.fail:
            raise OSError("tmux gone")
        self.live.discard((session, window))


class CrossAgent(unittest.TestCase):
    def setUp(self):
        state = Path(tempfile.mkdtemp())
        self.mesh = FakeChatMesh(agents=(AGP, ADEX))
        runner = NS(cfg=NS(tmux_binary="tmux", state_dir=state),
                    agents=[NS(agent=NS(name="AGP")), NS(agent=NS(name="adex"))])
        self.tmux = Tmux()
        self.pool = None
        self.hosts = {}
        for aid, name in ((AGP, "AGP"), (ADEX, "adex")):
            client = ChatClient("https://mesh.test", "secret", opener=self.mesh)
            h = ChatHost(runner, client, {aid: name}, max_per_agent=1, host_cap=1, idle_minutes=10,
                         pool=self.pool)
            self.pool = h.pool
            h.tmux = self.tmux
            h.service.spawn = lambda fn: None          # no worker process: the pool and Stop are under test
            self.hosts[aid] = h
        self.pool.clock = lambda: self.t[0]

    t = [1000.0]

    def window(self, name, thread):
        return (interactive.session_name(name), window_key(thread))

    def open_chat(self, aid, name, thread):
        self.mesh.queue_turn("hi", agent=aid, thread=thread)
        self.assertEqual(self.hosts[aid].service.step(1), 1)
        self.tmux.live.add(self.window(name, thread))
        self.end_turn(aid, thread)
        self.t[0] += 1

    def end_turn(self, aid, thread):
        svc = self.hosts[aid].service           # what the worker's finally-block does when a turn ends
        svc.running.clear()
        svc.running_on.clear()
        for t in self.mesh.turns.values():
            if t["thread_id"] == thread and t["state"] == "claimed":
                t["state"] = "done"
        self.pool.end_turn(aid, thread)

    def test_switch_agp_to_adex_stops_the_owners_window_and_back(self):
        self.open_chat(AGP, "AGP", "t-a")
        self.mesh.queue_turn("yo", agent=ADEX, thread="t-b")
        self.assertEqual(self.hosts[ADEX].service.step(1), 1)           # was KeyError: 'id-agp'
        self.assertEqual(self.tmux.live, set())                         # AGP's window stopped by its owner
        self.assertEqual(set(self.pool.slots), {(ADEX, "t-b")})
        self.tmux.live.add(self.window("adex", "t-b"))
        self.end_turn(ADEX, "t-b")
        self.mesh.queue_turn("back", agent=AGP, thread="t-a")
        self.assertEqual(self.hosts[AGP].service.step(1), 1)
        self.assertEqual(set(self.pool.slots), {(AGP, "t-a")})
        self.assertNotIn(self.window("adex", "t-b"), self.tmux.live)

    def test_same_agent_eviction(self):
        self.pool.max_per_agent = 1
        self.open_chat(AGP, "AGP", "t-1")
        self.mesh.queue_turn("x", agent=AGP, thread="t-2")
        self.assertEqual(self.hosts[AGP].service.step(1), 1)
        self.assertEqual(set(self.pool.slots), {(AGP, "t-2")})
        self.assertNotIn(self.window("AGP", "t-1"), self.tmux.live)

    def test_all_busy_queues_the_turn_and_returns_the_claim(self):
        self.mesh.queue_turn("one", agent=AGP, thread="t-a")
        self.assertEqual(self.hosts[AGP].service.step(1), 1)            # busy: no turn ended
        turn = self.mesh.queue_turn("two", agent=ADEX, thread="t-b")
        self.assertEqual(self.hosts[ADEX].service.step(1), 0)
        self.assertEqual(self.pool.queue, [(ADEX, "t-b")])
        self.assertEqual(turn["state"], "queued")
        self.assertEqual(set(self.pool.slots), {(AGP, "t-a")})

    def test_idle_reap_stops_each_owners_sessions_only(self):
        self.pool.max_host = 2
        self.open_chat(AGP, "AGP", "t-a")
        self.open_chat(ADEX, "adex", "t-b")
        self.t[0] += 11 * 60
        self.assertEqual(self.hosts[AGP].service.reap_idle(), ["t-a"])
        self.assertEqual(self.tmux.live, {self.window("adex", "t-b")})
        self.assertEqual(self.hosts[ADEX].service.reap_idle(), ["t-b"])
        self.assertEqual(self.tmux.live, set())
        self.assertEqual(self.pool.slots, {})

    def test_external_stop_failure_leaves_no_busy_slot_and_returns_the_claim(self):
        self.open_chat(AGP, "AGP", "t-a")
        self.tmux.fail = True
        turn = self.mesh.queue_turn("yo", agent=ADEX, thread="t-b")
        self.assertEqual(self.hosts[ADEX].service.step(1), 0)
        self.assertEqual(turn["state"], "queued")                       # claim handed back
        self.assertEqual(set(self.pool.slots), {(AGP, "t-a")})          # no busy slot without a worker
        self.assertFalse(self.pool.slots[(AGP, "t-a")].busy)
        self.assertEqual(self.hosts[ADEX].service.running, set())


if __name__ == "__main__":
    unittest.main()
