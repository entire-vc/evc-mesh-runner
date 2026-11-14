"""A restore into another slot keeps a Claude session only behind the path guard.

The old slot is leased to other work while the attempt sleeps, so its restore lands elsewhere. With the guard
installable (an interactive Claude launch) the session continues: the moves are recorded, the launch names the
guard's map, and the first resumed turn is told. Everywhere else the session starts fresh, as before."""
import datetime as dt
import json
import os
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from mesh_runner import interactive, path_guard
from mesh_runner import prompt as prompts
from mesh_runner.adapters.codex import CodexAdapter
from mesh_runner.runner import AgentRunner
from fakes import FakeMesh, calls, task, write_replies, wait_time
from test_runner import Bench

WAIT = wait_time(why='external work')
BLOCK = "[runner] Your working copies moved while the task waited:"


class MovedRestore(Bench):
    def parked(self, n):
        t = task(n)
        self.cfg.workspace_wait_release_minutes = 30
        write_replies(self.replies, {t['id'][:8]: WAIT})
        r = self.runner(FakeMesh([t]))
        r.tick()
        self.settle_all(r)
        return r, r.store.by_state('builder', ('waiting',))[0]

    def suspend_and_take(self, r, a, intruder):
        """Suspend the attempt's slot, lease it to other work, wake the attempt: its restore must move."""
        a = r.store.get(a.id)
        wait = dict(a.wait, started=(dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).isoformat())
        r.store.update(a, wait=wait)
        old = list(a.workspaces)
        r._suspend_waiting(a)
        self.assertEqual(r.pool.leased_to(a.id), [])
        # the intruder names the slot: a pool that may still grow would otherwise leave it alone
        self.assertEqual([str(r.pool.lease(intruder, 'app', preferred_path=old[0]))], old)
        r.store.update(a, state='ready', wake_text=f'#{a.task8} dependency met')     # the fixture replies by id
        return old

    def wake(self, r, a, text='dependency met'):
        r.store.update(r.store.get(a.id), state='ready', wake_text=f'#{a.task8} {text}')
        r._resume_one(r.store.get(a.id))

    def spy_env(self, r):
        seen, spawn = [], r.adapter.spawn

        def spy(launch, log_path):
            seen.append(dict(launch.env))
            return spawn(launch, log_path)
        patcher = mock.patch.object(r.adapter, 'spawn', side_effect=spy)
        patcher.start()
        self.addCleanup(patcher.stop)
        return seen

    def events(self, r, a, kind):
        return [json.loads(d) for _, k, d in r.store.events(a.id) if k == kind]

    def guarded(self):
        """The launch can carry the guard (the fixture adapter stands for the interactive Claude driver)."""
        patcher = mock.patch.object(AgentRunner, '_path_guard_ready', return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        return patcher

    def map_file(self, a):
        return path_guard.moves_path(interactive.agent_dir(self.cfg.state_dir, 'builder'), a.id)

    def test_verdict_keeps_a_moved_session_only_where_the_guard_is_installed(self):
        r, a = self.parked(201)
        old = self.suspend_and_take(r, a, 'intruder')
        a = r.store.get(a.id)
        paths, restored = r.pool.restore(a.id, expected_repos={'app'})
        self.assertNotEqual(paths, old)
        verdict = lambda att, rest=restored: r._restore_session_verdict(att, paths, rest)
        # headless claude (``claude -p``): its settings get no PreToolUse guard from the runner
        self.assertEqual(verdict(a), (False, 'path_changed'))
        r.adapter = interactive.InteractiveAdapter(self.adapter, 'builder', self.cfg.state_dir)
        self.assertEqual(verdict(a), (True, 'workspace_restore_moved'))
        # codex, even interactive: no hook of this kind
        r.alt_adapters = {'codex': interactive.InteractiveAdapter(CodexAdapter(), 'builder', self.cfg.state_dir)}
        as_codex = [dict(m, metadata=dict(m['metadata'], harness='codex')) for m in restored]
        self.assertEqual(verdict(replace(a, harness='codex'), as_codex), (False, 'path_changed'))
        # the guard script itself is missing
        with mock.patch.object(interactive, 'GUARD', Path('/nonexistent/path_guard.py')):
            self.assertEqual(verdict(a), (False, 'path_changed'))
        # the checkpoint did not record the paths this session ran in
        other = [dict(m, metadata=dict(m['metadata'], workspaces=['/elsewhere/slot-9'])) for m in restored]
        self.assertEqual(verdict(a, other), (False, 'path_changed'))
        # the checks before the paths still decide first
        self.assertEqual(verdict(replace(a, session_id='')), (False, 'session_missing'))
        foreign = [dict(m, metadata=dict(m['metadata'], session_id='foreign')) for m in restored]
        self.assertEqual(verdict(a, foreign), (False, 'session_unverified'))
        moved_cwd = [dict(m, metadata=dict(m['metadata'], cwd='/old/cwd')) for m in restored]
        self.assertEqual(verdict(a, moved_cwd), (False, 'cwd_or_harness_changed'))

    def test_without_the_guard_a_moved_restore_starts_fresh_as_before(self):
        r, a = self.parked(202)
        sid = a.session_id
        self.suspend_and_take(r, a, 'intruder')
        r._resume_one(r.store.get(a.id))
        cur = r.store.get(a.id)
        self.assertNotEqual(cur.session_id, sid)
        self.assertEqual([(e['intent'], e['reason']) for e in self.events(r, a, 'restore_session')],
                         [('start', 'path_changed')])
        self.assertEqual(self.events(r, a, 'workspace_moved'), [])
        self.assertIsNone(r.store.mark('builder', a.task_id, AgentRunner.WORKSPACE_MOVES))
        self.settle_all(r)
        self.assertFalse(calls(self.calls)[-1]['resume'])
        self.assertNotIn(BLOCK, calls(self.calls)[-1]['prompt'])

    def test_a_guarded_move_keeps_the_session_tells_it_once_and_chains_later_moves(self):
        self.guarded()
        r, a = self.parked(203)
        sid = a.session_id
        env = self.spy_env(r)
        old = self.suspend_and_take(r, a, 'intruder-1')
        r._resume_one(r.store.get(a.id))
        first = r.store.get(a.id)
        self.assertEqual(first.session_id, sid)
        self.assertNotEqual(first.workspaces, old)
        self.assertEqual([(e['intent'], e['reason']) for e in self.events(r, a, 'restore_session')],
                         [('resume', 'workspace_restore_moved')])
        self.assertEqual(self.events(r, a, 'workspace_moved'), [{'count': 1}])
        told = os.path.realpath(first.workspaces[0])            # the path the session is told (issued)
        expected = {old[0]: told, os.path.realpath(old[0]): told}
        mark = json.loads(r.store.mark('builder', a.task_id, AgentRunner.WORKSPACE_MOVES)[0])
        self.assertEqual((mark['session_id'], mark['attempt'], mark['moves']), (sid, a.id, expected))
        # the launch names the map; the driver installs the guard from it
        self.assertEqual(path_guard.read(self.map_file(a)), expected)
        self.assertEqual(env[-1][interactive.PATH_GUARD_ENV], str(self.map_file(a)))
        self.settle_all(r)
        call = calls(self.calls)[-1]
        self.assertTrue(call['resume'])
        self.assertTrue(call['prompt'].startswith(BLOCK), call['prompt'][:300])
        self.assertIn(f"  {old[0]} -> {told}\n", call['prompt'])
        self.assertIn('dependency met', call['prompt'])

        # the next turn of the same session: guarded still, told no more
        self.wake(r, a, 'second wake')
        self.assertEqual(env[-1][interactive.PATH_GUARD_ENV], str(self.map_file(a)))
        self.settle_all(r)
        call = calls(self.calls)[-1]
        self.assertTrue(call['resume'])
        self.assertNotIn(BLOCK, call['prompt'])
        self.assertIn('second wake', call['prompt'])

        # a second move: the first old path and the first new one both lead to the newest slot
        middle = self.suspend_and_take(r, a, 'intruder-2')
        self.assertEqual(middle, first.workspaces)
        r._resume_one(r.store.get(a.id))
        last = r.store.get(a.id)
        self.assertEqual(last.session_id, sid)
        newest = os.path.realpath(last.workspaces[0])
        moves = path_guard.read(self.map_file(a))
        for stale in (old[0], os.path.realpath(old[0]), middle[0], os.path.realpath(middle[0])):
            self.assertEqual(moves[stale], newest, stale)
        self.assertNotIn(last.workspaces[0], moves)
        self.assertNotIn(newest, moves)
        self.settle_all(r)
        prompt = calls(self.calls)[-1]['prompt']
        self.assertTrue(prompt.startswith(BLOCK))
        self.assertIn(f"  {middle[0]} -> {newest}\n", prompt)
        self.assertNotIn(f"  {old[0]} ->", prompt)                 # told already, on the first move's turn

    def test_a_moved_session_is_not_resumed_by_a_launch_without_the_guard(self):
        guard = self.guarded()
        r, a = self.parked(204)
        sid = a.session_id
        self.suspend_and_take(r, a, 'intruder')
        r._resume_one(r.store.get(a.id))
        self.assertEqual(r.store.get(a.id).session_id, sid)
        self.settle_all(r)
        guard.stop()                         # the lane now launches headless: nothing would refuse the old path
        self.wake(r, a, 'later wake')
        cur = r.store.get(a.id)
        self.assertNotEqual(cur.session_id, sid)
        launch = self.events(r, a, 'launch')[-1]
        self.assertEqual((launch['intent'], launch['restart_reason']), ('start', 'path_changed'))
        self.settle_all(r)
        self.assertFalse(calls(self.calls)[-1]['resume'])
        # the new session never saw the old paths: its later resumes need no guard
        self.wake(r, a, 'after the fresh start')
        self.settle_all(r)
        self.assertTrue(calls(self.calls)[-1]['resume'])
        self.assertEqual(r.store.get(a.id).session_id, cur.session_id)


class MovedPromptBlock(unittest.TestCase):
    def test_lists_each_move_and_says_the_old_paths_are_refused(self):
        block = prompts.moved_workspaces_block({"/w/app/slot-1": "/w/app/slot-2", "/w/lib/slot-4": "/w/lib/slot-1"})
        self.assertTrue(block.startswith(BLOCK))
        self.assertIn("  /w/app/slot-1 -> /w/app/slot-2\n", block)
        self.assertIn("  /w/lib/slot-4 -> /w/lib/slot-1\n", block)
        self.assertIn("Paths in earlier messages are stale: use the new ones; the old ones belong to other work "
                      "and tool calls naming them are refused.", block)
        self.assertEqual(prompts.moved_workspaces_block({}), "")
