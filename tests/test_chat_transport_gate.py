"""Transport admission must precede chat starts and leave queued/active work intact."""
import threading
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

from fake_chat_mesh import FakeChatMesh
from mesh_runner.chat_host import ChatHost
from mesh_runner.chat_pool import ChatPool
from mesh_runner.chat_protocol import ChatClient
from mesh_runner.chat_service import ChatService
from test_runner import Bench
from fakes import FakeMesh


class Standing(Bench):
    def test_hold_does_not_check_or_restart_existing_pane_and_recovery_checks(self):
        r = self.runner(FakeMesh([]))
        r.chat = Mock(error='', held_since=0)
        r._chat_due = 0
        r._fresh_env = Mock(side_effect=AssertionError('env must not run'))
        with patch.object(r, '_gate', return_value=('transport unavailable', {})) as gate:
            r.keep_chat()
        gate.assert_called_once_with('transport', '', 'chat')
        r.chat.check.assert_not_called()
        self.assertIn('transport', r._chat_hold)
        r._chat_due = 0
        with patch.object(r, '_gate', return_value=('', {})):
            r.keep_chat()
        r.chat.check.assert_called_once()
        self.assertEqual(r._chat_hold, '')
        r._fresh_env.assert_not_called()  # mock check does not request env

    def test_transport_phase_does_not_touch_task_hold_or_failure_budget(self):
        r = self.runner(FakeMesh([]))
        self.agent.start_gate_command = ['operator-gate']
        r._gate_task_only = True
        before = (dict(r._gate_hold), dict(r._gate_failures), r._agent_gate_hold)
        with patch.object(r, '_run_hook', return_value=(75, 'VPN transport unavailable', '')) as hook:
            reason, _ = r._gate('transport', '', 'chat')
        self.assertTrue(reason)
        self.assertTrue(r._gate_task_only)
        self.assertEqual(before, (r._gate_hold, r._gate_failures, r._agent_gate_hold))
        self.assertEqual(hook.call_args.kwargs['timeout'], 15)
        env = hook.call_args.args[1]
        self.assertEqual((env['MESH_RUNNER_PHASE'], env['MESH_RUNNER_TASK'], env['MESH_RUNNER_MODE']),
                         ('transport', '', 'chat'))

    def test_transport_allows_a_thirteen_second_operator_probe(self):
        r = self.runner(FakeMesh([]))
        self.agent.start_gate_command = ['operator-gate']
        def bounded_probe(command, env, *, timeout):
            # Model the operator's 12-second network probe / 13-second process
            # budget without spending real wall time in this regression.
            return (0, '', '') if timeout > 13 else (-1, '', 'timed out')
        with patch.object(r, '_run_hook', side_effect=bounded_probe) as hook:
            self.assertEqual(r._gate('transport', '', 'chat'), ('', {}))
            self.assertEqual(hook.call_args.kwargs['timeout'], 15)
            r._gate('reserve', 'task', 'work')
            self.assertEqual(hook.call_args.kwargs['timeout'], 120)


class Claims(unittest.TestCase):
    def test_unhealthy_agent_remains_queued_while_healthy_agent_starts_and_recovers(self):
        mesh = FakeChatMesh(agents=('a', 'b'))
        client = ChatClient('https://mesh.test', 'secret', opener=mesh)
        a, b = mesh.queue_turn(agent='a'), mesh.queue_turn(agent='b', thread='thread-b')
        launched = []
        svc = ChatService(client, ['a', 'b'], ChatPool(max_per_agent=1, max_host=5, idle_minutes=10), lambda agent: None,
                          lambda a, t: None, lambda a, t: True, spawn=launched.append)
        self.assertEqual(svc.step(1, claim_agents=['b']), 1)
        self.assertEqual(a['state'], 'queued')
        self.assertEqual(a['attempt'], 0)
        self.assertEqual(svc.running, {b['turn_id']})
        self.assertEqual(svc.step(1, claim_agents=['a']), 1)
        self.assertEqual(svc.running, {a['turn_id'], b['turn_id']})
        self.assertEqual(svc.agents, ['a', 'b'])

    def test_no_eligible_agents_still_process_commands_without_claiming(self):
        mesh = FakeChatMesh()
        client = ChatClient('https://mesh.test', 'secret', opener=mesh)
        turn = mesh.queue_turn()
        mesh.commands['stop'] = {'command_id': 'stop', 'kind': 'stop_session',
                                 'agent_id': 'agent-1', 'thread_id': 'thread-1'}
        stopped = []
        svc = ChatService(client, ['agent-1'], ChatPool(max_per_agent=1, max_host=5, idle_minutes=10), lambda a: None,
                          lambda a, t: stopped.append((a, t)), lambda a, t: True)
        svc.running.add('active')
        self.assertEqual(svc.step(1, claim_agents=[]), 0)
        self.assertEqual(turn['state'], 'queued')
        self.assertEqual(stopped, [('agent-1', 'thread-1')])
        self.assertEqual(svc.running, {'active'})
        self.assertFalse(any(path.startswith('/claim') for _, path in mesh.requests))

    def test_host_filters_before_claim_but_keeps_reap_and_commands_on_hold(self):
        host = ChatHost.__new__(ChatHost)
        host.by_id = {'a': 'A', 'b': 'B'}
        host.runners = {'A': NS(_gate=Mock(side_effect=OSError)),
                        'B': NS(_gate=Mock(return_value=('', {})))}
        self.assertEqual(host._claim_agents(), ['b'])
        host.runners['B']._gate.return_value = ('hold', {})
        stop = threading.Event()
        host.service = NS(recover=Mock(), step=Mock(), reap_idle=Mock(), free_slots=lambda: 1, hold_until=0)
        # One complete held iteration, including idle reap and bounded wait.
        def wait(seconds):
            self.assertEqual(seconds, 1.0)
            stop.set()
        with patch.object(stop, 'wait', side_effect=wait):
            host.loop(stop)
        host.service.step.assert_called_once_with(3, claim_agents=[])
        host.service.reap_idle.assert_called_once()
        host.service.recover.assert_called_once()
