"""Unreadable live argv holds; a freshly confirmed zombie cannot execute a writer."""
import os
import subprocess
import unittest
from unittest import mock
from mesh_runner import delivery_recovery as recovery


class ZombieProcess(unittest.TestCase):
    def test_only_exact_fresh_own_zombie_state_is_excluded(self):
        pid = os.getpid()
        for stat, owner, selected, expected in (
            ('Z', os.geteuid(), pid, True), ('Z+', os.geteuid(), pid, True),
            ('S', os.geteuid(), pid, False), ('R+', os.geteuid(), pid, False),
            ('SZ', os.geteuid(), pid, False), ('?', os.geteuid(), pid, False),
            ('Z', os.geteuid()+1, pid, False), ('Z', os.geteuid(), pid+1, False)):
            with self.subTest(stat=stat, owner=owner, selected=selected):
                def kernel(argv, **kwargs):
                    text = (f'{pid} {os.geteuid()}\n328 -2\n' if argv[1] == '-ax'
                            else f'{selected} {owner} {os.getppid()} {stat}\n')
                    return subprocess.CompletedProcess(argv, 0, text, '')
                with mock.patch.object(recovery.subprocess, 'run', side_effect=kernel), \
                     mock.patch.object(recovery, '_process_argv', return_value=[]):
                    self.assertEqual(recovery._session_process_absent('old-session', 'attempt'), expected)

    @unittest.skipUnless(hasattr(os, 'fork'), 'POSIX process state')
    def test_real_unreaped_child_is_not_a_live_writer(self):
        import time
        pid = os.fork()
        if pid == 0:
            os._exit(0)
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not recovery._zombie_now(pid):
                time.sleep(.02)
            self.assertTrue(recovery._zombie_now(pid))
            self.assertTrue(recovery.pid_alive(pid))
            self.assertFalse(recovery._process_argv(pid))
            run = subprocess.run
            def kernel(argv, *args, **kwargs):
                if argv[1] == '-ax':
                    return subprocess.CompletedProcess(argv, 0, f'{pid} {os.geteuid()}\n', '')
                return run(argv, *args, **kwargs)
            with mock.patch.object(recovery.subprocess, 'run', side_effect=kernel):
                self.assertTrue(recovery._session_process_absent('old-session', 'attempt'))
        finally:
            os.waitpid(pid, 0)
