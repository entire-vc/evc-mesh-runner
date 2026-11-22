"""A provider limit that account routing cannot route around holds the resume until the reset.

21.11 the on-limit command exited 1 (no account to switch to) 76 times in a row and the runner resumed the
same session ~20 s later each time. The test drives the real pre-send quota path (driver journal → recovery →
on-limit subprocess with rc=1 → ready) and then the real resume scheduler over several ticks."""
import datetime as dt
import os
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

import subprocess

from mesh_runner import interactive, limit_hold, tui_driver as td
from mesh_runner.config import AgentConfig, RunnerConfig
from mesh_runner.runner import AgentRunner
from mesh_runner.store import Store
from fakes import FakeMesh, task
from test_runner import Loop

MSK = dt.timezone(dt.timedelta(hours=3))


def pin_moscow(case):
    """The host's zone (the zone a notice without one speaks): Europe/Moscow, as on the Mini; CI may run in UTC."""
    old = os.environ.get('TZ')
    os.environ['TZ'] = 'Europe/Moscow'; time.tzset()

    def restore():
        if old is None:
            os.environ.pop('TZ', None)
        else:
            os.environ['TZ'] = old
        time.tzset()
    case.addCleanup(restore)


def at(hh, mm=0, day=21):
    return dt.datetime(2026, 11, day, hh, mm, tzinfo=MSK)


class NoticeReset(unittest.TestCase):
    def test_reset_with_zone_is_the_next_occurrence(self):
        reset, how = limit_hold.notice_reset(
            "usage limit reached: ⎿ You've hit your 5-hour limit · resets 12am (Europe/Moscow)", at(23, 47, 20))
        self.assertEqual((reset, how), (at(0, 0, 21), "notice"))

    def test_reset_in_another_zone(self):
        reset, how = limit_hold.notice_reset("You've hit your limit · resets 3:30pm (UTC)", at(14, 0))
        self.assertEqual(how, "notice")
        self.assertEqual(reset, dt.datetime(2026, 11, 21, 15, 30, tzinfo=dt.timezone.utc))

    def test_a_time_of_day_already_passed_is_no_reset(self):
        # the 21.11 incident: the notice of the 23:47 limit was still on screen at 02:12
        self.assertEqual(limit_hold.notice_reset(
            "usage limit reached: ⏺ Usage limit reached · continuing automatically at 12am · esc to cancel",
            at(2, 12)), (None, "notice_passed"))

    def test_a_weekly_window_may_reset_later_the_same_day(self):
        weekly = "You've hit your weekly limit · resets 12pm (Europe/Moscow)"
        self.assertEqual(limit_hold.notice_reset(weekly, at(2)), (at(12, 0), "notice"))
        # the same time of day for a five-hour window ten hours ahead is a notice that outlived its reset
        self.assertEqual(limit_hold.notice_reset("You've hit your 5-hour limit · resets 12pm (Europe/Moscow)",
                                                 at(2)), (None, "notice_passed"))

    def test_continuing_automatically_without_zone_is_local_time(self):
        self.assertEqual(limit_hold.notice_reset("Usage limit reached · continuing automatically at 12am",
                                                 at(22, 10, 20)), (at(0, 0, 21), "notice"))

    def test_dated_resets(self):
        self.assertEqual(limit_hold.notice_reset("You've hit your weekly limit · resets Nov 24, 3am (UTC)", at(10)),
                         (dt.datetime(2026, 11, 24, 3, tzinfo=dt.timezone.utc), "notice"))
        self.assertEqual(limit_hold.notice_reset("weekly limit: 0% left · resets at 14:10 on 23 Nov", at(10)),
                         (at(14, 10, 23), "notice"))
        self.assertEqual(limit_hold.notice_reset("resets at 14:10 on 15 Nov", at(10)), (None, "notice_passed"))

    def test_no_reset_named(self):
        for text in ("", "usage limit reached", "resets <reset-time>", "resets 25:00", "resets 1am (Mars/Base)",
                     "the counter resets 3 times", "reset 12 workspaces", "resets at 14 on the dot"):
            with self.subTest(text=text):
                self.assertIsNone(limit_hold.notice_reset(text, at(10))[0])

    def test_typed_reset_comes_first_and_must_be_ahead(self):
        now = at(10)
        typed = now + dt.timedelta(hours=2)
        self.assertEqual(limit_hold.reset_time({"resets_at": typed.timestamp()}, "resets 11am", now),
                         (typed.astimezone(dt.timezone.utc), "rate_limit_event"))
        self.assertEqual(limit_hold.reset_time({"resets_at": (now - dt.timedelta(minutes=1)).timestamp()},
                                               "resets 11am", now), (at(11), "notice"))

    def test_hold_seconds(self):
        now = at(10)
        self.assertEqual(limit_hold.hold_seconds(9, at(11), now), 3630)
        pauses = [limit_hold.hold_seconds(n, None, now) for n in range(1, 7)]
        self.assertEqual(pauses, [1800, 3600, 7200, 7200, 7200, 7200])
        # without a known reset a five-hour window costs at most four tries in a row
        self.assertGreater(sum(pauses[:4]), 5 * 3600)


class UnswitchedLimitHoldsTheResume(unittest.TestCase):
    PROMPT = 'exact pending prompt'

    def bench(self, notice):
        pin_moscow(self)
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        root = Path(directory.name); state = root / 'state'; state.mkdir()
        profile = root / 'profile'; profile.mkdir()
        switch = root / 'switch.py'
        switch.write_text('print("no switch: passthrough no_selection:source_has_no_sole_quota_trigger")\n'
                          'raise SystemExit(1)\n')
        agent = AgentConfig(name='Steward', key='fixture-key', harness='claude', profile_dir=profile, repos=[],
                            on_limit_command=[sys.executable, str(switch)])
        cfg = RunnerConfig(mesh_url='https://mesh.example.test', state_dir=state, agents=[agent])
        store = Store(state / 'runner.sqlite'); self.addCleanup(store.db.close)
        runner = AgentRunner(cfg, agent, store)
        attempt = store.create(agent.name, 'fixture-task', 'claude')
        self.store, self.runner, self.root, self.state = store, runner, root, state
        self.cfg, self.agent = cfg, agent
        self.attempt_id = attempt.id
        self.notice = notice
        return attempt

    def limit_turn(self, n):
        """One resumed turn the driver ended before the paste: the limit notice was on the screen."""
        store, runner, root = self.store, self.runner, self.root
        attempt = store.get(self.attempt_id)
        log = root / f'turn{n}.jsonl'
        log.write_text(json.dumps({'type': 'result', 'subtype': 'error_during_execution', 'is_error': True,
                                   'result': 'usage limit reached: ' + self.notice}) + '\n')
        sha = hashlib.sha256(self.PROMPT.encode()).hexdigest()
        intent = {'kind': 'interactive', 'token': f'token-{n}', 'sha256': sha, 'harness': 'claude',
                  'prompt': self.PROMPT, 'log_path': str(log), 'turn': 2, 'resume': True, 'workspaces': [],
                  'source_read_at': 'source', 'prior': {'turns': 1, 'log_path': str(root / 'prior.jsonl')}}
        store.update(attempt, state='running', turns=2, log_path=str(log), pid=0,
                     wait={'interactive_prompt_intent': intent})
        driver_dir = interactive.agent_dir(self.state, 'Steward'); driver_dir.mkdir(parents=True, exist_ok=True)
        interactive.write_delivery(driver_dir, attempt.id, {'version': 1, 'token': intent['token'], 'sha256': sha,
                                                           'phase': 'pre-send-failed', 'harness': 'claude',
                                                           'failure_kind': 'quota'})
        self.assertFalse(runner._reconcile_interactive_prompt(store.get(attempt.id)))
        self.assertEqual(store.get(attempt.id).state, 'ready')

    def resumes(self, ticks, clock, wall):
        """Run the real resume scheduler at each (monotonic, wall-clock) tick; count resumes of the task.
        Each resume meets the limit again (the account is still exhausted)."""
        launched = []
        runner = self.runner

        def resume_one(a):
            launched.append(clock[0])
            self.limit_turn(len(launched) + 1)
        with mock.patch.object(runner, '_resume_one', side_effect=resume_one), \
                mock.patch.object(runner, 'host_hold', return_value=False), \
                mock.patch.object(runner, 'paused', return_value=False), \
                mock.patch.object(runner, '_branch_blocked', return_value=False), \
                mock.patch.object(runner, '_maintenance_held', return_value=False), \
                mock.patch.object(runner.ramp, 'open', return_value=True), \
                mock.patch.object(runner.ramp, 'ready', return_value=True), \
                mock.patch.object(runner, '_between'):
            for mono, when in ticks:
                clock[0], wall[0] = mono, when
                runner.resume_ready(1)
        return launched

    def run_night(self, notice, start, minutes, restart=False):
        self.bench(notice)
        clock, wall = [1000.0], [start]
        with mock.patch('mesh_runner.runner.time.monotonic', side_effect=lambda: clock[0]), \
                mock.patch('mesh_runner.runner.now', side_effect=lambda: wall[0].astimezone(dt.timezone.utc)):
            self.limit_turn(1)
            if restart:                  # the runner restarts: a new process, a new monotonic clock, same store
                self.runner = AgentRunner(self.cfg, self.agent, self.store)
                clock[0] = 50.0
                ticks = [(50.0 + 20 * i, start + dt.timedelta(seconds=20 * i)) for i in range(1, minutes * 3)]
                return self.resumes(ticks, clock, wall)
            ticks = [(1000.0 + 20 * i, start + dt.timedelta(seconds=20 * i)) for i in range(1, minutes * 3)]
            return self.resumes(ticks, clock, wall)

    def test_known_reset_no_resume_before_it(self):
        start = at(23, 47, 20)
        launched = self.run_night("⎿ You've hit your 5-hour limit · resets 12am (Europe/Moscow)", start, 20)
        reset_mono = 1000.0 + (at(0, 0, 21) - start).total_seconds()
        self.assertLessEqual(len([m for m in launched if m < reset_mono]), 1, launched[:5])
        self.assertEqual([m for m in launched if m < reset_mono], [])
        self.assertTrue(launched and launched[0] >= reset_mono, launched)      # it does resume after the reset
        event = [e for e in self.store.events(self.attempt_id) if e[1] == 'limit_hold'][0]
        self.assertEqual(json.loads(event[2])['source'], 'notice')

    def test_a_restart_before_the_reset_keeps_the_hold(self):
        start = at(23, 47, 20)
        launched = self.run_night("⎿ You've hit your 5-hour limit · resets 12am (Europe/Moscow)", start, 20,
                                  restart=True)
        reset_mono = 50.0 + (at(0, 0, 21) - start).total_seconds()
        self.assertEqual([m for m in launched if m < reset_mono], [], launched[:5])
        self.assertTrue(launched and launched[0] >= reset_mono, launched[:5])

    def test_a_weekly_limit_holds_until_its_reset(self):
        start = at(2)
        launched = self.run_night("You've hit your weekly limit · resets 12pm (Europe/Moscow)", start, 11 * 60)
        noon_mono = 1000.0 + (at(12, 0) - start).total_seconds()
        self.assertEqual([round((m - 1000.0) / 60) for m in launched if m < noon_mono], [])
        self.assertTrue(launched and launched[0] >= noon_mono, launched[:5])

    def test_a_pool_change_before_the_restarted_runner_reads_the_hold_lifts_it(self):
        self.bench("⎿ You've hit your 5-hour limit · resets 4am (Europe/Moscow)")
        with mock.patch('mesh_runner.runner.now', side_effect=lambda: at(2, 12).astimezone(dt.timezone.utc)):
            self.limit_turn(1)
            a = self.store.get(self.attempt_id)
            restarted = AgentRunner(self.cfg, self.agent, self.store)
            self.assertTrue(restarted.invalidate_capacity_holds("pool state changed"))
            self.assertLess(restarted._resume_hold_until(a), time.monotonic())
            self.assertFalse((self.store.mark(self.agent.name, a.task_id, 'limit_hold_until') or ('',))[0])
            control = AgentRunner(self.cfg, self.agent, self.store)
            self.assertLess(control._resume_hold_until(a), time.monotonic())

    def test_a_confirmed_pool_change_lifts_the_hold_for_good(self):
        self.bench("⎿ You've hit your 5-hour limit · resets 4am (Europe/Moscow)")
        with mock.patch('mesh_runner.runner.now', side_effect=lambda: at(2, 12).astimezone(dt.timezone.utc)):
            self.limit_turn(1)
            a = self.store.get(self.attempt_id)
            self.assertGreater(self.runner._resume_hold_until(a), time.monotonic() + 3600)
            self.assertTrue(self.runner.invalidate_capacity_holds("pool state changed"))
            self.assertLess(self.runner._resume_hold_until(a), time.monotonic())
            restarted = AgentRunner(self.cfg, self.agent, self.store)
            self.assertLess(restarted._resume_hold_until(a), time.monotonic())     # nothing to read back

    def test_unknown_reset_grows_the_pause(self):
        launched = self.run_night("⏺ Usage limit reached · continuing automatically at 12am · esc to cancel",
                                  at(2, 12), 300)
        # 21.11: 76 resumes in 80 minutes; now four limits fill five hours
        self.assertLessEqual(len(launched), 4, launched[:5])
        gaps = [b - a for a, b in zip([1000.0] + launched, launched)]
        self.assertEqual(gaps, sorted(gaps))
        self.assertEqual(self.store.mark('Steward', 'fixture-task', 'limit_hold_n')[0], str(len(launched) + 1))
        holds = [json.loads(e[2]) for e in self.store.events(self.attempt_id) if e[1] == 'limit_hold']
        # 12am read at 02:12 MSK (23:12 UTC on the store's clock) is the reset already past, not one 48 min ahead
        self.assertEqual((holds[0]['source'], holds[0]['seconds'], holds[0]['reset_at']), ('notice_passed', 1800, None))



class SettledLimitTurn(Loop):
    """The other path: a turn the provider limit ended after it ran (``_settle``)."""
    def no_switch(self):
        script = self.root / 'no-switch.py'
        script.write_text('print("no switch: passthrough no_selection:source_has_no_sole_quota_trigger")\n'
                          'raise SystemExit(1)\n')
        self.agent.on_limit_command = [sys.executable, str(script)]

    def test_limit_after_a_turn_holds_until_the_notice_reset(self):
        t = task(1)
        r = self.runner(FakeMesh([t], max_concurrent=1))
        self.no_switch()
        a = self.error_envelope(r, t, "usage limit reached: You've hit your 5-hour limit · resets 3am (UTC)")
        held = r._resume_hold[a.id] - time.monotonic()
        reset = dt.datetime.now(dt.timezone.utc).replace(hour=3, minute=0, second=0, microsecond=0)
        if reset <= dt.datetime.now(dt.timezone.utc):
            reset += dt.timedelta(days=1)
        expected = (reset - dt.datetime.now(dt.timezone.utc)).total_seconds()
        if expected <= limit_hold.TIME_ONLY_AHEAD.total_seconds():
            self.assertAlmostEqual(held, expected + 30, delta=5)
        else:                                   # 3am UTC is more than a window away now: the growing pause
            self.assertAlmostEqual(held, limit_hold.BACKOFF_SECONDS, delta=5)
        self.assertEqual(r.store.get(a.id).state, 'ready')

    def test_a_turn_that_ran_ends_the_streak(self):
        t = task(1)
        r = self.runner(FakeMesh([t], max_concurrent=1))
        self.no_switch()
        a = self.error_envelope(r, t, "usage limit reached")
        first = r._resume_hold[a.id] - time.monotonic()
        r.store.update(a, state='running', turns=2, log_path=a.log_path)
        r.settle(r.store.get(a.id))
        self.assertGreater(r._resume_hold[a.id] - time.monotonic(), first + 60)       # second in a row: longer
        self.assertEqual(r.store.mark(self.agent.name, t['id'], 'limit_hold_n')[0], '2')
        path = self.root / f'{a.id}-ok.jsonl'
        path.write_text('{"type": "result", "is_error": false, "result": "done a piece"}\n')
        r.store.update(a, state='running', turns=3, log_path=str(path))
        r.settle(r.store.get(a.id))
        self.assertFalse(r.store.mark(self.agent.name, t['id'], 'limit_hold_n')[0])
        self.assertFalse(r.store.mark(self.agent.name, t['id'], 'limit_hold_until')[0])

    def test_an_authentication_failure_ends_the_streak(self):
        t = task(1)
        r = self.runner(FakeMesh([t], max_concurrent=1))
        self.no_switch()
        a = self.error_envelope(r, t, "usage limit reached")
        self.assertEqual(r.store.mark(self.agent.name, t['id'], 'limit_hold_n')[0], '1')
        path = self.root / f'{a.id}-auth.jsonl'
        path.write_text(json.dumps({"type": "result", "subtype": "success", "is_error": True, "session_id": "s",
                                    "result": "Failed to authenticate: OAuth session expired and could not be refreshed"})
                        + "\n")
        r.store.update(a, state='running', turns=2, log_path=str(path))
        r.settle(r.store.get(a.id))
        self.assertIn("authentication held", r.store.get(a.id).note)
        self.assertFalse(r.store.mark(self.agent.name, t['id'], 'limit_hold_n')[0])



class FramesTmux:
    """External process boundary: a pane that shows ``frames[i]``; Escape moves to the next frame."""
    def __init__(self, *frames):
        self.frames, self.i, self.keys = list(frames), 0, []

    def run(self, *args, **kwargs):
        if args[0] == 'send-keys':
            self.keys.append(args[-1])
            self.i = min(self.i + 1, len(self.frames) - 1)
            return subprocess.CompletedProcess(args, 0, '', '')
        assert args[0] == 'capture-pane', args
        return subprocess.CompletedProcess(args, 0, self.frames[self.i], '')


OFFER = "⏺ Usage limit reached · continuing automatically at 12am · esc to cancel"
IDLE = "\n".join(["╭────╮", "│ >  │", "╰────╯", "  ? for shortcuts"])


class StaleNoticeIsNoLimit(unittest.TestCase):
    """21.11 the only real limit (23:47, reset 00:00) was followed by ~50 failed resumes of one card up to
    02:31 while the same account ran another card's turn: the resumed session showed the past offer."""
    def driver(self, tmux, wall):
        d = tempfile.TemporaryDirectory(); self.addCleanup(d.cleanup)
        drv = td.Driver('claude', 'Steward', Path(d.name), tmux, 1, Path(d.name), 'fixture-session', 'resume',
                        'attempt', ['claude'], out=open(Path(d.name) / 'out.jsonl', 'w'), env={},
                        sleep=lambda s: None)
        self.addCleanup(drv.out.close)
        drv.wall = lambda: wall
        return drv

    def test_an_offer_past_its_time_is_dismissed_and_is_no_limit(self):
        tmux = FramesTmux("history\n" + OFFER, "history\n" + IDLE)
        self.assertEqual(self.driver(tmux, at(2, 12)).live_limit_notice(), "")
        self.assertEqual(tmux.keys, ["Escape"])

    def test_a_past_notice_left_in_history_is_no_limit_and_gets_no_keys(self):
        tmux = FramesTmux("⎿ You've hit your 5-hour limit · resets 12am (Europe/Moscow)\n" + IDLE)
        self.assertEqual(self.driver(tmux, at(2, 12)).live_limit_notice(), "")
        self.assertEqual(tmux.keys, [])

    def test_a_live_limit_stays_a_limit(self):
        for wall, frame in ((at(23, 50, 20), "x\n" + OFFER),
                            (at(2, 12), "x\n⎿ You've hit your 5-hour limit · resets 4am (Europe/Moscow)"),
                            (at(2, 12), "x\n⎿ You've hit your usage limit · resets <reset-time>")):
            with self.subTest(frame=frame):
                tmux = FramesTmux(frame)
                self.assertTrue(self.driver(tmux, wall).live_limit_notice())
                self.assertEqual(tmux.keys, [])

    def test_a_live_limit_shown_after_the_dismissed_offer_stays_a_limit(self):
        live = "x\n⎿ You've hit your 5-hour limit · resets 4am (Europe/Moscow)"
        tmux = FramesTmux("x\n" + OFFER, live)
        self.assertIn("resets 4am", self.driver(tmux, at(2, 12)).live_limit_notice())

    def test_the_turn_goes_on_to_the_paste(self):
        """The turn itself: a past offer no longer ends it as a limit before the paste (red on 0.2.113)."""
        tmux = FramesTmux("history\n" + OFFER, "history\n" + IDLE)
        drv = self.driver(tmux, at(2, 12))

        class Pasting(Exception):
            pass
        with mock.patch.object(drv, 'wait_background', return_value=True), \
                mock.patch.object(drv, 'wait_idle', side_effect=Pasting):
            with self.assertRaises(Pasting):
                drv.turn('prompt')


if __name__ == '__main__':
    unittest.main()
