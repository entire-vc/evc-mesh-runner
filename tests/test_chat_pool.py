import unittest

from mesh_runner.chat_pool import ChatPool


class Pool(unittest.TestCase):
    def setUp(self):
        self.t = [1000.0]
        self.p = ChatPool(max_per_agent=2, max_host=3, idle_minutes=10, clock=lambda: self.t[0])

    def test_warm_thread_keeps_its_slot_and_busy_thread_waits(self):
        self.assertTrue(self.p.admit("a", "t1").ok)
        self.assertFalse(self.p.admit("a", "t1").ok)         # one turn per thread
        self.p.end_turn("a", "t1")
        adm = self.p.admit("a", "t1")
        self.assertTrue(adm.ok and adm.evict is None)

    def test_agent_limit_evicts_longest_idle_then_queues(self):
        self.p.admit("a", "t1"); self.t[0] += 5; self.p.end_turn("a", "t1")
        self.p.admit("a", "t2"); self.t[0] += 5; self.p.end_turn("a", "t2")
        adm = self.p.admit("a", "t3")
        self.assertTrue(adm.ok)
        self.assertEqual(adm.evict.thread, "t1")             # idlest first
        self.p.admit("a", "t2")                              # both live slots busy now
        q = self.p.admit("a", "t4")
        self.assertEqual((q.ok, q.reason, q.position), (False, "agent_limit", 1))
        self.assertEqual(self.p.admit("a", "t5").position, 2)

    def test_host_limit_counts_card_sessions(self):
        used = [2]
        p = ChatPool(max_per_agent=5, max_host=3, host_used=lambda: used[0], clock=lambda: self.t[0])
        self.assertTrue(p.admit("a", "t1").ok)
        self.assertEqual(p.admit("b", "t2").reason, "host_limit")

    def test_idle_expiry_frees_the_process(self):
        self.p.admit("a", "t1"); self.p.end_turn("a", "t1")
        self.t[0] += 601
        self.assertEqual([s.thread for s in self.p.expired()], ["t1"])
        self.assertTrue(self.p.release("a", "t1"))
        self.assertEqual(self.p.expired(), [])

    def test_busy_session_never_expires(self):
        self.p.admit("a", "t1"); self.t[0] += 9999
        self.assertEqual(self.p.expired(), [])


if __name__ == "__main__":
    unittest.main()
