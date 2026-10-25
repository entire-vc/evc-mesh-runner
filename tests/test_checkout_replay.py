"""The checkout acquire survives a lost response: the same (session_id, request_id) is replayed."""
import uuid

from mesh_runner.mesh import Checkout, MeshError
from mesh_runner.runner import CHECKOUT_PENDING, AgentRunner
from mesh_runner.store import Store
from fakes import FakeMesh, task
from test_runner import Bench

TIMEOUT = MeshError(0, "TimeoutError: The read operation timed out")


class ReplayMesh(FakeMesh):
    """Mesh's POST /tasks/:id/checkout: a repeat with the holder's session_id and request_id
    on a live lease returns that lease; any other request on a live lease is a 409."""

    def __init__(self, tasks):
        super().__init__(tasks)
        self.leases: dict[str, dict] = {}
        self.posts: list[tuple[str, str]] = []       # (session_id, request_id) as sent
        self.lose = 0                                 # answers to drop after the lease is recorded
        self.errors: list[MeshError] = []             # raised before the lease is recorded

    def checkout(self, tid, session_id, request_id, ttl_minutes=120):
        self.posts.append((session_id, request_id))
        if self.errors:
            raise self.errors.pop(0)
        lease = self.leases.get(tid)
        if lease and lease["live"]:
            if (lease["session"], lease["request"]) != (session_id, request_id):
                raise MeshError(409, "Task is already checked out")
        elif lease and (lease["session"], lease["request"]) == (session_id, request_id):
            raise MeshError(409, "Task is already checked out")      # a released or expired scope is not replayed
        else:
            lease = self.leases[tid] = {"session": session_id, "request": request_id, "live": True,
                                        "token": str(uuid.uuid4())}
        self.checkouts[tid] = {"session": session_id}
        if self.lose:
            self.lose -= 1
            raise TIMEOUT
        return Checkout(tid, lease["token"], 1, "", session_id, request_id)

    def release(self, co):
        super().release(co)
        if co.task_id in self.leases:
            self.leases[co.task_id]["live"] = False

    def other_holds(self, tid):
        self.leases[tid] = {"session": "another-session", "request": str(uuid.uuid4()), "live": True,
                            "token": str(uuid.uuid4())}
        self.checkouts[tid] = {"session": "another-session"}


class CheckoutReplay(Bench):
    def setUp(self):
        super().setUp()
        self.t = task(1)
        self.mesh = ReplayMesh([self.t])
        self.r = self.runner(self.mesh)

    def attempt(self, r=None):
        return (r or self.r).store.create("builder", self.t["id"], "claude", "t")

    def test_lost_response_is_replayed_with_the_same_request_id(self):
        self.mesh.lose = 1
        a = self.attempt()
        self.assertFalse(self.r._acquire(a))
        self.assertEqual(self.r.store.get(a.id).checkout, {})
        self.assertTrue(self.r._acquire(a))
        first, second = self.mesh.posts
        self.assertEqual(first, second)
        self.assertEqual(first[0], a.id)
        self.assertEqual(self.r.store.get(a.id).checkout["token"], self.mesh.leases[self.t["id"]]["token"])
        self.assertEqual(len(self.mesh.leases), 1)

    def test_a_server_error_is_also_an_unknown_outcome(self):
        a = self.attempt()
        self.mesh.errors = [MeshError(504, "gateway timeout")]
        self.assertFalse(self.r._acquire(a))
        self.assertIsNotNone(self.r._pending_checkout(a))
        self.assertTrue(self.r._acquire(a))

    def test_success_clears_the_pending_id(self):
        a = self.attempt()
        self.assertTrue(self.r._acquire(a))
        self.assertIsNone(self.r._pending_checkout(a))
        self.assertEqual(self.r.store.mark("builder", self.t["id"], CHECKOUT_PENDING)[0], "")

    def test_a_failed_save_of_the_granted_lease_keeps_the_pending_id(self):
        # the lease is persisted before the replay ids are cleared: if saving it fails, the
        # pending pair is still there and the next acquire replays the same request
        a = self.attempt()
        original = self.r.store.update

        def failing_update(attempt, **kw):
            if "checkout" in kw:
                raise RuntimeError("database is locked")
            return original(attempt, **kw)
        self.r.store.update = failing_update
        with self.assertRaises(RuntimeError):
            self.r._acquire(a)
        self.assertIsNotNone(self.r._pending_checkout(a))
        self.r.store.update = original
        self.assertTrue(self.r._acquire(a))
        self.assertEqual(self.mesh.posts[0], self.mesh.posts[1])
        self.assertEqual(len(self.mesh.leases), 1)

    def test_pending_id_is_written_before_the_request(self):
        a = self.attempt()
        seen = []
        original = self.mesh.checkout

        def spy(*args, **kw):
            seen.append(self.r._pending_checkout(a))
            return original(*args, **kw)
        self.mesh.checkout = spy
        self.assertTrue(self.r._acquire(a))
        self.assertEqual(seen, [(self.mesh.posts[0][0], self.mesh.posts[0][1])])

    def test_pending_id_survives_a_runner_restart(self):
        self.mesh.lose = 1
        a = self.attempt()
        self.assertFalse(self.r._acquire(a))
        r2 = AgentRunner(self.cfg, self.agent, Store(self.root / "state" / "r.sqlite"), mesh=self.mesh,
                         adapter=self.adapter)
        a2 = r2.store.get(a.id)
        self.assertTrue(r2._acquire(a2))
        self.assertEqual(self.mesh.posts[0], self.mesh.posts[1])
        self.assertEqual(len(self.mesh.leases), 1)

    def test_a_later_attempt_replays_the_lost_request_of_an_aborted_one(self):
        # _start_attempt aborts the attempt on a refused acquire; the next start is a new attempt
        self.mesh.lose = 1
        a1 = self.attempt()
        self.assertFalse(self.r._acquire(a1))
        self.r.store.update(a1, state="aborted", note="checkout refused")
        a2 = self.attempt()
        self.assertNotEqual(a1.id, a2.id)
        self.assertTrue(self.r._acquire(a2))
        self.assertEqual(self.mesh.posts[0], self.mesh.posts[1])
        co = self.r.store.get(a2.id).checkout
        self.assertEqual((co["session_id"], co["token"]), (a1.id, self.mesh.leases[self.t["id"]]["token"]))
        self.r._release_checkout(self.r.store.get(a2.id))        # heartbeat and release carry the stored session
        self.assertFalse(self.mesh.leases[self.t["id"]]["live"])

    def test_conflict_with_another_session_is_final_and_not_replayed(self):
        self.mesh.other_holds(self.t["id"])
        a = self.attempt()
        self.assertFalse(self.r._acquire(a))
        self.assertIsNone(self.r._pending_checkout(a))
        self.assertFalse(self.r._acquire(a))
        (s1, r1), (s2, r2) = self.mesh.posts
        self.assertNotEqual(r1, r2)                  # a refused id is never sent again
        self.assertEqual(self.r.store.get(a.id).checkout, {})

    def test_a_lost_request_then_a_conflict_forgets_the_stale_ids(self):
        self.mesh.errors = [TIMEOUT]
        a = self.attempt()
        self.assertFalse(self.r._acquire(a))
        lost = self.r._pending_checkout(a)
        self.mesh.other_holds(self.t["id"])          # the lease that never existed is now someone else's
        self.assertFalse(self.r._acquire(a))
        self.assertIsNone(self.r._pending_checkout(a))
        replayed, fresh = self.mesh.posts[1], self.mesh.posts[2]
        self.assertEqual(replayed, lost)
        self.assertNotEqual(fresh[1], lost[1])       # one retry with new ids in the same acquire

    def test_a_replay_of_an_expired_lease_starts_over_in_the_same_acquire(self):
        self.mesh.lose = 1
        a = self.attempt()
        self.assertFalse(self.r._acquire(a))
        self.mesh.leases[self.t["id"]]["live"] = False        # the server TTL ran out
        del self.mesh.checkouts[self.t["id"]]
        self.assertTrue(self.r._acquire(a))
        self.assertEqual(len(self.mesh.posts), 3)
        self.assertNotEqual(self.mesh.posts[0][1], self.mesh.posts[2][1])
        self.assertIsNone(self.r._pending_checkout(a))
