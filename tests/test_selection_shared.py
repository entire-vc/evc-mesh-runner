import unittest

from mesh_runner.mesh import MeshError
from mesh_runner import selection
from mesh_runner.selection import StatusCategories, open_blockers


class Member:
    def statuses(self, pid):
        return [{"id": "st-done-x1", "category": "done"}, {"id": "st-todo-x1", "category": "todo"}]


class Outsider:
    def statuses(self, pid):
        raise MeshError(403, "agent is not a member of this project")


class SharedCategories(unittest.TestCase):
    def setUp(self):
        # readers of other tests must not answer here
        self._readers, self._shared = list(selection._READERS), dict(selection._SHARED)
        selection._READERS.clear()
        selection._SHARED.clear()
        selection._NOBODY.clear()
        selection._READ_BY.clear()

    def tearDown(self):
        for r in self._readers:
            selection._READERS.add(r)
        selection._SHARED.update(self._shared)

    def test_an_outsider_reads_a_blocker_status_another_agent_already_knows(self):
        outsider = StatusCategories(Outsider())
        blocker = {"project_id": "p-x1", "status_id": "st-done-x1"}
        with self.assertRaises(MeshError):          # no agent of this process can read it: unknown
            outsider.category(blocker)
        StatusCategories(Member()).category({"project_id": "p-x1", "status_id": "st-todo-x1"})
        self.assertEqual(outsider.category(blocker), "done")

    def test_an_outsider_asks_another_agent_of_the_process_that_can_read_it(self):
        outsider = StatusCategories(Outsider())
        member = StatusCategories(Member())          # exists, has not read that project
        blocker = {"project_id": "p-x3", "status_id": "st-done-x1"}
        self.assertEqual(outsider.category(blocker), "done")
        del member

    def test_an_unreadable_blocker_card_is_read_by_another_agent(self):
        class Me(Outsider):
            def dependencies(self, tid):
                return {"outgoing": [{"dependency_type": "blocks", "depends_on_task_id": "b1"}]}

            def task(self, tid):
                raise MeshError(403, "forbidden")

        class Other(Member):
            def task(self, tid):
                return {"id": tid, "project_id": "p-x4", "status_id": "st-done-x1"}
        mine = StatusCategories(Me())
        with self.assertRaises(MeshError):
            open_blockers(mine.mesh, mine, "t1")
        other = StatusCategories(Other())
        self.assertEqual(open_blockers(mine.mesh, mine, "t1"), [])     # closed: not blocking
        del other

    def test_other_errors_still_raise(self):
        class Broken:
            def statuses(self, pid):
                raise MeshError(500, "boom")
        StatusCategories(Member()).category({"project_id": "p-x2", "status_id": "st-done-x1"})
        with self.assertRaises(MeshError):
            StatusCategories(Broken()).category({"project_id": "p-x2", "status_id": "st-done-x1"})


class _Spent(Exception):
    pass


class UniqueSweepAcrossAttempts(unittest.TestCase):
    """The cross-tenant uniqueness sweep asks every other reader once. At fleet size
    (65 readers, 76.7 s measured) it outlasts one wake's 45 s read budget; without memory of who
    already answered, every attempt restarted from zero and the wait was never pinned."""

    def setUp(self):
        self._readers = list(selection._READERS)
        selection._READERS.clear()
        selection._NOBODY.clear()
        selection._READ_BY.clear()
        getattr(selection, "_SWEPT", {}).clear()
        getattr(selection, "_SWEEP_LOCKS", {}).clear()

    def tearDown(self):
        selection._READERS.clear()
        for r in self._readers:
            selection._READERS.add(r)
        getattr(selection, "_SWEPT", {}).clear()
        getattr(selection, "_SWEEP_LOCKS", {}).clear()

    def _fleet(self, n, *, budget, collide_at=None):
        """``n`` other readers, all denied except ``collide_at`` (another card under the same ref);
        ``budget["left"]`` reads are allowed per attempt, after that a read fails as the spent budget
        does (MeshError status 0, kind timeout)."""
        asked = []

        class Reader:
            def __init__(self, i):
                self.i = i

            def task_by_short(self, short):
                if budget["left"] <= 0:
                    raise MeshError(0, "observation budget spent", kind="timeout")
                budget["left"] -= 1
                asked.append(self.i)
                if self.i == collide_at:
                    return {"id": "other-tenant-card"}
                raise MeshError(403, "not a member")

        me = StatusCategories(object())
        others = [StatusCategories(Reader(i)) for i in range(n)]
        return me, others, asked

    def test_known_id_sweep_converges_over_attempts(self):
        budget = {"left": 0}
        me, others, asked = self._fleet(10, budget=budget)
        read = lambda r: r.mesh.task_by_short("0000aaaa")         # noqa: E731
        budget["left"] = 6                                          # first attempt: budget for 6 reads
        with self.assertRaises(MeshError):                          # not all asked: identity unknown
            selection._read_unique_elsewhere("s:0000aaaa", me.mesh, read, known_id="own-card")
        budget["left"] = 6                                          # next attempt: only the 4 left
        got = selection._read_unique_elsewhere("s:0000aaaa", me.mesh, read, known_id="own-card")
        self.assertEqual(got, {"id": "own-card"})
        self.assertEqual(sorted(asked), list(range(10)))            # each reader asked exactly once
        del others

    def test_a_collision_found_in_an_earlier_attempt_is_kept(self):
        budget = {"left": 3}
        me, others, asked = self._fleet(6, budget=budget, collide_at=1)
        read = lambda r: r.mesh.task_by_short("0000aaaa")         # noqa: E731
        results = []
        for _ in range(2):                                          # two attempts cover all 6 readers
            budget["left"] = 3
            try:
                results.append(selection._read_unique_elsewhere("s:0000aaaa", me.mesh, read,
                                                                known_id="own-card"))
            except MeshError:
                results.append("fault")
        # whichever attempt meets the colliding reader: AMBIGUOUS then, and it stays AMBIGUOUS
        # on the next attempt from the remembered answer, without asking that reader again
        self.assertIs(results[-1], selection.AMBIGUOUS)
        self.assertLessEqual(len(asked), 6)
        self.assertEqual(len(asked), len(set(asked)))               # nobody asked twice
        del others

    def test_answers_expire(self):
        budget = {"left": 10}
        me, others, asked = self._fleet(3, budget=budget)
        read = lambda r: r.mesh.task_by_short("0000aaaa")         # noqa: E731
        selection._read_unique_elsewhere("s:0000aaaa", me.mesh, read, known_id="own-card")
        until, answers = selection._SWEPT["s:0000aaaa"]
        selection._SWEPT["s:0000aaaa"] = (0.0, answers)             # expired
        selection._read_unique_elsewhere("s:0000aaaa", me.mesh, read, known_id="own-card")
        self.assertEqual(len(asked), 6)                             # asked again after expiry
        del others

    def test_unpinned_result_is_read_fresh_not_from_the_remembered_answer(self):
        state = {"status": "todo", "left": 1}

        class Holder:
            def task_by_short(self, short):
                return {"id": "the-card", "status": state["status"]}

        class Denied:
            def task_by_short(self, short):
                if state["left"] <= 0:
                    raise MeshError(0, "observation budget spent", kind="timeout")
                state["left"] -= 1
                raise MeshError(403, "not a member")

        me = StatusCategories(object())
        holder = StatusCategories(Holder())
        denied = [StatusCategories(Denied()) for _ in range(3)]
        read = lambda r: r.mesh.task_by_short("0000aaaa")         # noqa: E731
        with self.assertRaises(MeshError):
            selection._read_unique_elsewhere("s:0000aaaa", me.mesh, read)
        state["status"], state["left"] = "done", 5
        got = selection._read_unique_elsewhere("s:0000aaaa", me.mesh, read)
        self.assertEqual(got, {"id": "the-card", "status": "done"})
        del holder, denied

    def test_a_remembered_reader_that_now_reads_another_card_is_not_returned(self):
        state = {"id": "pinned-card", "left": 1}

        class Holder:
            def task_by_short(self, short):
                return {"id": state["id"]}

        class Denied:
            def task_by_short(self, short):
                if state["left"] <= 0:
                    raise MeshError(0, "observation budget spent", kind="timeout")
                state["left"] -= 1
                raise MeshError(403, "not a member")

        me = StatusCategories(object())
        holder = StatusCategories(Holder())
        denied = [StatusCategories(Denied()) for _ in range(3)]
        read = lambda r: r.mesh.task_by_short("0000aaaa")         # noqa: E731
        for pinned in ("", "pinned-card"):
            with self.subTest(pinned=pinned):
                selection._SWEPT.clear()
                state["id"], state["left"] = "pinned-card", 1
                try:                                            # remembers the holder's answer
                    selection._read_unique_elsewhere("s:0000aaaa", me.mesh, read, pinned=pinned)
                except MeshError:
                    pass
                self.assertIn(holder, selection._SWEPT["s:0000aaaa"][1])
                state["id"], state["left"] = "another-card", 5  # the ref now reads as another card
                got = selection._read_unique_elsewhere("s:0000aaaa", me.mesh, read, pinned=pinned)
                self.assertNotEqual((got or {}).get("id"), "another-card")
                if pinned:
                    self.assertIsNone(got)
        del holder, denied
    def test_concurrent_sweeps_ask_each_reader_once(self):
        import threading as th
        gate, asked = th.Event(), []

        class Slow:
            def __init__(self, i):
                self.i = i

            def task_by_short(self, short):
                asked.append(self.i)
                gate.wait(2)                                    # hold the first sweep open
                raise MeshError(403, "not a member")

        me = StatusCategories(object())
        others = [StatusCategories(Slow(i)) for i in range(4)]
        read = lambda r: r.mesh.task_by_short("0000aaaa")         # noqa: E731
        out = []
        call = lambda: out.append(selection._read_unique_elsewhere(  # noqa: E731
            "s:0000aaaa", me.mesh, read, known_id="own-card"))
        threads = [th.Thread(target=call) for _ in range(2)]
        for t in threads:
            t.start()
        gate.set()
        for t in threads:
            t.join(10)
        self.assertEqual(out, [{"id": "own-card"}, {"id": "own-card"}])
        self.assertEqual(sorted(asked), [0, 1, 2, 3])               # the second sweep asked nobody again
        del others

    def test_a_wait_for_a_running_sweep_is_bounded_by_the_read_budget(self):
        from mesh_runner import observation_budget
        lock = selection._SWEEP_LOCKS.setdefault("s:0000bbbb", __import__("threading").Lock())
        lock.acquire()
        try:
            me = StatusCategories(object())
            with observation_budget.observe(0.2):
                with self.assertRaises(MeshError) as cm:
                    selection._read_unique_elsewhere("s:0000bbbb", me.mesh, lambda r: None, known_id="x")
            self.assertEqual(cm.exception.kind, "timeout")
        finally:
            lock.release()

if __name__ == "__main__":
    unittest.main()
