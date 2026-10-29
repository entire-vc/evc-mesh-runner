"""Infrastructure-unknown QA remains fail-closed, visible and periodically recoverable."""
import datetime as dt
import sys
from unittest import mock

from test_runner import Bench
from fakes import FakeMesh, task, write_replies, calls, FUTURE_AT
import mesh_runner.runner as mod
import mesh_runner.store as clock


class QAInfraHold(Bench):
    def test_infra_failure_notice_quiet_retry_and_recovery(self):
        self.infra_failure_retry(dt.datetime(2026, 10, 20, 12, tzinfo=dt.timezone.utc), retry_notices=1)

    def test_infra_failure_retry_across_utc_midnight_keeps_one_notice_per_day(self):
        self.infra_failure_retry(dt.datetime(2026, 10, 20, 22, 53, tzinfo=dt.timezone.utc), retry_notices=2)

    def infra_failure_retry(self, first, *, retry_notices):
        for category in ('qa_artifact_unresolved', 'qa_fail_closed'):
            with self.subTest(category=category):
                prior_calls = len(calls(self.calls))
                t = task(1 if category == 'qa_artifact_unresolved' else 2)
                mesh = FakeMesh([t])
                r = self.runner(mesh)
                reason = category + ':synthetic_unavailable'
                self.agent.start_gate_command = [sys.executable, '-c', 'import sys; print(' + repr(reason) + '); sys.exit(76)']
                review = lambda: dict(mesh.tasks[t['id']], _mode='review')
                with mock.patch.object(mod, 'now', return_value=first), mock.patch.object(clock, 'now', return_value=first):
                    r._start_one(review())
                self.assertEqual(mesh.comment_log.get(t['id'], []), [])
                r._gate_hold.clear(); r._hold_cache.clear()
                later = first + dt.timedelta(minutes=31)
                # Keep the refusals one continuous streak, rather than a stale outage.
                r.store.db.execute("UPDATE marks SET at=? WHERE task_id=? AND key='review_gate_hold'", (later.isoformat(), t['id']))
                with mock.patch.object(mod, 'now', return_value=later), mock.patch.object(clock, 'now', return_value=later):
                    r._start_one(review())
                    notes = mesh.comment_log.get(t['id'], [])
                    self.assertEqual(len(notes), 1)
                    self.assertIn('infrastructure', notes[0]['body'])
                    self.assertNotIn('It needs a reviewer other than', notes[0]['body'])
                    self.assertIsNotNone(r.store.mark(self.agent.name, t['id'], 'review_unstartable'))
                    self.assertEqual(r.store.by_state(self.agent.name, ('starting','running')), [])
                    for _ in range(3):
                        r._gate_hold.clear(); r._hold_cache.clear()
                        r._start_one(review())
                    self.assertEqual(len(mesh.comment_log[t['id']]), 1)
                retry_at = later + dt.timedelta(minutes=61)
                with mock.patch.object(mod, 'now', return_value=retry_at), mock.patch.object(clock, 'now', return_value=retry_at):
                    self.assertEqual(r._unstartable_hold(review(), []), '')
                    # Recovery never bypasses the start gate: an unavailable API still refuses.
                    r._gate_hold.clear(); r._hold_cache.clear()
                    r._start_one(review())
                    self.assertEqual(r.store.by_state(self.agent.name, ('starting','running')), [])
                    # Notice claims are per UTC day; midnight permits one new diagnostic.
                    self.assertEqual(len(mesh.comment_log[t['id']]), retry_notices)
                    self.assertIsNotNone(r.store.mark(self.agent.name, t['id'], 'review_unstartable'))
                    self.assertIn('infrastructure', r._unstartable_hold(review(), []))
                    for _ in range(3):
                        r._gate_hold.clear(); r._hold_cache.clear()
                        r._start_one(review())
                    self.assertEqual(len(mesh.comment_log[t['id']]), retry_notices)
                    self.assertEqual(r.store.by_state(self.agent.name, ('starting','running')), [])
                    self.assertEqual(len(calls(self.calls)), prior_calls)
                # Another scheduled refusal on the same UTC day remains quiet.
                same_day_retry = retry_at + dt.timedelta(minutes=61)
                with mock.patch.object(mod, 'now', return_value=same_day_retry), mock.patch.object(clock, 'now', return_value=same_day_retry):
                    self.assertEqual(r._unstartable_hold(review(), []), '')
                    r._gate_hold.clear(); r._hold_cache.clear()
                    r._start_one(review())
                    self.assertEqual(len(mesh.comment_log[t['id']]), retry_notices)
                    self.assertEqual(r.store.mark(mod.TASK_WIDE, t['id'], 'review_unstartable_told')[0],
                                     same_day_retry.date().isoformat())
                    self.assertIn('infrastructure', r._unstartable_hold(review(), []))
                    self.assertEqual(r.store.by_state(self.agent.name, ('starting','running')), [])
                    self.assertEqual(len(calls(self.calls)), prior_calls)
                # The next scheduled probe discovers recovery without requiring a card edit.
                self.agent.start_gate_command = [sys.executable, '-c', 'raise SystemExit(0)']
                write_replies(self.replies, {t['id'][:8]: f'⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 review follow-up'})
                with mock.patch.object(mod, 'now', return_value=same_day_retry + dt.timedelta(minutes=61)), mock.patch.object(clock, 'now', return_value=same_day_retry + dt.timedelta(minutes=61)):
                    r._gate_hold.clear(); r._hold_cache.clear()
                    r._start_one(review())
                self.settle_all(r)
                self.assertEqual(len(calls(self.calls)), prior_calls + 1)
                self.assertEqual(len(mesh.comment_log[t['id']]), retry_notices)
                r.store.db.close()

    def test_unknown_reason_does_not_become_review_hold(self):
        t = task(3); mesh = FakeMesh([t]); r = self.runner(mesh)
        r._review_gate_refused(t, 'qa_future_reason:unknown')
        self.assertEqual(mesh.comment_log.get(t['id'], []), [])
        self.assertIsNone(r.store.mark(self.agent.name, t['id'], 'review_unstartable'))
        r.store.db.close()
