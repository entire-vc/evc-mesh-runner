"""Native quota dialog fixture is sanitized at the tmux boundary; real driver logic."""
import io
from pathlib import Path
import subprocess
import tempfile
import unittest

from mesh_runner import tui_driver as td
from mesh_runner.interactive import read_delivery, write_delivery

FRAME = (Path(__file__).parent / 'fixtures/claude_weekly_limit.txt').read_text()


class CapturedTmux:
    """External process boundary: return a captured pane, never launch a provider."""
    def __init__(self, frame):
        self.frame = frame
        self.calls = []

    def run(self, *args, **kwargs):
        self.calls.append(args)
        if args[0] != 'capture-pane':
            raise AssertionError('quota dialog must not receive keys: ' + repr(args))
        return subprocess.CompletedProcess(args, 0, self.frame, '')


class NativeQuotaMenu(unittest.TestCase):
    def test_sanitized_native_weekly_frame_is_quota_not_running_turn(self):
        self.assertFalse(td.is_busy(FRAME))
        self.assertIn('weekly limit', td.limit_notice(FRAME))

    def test_all_native_menu_selections_and_spacing(self):
        for selected in (1, 2, 3):
            frame = FRAME.replace('❯ ', '').replace(f'     {selected}.', f'   ❯ {selected}.')
            if selected == 1:
                frame = frame.replace('   1.', '   ❯ 1.')
            with self.subTest(selected=selected):
                self.assertFalse(td.is_busy(frame))
                self.assertTrue(td.limit_notice(frame))

    def test_genuine_busy_marker_is_never_suppressed(self):
        for marker in ('Working (1s)', 'ctrl+b to run in background', '✽ Forging… (1m 2s)', 'esc to interrupt'):
            frame = FRAME.replace('<unclassified native UI line>', marker, 1)
            with self.subTest(marker=marker):
                self.assertTrue(td.is_busy(frame))
                self.assertEqual(td.limit_notice(frame), '')

    def test_permission_dialog_with_quoted_old_limit_stays_busy(self):
        frame = '''Assistant quoted: You've hit your weekly limit · resets 12pm
Do you want to proceed?
❯ 1. Yes, allow this command
  2. No
Enter to confirm · Esc to cancel
'''
        self.assertTrue(td.is_busy(frame))
        self.assertEqual(td.limit_notice(frame), '')

    def test_old_quoted_or_incomplete_quota_ui_does_not_hide_cancel(self):
        cases = [FRAME.replace('  ⎿', '  > ⎿'),
                 FRAME.replace('What do you want to do?', 'Do you want to proceed?'),
                 FRAME.replace('Stop and wait for limit to reset', 'Allow this tool'),
                 FRAME.replace('Ask your admin for more usage', 'Allow all commands'),
                 '```text\n' + FRAME + '```\n',
                 FRAME + '\n❯ new prompt\nEsc to cancel',
                 "Usage limit reached\nPermission needed\nEsc to cancel"]
        for frame in cases:
            with self.subTest(frame=frame[-60:]):
                self.assertTrue(td.is_busy(frame))
                self.assertEqual(td.limit_notice(frame), '')

    def test_background_agents_still_hold_even_with_quota_menu(self):
        frame = '────────────────────\n⏺ main\n◯ helper Running checks 1m 2s\n' + FRAME
        self.assertEqual(td.background_agents(frame), ['helper'])
        self.assertTrue(td.is_busy(frame))
        self.assertEqual(td.limit_notice(frame), '')

    def test_quota_notice_outside_local_tail_does_not_hide_cancel(self):
        frame = FRAME.replace('   What do you want to do?', '\n'.join(['older UI'] * 12) + '\n   What do you want to do?')
        self.assertTrue(td.is_busy(frame))
        self.assertEqual(td.limit_notice(frame), '')

    def test_existing_same_line_notice_behavior_is_preserved(self):
        frame = 'Usage limit reached · continuing automatically at 12pm · Esc to cancel'
        self.assertFalse(td.is_busy(frame))
        self.assertTrue(td.limit_notice(frame))

    def test_quota_menu_cannot_relabel_a_prompt_that_already_began_sending(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            journal = {'version': 1, 'token': 'fixture-intent', 'sha256': 'f' * 64, 'phase': 'send-intent'}
            write_delivery(root, 'attempt', journal)
            driver = td.Driver('claude', 'Riker', root, CapturedTmux(FRAME), 1, root, 'fixture-session',
                               'resume', 'attempt', ['claude'], out=io.StringIO(),
                               env={'_MESH_RUNNER_PROMPT_INTENT': journal['token'], '_MESH_RUNNER_PROMPT_SHA256': journal['sha256']})
            with self.assertRaisesRegex(td.DriverError, 'cannot prove this prompt was unsent'):
                driver.turn('fixture prompt')
            self.assertEqual(read_delivery(root, 'attempt'), journal)

    def test_real_turn_reports_quota_before_sleep_paste_or_send_intent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            journal = {'version': 1, 'token': 'fixture-intent', 'sha256': 'f' * 64, 'phase': 'received'}
            write_delivery(root, 'attempt', journal)
            boundary = CapturedTmux(FRAME)
            def no_sleep(seconds):
                self.fail('quota frame must not enter busy wait')
            driver = td.Driver('claude', 'Riker', root, boundary, 1, root, 'fixture-session',
                               'resume', 'attempt', ['claude'], out=io.StringIO(),
                               env={'_MESH_RUNNER_PROMPT_INTENT': journal['token'], '_MESH_RUNNER_PROMPT_SHA256': journal['sha256']},
                               sleep=no_sleep)
            with self.assertRaises(td.DriverError) as caught:
                driver.turn('fixture prompt, never sent')
            self.assertTrue(caught.exception.close)
            self.assertIn('usage limit reached:', str(caught.exception))
            self.assertEqual(read_delivery(root, 'attempt'), dict(journal, phase='pre-send-failed', harness='claude', failure_kind='quota'))
            self.assertTrue(boundary.calls)
            self.assertTrue(all(call[0] == 'capture-pane' for call in boundary.calls))


if __name__ == '__main__':
    unittest.main()
