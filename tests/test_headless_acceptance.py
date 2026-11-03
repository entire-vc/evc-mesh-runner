"""Native command-hook boundary: real subprocess, durable files and negative input."""
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from mesh_runner import headless_acceptance as hook, interactive


class ClaudeHook(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.prompt = 'mandatory continuation\nsecond line'
        self.digest = hashlib.sha256(self.prompt.encode()).hexdigest()
        self.intent = dict(token='fixture-token', sha256=self.digest, session_id='fixture-session')
        interactive.write_delivery(self.root, 'attempt', dict(self.intent, phase='native-exec-confirmed'))

    def invoke(self, **changes):
        event = dict(hook_event_name='UserPromptSubmit', session_id=self.intent['session_id'], prompt=self.prompt)
        event.update(changes)
        return subprocess.run([sys.executable, str(Path(hook.__file__)), str(self.root), 'attempt',
            self.intent['token'], self.digest, self.intent['session_id']], input=json.dumps(event),
            text=True, capture_output=True, timeout=5)

    def test_exact_native_input_is_durable_before_hook_success(self):
        self.assertEqual(self.invoke().returncode, 0)
        self.assertTrue(hook.accepted(self.root, 'attempt', self.intent))
        self.assertFalse(hook.accepted(self.root, 'other-attempt', self.intent))

    def test_wrong_session_prompt_or_event_blocks_without_receipt(self):
        for changes in (dict(session_id='wrong'), dict(prompt=self.prompt[:-1]), dict(hook_event_name='Stop')):
            with self.subTest(changes=list(changes)):
                self.assertEqual(self.invoke(**changes).returncode, 2)
                self.assertFalse(hook.accepted(self.root, 'attempt', self.intent))

    def test_changed_owner_journal_blocks_without_receipt(self):
        interactive.write_delivery(self.root, 'attempt', dict(self.intent, token='new-generation'))
        self.assertEqual(self.invoke().returncode, 2)
        self.assertFalse(hook.accepted(self.root, 'attempt', self.intent))

    def test_failed_durable_write_returns_native_blocking_exit(self):
        path = interactive.delivery_path(self.root, hook.receipt_key('attempt', self.intent['token']))
        path.mkdir()
        self.assertEqual(self.invoke().returncode, 2)
        self.assertFalse(hook.accepted(self.root, 'attempt', self.intent))
