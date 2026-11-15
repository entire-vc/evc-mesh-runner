"""Wait-root radar: the graph, the lane's start order, and what it never touches."""
import dataclasses
import datetime as dt
import http.server
import json
import threading
import time
import unittest

from fakes import AGENT_ID, FakeMesh, task as mesh_task
from test_runner import Bench
from test_queue_order import Helper

from mesh_runner import wait_roots
from mesh_runner.adapters.base import group_alive
from mesh_runner.config import AgentConfig
from mesh_runner.hostmem import StartRamp
from mesh_runner.mesh import MeshClient, MeshError
from mesh_runner.runner import AgentRunner
from mesh_runner.store import Store

NOW = dt.datetime(2026, 11, 11, 20, 0, tzinfo=dt.timezone.utc)
OURS = frozenset({"builder", "Steward"})


def tid(n):
    return f"{n:08x}-0000-4000-8000-{n:012x}"


def waiting(agent, target, since="2026-11-10T00:00:00Z", kind="card", **w):
    wait = {"kind": kind, "target": target[:8] if kind == "card" else target, "since": since, **w}
    if kind == "card":
        wait["target_id"] = target
    return {"agent": agent, "state": "waiting", "wait": wait, "updated_at": since}


def card(n, cat="todo", name="builder", atype="agent", gate=False, labels=()):
    return {"id": tid(n), "status_category": cat, "assignee_name": name, "assignee_type": atype,
            "assignee_id": "x" if name else None, "human_gate": gate, "labels": list(labels)}


class GraphTest(unittest.TestCase):
    def reader(self, cards):
        self.reads = []

        def read(task_id, agent):
            self.reads.append((task_id, agent))
            return cards.get(task_id)            # absent = unreadable with that lane's key
        return read

    def test_chain_ends_counted_and_classified(self):
        open_ = {
            tid(1): [waiting("A", tid(2), since="2026-11-01T00:00:00Z")],    # 1 -> 2 -> 3
            tid(2): [waiting("B", tid(3))],
            tid(4): [waiting("C", tid(3))],                                   # 4 -> 3
            tid(5): [waiting("A", tid(6))],                                   # 5 -> 6 (pipeline)
            tid(6): [waiting("D", "g/p#77", kind="pipeline")],
            tid(7): [waiting("A", tid(8))],                                   # 7 -> 8 (person)
            tid(9): [waiting("A", tid(10))],                                  # 9 -> 10 (other workspace)
            tid(11): [waiting("A", tid(12))],                                 # 11 -> 12 (being worked)
            tid(12): [{"agent": "E", "state": "running", "wait": {}, "updated_at": ""}],
            tid(13): [waiting("A", tid(14))],                                 # 13 -> 14 (gated, ours)
        }
        cards = {tid(3): card(3), tid(8): card(8, name="morgan", atype="user"),
                 tid(14): card(14, gate=True)}
        radar = wait_roots.build(open_, self.reader(cards), OURS, NOW)
        by = {r.task_id: r for r in radar.roots}
        self.assertEqual((by[tid(3)].kind, by[tid(3)].holder, by[tid(3)].behind), ("ready", "builder", 3))
        self.assertEqual(by[tid(3)].oldest, "2026-11-01T00:00:00Z")
        self.assertEqual((by[tid(6)].kind, by[tid(6)].holder), ("pipeline", "g/p#77"))
        self.assertEqual((by[tid(8)].kind, by[tid(8)].holder), ("human", "@morgan"))
        self.assertEqual(by[tid(10)].kind, "foreign")
        self.assertEqual((by[tid(12)].kind, by[tid(12)].holder), ("working", "E"))
        self.assertEqual(by[tid(14)].kind, "human")
        self.assertEqual(radar.weights(), {tid(3): 3})          # only the ready root of a lane weighs
        self.assertEqual(radar.card_waits, 8)
        self.assertEqual(radar.older, 1)                         # the 10-day-old wait on #1
        self.assertEqual(radar.roots[0].task_id, tid(3))         # heaviest first
        self.assertIn((tid(3), "B"), self.reads)                 # read with the waiting lane's key
        self.assertEqual(radar.depth, {1: 7, 2: 1})

    def test_startable_roots_by_lane_include_a_root_ready_to_resume(self):
        root, queued = tid(1), tid(2)
        open_ = {tid(10): [waiting("A", root)], tid(11): [waiting("A", root)],
                 tid(12): [waiting("A", queued)],
                 queued: [{"agent": "Steward", "state": "ready", "wait": {}, "updated_at": ""}]}
        radar = wait_roots.build(open_, self.reader({root: card(1)}), OURS, NOW)
        self.assertEqual(radar.weights(), {root: 2})                    # the lane's start order: unchanged
        self.assertEqual(radar.startable(), {"builder": {root: 2}, "Steward": {queued: 1}})

    def test_cycle_and_unknown_target_are_roots_without_weight(self):
        open_ = {tid(1): [waiting("A", tid(2))], tid(2): [waiting("B", tid(1))],
                 tid(3): [{"agent": "A", "state": "waiting", "wait": {"kind": "card", "target": "ffffffff"},
                           "updated_at": "2026-11-11T00:00:00Z"}]}
        radar = wait_roots.build(open_, self.reader({}), OURS, NOW)
        self.assertEqual(sorted(r.kind for r in radar.roots), ["cycle", "cycle", "unknown-target"])
        self.assertEqual(radar.weights(), {})

    def test_unread_card_is_never_ready(self):
        open_ = {tid(1): [waiting("A", tid(2))]}
        radar = wait_roots.build(open_, lambda t, a: {"_unread": True}, OURS, NOW)
        self.assertEqual(radar.roots[0].kind, "unread")
        self.assertEqual(radar.weights(), {})

    def test_status_and_render(self):
        open_ = {tid(1): [waiting("A", tid(2))], tid(4): [waiting("A", tid(2))]}
        radar = wait_roots.build(open_, self.reader({tid(2): card(2)}), OURS, NOW)
        st = wait_roots.status(radar)
        self.assertEqual(st["roots"][0], {"task": tid(2)[:8], "kind": "ready", "holder": "builder",
                                          "behind": 2, "oldest_wait_hours": 44.0})
        self.assertEqual(st["boosted"], {tid(2)[:8]: 2})
        self.assertIn("#00000002 ready", wait_roots.render(radar))

    def test_shared_end_keeps_each_lanes_access_result(self):
        for lanes in (("denied", "allowed"), ("allowed", "denied")):
            with self.subTest(lanes=lanes):
                reads = []

                def read(task_id, agent):
                    reads.append((task_id, agent))
                    return card(3) if agent == "allowed" else None

                radar = wait_roots.build({tid(i): [waiting(lane, tid(3))]
                                          for i, lane in enumerate(lanes, 1)}, read, OURS, NOW)
                self.assertEqual(reads, [(tid(3), lane) for lane in lanes])
                self.assertEqual(sorted((r.kind, r.behind) for r in radar.roots),
                                 [("foreign", 1), ("ready", 1)])
                self.assertEqual(radar.weights(), {tid(3): 1})
                self.assertEqual(radar.card_waits, 2)


class CardReaderTest(unittest.TestCase):
    def test_cache_is_scoped_to_lane_and_never_borrows_a_key(self):
        requests = []

        # HTTP boundary fixture: exercise real MeshClient diagnostic GETs with different keys.
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                key = self.headers.get("X-Agent-Key")
                requests.append((self.path, key))
                self.send_response(200 if key == "allowed" else 403)
                self.end_headers()
                self.wfile.write(json.dumps(card(3) if key == "allowed" else {}).encode())

            def log_message(self, *_):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        url = "http://%s:%d" % server.server_address
        clients = {lane: MeshClient(url, lane) for lane in ("allowed", "denied")}
        for lanes in (("denied", "allowed"), ("allowed", "denied")):
            with self.subTest(lanes=lanes):
                requests.clear()
                reader = wait_roots.CardReader(clients)
                for _ in range(2):
                    for lane in lanes:
                        self.assertEqual(reader(tid(3), lane), card(3) if lane == "allowed" else None)
                self.assertEqual(requests, [("/api/v1/tasks/" + tid(3), lane) for lane in lanes])
                self.assertEqual(reader(tid(3), "missing"), {"_unread": True})
                self.assertEqual(len(requests), 2)

    def test_forbidden_is_foreign_transport_error_is_unread(self):
        class C:
            def __init__(self, status):
                self.status, self.calls = status, 0

            def bounded_get(self, path, timeout):
                self.calls += 1
                raise MeshError(self.status, "x")
        r = wait_roots.CardReader({"A": C(403)})
        self.assertIsNone(r(tid(1), "A"))
        r = wait_roots.CardReader({"A": C(0)})
        self.assertEqual(r(tid(1), "A"), {"_unread": True})

    def test_budget(self):
        class C:
            def bounded_get(self, path, timeout):
                return {"id": path.rsplit("/", 1)[1]}
        r = wait_roots.CardReader({"A": C()}, budget=1)
        self.assertEqual(r(tid(1), "A"), {"id": tid(1)})
        self.assertEqual(r(tid(2), "A"), {"_unread": True})


class Cards:
    """bounded_get over a FakeMesh-like table; records every call."""
    def __init__(self, cards):
        self.cards, self.calls = cards, []

    def bounded_get(self, path, timeout):
        self.calls.append(path)
        t = path.rsplit("/", 1)[1]
        if t not in self.cards:
            raise MeshError(404, "not found")
        return self.cards[t]


class LaneOrderTest(Bench):
    """The radar on a live AgentRunner: start order, and the roots it must leave alone."""

    def setUp(self):
        super().setUp()
        self.light, self.heavy = mesh_task(1), mesh_task(2)        # same priority; light is first by age
        self.gated = mesh_task(3, human_gate=True)

    def radar(self, r, cards, waits):
        """Waiting attempts of another lane in the runner's store, then one radar pass over it."""
        store = Store(self.root / "state" / "r.sqlite")
        for i, (waiter, target) in enumerate(waits):
            a = store.create("other", waiter, "claude", "w")
            store.update(a, state="waiting", wait={"kind": "card", "target": target[:8], "target_id": target,
                                                   "since": "2026-11-05T00:00:00Z"})
        before = store.db.total_changes
        r.roots = wait_roots.RootRadar(self.cfg, clients={"other": Cards(cards)},
                                       db_path=self.root / "state" / "r.sqlite")
        r.roots.refresh()
        return store, before

    def ours(self, t, **kw):
        return dict(card(0), id=t["id"], **kw)

    def test_without_radar_own_order(self):
        r = self.runner(FakeMesh([self.light, self.heavy], max_concurrent=1))
        r.refresh_me()
        self.assertEqual([t["id"] for t in r.candidates()], [self.light["id"], self.heavy["id"]])

    def test_card_with_more_waiters_starts_first(self):
        mesh = FakeMesh([self.light, self.heavy], max_concurrent=1)
        r = self.runner(mesh)
        r.refresh_me()
        self.radar(r, {self.light["id"]: self.ours(self.light), self.heavy["id"]: self.ours(self.heavy)},
                   [(tid(100), self.light["id"]),
                    (tid(101), self.heavy["id"]), (tid(102), self.heavy["id"]), (tid(103), tid(101))])
        self.assertEqual(r.roots.weight(self.heavy["id"]), 3)    # 101, 102 and 103 (via 101)
        self.assertEqual(r.roots.weight(self.light["id"]), 1)
        self.assertEqual(r.start_new(1), 0)
        (a,) = r.store.by_state(self.agent.name, ("running",))
        self.assertEqual(a.task_id, self.heavy["id"])

    def test_queue_helper_cannot_override_heavier_root_start(self):
        helper = Helper(self.root, "reverse")
        self.agent.queue_order_command = helper.argv
        mesh = FakeMesh([self.light, self.heavy], max_concurrent=1)
        r = self.runner(mesh)
        r.refresh_me()
        self.radar(r, {self.light["id"]: self.ours(self.light), self.heavy["id"]: self.ours(self.heavy)},
                   [(tid(100), self.light["id"]), (tid(101), self.heavy["id"]),
                    (tid(102), self.heavy["id"])])
        r.start_new(1)
        (attempt,) = r.store.by_state(self.agent.name, ("running",))
        self.assertEqual(attempt.task_id, self.heavy["id"])
        self.assertTrue(helper.calls())

    def test_ready_work_root_starts_before_an_unrelated_review(self):
        review = mesh_task(4, cat="review")
        decision = mesh_task(5, labels=["decision-request"])
        mesh = FakeMesh([self.light, self.heavy, review, decision], max_concurrent=1)
        r = self.runner(mesh)
        r.refresh_me()
        plain = [review["id"], decision["id"], self.light["id"], self.heavy["id"]]
        self.assertEqual([t["id"] for t in r.candidates()], plain)          # no radar: order unchanged
        self.radar(r, {self.heavy["id"]: self.ours(self.heavy)},
                   [(tid(100), self.heavy["id"]), (tid(101), self.heavy["id"])])
        self.assertEqual(r.roots.weight(self.heavy["id"]), 2)
        got = [(t["id"], t["_mode"]) for t in r.candidates()]
        self.assertEqual(got, [(self.heavy["id"], "work"), (review["id"], "review"),
                               (decision["id"], "work"), (self.light["id"], "work")])
        self.assertEqual(r.start_new(1), 0)
        (a,) = r.store.by_state(self.agent.name, ("running",))
        self.assertEqual(a.task_id, self.heavy["id"])

    def test_gated_card_is_not_moved(self):
        mesh = FakeMesh([self.light, self.gated], max_concurrent=1)
        r = self.runner(mesh)
        r.refresh_me()
        self.radar(r, {self.gated["id"]: self.ours(self.gated)},       # the reader saw no gate yet
                   [(tid(100), self.gated["id"]), (tid(101), self.gated["id"])])
        self.assertEqual(r.roots.weight(self.gated["id"]), 2)
        self.assertEqual([t["id"] for t in r.candidates()], [self.light["id"], self.gated["id"]])

    def test_human_and_foreign_roots_get_no_start_and_no_write(self):
        person, foreign = tid(200), tid(201)
        mesh = FakeMesh([self.light], max_concurrent=2)
        r = self.runner(mesh)
        r.refresh_me()
        cards = {person: card(200, name="morgan", atype="user")}          # foreign: unreadable (404)
        store, before = self.radar(r, cards, [(tid(100), person), (tid(101), person), (tid(102), foreign)])
        kinds = {x.task_id: x.kind for x in r.roots.radar.roots}
        self.assertEqual(kinds, {person: "human", foreign: "foreign"})
        self.assertEqual(r.roots.radar.weights(), {})
        self.assertEqual(store.db.total_changes, before)                 # the pass wrote no store row
        comments_before = json.dumps(mesh.comment_log, sort_keys=True, default=str)
        r.start_new(2)
        started = {a.task_id for a in r.store.by_state(self.agent.name, ("running", "starting"))}
        self.assertEqual(started, {self.light["id"]})
        for root in (person, foreign):
            self.assertEqual(r.store.on_task(root), [])                 # no attempt, no record
            self.assertNotIn(root, mesh.comment_log)                     # no comment on the root
            self.assertNotIn(root, mesh.runtime)
        self.assertEqual(json.dumps({k: v for k, v in mesh.comment_log.items() if k != self.light["id"]},
                                    sort_keys=True, default=str),
                         json.dumps({k: v for k, v in json.loads(comments_before).items()
                                     if k != self.light["id"]}, sort_keys=True))
        self.assertTrue(all(p.startswith("/tasks/") for p in r.roots._reader.clients["other"].calls))


class FakeHostGate:
    """A host hold the test lifts by hand: what host_hold(), the look before a start and the ramp read."""
    def __init__(self):
        self.holding, self.released_at = True, None

    def check(self, fresh=False):
        return self.holding

    def describe(self):
        return "test hold"


class RampAcrossLanesTest(Bench):
    """After a host hold the ramp gave its one start per interval to whichever lane asked first;
    a root with six waiting cards behind it was passed by every card nobody waits on. Two lanes share
    the runner's gates, ramp and radar; the light lane asks first and the first slot still goes to the root."""

    def setUp(self):
        super().setUp()
        profile = self.root / "profile-steward"
        profile.mkdir()
        self.steward = AgentConfig(name="Steward", key="k2", harness="claude", profile_dir=profile,
                                   repos=list(self.agent.repos), env=dict(self.agent.env))
        self.cfg = dataclasses.replace(self.cfg, agents=[self.agent, self.steward])
        self.now = [1000.0]
        self.gate = FakeHostGate()
        self.ramp = StartRamp(20, 300, (self.gate,), clock=lambda: self.now[0])
        self.light, self.root_card = mesh_task(1), mesh_task(2)
        self.light_lane = self.lane(self.agent, FakeMesh([self.light], max_concurrent=1))
        self.root_lane = self.lane(self.steward, FakeMesh([self.root_card], max_concurrent=1))
        store = Store(self.root / "state" / "r.sqlite")
        for n in range(6):                              # six cards of a third lane wait on the root
            a = store.create("other", tid(100 + n), "claude", "w")
            store.update(a, state="waiting", wait={"kind": "card", "target": self.root_card["id"][:8],
                                                   "target_id": self.root_card["id"],
                                                   "since": "2026-11-14T13:44:40Z"})
        radar = wait_roots.RootRadar(self.cfg, db_path=self.root / "state" / "r.sqlite", clients={
            "other": Cards({self.root_card["id"]: dict(card(2, name="Steward"), id=self.root_card["id"])})})
        radar.refresh()
        for r in (self.light_lane, self.root_lane):
            r.roots = radar
        self.addCleanup(self.stop_steward)

    def lane(self, agent, mesh):
        r = AgentRunner(self.cfg, agent, Store(self.root / "state" / "r.sqlite"), mesh=mesh, adapter=self.adapter)
        self.__dict__.setdefault("_runners", []).append(r)
        r.memgate = r.loadgate = self.gate            # one host: the runner shares its gates and ramp
        r.ramp = self.ramp
        r.refresh_me()
        return r

    def stop_steward(self):
        for a in self.root_lane.store.by_state("Steward", ("starting", "running", "unknown")):
            if a.pid:
                self.root_lane._kill(a)
                deadline = time.monotonic() + 5
                while group_alive(a.pid, a.stamp) and time.monotonic() < deadline:
                    time.sleep(0.05)

    def started(self, r):
        return [a.task_id for a in r.store.by_state(r.agent.name, ("starting", "running", "unknown"))]

    def test_first_slot_after_a_hold_goes_to_the_heaviest_root_of_another_lane(self):
        self.assertEqual(self.light_lane.roots.weight(self.root_card["id"]), 6)
        for r in (self.light_lane, self.root_lane):    # the hold: both lanes tick, nothing starts
            r.tick()
        self.assertEqual(self.started(self.light_lane) + self.started(self.root_lane), [])
        self.gate.holding, self.gate.released_at = False, self.now[0]    # the hold lifts: the ramp opens
        self.now[0] += 1
        self.light_lane.tick()                          # the lane with the card nobody waits on asks first
        self.assertEqual(self.started(self.light_lane), [], "the first slot is kept for the root")
        with self.assertLogs("mesh_runner", "INFO") as logs:
            self.root_lane.tick()
        self.assertEqual(self.started(self.root_lane), [self.root_card["id"]])
        self.assertTrue(any("host ramp: start #%s goes first: a wait root with 6 waiting" % self.root_card["id"][:8]
                            in m for m in logs.output), logs.output)
        self.now[0] += 5
        self.light_lane.tick()
        self.assertEqual(self.started(self.light_lane), [], "one start per interval")
        self.now[0] += 20                               # the next slot: nothing heavier claims it
        self.light_lane.tick()
        self.assertEqual(self.started(self.light_lane), [self.light["id"]])

    def test_a_root_ready_to_resume_claims_the_slot_too(self):
        """The case of the alert: the root's own attempt was woken (state ready) and waited for a resume."""
        store = Store(self.root / "state" / "r.sqlite")
        a = store.create("Steward", self.root_card["id"], "claude", "root")
        store.update(a, state="ready")
        self.root_lane.roots.refresh()
        self.assertEqual(self.root_lane.roots.startable_for("Steward"), {self.root_card["id"]: 6})
        self.assertEqual(self.root_lane.roots.weight(self.root_card["id"]), 0, "a lane's start order is unchanged")
        self.root_lane._claim_ramp(1)                   # the hold: the lane claims its queued root
        self.gate.holding, self.gate.released_at = False, self.now[0]
        self.now[0] += 1
        self.assertFalse(self.ramp.ready(self.light["id"], 0))
        self.assertTrue(self.ramp.ready(self.root_card["id"], 6))
        self.root_lane._resume_hold[a.id] = time.monotonic() + 60
        self.root_lane._claim_ramp(1)                   # held locally: the claim is withdrawn at once
        self.assertTrue(self.ramp.ready(self.light["id"], 0))

    def test_a_root_its_lane_would_not_start_claims_nothing(self):
        """A root a start-time hold stops (here a dependency on an open card) must not keep the slot
        from the other lane: its own start path would refuse it."""
        blocker = mesh_task(3, cat="in_progress")
        blocker["assignee_id"] = "someone-else"
        mesh = self.root_lane.mesh
        mesh.tasks[blocker["id"]] = blocker
        mesh.deps[self.root_card["id"]] = [{"dependency_type": "blocks", "depends_on_task_id": blocker["id"]}]
        self.assertTrue(self.root_lane.hold_reason(dict(self.root_card, _mode="work"), "work", log_change=False))
        for r in (self.light_lane, self.root_lane):
            r.tick()
        self.gate.holding, self.gate.released_at = False, self.now[0]
        self.now[0] += 1
        self.light_lane.tick()
        self.assertEqual(self.started(self.light_lane), [self.light["id"]], "a held root claims nothing")

    def test_a_paused_lane_claims_nothing(self):
        self.root_lane.agent.paused = True               # positive control: the root's lane cannot start it
        for r in (self.light_lane, self.root_lane):
            r.tick()
        self.gate.holding, self.gate.released_at = False, self.now[0]
        self.now[0] += 1
        self.light_lane.tick()
        self.assertEqual(self.started(self.light_lane), [self.light["id"]], "a paused lane claims nothing")


if __name__ == "__main__":
    unittest.main()
