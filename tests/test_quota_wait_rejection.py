"""Provider capacity belongs to account routing, never a task's time predicate."""
import datetime as dt
import unittest

from mesh_runner.outcome import parse_outcome
from fakes import FakeMesh, calls, task, write_replies, FUTURE_AT
from test_runner import Bench

NOW = dt.datetime(2026, 11, 4, 11, tzinfo=dt.timezone.utc)


def declaration(event='codex/test/7d-reset', why='provider reset_at verified codex_session_log'):
    return f'⏳ WAIT {FUTURE_AT}: external={event} source=card:#abcdef12 {why}'


class QuotaWaitGrammar(unittest.TestCase):
    def test_quota_and_provider_retry_are_not_external_task_events(self):
        for event, why in [('codex/test/7d-reset', 'provider reset_at verified codex_session_log'),
                           ('quota-reset', 'remaining 92 percent, future reset is metadata'),
                           ('provider-retry', 'provider Retry-After expires'),
                           ('capacity', 'weekly usage limit resets'),
                           ('capacity', 'лимит аккаунта, сброс квоты')]:
            with self.subTest(event=event, why=why):
                outcome = parse_outcome(declaration(event, why), NOW)
                self.assertEqual(outcome.kind, 'unparsed')
                self.assertIsNone(outcome.until)
                self.assertIn('account', outcome.note)

    def test_malformed_quota_timer_does_not_become_an_ordinary_correction_hold(self):
        for line in ('⏳ WAIT 24h: quota reset', '⏳ WAIT tomorrow: provider Retry-After'):
            outcome = parse_outcome(line, NOW)
            self.assertEqual(outcome.kind, 'unparsed')
            self.assertIn('account', outcome.note)

    def test_nonquota_resource_reset_and_scheduled_event_remain_valid(self):
        for event, why in [('backup-window', 'published backup becomes available'),
                           ('database-reset', 'maintenance completes at the published time')]:
            with self.subTest(event=event):
                self.assertEqual(parse_outcome(declaration(event, why), NOW).kind, 'time')


class QuotaWaitSettlement(Bench):
    def test_wrong_quota_wait_continues_same_attempt_without_correction_parking(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        self.cfg.mesh_effects = True
        self.cfg.max_no_outcome = 0
        write_replies(self.replies, {t['id'][:8]: declaration(),
                      t['id'][:8] + ':resume': declaration('backup-window', 'published backup becomes available')})
        r = self.runner(mesh)
        r.tick()
        self.settle_all(r)
        a = r.store.by_state(self.agent.name, ('waiting',))[0]
        self.assertEqual(a.turns, 2)
        self.assertEqual(a.wait['external_time']['event'], 'backup-window')
        self.assertFalse(r.store.by_state(self.agent.name, ('failed',)))
        self.assertEqual(len(calls(self.calls)), 2)

    def test_repeated_quota_declarations_do_not_increment_failures_or_require_account_gate(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        r.refresh_me()
        a = r.store.create(self.agent.name, t['id'], 'claude')
        r.store.update(a, state='ready', no_outcome=2)
        for _ in range(3):
            r._reject_quota_time_wait(a, t)
        current = r.store.get(a.id)
        self.assertEqual((current.state, current.no_outcome), ('ready', 2))
        self.assertFalse(r._quota_admission_required(current))
        self.assertFalse(calls(self.calls))
        self.assertFalse(mesh.tasks[t['id']].get('start_after'))

    def test_persisted_quota_wait_clears_only_its_own_schedule_and_keeps_receiver(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        self.cfg.mesh_effects = True
        r = self.runner(mesh)
        r.refresh_me()
        a = r.store.create(self.agent.name, t['id'], 'claude')
        wait = {'kind': 'time', 'until': FUTURE_AT, 'line': declaration(),
                'since': NOW.isoformat(), 'started': NOW.isoformat(), 'scheduled': FUTURE_AT,
                'external_time': {'event': 'codex/test/7d-reset', 'why': 'provider reset_at', 'at': FUTURE_AT}}
        mesh.tasks[t['id']]['start_after'] = FUTURE_AT
        mesh.tasks[t['id']]['human_gate'] = True
        r.store.update(a, state='waiting', session_id='same-session', wait=wait)
        r._wake_one(a)
        current = r.store.get(a.id)
        self.assertEqual(current.state, 'ready')
        self.assertEqual(current.session_id, 'same-session')
        self.assertFalse(mesh.tasks[t['id']].get('start_after'))
        self.assertTrue(mesh.tasks[t['id']]['human_gate'])
        self.assertFalse(calls(self.calls))
        # Existing human admission, rather than the removed quota date, still controls the next turn.
        r.resume_ready(1)
        self.settle_all(r)
        self.assertFalse(calls(self.calls))

    def test_persisted_quota_wait_does_not_clear_another_author_schedule(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        self.cfg.mesh_effects = True
        r = self.runner(mesh)
        r.refresh_me()
        a = r.store.create(self.agent.name, t['id'], 'claude')
        wait = {'kind': 'time', 'until': FUTURE_AT, 'line': declaration(), 'scheduled': FUTURE_AT}
        foreign = (dt.datetime.fromisoformat(FUTURE_AT) + dt.timedelta(hours=1)).isoformat()
        mesh.tasks[t['id']]['start_after'] = foreign
        r.store.update(a, state='waiting', wait=wait)
        r._wake_one(a)
        self.assertEqual(r.store.get(a.id).state, 'ready')
        self.assertEqual(mesh.tasks[t['id']]['start_after'], foreign)
        r.resume_ready(1)
        self.settle_all(r)
        self.assertFalse(calls(self.calls))
        self.assertEqual(r.store.get(a.id).state, 'ready')
        # Only the owner removing the independent schedule permits an ordinary turn.
        mesh.tasks[t['id']]['start_after'] = None
        write_replies(self.replies, {t['id'][:8]: declaration('backup-window', 'published backup becomes available')})
        r._resume_hold.clear()
        r.tick()
        self.settle_all(r)
        self.assertEqual(len(calls(self.calls)), 1)
        self.assertEqual(r.store.get(a.id).state, 'waiting')


class QuotaAdmission(Bench):
    def capacity_gate(self, *, reserve_ack=True, admit_ack=True):
        """Operator hook is an external CLI boundary; runner business paths remain real."""
        import sys
        self.capacity = self.root / 'capacity'
        self.gate_calls = self.root / 'gate-calls'
        gate = self.root / 'account-gate.py'
        gate.write_text('''import os, pathlib, sys
capacity = pathlib.Path(sys.argv[1])
with open(sys.argv[2], 'a') as log:
    log.write(os.environ['MESH_RUNNER_PHASE'] + ':' + os.environ.get('MESH_RUNNER_REQUIRE_ACCOUNT_CAPACITY', '0') + '\\n')
if os.environ.get('MESH_RUNNER_REQUIRE_ACCOUNT_CAPACITY') == '1':
    if not capacity.exists():
        print('account capacity unavailable')
        sys.exit(75)
    phase = os.environ['MESH_RUNNER_PHASE']
    if (phase == 'reserve' and sys.argv[3] == 'True') or (phase == 'admit' and sys.argv[4] == 'True'):
        print('ACCOUNT_CAPACITY_READY=1')
''')
        self.agent.start_gate_command = [sys.executable, str(gate), str(self.capacity), str(self.gate_calls),
                                         str(reserve_ack), str(admit_ack)]

    def quota_replies(self, t):
        write_replies(self.replies, {t['id'][:8]: 'ERROR:usage limit reached',
            t['id'][:8] + ':resume': declaration('backup-window', 'published backup becomes available')})

    def test_no_eligible_account_keeps_attempt_without_provider_retries_or_lane_pause(self):
        t = task(1)
        self.capacity_gate()
        self.quota_replies(t)
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        r.tick()
        self.settle_all(r)
        a = r.store.by_state(self.agent.name, ('ready',))[0]
        session = a.session_id
        for _ in range(4):
            r.tick()
        self.assertEqual(len(calls(self.calls)), 1)
        self.assertEqual((a.no_outcome, r._starts_paused_until, r._agent_gate_hold), (0, 0.0, None))
        self.assertFalse(mesh.tasks[t['id']].get('start_after'))
        self.assertNotEqual(a.wait.get('kind'), 'time')
        self.assertEqual(r.store.mark(self.agent.name, t['id'], 'quota_admission_required')[0], a.id)
        # The same pending task can immediately resume after a fresh gate proof.
        self.capacity.touch()
        r._resume_hold.clear()  # advance the existing local gate recheck cadence
        r.tick()
        self.settle_all(r)
        current = r.store.get(a.id)
        self.assertEqual((current.session_id, current.turns, current.no_outcome), (session, 2, 0))
        self.assertEqual(current.wait['external_time']['event'], 'backup-window')
        self.assertFalse(r.store.mark(self.agent.name, t['id'], 'quota_admission_required')[0])

    def test_quota_hold_does_not_block_another_task_on_the_lane(self):
        t, other = task(1), task(2)
        self.capacity_gate()
        write_replies(self.replies, {t['id'][:8]: 'ERROR:usage limit reached',
            other['id'][:8]: declaration('backup-window', 'published backup becomes available')})
        r = self.runner(FakeMesh([t, other], max_concurrent=1))
        r.tick()
        self.settle_all(r)
        self.assertEqual(len(calls(self.calls)), 2)
        self.assertEqual(self.states(r)[t['id'][:8]], 'ready')
        self.assertEqual(self.states(r)[other['id'][:8]], 'waiting')
        self.assertEqual(r._starts_paused_until, 0.0)
        self.assertIsNone(r._agent_gate_hold)

    def test_fresh_ack_is_required_in_each_phase_not_reused_from_reserve(self):
        t = task(1)
        self.capacity_gate(admit_ack=False)
        self.capacity.touch()
        self.quota_replies(t)
        r = self.runner(FakeMesh([t], max_concurrent=1))
        r.tick()
        self.settle_all(r)
        self.assertEqual(len(calls(self.calls)), 1)
        a = r.store.by_state(self.agent.name, ('ready',))[0]
        self.assertTrue(r._quota_admission_required(a))
        self.assertIn('reserve:1', self.gate_calls.read_text())
        self.assertIn('admit:1', self.gate_calls.read_text())
        self.assertIsNone(r._agent_gate_hold)

    def test_missing_capable_gate_survives_restart_without_spawning_or_failing(self):
        t = task(1)
        self.quota_replies(t)
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        r.tick()
        self.settle_all(r)
        first = r.store.by_state(self.agent.name, ('ready',))[0]
        restarted = self.runner(mesh)
        for _ in range(3):
            restarted.tick()
        current = restarted.store.get(first.id)
        self.assertEqual((current.state, current.session_id, current.no_outcome), ('ready', first.session_id, 0))
        self.assertEqual(len(calls(self.calls)), 1)
        self.assertIsNone(restarted._agent_gate_hold)
        self.assertEqual(restarted._starts_paused_until, 0.0)

    def test_account_selected_during_reserve_is_the_actual_spawn_environment(self):
        from pathlib import Path
        t = task(1)
        self.capacity_gate()
        self.capacity.touch()
        env_file = self.root / 'account.env'
        env_file.write_text('PROBE=old-account\n')
        self.agent.env_file = env_file
        operator = Path(self.agent.start_gate_command[1])
        operator.write_text('import os\nfrom pathlib import Path\n'
            + "if os.environ.get('MESH_RUNNER_REQUIRE_ACCOUNT_CAPACITY') == '1' and os.environ['MESH_RUNNER_PHASE'] == 'reserve':\n"
            + f"    Path({str(env_file)!r}).write_text('PROBE=new-account\\n')\n"
            + operator.read_text())
        self.quota_replies(t)
        r = self.runner(FakeMesh([t], max_concurrent=1))
        r.tick()
        self.settle_all(r)
        self.assertEqual(len(calls(self.calls)), 2)
        self.assertEqual(calls(self.calls)[-1]['probe'], 'new-account')
        self.assertEqual(self.states(r)[t['id'][:8]], 'waiting')

    def test_allowed_reserve_is_admitted_without_quota_pause(self):
        import sys
        t = task(1)
        self.capacity_gate()
        self.agent.on_limit_command = [sys.executable, '-c',
            f'from pathlib import Path; Path({str(self.capacity)!r}).touch()']
        self.quota_replies(t)
        r = self.runner(FakeMesh([t], max_concurrent=1))
        r.tick()
        self.settle_all(r)
        self.assertEqual(len(calls(self.calls)), 2)
        self.assertEqual(self.states(r)[t['id'][:8]], 'waiting')
        self.assertEqual(r._starts_paused_until, 0.0)


if __name__ == '__main__':
    unittest.main()
