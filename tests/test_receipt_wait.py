"""Receipt WAIT: durable registration + cursor, strict metadata, one wake, no loss between arm and declare."""
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from uuid import UUID, uuid4

from mesh_runner import receipt_wait as rw
from mesh_runner.outcome import parse_line
from mesh_runner.store import Store
from fakes import FakeMesh, task
from test_runner import Bench

import datetime as dt

NOW = 1792500000
OWNER = str(UUID(int=7))
SOURCE = str(UUID(int=8))
LANE = 'lane'


def record(**over):
    ask = str(uuid4())
    rec = {'kind': 'text', 'event': 'accepted', 'event_id': str(uuid4()), 'schema_version': 1,
           'producer': 'pavel-gate-alert', 'batch_id': str(uuid4()), 'request_id': str(uuid4()),
           'asks': [{'task_id': ask, 'ask_id': 'a' * 16, 'evidence_ref': 'task:' + ask}], 'task_ids': [ask],
           'bot_identity': '3f9147cca396bb68', 'chat_id': '134677653', 'message_id': 7,
           'provider_ts': NOW + 5, 'time_source': 'provider', 'terminal': True}
    rec.update(over)
    return {k: v for k, v in rec.items() if v is not ...}


def line(rec):
    return (json.dumps(rec) + '\n').encode()


class Journals(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.path = self.dir / 'bob' / rw.JOURNAL_NAME
        self.path.parent.mkdir()
        self.s = Store(self.dir / 'runner.sqlite')
        self.addCleanup(self.s.db.close)

    def arm(self, **journals):
        return rw.arm(self.s, LANE, SOURCE, OWNER, journals or {'bob': str(self.path)}, NOW)

    def append(self, *raw, path=None):
        with open(path or self.path, 'ab') as fh:
            for r in raw:
                fh.write(r if isinstance(r, bytes) else line(r))

    def scan(self):
        return rw.scan(self.s, LANE, SOURCE)

    # --- AC1: strict metadata, with a positive control for every negative
    def test_positive_control_accepted_receipt_wakes_with_allowlisted_evidence_only(self):
        self.arm()
        rec = record(text='PRIVATE-BODY', document_name='secret.pdf')
        self.append(rec)
        got = self.scan()
        self.assertEqual(got['message_id'], 7)
        self.assertEqual(got['task_id'], rec['task_ids'][0])
        self.assertNotIn('PRIVATE-BODY', json.dumps(got))
        self.assertNotIn('secret.pdf', json.dumps(rw.inspect(self.s, LANE, SOURCE)))

    def test_every_negative_row_stays_asleep(self):
        self.arm()
        ts = NOW + 5
        bad = [record(event=e) for e in ('missing', 'enqueued', 'suppressed', 'failed', 'unknown')]
        bad += [record(kind='document'), record(correlation='legacy_uncorrelated', task_ids=[], asks=[]),
                record(schema_version=True), record(schema_version=2), record(producer='other'),
                record(request_id='not-a-uuid'), record(batch_id='AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA'),
                record(task_ids=['x'], asks=[]), record(task_ids=[], asks=[{'task_id': 'x'}]),
                record(task_ids=[], asks=[]), record(bot_identity=''), record(bot_identity='a b'),
                record(chat_id='abc'), record(chat_id=True), record(chat_id=...), record(message_id=True),
                record(message_id=0), record(message_id='7'), record(provider_ts=True), record(provider_ts=0),
                record(time_source='observed'), record(provider_ts=NOW - 1), record(provider_ts=ts + 0.5)]
        for rec in bad:
            self.append(rec)
        self.append(b'{not json}\n', b'[]\n', b'"x"\n')
        self.assertIsNone(self.scan())
        self.append(record(message_id=99))                 # the positive control still works after them
        self.assertEqual(self.scan()['message_id'], 99)

    def test_task_correlation_from_asks_alone_and_receipt_task_need_not_be_the_source(self):
        self.arm()
        tid = str(uuid4())
        self.append(record(task_ids=[], asks=[{'task_id': tid}]))
        got = self.scan()
        self.assertEqual(got['task_id'], tid)
        self.assertNotEqual(got['task_id'], SOURCE)

    # --- AC1: registration, history, absence, partial rows, rotation
    def test_history_before_arming_does_not_wake_and_arm_is_idempotent(self):
        self.append(record(provider_ts=NOW + 1), record(message_id=2, provider_ts=NOW + 2))
        reg = self.arm()
        self.assertEqual(reg['journals']['bob']['offset'], self.path.stat().st_size)
        self.assertIsNone(self.scan())
        self.assertEqual(self.arm(), reg)                  # same identity and baseline
        # a historical row appended after arming still cannot pass the provider-time floor
        self.append(record(message_id=3, provider_ts=NOW - 100))
        self.assertIsNone(self.scan())

    def test_row_partial_at_arm_time_is_history_even_when_completed_later(self):
        raw = line(record())
        self.append(line(record(event='failed')), raw[:40])
        reg = self.arm()
        self.assertEqual(reg['journals']['bob']['offset'], self.path.stat().st_size)
        self.append(raw[40:])
        self.assertIsNone(self.scan())
        self.append(record(message_id=8))                 # control: the next complete row wakes
        self.assertEqual(self.scan()['message_id'], 8)

    def test_pre_arm_partial_whose_suffix_is_itself_valid_json_is_still_history(self):
        self.append(b'  ')                                # whitespace prefix of an unfinished row
        self.arm()
        self.append(line(record(message_id=6)))           # its "rest" would parse alone as a valid receipt
        self.assertIsNone(self.scan())
        self.append(record(message_id=9))
        self.assertEqual(self.scan()['message_id'], 9)

    def test_unfinished_pre_arm_row_keeps_waiting_for_its_newline(self):
        self.append(b'{"kind":')
        self.arm()
        self.append(b'"te')
        self.assertIsNone(self.scan())
        self.append(b'xt"}\n', record(message_id=4))
        self.assertEqual(self.scan()['message_id'], 4)

    def test_missing_journal_is_registered_absent_then_new_complete_lines_count(self):
        reg = self.arm()
        self.assertEqual(reg['journals']['bob']['state'], 'absent')
        self.assertIsNone(self.scan())
        self.append(record())
        self.assertEqual(self.scan()['message_id'], 7)
        self.assertEqual(rw.inspect(self.s, LANE, SOURCE)['journals']['bob']['state'], 'tracking')

    def test_partial_line_is_not_consumed_until_complete(self):
        self.arm()
        raw = line(record())
        self.append(raw[:40])
        self.assertIsNone(self.scan())
        self.assertEqual(rw.inspect(self.s, LANE, SOURCE)['journals']['bob']['offset'], 0)
        self.append(raw[40:])
        self.assertEqual(self.scan()['message_id'], 7)

    def test_cursor_and_pending_survive_restart(self):
        self.arm()
        self.append(record(message_id=1, event='failed'), record(message_id=2))
        self.scan()
        self.s.db.close()
        self.s = Store(self.dir / 'runner.sqlite')
        state = rw.inspect(self.s, LANE, SOURCE)
        self.assertEqual(state['pending']['message_id'], 2)
        self.assertEqual(state['journals']['bob']['offset'], self.path.stat().st_size)
        self.append(record(message_id=3))
        self.assertEqual(self.scan()['message_id'], 2)     # the first receipt stays the evidence

    def test_rotation_truncation_removal_and_symlink_fail_closed(self):
        for how in ('rotate', 'truncate', 'remove', 'symlink'):
            with self.subTest(how):
                self.s.db.execute('DELETE FROM marks')
                self.path.unlink(missing_ok=True)
                self.append(record(event='failed'))
                self.arm()
                self.append(record(event='failed'))
                if how == 'rotate':
                    os.rename(self.path, self.path.with_suffix('.1'))
                    self.append(record())
                elif how == 'truncate':
                    self.path.write_bytes(b'')
                    self.append(record())
                elif how == 'remove':
                    self.path.unlink()
                elif how == 'symlink':
                    target = self.dir / 'elsewhere'
                    target.write_bytes(line(record()))
                    self.path.unlink()
                    os.symlink(target, self.path)
                self.assertIsNone(self.scan())
                self.assertEqual(rw.inspect(self.s, LANE, SOURCE)['journals']['bob']['state'], 'blocked')
                self.path.with_suffix('.1').unlink(missing_ok=True)
        with self.assertRaisesRegex(ValueError, 'not a regular file'):
            self.s.db.execute('DELETE FROM marks')
            self.arm()

    def test_blocked_journal_resumes_only_after_explicit_rebaseline(self):
        self.append(record(event='failed'))
        self.arm()
        self.path.write_bytes(b'')
        self.scan()
        self.append(record())
        self.assertIsNone(self.scan())
        rw.rebaseline(self.s, LANE, SOURCE, 'bob')
        self.assertIsNone(self.scan())                     # the row written before is skipped, not woken
        self.append(record(message_id=5))
        self.assertEqual(self.scan()['message_id'], 5)

    def test_oversize_and_corrupt_state_fail_closed(self):
        self.arm()
        self.append(b'x' * (rw.MAX_LINE + 10))
        self.assertIsNone(self.scan())
        self.assertEqual(rw.inspect(self.s, LANE, SOURCE)['journals']['bob']['reason'], 'line_too_long')
        self.s.mark(LANE, SOURCE, rw.REG_KEY, '{broken')
        self.assertIsNone(self.scan())
        self.assertFalse(rw.inspect(self.s, LANE, SOURCE)['registered'])
        with self.assertRaises(ValueError):
            rw.registration(self.s, LANE, SOURCE)

    def test_complete_overlong_row_blocks_the_journal_and_earlier_rows_still_wake(self):
        self.arm()
        self.append(record(message_id=3), b'x' * (rw.MAX_LINE + 10) + b'\n', record(message_id=4))
        got = self.scan()
        self.assertEqual(got['message_id'], 3)                          # the row before it was delivered
        j = rw.inspect(self.s, LANE, SOURCE)['journals']['bob']
        self.assertEqual((j['state'], j['reason']), ('blocked', 'line_too_long'))
        self.s.mark(LANE, SOURCE, rw.PENDING_KEY, '')
        self.assertIsNone(self.scan())                                  # blocked: the row after it is not read

    def test_over_long_row_that_was_partial_at_arm_time_blocks_instead_of_being_skipped(self):
        self.append(b'{"kind":')
        self.arm()
        self.append(b'y' * (rw.MAX_LINE + 10) + b'\n', record(message_id=4))
        self.assertIsNone(self.scan())
        j = rw.inspect(self.s, LANE, SOURCE)['journals']['bob']
        self.assertEqual((j['state'], j['reason']), ('blocked', 'line_too_long'))

    def test_concurrent_disarm_is_not_undone_by_a_scan_in_flight(self):
        self.arm()
        self.append(record(message_id=3))
        real = rw._read

        def read_then_disarm(path, cur):
            out = real(path, cur)
            rw.disarm(self.s, LANE, SOURCE)                             # the operator acts mid-scan
            return out
        with mock.patch.object(rw, '_read', side_effect=read_then_disarm):
            self.assertIsNone(self.scan())
        state = rw.inspect(self.s, LANE, SOURCE)
        self.assertEqual(state['state'], 'disarmed')
        self.assertIsNone(state['pending'])

    def test_bad_arm_inputs(self):
        with self.assertRaises(ValueError):
            rw.arm(self.s, LANE, 'short', OWNER, {'bob': str(self.path)}, NOW)
        with self.assertRaises(ValueError):
            rw.arm(self.s, LANE, SOURCE, OWNER, {'bob': 'relative/telegram-receipts.jsonl'}, NOW)
        with self.assertRaises(ValueError):
            rw.arm(self.s, LANE, SOURCE, OWNER, {'bob': str(self.dir / 'passwd')}, NOW)
        self.arm()
        with self.assertRaisesRegex(ValueError, 'another owner or journals'):
            rw.arm(self.s, LANE, SOURCE, str(UUID(int=9)), {'bob': str(self.path)}, NOW)

    # --- AC2: dedup of provider tuple and the rollback that keeps history
    def test_same_provider_message_under_other_event_ids_is_one_receipt(self):
        self.arm()
        self.append(record(message_id=4))
        first = self.scan()
        self.append(record(message_id=4, event_id=str(uuid4())), record(message_id=4, event_id=str(uuid4())))
        self.assertEqual(self.scan(), first)
        seen = [k for k in self.s.db.execute("SELECT key FROM marks WHERE key LIKE 'receipt_seen:%'")]
        self.assertEqual(len(seen), 1)
        # the dedup mark itself, not only the one-shot pending: with the pending evidence gone,
        # a replay of the same provider message under a new event_id still creates nothing
        self.s.mark(LANE, SOURCE, rw.PENDING_KEY, '')
        self.append(record(message_id=4, event_id=str(uuid4())))
        self.assertIsNone(self.scan())
        self.append(record(message_id=5))                 # control: another provider message does
        self.assertEqual(self.scan()['message_id'], 5)

    def test_read_retry_after_a_crash_before_commit_rereads_the_same_lines_once(self):
        self.arm()
        self.append(record(message_id=4))
        real = self.s.mark
        calls = []

        def crash(agent, task_id, key, value=None):
            calls.append(key)
            if key == rw.PENDING_KEY and value is not None:
                raise RuntimeError('crash before commit')
            return real(agent, task_id, key, value)
        with mock.patch.object(self.s, 'mark', side_effect=crash), self.assertRaises(RuntimeError):
            self.scan()
        self.assertFalse(self.s.db.in_transaction)
        state = rw.inspect(self.s, LANE, SOURCE)
        self.assertIsNone(state['pending'])
        self.assertEqual(state['journals']['bob']['state'], 'absent')     # nothing was half-committed
        self.assertEqual(self.scan()['message_id'], 4)

    def test_disarm_and_rearm_keep_baseline_and_dedup_history_but_delivery_starts_a_new_generation(self):
        reg = self.arm()
        self.append(record(message_id=4))
        self.scan()
        rw.disarm(self.s, LANE, SOURCE)
        self.append(record(message_id=5))
        self.assertIsNone(self.scan())                                     # disarmed: no observation
        again = self.arm()
        self.assertEqual(again['reg_id'], reg['reg_id'])
        self.assertEqual(self.scan()['message_id'], 4)
        self.s.mark(LANE, SOURCE, rw.DELIVERED_KEY, reg['reg_id'])
        self.assertIsNone(self.scan())                                     # one-shot
        fresh = self.arm()
        self.assertNotEqual(fresh['reg_id'], reg['reg_id'])
        self.assertIsNone(self.scan())                                     # old rows are history for the new one


class Cli(Journals):
    def run_cli(self, *argv, flag=True):
        import contextlib
        import io
        from types import SimpleNamespace
        from mesh_runner.cli import main
        cfg = SimpleNamespace(state_dir=self.dir, agents=[SimpleNamespace(name=LANE)], receipt_wait_v1=flag)
        with mock.patch('mesh_runner.cli.config_mod.load', return_value=cfg), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            code = main(['receipt-wait', '--config', 'unused', '--agent', LANE, '--task', SOURCE, *argv])
        return code, json.loads(out.getvalue())

    def test_arm_inspect_disarm_rebaseline_round_trip_and_refusals(self):
        self.append(record(provider_ts=NOW + 1))
        code, out = self.run_cli('arm', '--owner-id', OWNER, '--journal', f'bob={self.path}')
        self.assertEqual((code, out['state'], out['journals']['bob']['state']), (0, 'armed', 'tracking'))
        again = self.run_cli('arm', '--owner-id', OWNER, '--journal', f'bob={self.path}')[1]
        self.assertEqual(again['reg_id'], out['reg_id'])
        self.assertEqual(self.run_cli('inspect')[1]['delivered'], False)
        self.assertEqual(self.run_cli('disarm')[1]['state'], 'disarmed')
        self.assertEqual(self.run_cli('rebaseline', '--name', 'bob')[0], 0)
        code, out = self.run_cli('arm', '--owner-id', OWNER, '--journal', f'bob={self.dir}/x/other.jsonl')
        self.assertEqual(code, 1)
        self.assertIn('error', out)
        self.assertEqual(self.run_cli('inspect', flag=False)[0], 2)      # control: flag off refuses every action


class Syntax(unittest.TestCase):
    def test_parse_and_negative_controls(self):
        now = dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc)
        out = parse_line('⏳ WAIT receipt:#a7a01d47', now)
        self.assertEqual((out.kind, out.target, out.is_wait), ('receipt', 'a7a01d47', True))
        self.assertEqual(parse_line('⏳ WAIT receipt:#a7a01d47 — first delivery', now).kind, 'receipt')
        self.assertEqual(parse_line('⏳ WAIT card:#a7a01d47', now).kind, 'card')           # control: card still parses
        for bad in ('⏳ WAIT receipt:a7a01d47x', '⏳ WAIT receipt:/tmp/telegram-receipts.jsonl', '⏳ WAIT receipt:#abc',
                    '⏳ WAIT receipt:'):
            self.assertEqual(parse_line(bad, now).kind, 'unparsed', bad)


class RunnerReceipt(Bench):
    """The runner side: declaration binds to the live registration; the wake is atomic and one-shot."""
    def setUp(self):
        super().setUp()
        self.cfg.receipt_wait_v1 = True
        self.journal = self.root / 'bob' / rw.JOURNAL_NAME
        self.journal.parent.mkdir()
        self.mesh_task = task(1)
        self.r = self.runner(FakeMesh([self.mesh_task]))
        self.r.refresh_me()
        self.a = self.r.store.create(self.agent.name, self.mesh_task['id'], 'claude')
        self.r.store.update(self.a, state='running', turns=1)

    def arm(self, owner=None):
        return rw.arm(self.r.store, self.agent.name, self.a.task_id, owner or self.r.agent_id,
                      {'bob': str(self.journal)}, NOW)

    def declare(self):
        path = self.root / 'turn.jsonl'
        path.write_text(json.dumps(dict(type='result', subtype='success', is_error=False,
                        result=f'⏳ WAIT receipt:#{self.a.task8}', session_id='s')) + '\n')
        self.r.store.update(self.a, state='running', log_path=str(path))
        self.r.settle(self.a)

    def append(self, **over):
        with open(self.journal, 'ab') as fh:
            fh.write(line(record(**over)))

    def waiting(self):
        self.arm()
        self.declare()
        self.assertEqual((self.a.state, self.a.wait['kind']), ('waiting', 'receipt'))

    def test_declaration_without_a_live_registration_is_sent_back_with_guidance(self):
        self.declare()
        self.assertNotEqual(self.a.state, 'waiting')
        self.assertNotEqual(self.a.wait.get('kind'), 'receipt')
        self.assertIsNone(self.r.store.mark(self.agent.name, self.a.task_id, rw.REG_KEY))
        self.arm()                                       # control: the same declaration binds once armed
        self.declare()
        self.assertEqual((self.a.state, self.a.wait['kind']), ('waiting', 'receipt'))

    def test_declaration_with_wrong_owner_or_disarmed_registration_is_refused(self):
        self.arm(owner=str(UUID(int=99)))
        self.declare()
        self.assertNotEqual(self.a.state, 'waiting')
        rw.disarm(self.r.store, self.agent.name, self.a.task_id)
        self.assertIsNone(self.r._receipt_registration(self.a, parse_line(f'⏳ WAIT receipt:#{self.a.task8}', dt.datetime.now(dt.timezone.utc))))

    def test_receipt_before_the_declaration_is_kept_and_wakes_once(self):
        self.arm()
        self.r._scan_receipts()
        self.append()                                   # lands between arming and the declaration
        self.r._scan_receipts()
        self.declare()
        self.assertEqual(self.a.state, 'waiting')
        self.r._wake_one(self.a)
        self.assertEqual(self.a.state, 'ready')
        self.assertIn('provider-accepted Telegram text receipt', self.a.wake_text)
        self.assertNotIn('PRIVATE', self.a.wake_text)
        self.assertTrue(rw.delivered(self.r.store, self.agent.name, self.a.task_id, self.a.wait['receipt_reg']))

    def test_nothing_wakes_without_a_receipt_and_history_is_ignored(self):
        self.append(provider_ts=NOW - 50)
        self.waiting()
        for _ in range(3):
            self.r._wake_one(self.a)
        self.assertEqual(self.a.state, 'waiting')
        self.append(event='failed')
        self.r._wake_one(self.a)
        self.assertEqual(self.a.state, 'waiting')

    def test_duplicates_retry_and_restart_never_wake_twice(self):
        self.waiting()
        self.append(message_id=11)
        self.r._wake_one(self.a)
        self.assertEqual(self.a.state, 'ready')
        # admission refusal keeps the ready outbox; a repeat tick does not add another wake
        text = self.a.wake_text
        self.append(message_id=11, event_id=str(uuid4()))
        self.r._wake_one(self.a)
        self.assertEqual((self.a.state, self.a.wake_text), ('ready', text))
        # the session consumes it, declares again: the delivered registration cannot be re-bound
        self.declare()
        self.assertNotEqual(self.a.state, 'waiting')
        r2 = self.runner(self.r.mesh)                      # restart: the same store file, a new process
        r2.refresh_me()
        r2._scan_receipts()
        r2._wake_one(r2.store.get(self.a.id))
        self.assertNotEqual(r2.store.get(self.a.id).state, 'waiting')
        self.assertTrue(rw.delivered(r2.store, self.agent.name, self.a.task_id, self.a.wait.get('receipt_reg') or
                                     rw.registration(r2.store, self.agent.name, self.a.task_id)['reg_id']))

    def test_stale_generation_or_owner_cannot_wake(self):
        self.waiting()
        wait = copy.deepcopy(self.a.wait)
        self.r.store.update(self.a, wait=dict(wait, receipt_reg=str(uuid4())))
        self.append()
        self.r._wake_one(self.a)
        self.assertEqual(self.a.state, 'waiting')
        self.r.store.update(self.a, wait=wait)
        reg = rw.registration(self.r.store, self.agent.name, self.a.task_id)
        reg['owner_id'] = str(UUID(int=99))
        self.r.store.mark(self.agent.name, self.a.task_id, rw.REG_KEY, json.dumps(reg))
        self.r._wake_one(self.a)
        self.assertEqual(self.a.state, 'waiting')

    def test_flag_off_observes_nothing(self):
        self.waiting()
        self.cfg.receipt_wait_v1 = False
        self.append()
        self.r._wake_one(self.a)
        self.assertEqual(self.a.state, 'waiting')
        self.assertIsNone(rw.pending(self.r.store, self.agent.name, self.a.task_id, self.a.wait['receipt_reg']))

    def test_failed_commit_does_not_acknowledge_delivery(self):
        self.waiting()
        self.append()
        with mock.patch.object(self.r.store, 'update_with_marks', return_value=False):
            self.r._wake_one(self.a)
        self.assertFalse(rw.delivered(self.r.store, self.agent.name, self.a.task_id, self.a.wait['receipt_reg']))
        self.r._wake_one(self.a)                           # the pending evidence was kept: it wakes now
        self.assertEqual(self.a.state, 'ready')


if __name__ == '__main__':
    unittest.main()
