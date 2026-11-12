"""Gate output privacy and lane-wide retry regression coverage."""
import unittest
from unittest.mock import patch
from fakes import FakeMesh, task
from test_runner import Bench
from mesh_runner.runner import gate_hold_reason, GATE_HOLD_SECONDS


class PublicReason(unittest.TestCase):
    def test_equals_in_legacy_route_is_classified_without_copying_values(self):
        for prefix, expected in (
            ('route: unavailable', 'runtime profile unavailable'),
            ('route: reserve_refused:pool_full:pool', 'runtime capacity unavailable'),
            ('capacity: full', 'runtime capacity unavailable'),
            ('route: qa_no_selection:attempt_limit', 'qa_no_selection: review admission unavailable'),
            ('waits for a human decision: question', 'waits for a human decision')):
            self.assertEqual(gate_hold_reason(prefix+' profile=SECRET trace=PRIVATE'), expected)

    def test_structured_code_never_copies_arbitrary_reason_or_unknown_code(self):
        self.assertEqual(gate_hold_reason('HOLD_REASON_CODE=capacity\nHOLD_REASON=SECRET'),
                         'runtime capacity unavailable')
        self.assertEqual(gate_hold_reason('HOLD_REASON_CODE=SECRET\nHOLD_REASON=PRIVATE'),
                         'held by the start gate')
        self.assertEqual(gate_hold_reason('TOKEN=SECRET'), 'held by the start gate')
        self.assertEqual(gate_hold_reason('route: reserve_refused:unknown profile=SECRET'),
                         'held by the start gate')
        self.assertEqual(gate_hold_reason('route: unfamiliar profile=SECRET'),
                         'held by the start gate')


class SharedGateBackoff(Bench):
    def test_other_tasks_and_ticks_do_not_reask_lane_hold(self):
        a,b=task(1,'urgent'),task(2,'low')
        r=self.runner(FakeMesh([a,b],max_concurrent=2))
        self.agent.start_gate_command=['operator-gate']
        with patch.object(r,'_run_hook',return_value=(75,'route: unavailable profile=SECRET','')) as hook:
            r.tick();r.tick();r.tick()
        self.assertEqual(hook.call_count,1)
        self.assertEqual(r.hold_reason(b,'work'),'gate: runtime profile unavailable')

    def test_retry_is_exponential_bounded_and_admission_success_resets(self):
        r=self.runner(FakeMesh([]));self.agent.start_gate_command=['operator-gate']
        with patch.object(r,'_run_hook',return_value=(75,'HOLD_REASON_CODE=capacity','')),patch('mesh_runner.runner.time.monotonic',return_value=1000):
            intervals=[]
            for _ in range(7):
                r._agent_gate_hold=None
                r._gate('reserve','one','work')
                intervals.append(r._gate_hold['one'][1]-1000)
        self.assertEqual(intervals,[120,240,480,600,600,600,600])
        with patch.object(r,'_run_hook',return_value=(0,'','')):
            r._gate('admit','one','work')
        self.assertIsNone(r._agent_gate_hold)
        self.assertFalse(r._gate_failures)

    def test_observed_task_update_invalidates_shared_hold_without_bypassing_gate(self):
        a=task(1);r=self.runner(FakeMesh([a]));self.agent.start_gate_command=['operator-gate']
        with patch.object(r,'_run_hook',return_value=(75,'HOLD_REASON_CODE=profile','')) as hook:
            r._start_one(a)
            changed=dict(a,updated_at='new-event')
            r._gate_event(changed)
            self.assertIsNone(r._agent_gate_hold)
            r._gate('reserve',a['id'],'work')
            self.assertEqual(hook.call_count,2)
            self.assertIsNotNone(r._agent_gate_hold)  # the update is no approval

    def test_unrelated_task_update_does_not_lift_lane_hold(self):
        a,b=task(1),task(2);r=self.runner(FakeMesh([a,b]))
        self.agent.start_gate_command=['operator-gate']
        with patch.object(r,'_run_hook',return_value=(75,'HOLD_REASON_CODE=profile','')):
            r._start_one(a)
        held=r._agent_gate_hold
        r._gate_event(dict(b,updated_at='unrelated-event'))
        self.assertEqual(r._agent_gate_hold,held)

    def test_qa_equals_reason_still_records_unreviewable_streak(self):
        t=task(1);r=self.runner(FakeMesh([t]));self.agent.start_gate_command=['operator-gate']
        with patch.object(r,'_run_hook',return_value=(76,'route: qa_no_selection:attempt_limit profile=SECRET trace=PRIVATE','')):
            reason,_=r._gate('reserve',t['id'],'review')
        r._review_gate_refused(t,reason)
        self.assertTrue(r.store.mark('builder',t['id'],'review_gate_hold')[0])
        self.assertNotIn('SECRET',reason)

    def test_confirmed_local_capacity_release_lifts_backoff(self):
        r=self.runner(FakeMesh([]));self.agent.start_gate_command=['operator-gate']
        self.agent.turn_end_command=['operator-end']
        with patch.object(r,'_run_hook',return_value=(75,'HOLD_REASON_CODE=capacity','')):
            r._gate('reserve','one','work')
        with patch.object(r,'_run_hook',return_value=(1,'','')):
            r._turn_end('other','work',None,{'RES_ID':'reservation'},'turn-end')
        self.assertIsNotNone(r._agent_gate_hold)  # failed release is no capacity event
        with patch.object(r,'_run_hook',return_value=(0,'','')):
            r._turn_end('other','work',None,{'RES_ID':'reservation'},'turn-end')
        self.assertIsNone(r._agent_gate_hold)
        self.assertNotIn('one',r._gate_hold)
        with patch.object(r,'_run_hook',return_value=(75,'HOLD_REASON_CODE=profile','')):
            r._gate('reserve','one','work')
        with patch.object(r,'_run_hook',return_value=(0,'','')):
            r._turn_end('other','work',None,{'RES_ID':'reservation'},'turn-end')
        self.assertIsNotNone(r._agent_gate_hold)  # capacity release does not repair a profile

    def test_task_only_hold_does_not_hold_other_tasks(self):
        r=self.runner(FakeMesh([]));self.agent.start_gate_command=['operator-gate']
        with patch.object(r,'_run_hook',return_value=(76,'HOLD_REASON_CODE=human','')):
            reason,_=r._gate('reserve','one','work')
        self.assertEqual(reason,'waits for a human decision')
        self.assertTrue(r._gate_task_only)
        self.assertIsNone(r._agent_gate_hold)

class StaleCapacityBackoff(Bench):
    def _held(self,code='capacity'):
        r=self.runner(FakeMesh([]));self.agent.start_gate_command=['operator-gate']
        with patch.object(r,'_run_hook',return_value=(75,'HOLD_REASON_CODE='+code,'')):
            r._gate('reserve','one','work')
        return r

    def test_legacy_pool_exhausted_is_normalised_to_capacity_only(self):
        for line in ('runtime.capacity: server admit hold acquire_refused:pool_exhausted',
                     'capacity: runtime.capacity: server admit hold acquire_refused:pool_exhausted'):
            self.assertEqual(gate_hold_reason(line),'runtime capacity unavailable')
        for line in ('runtime.capacity: server admit hold acquire_refused:auth_revoked','some other refusal'):
            self.assertNotEqual(gate_hold_reason(line),'runtime capacity unavailable')

    def test_legacy_text_refusal_is_lifted_by_local_turn_end(self):
        r=self.runner(FakeMesh([]));self.agent.start_gate_command=['operator-gate']
        self.agent.turn_end_command=['operator-end']
        with patch.object(r,'_run_hook',return_value=(75,'runtime.capacity: server admit hold acquire_refused:pool_exhausted','')):
            r._gate('reserve','one','work')
        with patch.object(r,'_run_hook',return_value=(0,'','')):
            r._turn_end('other','work',None,{'RES_ID':'x'},'turn-end')
        self.assertIsNone(r._agent_gate_hold)

    def test_semantic_change_lifts_capacity_hold_and_asks_gate_exactly_once(self):
        r=self._held()
        self.assertIsNotNone(r._agent_gate_hold)
        self.assertTrue(r.invalidate_capacity_holds('pool state changed'))
        self.assertIsNone(r._agent_gate_hold);self.assertNotIn('one',r._gate_hold)
        with patch.object(r,'_run_hook',return_value=(0,'','')) as hook:
            self.assertEqual(r._gate('reserve','one','work')[0],'')
            self.assertEqual(hook.call_count,1)
        self.assertFalse(r.invalidate_capacity_holds('again'))     # nothing left to lift

    def test_repeated_pool_full_restarts_backoff_from_base(self):
        r=self._held();r.invalidate_capacity_holds('x')
        with patch.object(r,'_run_hook',return_value=(75,'HOLD_REASON_CODE=capacity','')),patch('mesh_runner.runner.time.monotonic',return_value=1000):
            r._gate('reserve','one','work')
        self.assertEqual(r._gate_hold['one'][1]-1000,GATE_HOLD_SECONDS)
        self.assertIsNotNone(r._agent_gate_hold)

    def test_human_and_task_only_holds_are_not_cleared(self):
        r=self._held('human')
        self.assertFalse(r.invalidate_capacity_holds('x'));self.assertIsNotNone(r._agent_gate_hold)
        r=self.runner(FakeMesh([]));self.agent.start_gate_command=['operator-gate']
        r._gate_hold['t']=('waits for a human decision',10**9)
        with patch.object(r,'_run_hook',return_value=(76,'HOLD_REASON_CODE=human','')):
            r._gate('reserve','two','work')
        self.assertFalse(r.invalidate_capacity_holds('x'));self.assertIn('t',r._gate_hold);self.assertIsNone(r._agent_gate_hold)

    def test_route_change_lifts_hold_but_same_route_does_not(self):
        r=self._held('profile')
        r._route_event();self.assertIsNotNone(r._agent_gate_hold)
        r._routed_harness='codex'
        with patch.object(r,'_run_hook',return_value=(0,'','')) as hook:
            self.assertEqual(r._gate('reserve','one','work')[0],'')
            self.assertEqual(hook.call_count,1)
        self.assertIsNone(r._agent_gate_hold)

    def test_capacity_ready_delay_is_lifted_only_for_capacity_origin(self):
        r=self.runner(FakeMesh([]));import time
        r._resume_hold['cap']=time.monotonic()+120;r._capacity_resume.add('cap')
        r._resume_hold['other']=time.monotonic()+120
        self.assertTrue(r.invalidate_capacity_holds('x'))
        self.assertNotIn('cap',r._resume_hold);self.assertIn('other',r._resume_hold)

    def test_pools_changed_skips_other_workspace(self):
        r=self._held();r.me={'workspace_id':'ws-a'}
        self.runner_group=type('G',(),{})()
        from mesh_runner.runner import Runner
        g=Runner.__new__(Runner);g.agents=[r]
        g._pools_changed('ws-b');self.assertIsNotNone(r._agent_gate_hold)
        g._pools_changed('ws-a');self.assertIsNone(r._agent_gate_hold)

if __name__=='__main__':unittest.main()


class RouteHoldAlert(Bench):
    """A lane the runtime route holds with one reason for 30 min gets one operator alert."""
    REASON = 'route: claude_binding_team_profile_missing'

    def alerts(self, r):
        import json
        path = r.cfg.state_dir / "events.jsonl"
        rows = [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []
        return [e for e in rows if e["kind"] == "operator_alert"]

    def at(self, minutes):
        import datetime as dt
        return dt.datetime(2026, 11, 12, 5, 28, tzinfo=dt.timezone.utc) + dt.timedelta(minutes=minutes)

    def test_alert_fires_once_after_thirty_minutes_of_one_reason(self):
        r = self.runner(FakeMesh([]))
        fired = [r._note_route_hold(self.REASON, self.at(m)) for m in (0, 10, 29, 30, 31, 90, 200)]
        self.assertEqual(fired, [False, False, False, True, False, False, False])
        alerts = self.alerts(r)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["alert"], "route_hold:builder")
        self.assertEqual(alerts[0]["reason"], self.REASON)
        self.assertIn("Lane builder", alerts[0]["text"])

    def test_exactly_one_alert_for_a_long_episode(self):
        # regression: no repeat after 6 h or 12 h of the same hold
        r = self.runner(FakeMesh([]))
        fired = [r._note_route_hold(self.REASON, self.at(m)) for m in (0, 30, 31, 360, 361, 720, 1440)]
        self.assertEqual(fired, [False, True, False, False, False, False, False])
        self.assertEqual(len(self.alerts(r)), 1)

    def test_changed_reason_or_passed_gate_starts_a_new_episode(self):
        r = self.runner(FakeMesh([]))
        r._note_route_hold(self.REASON, self.at(0))
        r._note_route_hold('route: unavailable', self.at(20))          # another reason: a new episode
        self.assertFalse(r._note_route_hold('route: unavailable', self.at(45)))
        self.assertTrue(r._note_route_hold('route: unavailable', self.at(50)))
        r._route_hold_seen = None
        self.agent.start_gate_command = ['operator-gate']
        with patch.object(r, '_run_hook', return_value=(75, self.REASON, '')):
            r._gate('reserve', 'one', 'work')
        self.assertEqual(r._route_hold_seen[0], self.REASON)
        with patch.object(r, '_run_hook', return_value=(0, '', '')):
            r._gate('admit', 'one', 'work')
        self.assertIsNone(r._route_hold_seen)                            # a passed gate ends the episode

    def test_a_non_route_hold_ends_the_episode(self):
        # regression: route@0, capacity@20, the same route@31 is a new episode
        r = self.runner(FakeMesh([]))
        fired = [r._note_route_hold(reason, self.at(m)) for reason, m in
                 ((self.REASON, 0), ('runtime capacity unavailable', 20), (self.REASON, 31), (self.REASON, 60),
                  (self.REASON, 61), (self.REASON, 62))]
        self.assertEqual(fired, [False, False, False, False, True, False])
        self.assertIn("for 30 min", self.alerts(r)[0]["text"])

    def test_non_route_holds_never_alert(self):
        r = self.runner(FakeMesh([]))
        for m in (0, 40, 400):
            self.assertFalse(r._note_route_hold('runtime capacity unavailable', self.at(m)))
            self.assertFalse(r._note_route_hold('card writer still active (holder=x)', self.at(m)))
        self.assertEqual(self.alerts(r), [])
