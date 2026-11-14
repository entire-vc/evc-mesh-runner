"""Public scheduling retries unavailable sibling evidence, never a QA verdict.

Mesh and the gate subprocess stand at external boundaries. SQLite, scheduling,
admission, the fake harness process and restart recovery use production paths.
"""
import datetime as dt
import json
import sys
import time
from unittest import mock

from test_runner import Bench
from fakes import FakeGitLab, FakeMesh, calls, task, write_replies, FUTURE_AT, wait_time
import mesh_runner.runner as mod
import mesh_runner.store as store_clock


REASON = 'qa_no_selection:sibling_evidence_unknown'
GATE = '''import json, os, pathlib, sys
mode = pathlib.Path(sys.argv[1]).read_text().strip()
with pathlib.Path(sys.argv[2]).open('a') as out:
    out.write(json.dumps({'phase': os.environ['MESH_RUNNER_PHASE'], 'mode': mode}) + '\\n')
if mode == 'allow':
    raise SystemExit(0)
print('route: ' + mode + ' trace=synthetic-profile:pool_evidence_unknown')
raise SystemExit(76)
'''


WINDOW = dt.timedelta(minutes=220)  # the scenario advances the clock up to +214 min


class Clock:
    def __init__(self, real_now=None):
        self.elapsed = 0
        # Three hours back, not a fixed hour: the fake harness stamps its typed outcome with the real
        # clock, and the runner reads only outcomes newer than the turn start (origin + ~152 min).
        real_now = real_now or dt.datetime.now(dt.timezone.utc)
        origin = (real_now - dt.timedelta(hours=3)).replace(second=0, microsecond=0)
        # The runner tells "no reviewer could be started" at most once per UTC day; a window across
        # midnight legally sends a second notice. Shift back so the whole window [origin, origin+WINDOW]
        # ends before midnight; the new origin is never later, so the outcome-newer-than-turn-start
        # condition still holds.
        end = origin + WINDOW
        if end.date() != origin.date():
            origin = dt.datetime.combine(end.date(), dt.time(), dt.timezone.utc) - WINDOW - dt.timedelta(minutes=1)
        self.origin = origin

    def now(self):
        return self.origin + dt.timedelta(seconds=self.elapsed)

    def monotonic(self):
        return 100000 + self.elapsed

    def __getattr__(self, name):
        return getattr(time, name)  # child-process timeouts retain the real clock


class SiblingEvidenceRetry(Bench):
    def test_recovery_after_restart_without_card_edit(self):
        self.public_case('allow')

    def test_recovered_evidence_still_refuses_author(self):
        self.public_case('qa_refused:reviewer_is_author_or_identity_unknown')

    def test_recovered_evidence_still_refuses_reviewer_absence(self):
        self.public_case('qa_refused:strict_independent_review_unavailable')

    def public_case(self, recovered):
        clock = Clock()
        t = task(1, cat='review')

        class Mesh(FakeMesh):
            def add_comment(self, tid, body, author='agent-1', at=None, key=''):
                return super().add_comment(tid, body, author=author, at=at or clock.now())

        mesh = Mesh([t])
        gate, mode, gate_log = (self.root / p for p in ('gate.py', 'gate-mode', 'gate-calls'))
        gate.write_text(GATE)
        mode.write_text(REASON)
        gate_log.write_text('')
        self.agent.start_gate_command = [sys.executable, str(gate), str(mode), str(gate_log)]
        write_replies(self.replies, {t['id'][:8]: wait_time(why="synthetic review follow-up")})
        runners = []

        def boot():
            r = self.runner(mesh, gitlab=FakeGitLab())
            runners.append(r)
            r.refresh_me()
            return r

        def rows():
            return [json.loads(line) for line in gate_log.read_text().splitlines()]

        def parked(r, gate_count):
            self.assertEqual(len(rows()), gate_count)
            self.assertEqual(self.states(r), {})
            self.assertEqual(mesh.checkouts, {})
            self.assertEqual(calls(self.calls), [])

        try:
            with mock.patch.object(mod, 'now', clock.now), \
                 mock.patch.object(store_clock, 'now', clock.now), \
                 mock.patch.object(mod, 'time', clock):
                r = boot()
                signature = r._review_state_sig(t)
                r.start_new(1)
                parked(r, 1)
                self.assertEqual(mesh.comment_log, {})
                clock.elapsed = 31 * 60
                r.start_new(1)
                parked(r, 2)
                saved = r.store.mark(self.agent.name, t['id'], 'review_unstartable')
                self.assertEqual(saved[0], REASON)
                self.assertEqual(len(mesh.comment_log[t['id']]), 1)
                r.store.db.close()
                r = boot()
                self.assertEqual(r.store.mark(self.agent.name, t['id'], 'review_unstartable'), saved)
                for second in range(32 * 60, 32 * 60 + 20):
                    clock.elapsed = second
                    r.start_new(1)
                clock.elapsed = 91 * 60 - 1
                r.start_new(1)
                parked(r, 2)
                clock.elapsed = 91 * 60 + 1
                r.start_new(1)
                parked(r, 3)  # still unavailable: a real gate refusal rearms retry
                rearmed = r.store.mark(self.agent.name, t['id'], 'review_unstartable')
                self.assertNotEqual(rearmed[1], saved[1])
                r.store.db.close()
                r = boot()
                self.assertEqual(r.store.mark(self.agent.name, t['id'], 'review_unstartable'), rearmed)
                mode.write_text(recovered)  # external recovery; do not edit the card or marks
                clock.elapsed = 151 * 60
                r.start_new(1)
                parked(r, 3)
                clock.elapsed = 152 * 60 + 2
                r.start_new(1)
                if recovered == 'allow':
                    self.assertEqual([row['phase'] for row in rows()[-2:]], ['reserve', 'admit'])
                    self.assertEqual(len(rows()), 5)
                    finished = self.finish_turn(r)
                    self.assertEqual((finished.state, finished.mode, finished.turns), ('waiting', 'review', 1))
                    self.assertEqual(len(calls(self.calls)), 1)
                else:
                    parked(r, 4)
                    self.assertEqual(r.store.mark(self.agent.name, t['id'], 'review_unstartable')[0],
                                     'qa_refused: review admission unavailable')
                    r.store.db.close()
                    r = boot()
                    mode.write_text('allow')
                    clock.elapsed = 214 * 60
                    r.start_new(1)
                    parked(r, 4)  # actual QA verdict still needs observable card change
                self.assertEqual(r._review_state_sig(t), signature)
                # one runner notice; a started review turn's wait_for adds the agent's own comment
                notices = [c['body'] for c in mesh.comment_log[t['id']] if not c['body'].startswith('Waiting on ')]
                self.assertEqual(len(notices), 1)
                self.assertIn('independence remains required', notices[0])
        finally:
            for r in runners:
                for proc in r.procs.values():
                    if proc.poll() is None:
                        proc.terminate()
                        proc.wait(timeout=5)
                r.store.db.close()

    def test_clock_window_stays_inside_one_utc_day(self):
        day = dt.datetime(2026, 11, 13, tzinfo=dt.timezone.utc)
        for hh, mm in ((0, 0), (0, 31), (0, 40), (0, 41), (1, 30), (3, 0), (3, 39), (10, 0), (21, 30), (23, 0), (23, 59)):
            real_now = day.replace(hour=hh, minute=mm, second=17)
            origin = Clock(real_now).origin
            self.assertEqual(origin.date(), (origin + WINDOW).date(), real_now)
            self.assertLessEqual(origin, real_now - dt.timedelta(hours=3), real_now)

    def test_only_exact_sibling_unknown_tag_is_added(self):
        for prefix in ('', 'route: '):
            self.assertTrue(mod.AgentRunner._qa_infra_refusal(prefix + REASON))
        for reason in ('qa_no_selection:no_available_route', 'qa_no_selection:attempt_limit',
                       'qa_refused:reviewer_is_author_or_identity_unknown',
                       'qa_refused:strict_independent_review_unavailable',
                       REASON + ':extra', REASON + 'suffix', 'prefix ' + REASON,
                       'qa_no_selection:sibling_evidence_unknown_future'):
            self.assertFalse(mod.AgentRunner._qa_infra_refusal(reason), reason)

    def test_wire_reason_keeps_only_the_finite_tag(self):
        for prefix in ('', 'route: '):
            wire = prefix + REASON + ' profile=PRIVATE trace=PRIVATE'
            self.assertEqual(mod.gate_hold_reason(wire), REASON)
        for suffix in (':extra', 'suffix', '_future'):
            cleaned = mod.gate_hold_reason('route: ' + REASON + suffix + ' trace=PRIVATE')
            self.assertEqual(cleaned, 'qa_no_selection: review admission unavailable')
            self.assertFalse(mod.AgentRunner._qa_infra_refusal(cleaned))
        self.assertEqual(mod.gate_hold_reason('HOLD_REASON_CODE=unknown\nroute: ' + REASON),
                         'held by the start gate')
