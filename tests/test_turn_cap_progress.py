"""At max_turns an attempt that still makes progress continues in a fresh session on the same agent."""
import dataclasses
import json
import tempfile
import unittest
from pathlib import Path

from mesh_runner import config as config_mod
from mesh_runner.workspace import DIRTY_HOLD_NOTE, RepoSpec, git
from fakes import AGENT_ID, FakeMesh, calls, next_turn, task, turn_rows, write_replies, wait_time
from test_runner import Bench
from test_workspace import make_repo


class TurnCapBench(Bench):
    def setUp(self):
        super().setUp()
        self.cfg.max_turns = 12
        self.cfg.daily_start_cap = 4
        self.agent.lead = "lead"

    def capped(self, r, t, outcomes, *, turns=None):
        """An attempt at its turn cap, the given outcomes already declared, about to resume
        (the point where the cap of a work attempt is applied)."""
        r.refresh_me()
        a = r.store.create(self.agent.name, t["id"], "claude")
        for kind in outcomes:
            r.store.outcome(a, kind)
        r.store.update(a, state="ready", turns=turns or self.cfg.max_turns, session_id="s-cap",
                       wake_text="continue")
        r._resume_one(r.store.get(a.id))
        return r.store.get(a.id)

    def declare(self, mesh, t, turn):
        """Mesh holds the turn command ``turn`` as the capped turn's typed outcome (and its wait)."""
        row, wait = turn_rows(turn)
        mesh.outcomes = {t["id"]: [row]}
        mesh.waits = {t["id"]: wait} if wait else {}

    def last_turn(self, r, t, outcomes, turn):
        """The capped turn itself settles through the production path, declaring ``turn``."""
        self.declare(r.mesh, t, turn)
        text = "done for now"
        r.refresh_me()
        a = r.store.create(self.agent.name, t["id"], "claude")
        for kind in outcomes:
            r.store.outcome(a, kind)
        path = self.root / f"{a.id}.jsonl"
        path.write_text(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                                    "result": text, "session_id": "s-cap"}) + "\n")
        r.store.update(a, state="running", turns=self.cfg.max_turns, log_path=str(path))
        r.settle(a)
        return r.store.get(a.id)

    def bodies(self, mesh, t):
        return [c["body"] for c in mesh.comment_log.get(t["id"], [])]


class Continue(TurnCapBench):
    def test_progressing_attempt_continues_on_the_same_agent_in_a_new_attempt(self):
        t = task(1, cat="in_progress")
        mesh = FakeMesh([t], max_concurrent=1)
        write_replies(self.replies, {t["id"][:8]: wait_time(why="soak")})
        r = self.runner(mesh)
        a = self.capped(r, t, ["step", "step", "next", "wait", "step", "wait", "next", "step", "wait"])
        self.assertEqual(a.state, "completed")
        self.assertTrue(a.note.startswith("turn cap: continued in a fresh session"), a.note)
        self.assertEqual(mesh.tasks[t["id"]]["assignee_id"], AGENT_ID)         # no lead
        self.assertEqual(self.bodies(mesh, t), [])                              # no hand-up comment
        self.assertIn(("turn_cap", "continued: " + a.note), [(k, d) for _, k, d in r.store.events(a.id)])
        self.settle_all(r)                                                      # the next tick starts it afresh
        attempts = [x for x in r.store.by_state(self.agent.name, ("running", "waiting", "completed", "ready"))
                    if x.task_id == t["id"]]
        self.assertEqual(len(attempts), 2)
        fresh = next(x for x in attempts if x.id != a.id)
        self.assertEqual(fresh.turns, 1)
        self.assertEqual(r.store.starts_today(self.agent.name, t["id"]), 2)     # counted as a start

    def test_capped_turn_declaring_a_wait_waits_then_continues_on_wake(self):
        t = task(1, cat="in_progress")
        mesh = FakeMesh([t], max_concurrent=1)
        write_replies(self.replies, {t["id"][:8]: wait_time(why="soak")})
        r = self.runner(mesh)
        a = self.last_turn(r, t, ["step", "next", "step"], wait_time(why="soak"))
        self.assertEqual(a.state, "waiting")                       # the wait is recorded, not lost to the cap
        self.assertEqual(mesh.tasks[t["id"]]["assignee_id"], AGENT_ID)
        self.assertEqual(self.bodies(mesh, t), [])
        self.assertEqual(r.store.recent_outcomes(a.id, 1), ["wait"])
        r.store.update(a, state="ready", wake_text="the wait is over")        # the wake
        r._resume_one(r.store.get(a.id))
        a = r.store.get(a.id)
        self.assertEqual(a.state, "completed")
        self.assertTrue(a.note.startswith("turn cap: continued in a fresh session"), a.note)
        self.assertEqual(mesh.tasks[t["id"]]["assignee_id"], AGENT_ID)
        self.assertEqual(self.bodies(mesh, t), [])

    def test_capped_turn_declaring_a_wait_hands_up_on_wake_when_stagnant(self):
        t = task(1, cat="in_progress")
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        a = self.last_turn(r, t, ["wait"] * 5, wait_time(why="soak"))
        self.assertEqual(a.state, "waiting")
        self.assertEqual(mesh.tasks[t["id"]]["assignee_id"], AGENT_ID)
        r.store.update(a, state="ready", wake_text="the wait is over")
        r._resume_one(r.store.get(a.id))
        a = r.store.get(a.id)
        self.assertIn("routed to lead", a.note)
        self.assertEqual(mesh.tasks[t["id"]]["assignee_id"], "agent-lead")
        self.assertIn("no recent progress", self.bodies(mesh, t)[0])

    def test_capped_turn_declaring_next_counts_and_continues(self):
        t = task(1, cat="in_progress")
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        a = self.last_turn(r, t, ["step", "step"], next_turn("more to do"))
        self.assertEqual(a.state, "ready")                         # the NEXT is recorded and counted
        self.assertEqual(r.store.recent_outcomes(a.id, 1), ["next"])
        r._resume_one(a)
        a = r.store.get(a.id)
        self.assertTrue(a.note.startswith("turn cap: continued in a fresh session"), a.note)
        self.assertEqual(mesh.tasks[t["id"]]["assignee_id"], AGENT_ID)

    def test_a_review_attempt_is_capped_at_settle_before_the_outcome_is_read(self):
        """Only a work attempt has its cap moved to the resume; a review is stopped when the turn settles."""
        t = task(1, cat="review")
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        r.refresh_me()
        self.declare(mesh, t, wait_time(why="soak"))
        a = r.store.create(self.agent.name, t["id"], "claude", mode="review")
        path = self.root / f"{a.id}.jsonl"
        path.write_text(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                                    "result": "idle",
                                    "session_id": "s-cap"}) + "\n")
        r.store.update(a, state="running", turns=self.cfg.max_turns, log_path=str(path))
        r.settle(a)
        a = r.store.get(a.id)
        self.assertNotEqual(a.state, "waiting")
        self.assertIn(f"stopped after {self.cfg.max_turns} turns", a.note)
        self.assertEqual(r.store.recent_outcomes(a.id, 1), [])                 # the wait was never recorded
        self.assertNotIn("turn_cap", [k for _, k, _ in r.store.events(a.id)])

    def test_stagnant_attempt_goes_to_the_lead_as_before(self):
        t = task(1, cat="in_progress")
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        a = self.capped(r, t, ["wait"] * 8 + ["step", "next"])
        self.assertEqual(mesh.tasks[t["id"]]["assignee_id"], "agent-lead")
        self.assertIn("routed to lead", a.note)
        self.assertTrue(any("RUNNER GAVE UP" in b for b in self.bodies(mesh, t)))
        self.assertIn("no recent progress", self.bodies(mesh, t)[0])

    def test_only_the_last_ten_outcomes_count(self):
        t = task(1, cat="in_progress")
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        a = self.capped(r, t, ["step"] * 5 + ["wait"] * 10)       # old progress, then ten idle waits
        self.assertEqual(mesh.tasks[t["id"]]["assignee_id"], "agent-lead")
        self.assertIn("routed to lead", a.note)

    def test_progress_min_is_configurable(self):
        t = task(1, cat="in_progress")
        mesh = FakeMesh([t], max_concurrent=1)
        self.cfg.turn_cap_progress_min = 2
        r = self.runner(mesh)
        a = self.capped(r, t, ["wait", "step", "wait", "next"])
        self.assertTrue(a.note.startswith("turn cap: continued"), a.note)

    def test_spent_start_cap_stops_the_continuation(self):
        t = task(1, cat="in_progress")
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        r.refresh_me()
        for _ in range(self.cfg.daily_start_cap - 1):              # three earlier starts today
            earlier = r.store.create(self.agent.name, t["id"], "claude")
            r.store.update(earlier, state="completed", turns=1, note="turn cap: continued in a fresh session")
        a = self.capped(r, t, ["step"] * 9)                        # this is the fourth
        self.assertEqual(r.store.starts_today(self.agent.name, t["id"]), self.cfg.daily_start_cap)
        self.assertEqual(mesh.tasks[t["id"]]["assignee_id"], "agent-lead")
        self.assertIn("routed to lead", a.note)
        self.assertIn("start cap reached", self.bodies(mesh, t)[0])

    def test_continuation_chain_ends_at_the_start_cap(self):
        """Every capped attempt keeps progressing; the restarts stop at daily_start_cap."""
        t = task(1, cat="in_progress")
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        for n in range(1, self.cfg.daily_start_cap + 1):
            a = self.capped(r, t, ["step"] * 9)
            self.assertEqual(mesh.tasks[t["id"]]["assignee_id"],
                             "agent-lead" if n == self.cfg.daily_start_cap else AGENT_ID, n)
        self.assertIn("routed to lead", a.note)

    def test_without_a_start_cap_the_loop_would_be_unbounded_so_it_hands_up(self):
        t = task(1, cat="in_progress")
        mesh = FakeMesh([t], max_concurrent=1)
        self.cfg.daily_start_cap = 0
        r = self.runner(mesh)
        a = self.capped(r, t, ["step"] * 9)
        self.assertEqual(mesh.tasks[t["id"]]["assignee_id"], "agent-lead")
        self.assertIn("daily_start_cap is off", self.bodies(mesh, t)[0])
        self.assertIn("routed to lead", a.note)


class CarriedSlot(TurnCapBench):
    """A continued attempt's dirty working copy goes on with the card: the next start of the same card
    gets it at the same path, leased, with the work in progress untouched."""

    def unfinished_merge(self, slot):
        """An unfinished merge with a conflict, a staged new file and ignored state in ``slot``."""
        g = lambda *a, **k: git(slot, "-c", "user.name=t", "-c", "user.email=t@example.com", *a, **k)
        g("checkout", "-q", "-b", "side")
        (slot / "README").write_text("side\n")
        g("commit", "-qam", "side")
        g("checkout", "-q", "-b", "work", "origin/main")
        (slot / "README").write_text("work\n")
        g("commit", "-qam", "work")
        g("merge", "side", check=False)                         # conflict: MERGE_HEAD stays
        (slot / "staged.txt").write_text("staged\n")
        g("add", "staged.txt")
        (slot / "cache").mkdir(exist_ok=True)
        (slot / "cache" / "state").write_text("ignored state\n")

    def snapshot(self, slot):
        gitdir = Path(git(slot, "rev-parse", "--absolute-git-dir"))
        return {"head": git(slot, "rev-parse", "HEAD"),
                "merge_head": (gitdir / "MERGE_HEAD").read_text() if (gitdir / "MERGE_HEAD").exists() else "",
                "status": git(slot, "status", "--porcelain", "--untracked-files=all", "--ignored"),
                "readme": (slot / "README").read_text(),
                "ignored": (slot / "cache" / "state").read_text() if (slot / "cache" / "state").exists() else ""}

    def dirty_capped(self, r, t):
        """An attempt at its turn cap, still progressing, whose slot holds an unfinished merge."""
        r.refresh_me()
        a = r.store.create(self.agent.name, t["id"], "claude")
        slot = r.pool.lease(a.id, "app")
        r.store.update(a, workspaces=[str(slot)])
        self.unfinished_merge(slot)
        before = self.snapshot(slot)
        self.assertTrue(before["merge_head"])
        for kind in ["step"] * 9:
            r.store.outcome(a, kind)
        r.store.update(a, state="ready", turns=self.cfg.max_turns, session_id="s-cap", wake_text="continue")
        r._resume_one(r.store.get(a.id))
        a = r.store.get(a.id)
        self.assertTrue(a.note.startswith("turn cap: continued in a fresh session"), a.note)
        self.assertEqual(self.row(r, slot)[1:], ("held", a.id, DIRTY_HOLD_NOTE))
        return a, slot, before

    def row(self, r, slot):
        x = next(x for x in r.pool.rows() if x["path"] == str(slot))
        return x["path"], x["state"], x["attempt"], x["note"]

    def started(self, r, t, capped):
        r.tick()
        fresh = [x for x in r.store.on_task(t["id"]) if x.id != capped.id and x.agent == self.agent.name]
        self.assertEqual(len(fresh), 1, [(x.id[:8], x.state, x.note) for x in fresh])
        return fresh[0]

    def test_next_start_of_the_card_gets_its_dirty_slot_leased_at_the_same_path(self):
        t = task(1, cat="in_progress")
        mesh = FakeMesh([t], max_concurrent=1)
        write_replies(self.replies, {t["id"][:8]: "SLEEP:30:done"})
        r = self.runner(mesh)
        a, slot, before = self.dirty_capped(r, t)
        b = self.started(r, t, a)
        self.assertEqual(b.workspaces, [str(slot)])                             # same path, not a new slot
        self.assertEqual(self.row(r, slot)[1:3], ("leased", b.id))
        self.assertEqual(len(r.pool.rows()), 1)                                 # no second slot was made
        self.assertEqual(self.snapshot(slot), before)                           # the work is untouched
        self.assertEqual(r.store.mark(self.agent.name, t["id"], r.TURN_CAP_CARRY)[0], "")   # spent by the launch
        self.assertIn("turn_cap_carry", [k for _, k, _ in r.store.events(b.id)])

    def test_a_start_that_aborts_before_its_turn_passes_the_slot_on(self):
        """A start refused after its lease (the admission gate) leaves the slot held by an attempt that never
        ran a turn; the carry is not spent, and the start after it still gets the same working copy."""
        t = task(1, cat="in_progress")
        mesh = FakeMesh([t], max_concurrent=1)
        write_replies(self.replies, {t["id"][:8]: "SLEEP:30:done"})
        r = self.runner(mesh)
        a, slot, before = self.dirty_capped(r, t)
        real_gate, calls = r._gate, []

        def gate(phase, *args, **kw):
            if phase == "admit" and not calls:
                calls.append(phase)
                return "held for the test", None
            return real_gate(phase, *args, **kw)
        r._gate = gate
        b = self.started(r, t, a)
        self.assertEqual((b.state, b.turns), ("aborted", 0), b.note)
        self.assertEqual(self.row(r, slot)[1:], ("held", b.id, DIRTY_HOLD_NOTE))
        r._gate_hold.clear()
        r._task_backoff.clear()
        r.tick()
        c = next(x for x in r.store.on_task(t["id"]) if x.id not in (a.id, b.id))
        self.assertEqual(c.workspaces, [str(slot)])
        self.assertEqual(self.row(r, slot)[1:3], ("leased", c.id))
        self.assertEqual(self.snapshot(slot), before)

    def test_a_launch_readiness_hold_keeps_the_inherited_slot_leased(self):
        """The launch is refused for readiness (the attempt stays ready for its repair): only fresh leases
        go back to the pool; the inherited working copy stays leased to that attempt, untouched."""
        t = task(1, cat="in_progress")
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        a, slot, before = self.dirty_capped(r, t)

        def refused(att, *args, **kw):
            r.store.update(att, state="ready", note="launch readiness exec-denied: test")
            return False
        r._spawn = refused
        b = self.started(r, t, a)
        b = r.store.get(b.id)
        self.assertEqual((b.state, b.turns), ("ready", 0), b.note)
        self.assertEqual(b.workspaces, [str(slot)])
        self.assertEqual(self.row(r, slot)[1:3], ("leased", b.id))
        self.assertEqual(self.snapshot(slot), before)

    def test_a_launch_readiness_hold_returns_the_fresh_leases_of_the_other_repositories(self):
        """A card on two repositories, only one of them carried: the readiness hold keeps the inherited
        working copy and returns the fresh lease of the other repository to the pool."""
        second_root = self.root / "second-repo"
        second_root.mkdir()
        self.agent.repos.append(RepoSpec("second", make_repo(second_root), max_slots=1))
        t = task(1, cat="in_progress")
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        a, slot, before = self.dirty_capped(r, t)

        def refused(att, *args, **kw):
            r.store.update(att, state="ready", note="launch readiness exec-denied: test")
            return False
        r._spawn = refused
        b = r.store.get(self.started(r, t, a).id)
        self.assertEqual((b.state, b.turns), ("ready", 0), b.note)
        self.assertEqual(b.workspaces, [str(slot)])
        self.assertEqual(self.row(r, slot)[1:3], ("leased", b.id))
        second = [(x["state"], x["attempt"]) for x in r.pool.rows() if x["pool"].endswith("/second")]
        self.assertNotIn(("leased", b.id), second, "the fresh lease of the other repository went back")
        self.assertFalse(r.pool.full("second"))
        self.assertEqual(self.snapshot(slot), before)

    def test_a_full_pool_does_not_hold_the_start_that_inherits(self):
        """The pool at its maximum, the card's own dirty slot among the rows: the start is not held for a
        free slot it does not need."""
        self.agent.repos[0] = dataclasses.replace(self.agent.repos[0], max_slots=1)
        t = task(1, cat="in_progress")
        mesh = FakeMesh([t], max_concurrent=1)
        write_replies(self.replies, {t["id"][:8]: "SLEEP:30:done"})
        r = self.runner(mesh)
        a, slot, before = self.dirty_capped(r, t)
        self.assertTrue(r.pool.full("app"))
        b = self.started(r, t, a)
        self.assertEqual(b.workspaces, [str(slot)])
        self.assertEqual(self.row(r, slot)[1:3], ("leased", b.id))
        self.assertEqual(self.snapshot(slot), before)

    def test_another_card_never_gets_the_held_slot(self):
        t, other = task(1, "low", cat="in_progress"), task(2, "urgent", cat="in_progress")
        mesh = FakeMesh([t, other], max_concurrent=1)                            # room for ``other`` only
        write_replies(self.replies, {other["id"][:8]: "SLEEP:30:done"})
        r = self.runner(mesh)
        a, slot, before = self.dirty_capped(r, t)
        b = self.started(r, other, a)
        self.assertEqual([x.id for x in r.store.on_task(t["id"])], [a.id])
        self.assertNotIn(str(slot), b.workspaces)
        self.assertEqual(self.row(r, slot)[1:], ("held", a.id, DIRTY_HOLD_NOTE))
        self.assertEqual(self.snapshot(slot), before)

    def test_a_card_closed_after_the_cap_inherits_nothing_and_the_dead_slot_pass_frees_it(self):
        t = task(1, cat="in_progress")
        mesh = FakeMesh([t], max_concurrent=1)
        write_replies(self.replies, {t["id"][:8]: "SLEEP:30:done"})
        r = self.runner(mesh)
        a, slot, before = self.dirty_capped(r, t)
        mesh.set_category(t["id"], "done")
        self.assertEqual(r.free_dead_slots(), [str(slot)])                       # as before: archived, freed
        self.assertEqual(self.row(r, slot)[1:], ("free", "", ""))
        r.tick()                                                                 # a closed card is not started
        self.assertEqual([x.id for x in r.store.on_task(t["id"])], [a.id])
        refs = git(self.agent.repos[0].base, "for-each-ref", "--format=%(refname)",
                   "refs/mesh-runner/archive").split()
        self.assertEqual(len(refs), 1)
        mesh.set_category(t["id"], "in_progress")                                # reopened later
        b = self.started(r, t, a)
        after = self.snapshot(Path(b.workspaces[0]))
        self.assertEqual((after["merge_head"], after["readme"]), ("", "x\n"))  # a fresh checkout, no old work
        self.assertEqual(self.row(r, Path(b.workspaces[0]))[1:3], ("leased", b.id))

    def test_a_stagnant_cap_carries_nothing(self):
        """Handed up (no recent progress): no carry is written, the held slot stays with the old attempt."""
        t = task(1, cat="in_progress")
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        r.refresh_me()
        a = r.store.create(self.agent.name, t["id"], "claude")
        slot = r.pool.lease(a.id, "app")
        r.store.update(a, workspaces=[str(slot)])
        self.unfinished_merge(slot)
        for kind in ["wait"] * 10:
            r.store.outcome(a, kind)
        r.store.update(a, state="ready", turns=self.cfg.max_turns, session_id="s-cap", wake_text="continue")
        r._resume_one(r.store.get(a.id))
        self.assertIsNone(r.store.mark(self.agent.name, t["id"], r.TURN_CAP_CARRY))


class ConfigKeys(unittest.TestCase):
    def load(self, extra=""):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        key = Path(d.name) / "k"
        key.write_text("secret")
        key.chmod(0o600)
        p = Path(d.name) / "runner.toml"
        p.write_text(f'[runner]\nmesh_url = "http://mesh"\nstate_dir = "{d.name}/s"\n{extra}\n'
                     f'[[agents]]\nname = "a"\nkey_file = "{key}"\nprofile_dir = "{d.name}"\n')
        return config_mod.load(p)

    def test_default_progress_min(self):
        self.assertEqual(self.load().turn_cap_progress_min, 3)

    def test_progress_min_is_fixed(self):
        # no longer a [runner] option: a file that sets it keeps 3
        with self.assertLogs("mesh_runner", "WARNING") as logs:
            self.assertEqual(self.load("turn_cap_progress_min = 5\n").turn_cap_progress_min, 3)
        self.assertIn("turn_cap_progress_min is no longer an option", "\n".join(logs.output))
