"""A turn that ends while one start leases or restores working copies gives its reservation back
before that start ends.

The passes between the units of a tick's start loop (``_between``) wait for the whole unit, and
one start of a lane with twelve repositories took up to 14 minutes on the fleet host (0.2.106):
the restore of every working copy runs inside a single unit. Two turns that ended in
it kept their capacity reservations for 2.5 minutes, until the next unit. Now the pool calls the
runner's release pass between repositories (``WorkspacePool.between``), outside every repository
lock, and a failing pass never fails the lease or the restore.
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import test_mid_tick_settle as mts
from fakes import task
from test_runner import Bench
from mesh_runner.store import Store
from mesh_runner.workspace import RepoSpec, WorkspacePool, base_lock
from test_workspace import make_repo


class StartUnitRelease(Bench):
    # the fixture of test_mid_tick_settle (gate hook logging reserve/admit/end), not its tests
    released, held_turns, end_turn = (mts.MidTickSettle.released, mts.MidTickSettle.held_turns,
                                      mts.MidTickSettle.end_turn)

    def setUp(self):
        super().setUp()
        # mock: external boundary — the operator's gate hook (server reservation reserve/admit/end) logs its calls
        self.gate_log = self.root / "gate.log"
        hook = self.root / "hook.sh"
        hook.write_text(f'#!/bin/sh\necho "$MESH_RUNNER_PHASE $MESH_RUNNER_TASK" >> {self.gate_log}\n'
                        'echo RES_ID=r1\n')
        hook.chmod(0o755)
        self.agent.start_gate_command = [str(hook)]
        self.agent.turn_end_command = [str(hook)]

    def test_a_turn_ending_during_a_starts_lease_gives_its_slot_back_before_that_start_spawns(self):
        r, mesh, (t1,), (a,), (harness,) = self.held_turns()
        t2 = task(2)
        mesh.tasks[t2["id"]] = t2
        mesh.max_concurrent = 2
        real_lease, real_spawn, at_spawn = r.pool.lease, r._spawn, []

        def lease(attempt_id, repo, **kwargs):
            if attempt_id != a.id and not at_spawn and self.released(t1) == 0:
                self.end_turn(r, a, harness)                       # the turn ends inside the unit
            return real_lease(attempt_id, repo, **kwargs)

        def spawn(b, *args, **kwargs):
            if b.task_id == t2["id"]:
                at_spawn.append((self.released(t1), r.store.get(a.id).state))
            return real_spawn(b, *args, **kwargs)

        # mock: none; the wrappers only observe the real lease and spawn of the second card's start
        with mock.patch.object(r.pool, "lease", side_effect=lease), \
                mock.patch.object(r, "_spawn", side_effect=spawn):
            r.tick()
        self.assertEqual(at_spawn, [(1, "running")])   # back before the same start spawned, not settled
        r.settle_finished()                                        # the next tick's settle step
        self.assertEqual((r.store.get(a.id).state, self.released(t1)), ("waiting", 1))

    def test_no_release_pass_inside_a_start_before_the_ticks_settle_step(self):
        r, mesh, (t1,), (a,), (harness,) = self.held_turns()
        self.end_turn(r, a, harness)
        r._settle_armed = False                                    # a new tick, its settle step not run yet
        r.pool.between_repos()
        self.assertEqual(self.released(t1), 0)
        self.assertEqual(r.store.get(a.id).state, "running")


class PoolBetweenRepos(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        bases = []
        for name in ("a", "b"):
            (root / name).mkdir()
            bases.append(make_repo(root / name))
        self.store = Store(root / "state.sqlite")
        self.addCleanup(self.store.db.close)
        self.pool = WorkspacePool(self.store, root / "pool",
                                  [RepoSpec("app", bases[0], max_slots=2), RepoSpec("lib", bases[1], max_slots=2)],
                                  identity=("Agent", "agent@example.com"), owner="agent")
        self.bases = bases
        for repo in ("app", "lib"):
            self.pool.lease("one", repo)
        self.pool.suspend("one", {"session_id": "s"}, writer_stopped=True)

    def phases(self):
        return [p for (p,) in self.store.db.execute(
            "SELECT phase FROM workspace_checkpoints WHERE attempt='one' ORDER BY repo")]

    def test_the_hook_runs_after_each_restored_repository_outside_its_lock(self):
        seen = []

        def hook():
            free = []
            for base in self.bases:                                # every repository lock is free
                with base_lock(base):
                    free.append(True)
            seen.append((self.phases(), free))
        self.pool.between = hook
        paths, _ = self.pool.restore("one", expected_repos={"app", "lib"})
        self.assertEqual(len(paths), 2)
        self.assertEqual(seen, [(["restored", "released"], [True, True]),
                                (["restored", "restored"], [True, True])])

    def test_a_failing_hook_does_not_fail_the_restore(self):
        self.pool.between = mock.Mock(side_effect=RuntimeError("boom"))
        with self.assertLogs("mesh_runner", "WARNING") as logs:
            paths, _ = self.pool.restore("one", expected_repos={"app", "lib"})
        self.assertEqual(len(paths), 2)
        self.assertEqual(self.pool.between.call_count, 2)
        self.assertIn("between-repositories hook failed: boom", "\n".join(logs.output))
        self.assertEqual(self.phases(), ["restored", "restored"])


if __name__ == "__main__":
    unittest.main()
