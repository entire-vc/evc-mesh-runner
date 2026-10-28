"""CI progress breaks idle-wait streaks, without forgiving work or churn caps."""
import copy
import datetime as dt
import sqlite3
import urllib.parse
from unittest import mock

from mesh_runner.store import iso
from mesh_runner.wake import GitLab
from fakes import AGENT_ID, FakeMesh, task, write_replies
from test_runner import Bench


class GitLabHTTP(GitLab):
    """External boundary: provider HTTP responses; all GitLab evaluation stays real."""
    def __init__(self):
        super().__init__('https://git.example', 'test')
        self.rows = {}
        self.mrs = {}
        self.errors = 0

    def get(self, path):
        if self.errors:
            self.errors -= 1
            raise OSError("provider temporarily unavailable")
        project, resource, number = path.removeprefix('projects/').rsplit('/', 2)
        project = urllib.parse.unquote(project)
        if resource == 'pipelines':
            return copy.deepcopy(self.rows.setdefault(f'{project}#{number}', {
                'id': int(number), 'sha': 'a' * 40, 'status': 'running'}))
        return copy.deepcopy(self.mrs[f'{project}!{number}'])


class CIIdleBudget(Bench):
    def runner(self, mesh, gitlab=None):
        r = super().runner(mesh, gitlab)
        self.addCleanup(r.store.db.close)
        return r

    def setup_flow(self):
        self.agent.lead = 'lead'
        t = task(1)
        mesh, gl = FakeMesh([t], max_concurrent=1), GitLabHTTP()
        return self.runner(mesh, gl), t, gl

    def turn(self, r, t, line, *, first=False):
        write_replies(self.replies, {t['id'][:8]: line, t['id'][:8] + ':resume': line})
        if first:
            r.refresh_me()
            self.assertEqual(r.start_new(1), 0)
        else:
            self.assertEqual(r.resume_ready(1), 0)
        return self.finish_turn(r)

    def wake_comment(self, r, a):
        r.mesh.add_comment(a.task_id, 'FYI: no awaited event', author='bystander')
        r._wake_one(a)
        self.assertEqual(a.state, 'ready')

    def wake_ci(self, r, a, gl):
        gl.rows[a.wait['target']]['status'] = 'success'
        r._wake_one(a)
        self.assertEqual(a.state, 'ready')

    def assert_waiting(self, r, a):
        self.assertEqual(a.state, 'waiting', a.note)
        self.assertFalse(any('TURN CAP' in c['body'] for c in r.mesh.comment_log.get(a.task_id, [])))

    def test_observed_f866205a_nine_waits_and_twelve_steps_need_only_last_ci_receipt(self):
        # Exact W/S order from the 2026-10-20 production outcome receipt.
        sequence = 'WWWW S WW SSSSSS W SS W SSS W'.replace(' ', '')
        self.assertEqual((sequence.count('W'), sequence.count('S')), (9, 12))
        r, t, gl = self.setup_flow()
        last_step = sequence.rindex('S') + 1
        for index, kind in enumerate(sequence, 1):
            if index > 1:
                if kind == 'W':
                    self.wake_comment(r, a)
                elif index == last_step:
                    self.wake_ci(r, a, gl)
                else:
                    r.mesh.set_category(next(k for k in r.mesh.tasks if k.startswith(a.wait['target'])), 'done')
                    r._wake_one(a)
            # Historical steps have no CI proof: ordinary card closure suffices.
            # Only the final S is a fresh, verified CI transition.
            if index < len(sequence) and sequence[index] == 'S' and index + 1 != last_step:
                child = task(0xa0000000 + index, assignee_id='other')
                r.mesh.tasks[child['id']] = child
                line = f"⏳ WAIT card:#{child['id'][:8]}"
            else:
                line = f'⏳ WAIT pipeline:g/app#{index}'
            a = self.turn(r, t, line, first=index == 1)
            self.assert_waiting(r, a)
        self.assertEqual(r.store.outcomes_today('builder', t['id']), {'wait': 9, 'step': 12})

    def test_nine_consecutive_idle_waits_after_ci_still_cap(self):
        r, t, gl = self.setup_flow()
        a = self.turn(r, t, '⏳ WAIT pipeline:g/app#1', first=True)
        self.wake_ci(r, a, gl)
        a = self.turn(r, t, '⏳ WAIT pipeline:g/app#2')
        for count in range(1, 10):
            self.wake_comment(r, a)
            a = self.turn(r, t, f'⏳ WAIT pipeline:g/app#{count + 2}')
            if count <= 8:
                self.assert_waiting(r, a)
        self.assertEqual(a.state, 'completed')
        self.assertIn('routed to lead', a.note)
        self.assertEqual(r.mesh.tasks[t['id']]['assignee_id'], 'agent-lead')

    def test_repeat_pipeline_result_and_alias_do_not_reset_again(self):
        self.cfg.daily_wait_budget = 1
        r, t, gl = self.setup_flow()
        a = self.turn(r, t, '⏳ WAIT pipeline:g/app#1', first=True)
        self.wake_ci(r, a, gl)
        a = self.turn(r, t, '⏳ WAIT pipeline:g/app#2')  # genuine step: reset
        self.wake_comment(r, a)
        gl.mrs['g/app!9'] = {'sha': 'a' * 40, 'state': 'opened', 'head_pipeline': {
            'id': 1, 'sha': 'a' * 40, 'status': 'running'}}
        a = self.turn(r, t, '⏳ WAIT mr:g/app!9')        # idle 1
        self.assert_waiting(r, a)
        gl.mrs['g/app!9']['head_pipeline']['status'] = 'success'
        r._wake_one(a)                                # same pipeline #1 under its MR alias
        a = self.turn(r, t, '⏳ WAIT pipeline:g/app#3')
        self.assert_waiting(r, a)
        self.wake_comment(r, a)
        a = self.turn(r, t, '⏳ WAIT pipeline:g/app#4')  # idle 2: duplicate result cannot reset
        self.assertEqual(a.state, 'completed')
        self.assertIn('routed to lead', a.note)

    def test_time_and_card_steps_do_not_reset_idle_budget(self):
        for kind in ('time', 'card', 'decision'):
            with self.subTest(kind=kind):
                # Separate durable task boundary for each case.
                r, _, gl = self.setup_flow()
                t = task({'time': 10, 'card': 11, 'decision': 12}[kind])
                r.mesh.tasks = {t['id']: t}
                self.cfg.daily_wait_budget = 1
                if kind == 'card':
                    child = task(0xabcdef12, assignee_id='other')
                    r.mesh.tasks[child['id']] = child
                line = {'time': '⏳ WAIT 2h: soak', 'card': '⏳ WAIT card:#abcdef12',
                        'decision': '⏳ WAIT decision:@lead'}[kind]
                a = self.turn(r, t, line, first=True)
                if kind == 'time':
                    # mock: external boundary — only the deadline clock advances.
                    with mock.patch('mesh_runner.runner.now', return_value=dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=3)):
                        r._wake_one(a)
                elif kind == 'decision':
                    r.mesh.add_comment(t['id'], 'ANSWER: go', author='lead')
                    r._wake_one(a)
                else:
                    r.mesh.set_category(child['id'], 'done')
                    r._wake_one(a)
                self.assertEqual(a.state, 'ready')
                a = self.turn(r, t, '⏳ WAIT pipeline:g/app#20')
                self.assert_waiting(r, a)
                self.wake_comment(r, a)
                a = self.turn(r, t, '⏳ WAIT pipeline:g/app#21')
                self.assertEqual(a.state, 'completed')
                self.assertEqual(r.store.outcomes_today('builder', t['id']), {'wait': 2, 'step': 1})

    def test_restart_pending_step_and_new_attempt_keep_streak(self):
        self.cfg.daily_wait_budget = 2
        r, t, gl = self.setup_flow()
        a = self.turn(r, t, '⏳ WAIT pipeline:g/app#1', first=True)
        self.wake_comment(r, a)
        a = self.turn(r, t, '⏳ WAIT pipeline:g/app#2')  # 2 old idle waits
        self.wake_ci(r, a, gl)
        r = self.runner(r.mesh, gl)                   # restart before consuming valid wake
        r.refresh_me()
        a = self.turn(r, t, '⏳ WAIT pipeline:g/app#3')  # persisted proof resets old idle waits
        self.assert_waiting(r, a)
        r._finish(a, 'completed', 'fixture: session replacement')
        r = self.runner(r.mesh, gl)
        a = self.turn(r, t, '⏳ WAIT pipeline:g/app#4', first=True)
        self.assert_waiting(r, a)                     # new attempt idle 1
        self.wake_comment(r, a)
        a = self.turn(r, t, '⏳ WAIT pipeline:g/app#5')
        self.assert_waiting(r, a)                     # idle 2
        self.wake_comment(r, a)
        a = self.turn(r, t, '⏳ WAIT pipeline:g/app#6')
        self.assertEqual(a.state, 'completed')        # idle 3, no attempt reset

    def test_task_boundary_and_utc_midnight_keep_daily_limits(self):
        self.cfg.daily_wait_budget = 1
        r, t, gl = self.setup_flow()
        a = self.turn(r, t, '⏳ WAIT pipeline:g/app#1', first=True)
        r._finish(a, 'completed', 'fixture: completed session')
        r.mesh.tasks[t['id']]['assignee_id'] = 'other'
        other = task(2)
        r.mesh.tasks[other['id']] = other
        a = self.turn(r, other, '⏳ WAIT pipeline:g/app#2', first=True)
        self.assert_waiting(r, a)                     # task 1 does not spend task 2 budget
        r._finish(a, 'completed', 'fixture: completed session')
        tomorrow = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)
        # mock: external boundary — UTC day roll-over, no budget/DAO mocks.
        with mock.patch('mesh_runner.store.now', return_value=tomorrow), mock.patch('mesh_runner.runner.now', return_value=tomorrow):
            a = self.turn(r, other, '⏳ WAIT pipeline:g/app#3', first=True)
            self.assert_waiting(r, a)
            self.assertEqual(r.store.outcomes_today('builder', other['id']), {'wait': 1})

    def test_already_final_seed_and_same_wait_are_not_new_ci_steps(self):
        for case in ('already-final', 'same-wait'):
            with self.subTest(case=case):
                self.cfg.daily_wait_budget = 1
                r, _, gl = self.setup_flow()
                t = task(20 if case == 'already-final' else 21)
                r.mesh.tasks = {t['id']: t}
                gl.rows['g/app#1'] = {'id': 1, 'sha': 'a' * 40,
                                         'status': 'success' if case == 'already-final' else 'running'}
                a = self.turn(r, t, '⏳ WAIT pipeline:g/app#1', first=True)
                if case == 'already-final':
                    r._wake_one(a)
                    next_line = '⏳ WAIT pipeline:g/app#2'
                else:
                    self.wake_ci(r, a, gl)
                    gl.rows['g/app#1']['status'] = 'running'  # a retry of the same pipeline
                    next_line = '⏳ WAIT pipeline:g/app#1'
                a = self.turn(r, t, next_line)
                self.assert_waiting(r, a)
                self.assertEqual(r.store.idle_waits_today('builder', t['id']), 1)
                self.wake_comment(r, a)
                a = self.turn(r, t, '⏳ WAIT pipeline:g/app#3')
                self.assertEqual(a.state, 'completed')

    def test_old_head_terminal_result_does_not_reset(self):
        self.cfg.daily_wait_budget = 1
        r, t, gl = self.setup_flow()
        gl.mrs['g/app!9'] = {'sha': 'a' * 40, 'state': 'opened', 'head_pipeline': {
            'id': 1, 'sha': 'b' * 40, 'status': 'running'}}
        a = self.turn(r, t, '⏳ WAIT mr:g/app!9', first=True)
        gl.mrs['g/app!9']['head_pipeline']['status'] = 'success'
        r._wake_one(a)
        a = self.turn(r, t, '⏳ WAIT pipeline:g/app#2')
        self.assert_waiting(r, a)
        self.assertEqual(r.store.idle_waits_today('builder', t['id']), 1)
        self.wake_comment(r, a)
        a = self.turn(r, t, '⏳ WAIT pipeline:g/app#3')
        self.assertEqual(a.state, 'completed')

    def test_typed_terminal_ci_step_resets_but_unknown_observation_does_not(self):
        self.cfg.daily_wait_budget = 1
        r, t, gl = self.setup_flow()
        baseline = f'⏳ WAIT pipeline:g/app#1 until=success after={"a" * 40}:1:running'
        a = self.turn(r, t, baseline, first=True)
        gl.rows['g/app#1'] = {}  # external provider returned no usable observation
        for _ in range(3):
            r._wake_one(a)
        self.assertEqual(a.state, 'waiting')
        self.assertEqual(r.store.idle_waits_today('builder', t['id']), 1)
        gl.rows['g/app#1'] = {'sha': 'a' * 40, 'id': 1, 'status': 'success'}
        r._wake_one(a)
        a = self.turn(r, t, '⏳ WAIT pipeline:g/app#2')
        self.assert_waiting(r, a)
        self.assertEqual(r.store.idle_waits_today('builder', t['id']), 0)
        self.wake_comment(r, a)
        a = self.turn(r, t, '⏳ WAIT pipeline:g/app#3')
        self.assert_waiting(r, a)

    def test_unknown_seed_and_already_final_observation_do_not_reset(self):
        self.cfg.daily_wait_budget = 1
        for index, (kind, seed) in enumerate(((kind, seed) for kind in ('pipeline', 'mr-head', 'mr-state')
                                             for seed in ('error', 'malformed')), 50):
            with self.subTest(kind=kind, seed=seed):
                r, _, gl = self.setup_flow()
                t = task(index)
                r.mesh.tasks = {t['id']: t}
                target = 'g/app#1' if kind == 'pipeline' else 'g/app!9'
                final = ({'id': 1, 'status': 'success', 'sha': 'a' * 40} if kind == 'pipeline' else
                         {'state': 'closed'} if kind == 'mr-state' else
                         {'state': 'opened', 'sha': 'a' * 40, 'head_pipeline': {
                             'id': 1, 'status': 'success', 'sha': 'a' * 40}})
                rows = gl.rows if kind == 'pipeline' else gl.mrs
                rows[target] = final if seed == 'error' else ({'state': 'opened'} if kind == 'mr-head' else {})
                gl.errors = int(seed == 'error')
                a = self.turn(r, t, f"⏳ WAIT {'pipeline' if kind == 'pipeline' else 'mr'}:{target}", first=True)
                rows[target] = final
                r._wake_one(a)
                self.assertEqual(a.state, 'ready')  # Preserve the legacy diagnostic wake.
                self.assertNotIn('ci_progress', a.wait)
                a = self.turn(r, t, '⏳ WAIT pipeline:g/app#2')
                self.assert_waiting(r, a)
                self.assertEqual(r.store.idle_waits_today(a.agent, a.task_id), 1)
                self.assertFalse(any(k == 'ci_progress' for _, k, _ in r.store.events(a.id)))
                self.wake_comment(r, a)
                a = self.turn(r, t, '⏳ WAIT pipeline:g/app#3')
                self.assertEqual(a.state, 'completed')

    def test_failed_seed_then_known_pending_then_terminal_can_reset(self):
        self.cfg.daily_wait_budget = 1
        for index, kind in enumerate(('pipeline', 'mr-head', 'mr-state'), 60):
            with self.subTest(kind=kind):
                r, _, gl = self.setup_flow()
                t = task(index)
                r.mesh.tasks = {t['id']: t}
                target = 'g/app#1' if kind == 'pipeline' else 'g/app!9'
                head = {'id': 1, 'status': 'running', 'sha': 'a' * 40}
                rows = gl.rows if kind == 'pipeline' else gl.mrs
                rows[target] = head if kind == 'pipeline' else {
                    'state': 'opened', 'sha': 'a' * 40, 'head_pipeline': head}
                gl.errors = 1
                a = self.turn(r, t, f"⏳ WAIT {'pipeline' if kind == 'pipeline' else 'mr'}:{target}", first=True)
                r._wake_one(a)  # First successful read establishes a known baseline.
                self.assert_waiting(r, a)
                rows[target]['state'] = 'closed' if kind == 'mr-state' else 'opened'
                head['status'] = 'success'
                r._wake_one(a)
                self.assertEqual(a.state, 'ready')
                self.assertIn('ci_progress', a.wait)
                a = self.turn(r, t, '⏳ WAIT pipeline:g/app#2')
                self.assert_waiting(r, a)
                self.assertEqual(r.store.idle_waits_today(a.agent, a.task_id), 0)
                self.wake_comment(r, a)
                a = self.turn(r, t, '⏳ WAIT pipeline:g/app#3')
                self.assert_waiting(r, a)

    def test_action_required_notices_do_not_reset_idle_budget_before_real_success(self):
        self.cfg.daily_wait_budget = 2
        for index, status in enumerate(('manual', 'failed', 'canceled', 'skipped'), 40):
            with self.subTest(status=status):
                r, _, gl = self.setup_flow()
                t = task(index)
                r.mesh.tasks = {t['id']: t}
                marker = f'⏳ WAIT pipeline:g/app#1 until=success after={"a" * 40}:1:running'
                gl.rows['g/app#1'] = {'id': 1, 'sha': 'a' * 40, 'status': 'running'}
                a = self.turn(r, t, marker, first=True)
                gl.rows['g/app#1']['status'] = status
                r._wake_one(a)
                self.assertEqual(a.state, 'ready')
                self.assertIn('action required', a.wake_text)
                self.assertNotIn('ci_progress', a.wait)
                self.assertIsNone(r.store.mark(a.agent, a.task_id, 'met_wake'))
                a = self.turn(r, t, '⏳ WAIT pipeline:g/app#2')
                self.assert_waiting(r, a)
                self.assertEqual(r.store.idle_waits_today(a.agent, a.task_id), 2)
                self.assertFalse(any(kind == 'ci_progress' for _, kind, _ in r.store.events(a.id)))
                self.wake_ci(r, a, gl)
                a = self.turn(r, t, '⏳ WAIT pipeline:g/app#3')
                self.assert_waiting(r, a)
                self.assertEqual(r.store.idle_waits_today(a.agent, a.task_id), 0)
                self.assertEqual(sum(kind == 'ci_progress' for _, kind, _ in r.store.events(a.id)), 1)
                self.wake_comment(r, a)
                a = self.turn(r, t, '⏳ WAIT pipeline:g/app#4')
                self.assert_waiting(r, a)

    def test_default_24_step_and_6_next_caps_survive_ci_progress(self):
        self.cfg.loop_streak_limit = 0         # the daily counters alone: the streak guard would stop 7 idle NEXTs first
        for kind, limit in (('step', 24), ('next', 6)):
            with self.subTest(kind=kind):
                r, _, gl = self.setup_flow()
                t = task(30 if kind == 'step' else 31)
                r.mesh.tasks = {t['id']: t}
                a = self.turn(r, t, '⏳ WAIT pipeline:g/app#1', first=True)
                for index in range(limit + 1):
                    if kind == 'step' or index == 0:
                        self.wake_ci(r, a, gl)
                    a = self.turn(r, t, f'⏳ WAIT pipeline:g/app#{index + 2}' if kind == 'step' else '↻ NEXT')
                    if index < limit:
                        self.assertEqual(a.state, 'waiting' if kind == 'step' else 'ready')
                self.assertEqual(a.state, 'completed')
                self.assertEqual(r.store.outcomes_today('builder', t['id'])[kind], limit + 1)

    def test_outcome_and_progress_receipt_are_atomic_and_replay_does_not_reset_twice(self):
        r, t, gl = self.setup_flow()
        a = self.turn(r, t, '⏳ WAIT pipeline:g/app#1', first=True)
        self.wake_ci(r, a, gl)
        write_replies(self.replies, {t['id'][:8] + ':resume': '⏳ WAIT pipeline:g/app#2'})
        self.assertEqual(r.resume_ready(1), 0)
        a = r.store.get(a.id)
        self.assertEqual(r.procs[a.id].wait(timeout=10), 0)
        # Real database fault: no DAO or settlement mocks.
        r.store.db.execute("CREATE TRIGGER fail_progress BEFORE INSERT ON events "
                           "WHEN NEW.kind='ci_progress' BEGIN SELECT RAISE(ABORT, 'disk fault'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            r.settle(a)
        self.assertEqual(r.store.outcomes_today('builder', t['id']), {'wait': 1})
        self.assertEqual(r.store.idle_waits_today('builder', t['id']), 1)
        self.assertEqual(r.store.mark('builder', t['id'], 'met_wake')[0], a.id)
        r.store.db.execute('DROP TRIGGER fail_progress')
        # Fault after outcome commit models a crash before met_wake consumption.
        r.store.db.execute("CREATE TRIGGER fail_consumption BEFORE UPDATE ON marks "
                           "WHEN NEW.key='met_wake' AND NEW.value='' "
                           "BEGIN SELECT RAISE(ABORT, 'crash after receipt'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            r.settle(a)
        self.assertEqual(r.store.idle_waits_today('builder', t['id']), 0)
        receipts = lambda: [d for _, k, d in r.store.events(a.id) if k == 'ci_progress']
        self.assertEqual(len(receipts()), 1)
        r.store.db.execute('DROP TRIGGER fail_consumption')
        r.settle(a)
        self.assertEqual(a.state, 'waiting')
        self.assertEqual(len(receipts()), 1)           # same step replay cannot create another reset
