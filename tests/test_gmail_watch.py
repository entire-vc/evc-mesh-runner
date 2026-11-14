"""Gmail WAIT: real parser + real store + real outbox/receiver; fakes only at the provider reader boundary.

Fixtures F1..F24 of the contract (docs/gmail-watch.md). The red control at the bottom runs the same
scenarios against a stub ``evaluate`` that returns every record: the filtering tests must fail on it.
"""
import datetime as dt
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from uuid import UUID

from mesh_runner import gmail_watch as gw
from mesh_runner.store import Store
from fakes import FakeMesh, task, typed, turn_rows, wait_registered
from test_runner import Bench

MAILBOX = 'box@example.org'
OBS = str(UUID(int=11))
SRC = str(UUID(int=12))
RESUME = 'agent-1'             # the lane's Mesh id in the fake Mesh
OTHER_OBS = str(UUID(int=14))
SENDER = 'sender@example.net'
THREAD = '1a16090fd8704d9e'
T0 = 1_793_000_000_000            # baseline, epoch ms
NOW = T0 + 3_600_000


def rec(mid, ms, frm=SENDER, thread=THREAD, labels=('INBOX',)):
    return {'message_id': mid, 'thread_id': thread, 'internal_ms': ms, 'from': f'Name <{frm}>', 'labels': list(labels)}


class FakeReader:
    """The only fake: the provider boundary. Records every request it is asked."""
    def __init__(self, records=(), now_ms=None):
        self.records, self.now_ms, self.fail, self.requests = list(records), now_ms, None, []

    def __call__(self, request):
        self.requests.append(request)
        if self.fail:
            raise self.fail
        return gw.Read([dict(r) for r in self.records], self.now_ms)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'runner.sqlite'
        self.s = Store(self.path)
        self.addCleanup(self.s.db.close)
        self.baseline_records = [rec('m0000001', T0 - 5000), rec('m0000002', T0 - 1000, labels=('SENT',))]
        self.reader = FakeReader(self.baseline_records)

    def thread_pred(self):
        return gw.build_predicate(from_addr=SENDER, thread=THREAD)

    def search_pred(self, domains=('vendor.example',), markers=('Release approval',)):
        return gw.build_predicate(domains=list(domains), markers=list(markers))

    def register(self, predicate=None, reader=None, **kw):
        kw.setdefault('observer_task', OBS)
        kw.setdefault('source_task', SRC)
        return gw.register(self.s, reader or self.reader, mailbox=MAILBOX, resume_agent=RESUME,
                           predicate=predicate or self.thread_pred(), **kw)

    def tick(self, reg_id, at=NOW, reader=None):
        return gw.tick(self.s, reader or self.reader, reg_id, at, rng=mock.Mock(uniform=lambda a, b: 0))

    def count(self, table, where='1=1', args=()):
        return self.s.db.execute(f'SELECT COUNT(*) FROM {table} WHERE {where}', args).fetchone()[0]


class Filtering(Base):
    def test_F1_new_exact_inbound_one_receipt_one_wake(self):
        reg = self.register()['registration_id']
        self.reader.records.append(rec('m0000003', T0 + 1000))
        t = self.tick(reg)
        self.assertEqual((t.outcome, t.message_id), ('fired', 'm0000003'))
        self.assertEqual(self.count('gmail_receipt'), 1)
        self.assertEqual([r['kind'] for r in gw.pending(self.s.db, reg, 'wake')], ['wake'])
        self.assertIn('message_id=m0000003', gw.wake_text(self.s, reg)[1])
        self.assertEqual(self.tick(reg).outcome, 'skipped')        # fired: nothing more happens

    def test_F2_to_F6_negatives_do_not_fire(self):
        reg = self.register()['registration_id']
        self.reader.records += [
            rec('m00000a2', T0 + 10, labels=('SENT',)),                 # F2 our own SENT in the thread
            rec('m00000a3', T0 - 1500),                                 # F3 older than the baseline (T0-1000)
            rec('m00000a4', T0 + 10, frm='other@example.net'),          # F4 other From
            rec('m00000a5', T0 + 10, thread='1aaaaaaaaaaaaaaa'),        # F5 other thread
        ]
        self.assertEqual(self.tick(reg).outcome, 'advanced')
        self.assertEqual((self.count('gmail_receipt'), self.count('gmail_outbox')), (0, 0))
        # F6: a label-only change of an old, already-seen message
        self.reader.records[0]['labels'] = ['INBOX', 'IMPORTANT', 'STARRED']
        self.assertEqual(self.tick(reg).outcome, 'advanced')
        self.assertEqual(self.count('gmail_outbox'), 0)
        # positive control: the same registration still fires on a real match
        self.reader.records.append(rec('m00000a6', T0 + 20))
        self.assertEqual(self.tick(reg).outcome, 'fired')

    def test_F7_same_response_twice_still_one(self):
        reg = self.register()['registration_id']
        self.reader.records.append(rec('m0000003', T0 + 1000))
        self.tick(reg)
        self.tick(reg)
        self.assertEqual((self.count('gmail_receipt'), self.count('gmail_outbox', "kind='wake'")), (1, 1))

    def test_F11_two_registrations_only_the_matching_one_fires(self):
        a = self.register()['registration_id']
        b = self.register(self.search_pred(), observer_task=SRC, source_task=SRC,
                          reader=FakeReader([], now_ms=T0))['registration_id']
        self.reader.records.append(rec('m0000003', T0 + 1000))
        self.assertEqual(self.tick(a).outcome, 'fired')
        self.assertEqual(self.tick(b, reader=FakeReader([rec('m0000003', T0 + 1000)])).outcome, 'advanced')
        self.assertEqual(gw.status(self.s, b)['state'], 'active')

    def test_F12_domain_boundary_and_lookalikes(self):
        reg = self.register(self.search_pred(), reader=FakeReader([], now_ms=T0))['registration_id']
        bad = ['x@notvendor.example', 'x@vendor.example.evil.tld', 'x@evilvendor.example', 'x@other.org']
        self.reader.records = [rec(f'm00000b{i}', T0 + 10 + i, frm=f, thread='1bbbbbbbbbbbbbbb') for i, f in enumerate(bad)]
        self.assertEqual(self.tick(reg).outcome, 'advanced')
        self.assertEqual(self.count('gmail_receipt'), 0)
        for ok in ('mail.vendor.example', 'vendor.example', 'MAIL.Vendor.EXAMPLE'):   # positive controls
            self.assertTrue(gw.domain_matches(gw.norm_domain(ok), 'vendor.example'), ok)
        self.reader.records.append(rec('m00000bf', T0 + 99, frm='noreply@mail.vendor.example', thread='1bbbbbbbbbbbbbb2'))
        self.assertEqual(self.tick(reg).outcome, 'fired')

    def test_F12_display_name_tricks_do_not_match(self):
        reg = self.register(self.search_pred(), reader=FakeReader([], now_ms=T0))['registration_id']
        self.reader.records = [{'message_id': 'm00000c1', 'thread_id': 't0000001', 'internal_ms': T0 + 5,
                                'from': '"a@vendor.example" <x@evil.example>', 'labels': []},
                               {'message_id': 'm00000c2', 'thread_id': 't0000001', 'internal_ms': T0 + 6,
                                'from': 'a@vendor.example, b@evil.example', 'labels': []}]
        self.assertEqual(self.tick(reg).outcome, 'advanced')

    def test_marker_form_proves_arrival_only_in_the_comment(self):
        reg = self.register(self.search_pred(), reader=FakeReader([], now_ms=T0))['registration_id']
        self.reader.records = [rec('m00000d1', T0 + 5, frm='a@vendor.example', thread='t0000009')]
        self.tick(reg)
        body = json.loads(gw.pending(self.s.db, reg, 'comment')[0]['payload_json'])['text']
        self.assertIn('not that it answers our request', body)
        self.assertNotIn('Release approval', body)                           # no marker/subject/body text


class Registration(Base):
    def test_baseline_seeds_known_thread_ids_and_is_idempotent(self):
        out = self.register()
        self.assertEqual(out['baseline'], {'max_internal_ms': T0 - 1000, 'seen_ids_count': 2})
        again = self.register()
        self.assertTrue(again['existing'])
        self.assertEqual(again['registration_id'], out['registration_id'])
        self.assertEqual(self.count('gmail_registration'), 1)

    def test_dry_run_reads_and_writes_nothing(self):
        out = self.register(dry_run=True)
        self.assertTrue(out['dry_run'])
        self.assertEqual(self.count('gmail_registration'), 0)
        self.assertEqual(len(self.reader.requests), 1)

    def test_form_b_baseline_is_provider_time_never_the_local_clock(self):
        out = self.register(self.search_pred(), reader=FakeReader([], now_ms=T0 + 7))
        self.assertEqual(out['baseline']['max_internal_ms'], T0 + 7)
        with self.assertRaises(ValueError):                              # no provider time at all: refuse
            self.register(self.search_pred(markers=('other',)), reader=FakeReader([]))

    def test_F21_since_catch_up_fires_the_existing_match_on_the_first_tick(self):
        existing = rec('m00000e1', T0 + 500, frm='a@vendor.example', thread='t0000007')
        reader = FakeReader([existing], now_ms=T0 + 900)
        reg = self.register(self.search_pred(), reader=reader, since_ms=T0)['registration_id']
        self.assertEqual(self.count('gmail_seen', 'registration_id=?', (reg,)), 0)
        self.assertEqual(self.tick(reg, reader=reader).outcome, 'fired')

    def test_F22_same_register_without_since_does_not_fire_for_it(self):
        existing = rec('m00000e1', T0 + 500, frm='a@vendor.example', thread='t0000007')
        reader = FakeReader([existing], now_ms=T0 + 900)
        reg = self.register(self.search_pred(), reader=reader)['registration_id']
        self.assertEqual(self.tick(reg, reader=reader).outcome, 'advanced')
        self.assertEqual(self.count('gmail_receipt'), 0)

    def test_F23_markers_with_query_syntax_are_refused_not_escaped(self):
        for bad in ('a"b', 'x OR {y}', '-from:me', 'to:me', "it's", 'a\\b', '(x)', 'ctl\x07', '', 'x' * 81, ' pad'):
            with self.assertRaises(ValueError, msg=repr(bad)):
                gw.validate_marker(bad)
        self.assertEqual(gw.validate_marker('Release approval'), 'Release approval')
        with self.assertRaises(ValueError):
            gw.build_predicate(domains=['vendor.example'], markers=['a"b'])
        reg = self.register(self.search_pred(), reader=FakeReader([], now_ms=T0))['registration_id']
        self.tick(reg, reader=self.reader)
        req = self.reader.requests[-1]
        self.assertEqual(sorted(req), ['after_s', 'form', 'mailbox', 'markers', 'sender_domains'])   # structured, no query

    def test_search_and_thread_form_cannot_be_mixed(self):
        with self.assertRaises(ValueError):
            gw.build_predicate(from_addr=SENDER, thread=THREAD, domains=['a.com'], markers=['x'])
        with self.assertRaises(ValueError):
            gw.build_predicate(from_addr=SENDER)

    def test_bad_ids_are_refused(self):
        with self.assertRaises(ValueError):
            self.register(observer_task='not-a-uuid')


class Capture(Base):
    def test_F15_message_between_register_snapshot_and_commit_fires_on_first_tick(self):
        late = rec('m0000f15', T0 + 50)
        reader = self.reader

        class Racing(FakeReader):
            def __call__(inner, request):                                # the message lands right after the snapshot read
                out = FakeReader.__call__(inner, request)
                inner.records.append(late)
                return out
        racing = Racing(self.baseline_records)
        reg = self.register(reader=racing)['registration_id']
        self.assertNotIn(late['message_id'], {r[0] for r in self.s.db.execute('SELECT message_id FROM gmail_seen')})
        self.assertEqual(self.tick(reg, reader=racing).outcome, 'fired')

    def test_F16_message_between_tick_read_and_commit_fires_on_next_tick(self):
        reg = self.register()['registration_id']
        late = rec('m0000f16', T0 + 70)

        class Racing(FakeReader):
            def __call__(inner, request):
                out = FakeReader.__call__(inner, request)
                inner.records.append(late)                                # after this read
                return out
        racing = Racing(self.baseline_records)
        self.assertEqual(self.tick(reg, reader=racing).outcome, 'advanced')
        self.assertEqual(self.tick(reg, reader=racing).outcome, 'fired')

    def test_F17_late_indexed_message_below_high_water_fires_once(self):
        reg = self.register()['registration_id']
        self.reader.records.append(rec('m0000a17', T0 + 5000, frm='other@example.net'))
        self.tick(reg)                                                    # high water moves to T0+5000
        self.assertGreater(self.s.db.execute('SELECT high_water_internal_ms FROM gmail_registration').fetchone()[0], T0)
        self.reader.records.append(rec('m0000b17', T0 + 2000))            # older than high water, inside the lookback
        self.assertEqual(self.tick(reg).outcome, 'fired')
        self.assertEqual(self.reader.requests[-1]['after_s'], (T0 + 5000) // 1000 - gw.LOOKBACK_S)

    def test_F18_cancel_vs_fire_lost_cas_leaves_no_receipt_and_no_outbox(self):
        reg = self.register()['registration_id']
        self.reader.records.append(rec('m0000003', T0 + 1000))
        real = self.reader.__call__

        def cancel_during_read(request):                                 # the operator cancels while the provider is read
            out = real(request)
            gw.cancel(self.s, reg, 'race')
            return out
        t = gw.tick(self.s, cancel_during_read, reg, NOW)
        self.assertEqual(t.outcome, 'skipped')
        self.assertEqual(self.count('gmail_receipt'), 0)
        self.assertEqual([r['idempotency_key'] for r in gw.pending(self.s.db, reg, 'wake')], [f'gmail:{reg}:cancelled:wake'])
        self.assertEqual(self.count('gmail_outbox', "kind='comment'"), 0)

    def test_F13_cancel_wakes_once_and_nothing_fires_later(self):
        reg = self.register()['registration_id']
        self.assertEqual(gw.cancel(self.s, reg, 'not needed')['state'], 'cancelled')
        gw.cancel(self.s, reg, 'again')
        self.assertEqual(self.count('gmail_outbox'), 1)
        self.reader.records.append(rec('m0000003', T0 + 1000))
        self.assertEqual(self.tick(reg).outcome, 'skipped')
        self.assertEqual(self.count('gmail_receipt'), 0)


class Failures(Base):
    def test_F9_errors_keep_baseline_back_off_and_alert_once(self):
        reg = self.register()['registration_id']
        before = dict(self.s.db.execute('SELECT baseline_internal_ms b, high_water_internal_ms h FROM gmail_registration').fetchone())
        seen = self.count('gmail_seen')
        self.reader.fail = gw.GmailError('auth')
        alerts, waits = [], []
        for i in range(8):
            t = self.tick(reg, at=NOW + i)
            alerts.append(t.alert)
            waits.append(self.s.db.execute('SELECT next_check_at - ? FROM gmail_registration', (NOW + i,)).fetchone()[0])
            self.assertEqual(t.outcome, 'error')
        self.assertEqual([a for a in alerts if a], ['auth'])            # one alert per class until recovery
        self.assertEqual(waits[:4], [30_000, 60_000, 120_000, 240_000])
        self.assertEqual(waits[-1], 1_800_000)                            # capped
        self.assertEqual(self.count('gmail_outbox'), 0)                   # no wake, no turn
        self.assertEqual(dict(self.s.db.execute('SELECT baseline_internal_ms b, high_water_internal_ms h FROM gmail_registration').fetchone()), before)
        self.assertEqual(self.count('gmail_seen'), seen)
        self.assertEqual(gw.status(self.s, reg)['state'], 'error')
        self.assertEqual(self.s.db.execute('SELECT state FROM gmail_registration').fetchone()[0], 'active')

    def test_F10_recovery_fires_once_for_a_message_that_arrived_during_the_outage(self):
        reg = self.register()['registration_id']
        self.reader.fail = gw.GmailError('transport')
        for i in range(6):
            self.tick(reg, at=NOW + i)
        self.reader.records.append(rec('m0000f10', T0 + 100))
        self.reader.fail = None
        t = self.tick(reg, at=NOW + 10)
        self.assertEqual((t.outcome, t.message_id), ('fired', 'm0000f10'))
        self.assertEqual(self.s.db.execute('SELECT consec_errors FROM gmail_registration').fetchone()[0], 0)

    def test_garbage_responses_are_parse_failures_not_wakes(self):
        reg = self.register()['registration_id']
        bad = lambda request: gw.Read('not a list')              # noqa: E731
        self.assertEqual(self.tick(reg, reader=bad).error_class, 'parse')
        junk = lambda request: gw.Read([{'message_id': 'x'}, {'from': 'a@b.c'}, 5, None])   # noqa: E731
        self.assertEqual(self.tick(reg, reader=junk).outcome, 'advanced')   # unusable records skipped, never guessed
        self.assertEqual(self.count('gmail_outbox'), 0)

    def test_reader_bug_is_a_transport_fault_not_a_crash(self):
        reg = self.register()['registration_id']
        self.reader.fail = RuntimeError('boom')
        self.assertEqual(self.tick(reg).error_class, 'transport')

    def test_subprocess_reader_maps_exit_codes_and_never_uses_a_shell(self):
        import subprocess
        for rc, cls in ((3, 'auth'), (4, 'rate'), (1, 'transport')):
            with mock.patch('subprocess.run', return_value=subprocess.CompletedProcess([], rc, b'', b'')) as run:
                with self.assertRaises(gw.GmailError) as ctx:
                    gw.SubprocessReader(['wrapper'])({'mailbox': MAILBOX})
                self.assertEqual(ctx.exception.cls, cls)
                self.assertNotIn('shell', run.call_args.kwargs)
                self.assertIsInstance(run.call_args.args[0], list)
        with mock.patch('subprocess.run', return_value=subprocess.CompletedProcess([], 0, b'<html>', b'')):
            with self.assertRaises(gw.GmailError) as ctx:
                gw.SubprocessReader(['wrapper'])({})
            self.assertEqual(ctx.exception.cls, 'parse')
        ok = json.dumps({'now_ms': 5, 'records': []}).encode()
        with mock.patch('subprocess.run', return_value=subprocess.CompletedProcess([], 0, ok, b'')):
            self.assertEqual(gw.SubprocessReader(['wrapper'])({}).provider_now_ms, 5)
        with self.assertRaises(ValueError):
            gw.SubprocessReader('ssh host cmd')


class Delivery(Base):
    """Outbox -> the REAL receiver (FakeMesh comment store), crash windows included."""
    def fired(self):
        reg = self.register()['registration_id']
        self.reader.records.append(rec('m0000003', T0 + 1000))
        self.tick(reg)
        return reg

    def test_F19_crash_after_commit_before_send_and_replay_after_send_before_ack(self):
        reg = self.fired()
        mesh = FakeMesh([])
        # crash after commit, before any send: a restarted sender (new process, same file) delivers
        s2 = Store(self.path)
        self.addCleanup(s2.db.close)
        self.assertEqual(gw.deliver_comments(s2, mesh, RESUME), 1)
        self.assertEqual(len(mesh.comments_all(SRC)), 1)
        # replay: the row was posted but the ack was lost -> forced back to pending
        self.s.db.execute("UPDATE gmail_outbox SET state='pending', acked_at=NULL WHERE kind='comment'")
        self.assertEqual(gw.deliver_comments(self.s, mesh, RESUME), 0)    # found by key, not posted again
        self.assertEqual(len(mesh.comments_all(SRC)), 1)
        self.assertEqual(self.count('gmail_outbox', "kind='comment' AND state='acked'"), 1)
        body = mesh.comments_all(SRC)[0]['body']
        self.assertIn('m0000003', body)
        self.assertIn(f'outbox-key: gmail:{reg}:m0000003:comment', body)

    def test_F24_kill_between_post_and_ack_restart_still_one_comment(self):
        self.fired()
        mesh = FakeMesh([])
        real_ack = gw.ack
        with mock.patch.object(gw, 'ack', side_effect=KeyboardInterrupt):   # killed right after the POST
            with self.assertRaises(KeyboardInterrupt):
                gw.deliver_comments(self.s, mesh, RESUME)
        self.assertEqual(len(mesh.comments_all(SRC)), 1)
        self.assertEqual(self.count('gmail_outbox', "kind='comment' AND state='pending'"), 1)
        gw.deliver_comments(self.s, mesh, RESUME)                           # restart
        self.assertEqual(len(mesh.comments_all(SRC)), 1)
        self.assertEqual(self.count('gmail_outbox', "kind='comment' AND state='pending'"), 0)
        self.assertIs(gw.ack, real_ack)

    def test_mesh_outage_keeps_the_row_pending_without_raising(self):
        self.fired()
        mesh = FakeMesh([])
        mesh.comments_all = mock.Mock(side_effect=RuntimeError('down'))
        self.assertEqual(gw.deliver_comments(self.s, mesh, RESUME), 0)
        self.assertEqual(self.count('gmail_outbox', "kind='comment' AND state='pending'"), 1)

    def test_observer_only_registration_gets_a_wake_and_no_comment(self):
        reg = self.register(source_task='')['registration_id']
        self.reader.records.append(rec('m0000003', T0 + 1000))
        self.tick(reg)
        self.assertEqual((self.count('gmail_outbox', "kind='wake'"), self.count('gmail_outbox', "kind='comment'")), (1, 0))


class Waits(unittest.TestCase):
    def test_F14_parse(self):
        now = dt.datetime.now(dt.timezone.utc)
        o = typed(wait_registered('gmail', 'gmr_3f9a1c07d2be'), now=now)
        self.assertEqual((o.kind, o.target, o.is_wait), ('gmail', 'gmr_3f9a1c07d2be', True))
        self.assertEqual(typed(wait_registered('gmail', 'gmr_3f9a1c07d2be', note='why'), now=now).note.strip(': '), 'why')
        for bad in ('3f9a1c07d2be', 'gmr_zz', 'gmr_3f9a1c07d2bez', ''):
            o = typed(wait_registered('gmail', bad), now=now)
            self.assertEqual(o.kind, 'unparsed', bad)
            self.assertNotIn('absolute', o.note)                        # never routed to the time guidance


class RunnerGmail(Bench):
    """Declaration binds to a live registration; the wake is atomic and one-shot; no polling turns."""
    def setUp(self):
        super().setUp()
        self.cfg.gmail_watch_v1 = True
        self.cfg.gmail_watch_reader = ('wrapper',)
        self.mesh_task = task(1)
        self.r = self.runner(FakeMesh([self.mesh_task]))
        self.r.refresh_me()
        self.obs = self.mesh_task['id']
        self.a = self.r.store.create(self.agent.name, self.obs, 'claude')
        self.r.store.update(self.a, state='running', turns=1)
        self.reader = FakeReader([rec('m0000001', T0 - 5000)])
        self.r._gmail_reader_obj = self.reader

    def reg(self, observer=None, **kw):
        return gw.register(self.r.store, self.reader, mailbox=MAILBOX, observer_task=observer or self.obs,
                           resume_agent=RESUME, predicate=gw.build_predicate(from_addr=SENDER, thread=THREAD),
                           source_task=SRC, **kw)['registration_id']

    def declare(self, reg_id):
        path = self.root / 'turn.jsonl'
        path.write_text(json.dumps(dict(type='result', subtype='success', is_error=False,
                        result='Recorded the outcome through Mesh.', session_id='s')) + '\n')
        at = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=5)).isoformat()
        row, wait = turn_rows(wait_registered('gmail', reg_id), at=at)
        self.r.mesh.outcomes = {self.obs: [row]}
        self.r.mesh.waits = {self.obs: wait}
        self.r.store.update(self.a, state='running', log_path=str(path))
        self.r.settle(self.a)

    def waiting(self):
        reg = self.reg()
        self.declare(reg)
        self.assertEqual((self.a.state, self.a.wait['kind']), ('waiting', 'gmail'))
        return reg

    def scan_at(self, ahead_ms):
        """One lane scan ``ahead_ms`` after the real clock (registrations are stamped with it)."""
        import time as _t
        real = _t.time()
        with mock.patch('time.time', return_value=real + ahead_ms / 1000):
            self.r._scan_gmail()

    def test_declaration_binds_only_to_a_live_registration_of_this_card_and_lane(self):
        self.declare('gmr_000000000000')                                  # unknown id
        self.assertNotEqual(self.a.wait.get('kind'), 'gmail')
        self.r.store.update(self.a, no_outcome=0)      # a second refusal in a row would end the attempt
        foreign = self.reg(observer=OTHER_OBS)                            # registered for another card
        self.declare(foreign)
        self.assertNotEqual(self.a.wait.get('kind'), 'gmail')
        self.assertEqual(self.a.state, 'ready')                           # sent back, still live
        gw.cancel(self.r.store, foreign)
        mine = self.reg(observer=self.obs)
        self.declare(mine)                                                # control: binds
        self.assertEqual((self.a.state, self.a.wait['kind']), ('waiting', 'gmail'))

    def test_cancelled_registration_cannot_be_declared(self):
        reg = self.reg()
        gw.cancel(self.r.store, reg)
        self.r.store.update(self.a, state='running')
        parsed = typed(wait_registered('gmail', reg))
        self.assertIsNone(self.r._gmail_registration(self.a, parsed))

    def test_flag_off_declaration_is_refused_and_nothing_is_scanned(self):
        reg = self.reg()
        self.cfg.gmail_watch_v1 = False
        self.declare(reg)
        self.assertNotEqual(self.a.wait.get('kind'), 'gmail')
        self.scan_at(200_000)
        self.assertEqual(len(self.reader.requests), 1)                    # only the registration snapshot

    def test_F1_F8_end_to_end_one_wake_survives_restart_and_never_repeats(self):
        reg = self.waiting()
        self.reader.records.append(rec('m0000003', T0 + 1000))
        self.scan_at(200_000)                                                 # tick fires and commits...
        self.assertEqual(self.r.store.get(self.a.id).state, 'waiting')    # ...but the wake is not delivered yet
        # F8: the process dies here; a new runner on the same state file delivers it
        r2 = self.runner(self.r.mesh)
        r2.refresh_me()
        a2 = r2.store.get(self.a.id)
        r2._wake_one(a2)
        self.assertEqual(a2.state, 'ready')
        self.assertIn('gmail:%s fired message_id=m0000003' % reg, a2.wake_text)
        text = a2.wake_text
        r2._wake_one(a2)
        r2.settle if False else None
        self.assertEqual(a2.wake_text, text)
        self.assertEqual(self.r.store.db.execute("SELECT COUNT(*) FROM gmail_outbox WHERE kind='wake' AND state='acked'").fetchone()[0], 1)
        self.assertEqual(len(self.r.mesh.comments_all(SRC)), 1)           # the receipt comment went to the source card

    def test_message_before_the_declaration_is_kept_and_wakes_once(self):
        reg = self.reg()
        self.reader.records.append(rec('m0000003', T0 + 1000))
        self.scan_at(200_000)                                                 # fired while nobody waits
        self.declare(reg)                                                 # a fired-but-undelivered registration binds
        self.assertEqual(self.a.state, 'waiting')
        self.r._wake_one(self.a)
        self.assertEqual(self.a.state, 'ready')

    def test_nothing_wakes_and_no_turn_starts_without_an_event(self):
        self.waiting()
        for i in range(5):
            self.scan_at((i + 1) * 200_000)
            self.r._wake_one(self.a)
        self.assertEqual(self.a.state, 'waiting')
        self.assertEqual(self.a.turns, 1)

    def test_errors_do_not_wake_or_crash_the_lane(self):
        self.waiting()
        self.reader.fail = gw.GmailError('auth')
        for i in range(7):
            self.scan_at((i + 1) * 2_000_000)
            self.r._wake_one(self.a)
        self.assertEqual(self.a.state, 'waiting')
        self.assertEqual(gw.status(self.r.store)[0]['last_error_class'], 'auth')

    def test_a_registration_of_another_lane_is_not_scanned_here(self):
        reg = gw.register(self.r.store, self.reader, mailbox=MAILBOX, observer_task=self.obs, resume_agent=str(UUID(int=99)),
                          predicate=gw.build_predicate(from_addr=SENDER, thread=THREAD))['registration_id']
        self.reader.records.append(rec('m0000003', T0 + 1000))
        before = len(self.reader.requests)
        self.scan_at(200_000)
        self.assertEqual(len(self.reader.requests), before)
        self.assertEqual(gw.status(self.r.store, reg)['state'], 'active')

    def test_cancel_wakes_once_with_the_terminal_text(self):
        reg = self.waiting()
        gw.cancel(self.r.store, reg, 'owner closed the card')
        self.r._wake_one(self.a)
        self.assertEqual(self.a.state, 'ready')
        self.assertIn('cancelled', self.a.wake_text)

    def test_F20_two_reads_for_one_registration_cannot_overlap(self):
        reg = self.waiting()
        self.r._gmail_inflight = {reg}                                   # a read is in flight
        n = len(self.reader.requests)
        self.scan_at(10_000_000)
        self.assertEqual(len(self.reader.requests), n)
        self.assertFalse(hasattr(self.runner(self.r.mesh), '_gmail_inflight'))   # a restart starts empty


class Cli(unittest.TestCase):
    """The operator commands through the real argv parser and the real subprocess reader."""
    def test_register_status_cancel_roundtrip_and_flag_off(self):
        import io
        import sys
        from contextlib import redirect_stdout
        from mesh_runner import cli
        from mesh_runner.config import RunnerConfig
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        wrapper = Path(tmp.name) / 'reader.py'
        wrapper.write_text("import json, sys\njson.load(sys.stdin)\n"
                           "print(json.dumps({'now_ms': %d, 'records': [{'message_id': 'm0000001', 'thread_id': '%s', "
                           "'internal_ms': %d, 'from': 'a@example.net', 'labels': []}]}))\n" % (T0, THREAD, T0 - 5))
        cfg = RunnerConfig(mesh_url='http://mesh', state_dir=Path(tmp.name) / 'state', agents=[],
                           gmail_watch_v1=True, gmail_watch_reader=(sys.executable, str(wrapper)))

        def run(*argv, config=cfg):
            out = io.StringIO()
            with mock.patch('mesh_runner.cli.config_mod.load', return_value=config), redirect_stdout(out):
                code = cli.main(['watch', 'gmail', *argv, '--config', 'x.toml'])
            return code, (json.loads(out.getvalue()) if out.getvalue().strip() else None)
        args = ['--mailbox', MAILBOX, '--observer-task', OBS, '--resume-agent', RESUME, '--source-task', SRC,
                '--from', 'a@example.net', '--thread', THREAD]
        code, dry = run('register', *args, '--dry-run')
        self.assertEqual((code, dry['dry_run']), (0, True))
        code, reg = run('register', *args)
        self.assertEqual(code, 0)
        self.assertRegex(reg['registration_id'], r'^gmr_[0-9a-f]{12}$')
        self.assertEqual(run('register', *args)[1]['registration_id'], reg['registration_id'])
        self.assertEqual(run('status', reg['registration_id'])[1]['state'], 'active')
        self.assertEqual(run('cancel', reg['registration_id'], '--reason', 'done')[1]['state'], 'cancelled')
        code, bad = run('register', *args[:-2], '--sender-domain', 'x.example', '--marker', 'a"b')
        self.assertEqual(code, 1)                                          # mixed forms / bad marker refused
        off = RunnerConfig(mesh_url='http://mesh', state_dir=cfg.state_dir, agents=[], gmail_watch_v1=False)
        self.assertEqual(run('status', config=off)[0], 2)


class RedControl(Base):
    """A stub evaluate that returns every record must be caught by the filtering scenarios."""
    def test_stub_evaluate_lets_the_negatives_through(self):
        reg = self.register()['registration_id']
        self.reader.records += [rec('n1', T0 + 10, labels=('SENT',)), rec('n2', T0 - 50),
                                rec('n3', T0 + 10, frm='x@other.net'), rec('n4', T0 + 10, thread='1aaaaaaaaaaaaaaa')]
        with mock.patch.object(gw, 'evaluate', lambda p, b, s, records: sorted(records, key=lambda r: r['internal_ms'])):
            self.assertEqual(self.tick(reg).outcome, 'fired')            # the stub fires on junk: the real tests F2-F6 would fail
        self.assertEqual(self.count('gmail_receipt'), 1)
        real = self.register(self.thread_pred(), observer_task=OTHER_OBS)['registration_id']
        self.assertEqual(self.tick(real).outcome, 'advanced')            # the real evaluate does not


if __name__ == '__main__':
    unittest.main()
