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

if __name__=='__main__':unittest.main()
