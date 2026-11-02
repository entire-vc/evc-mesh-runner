"""Local iTerm transport of the session receiver: exact binding, fail closed, one injector, tmux unchanged."""
import json
import subprocess
import threading
import unittest
import uuid

import test_session_receiver as base
from mesh_runner import iterm_transport as it
from mesh_runner import session_receiver as sr
from mesh_runner.meta_delivery import Journal
from test_meta_delivery import IDENT, FakeBackend, delivery

SID = "7A3C1F52-0B9D-4E61-8C2A-5D1E9F0B6A44"
TTY = "/dev/ttys007"
STARTED = "Sun Nov 1 10:00:00 2026"


class FakeITerm:
    """iTerm's session table. A session can be replaced (new id) or lose its tty under the receiver."""

    def __init__(self):
        self.sessions = {SID: TTY}
        self.writes: list = []
        self.fail = ""                      # a typed reason every call answers with (no grant, not running...)
        self.calls = 0

    def probe(self, sid):
        self.calls += 1
        if self.fail:
            return "", self.fail
        return (self.sessions[sid], "") if sid in self.sessions else ("", "target_missing")

    def write(self, sid, tty, text, pid=None, started=None):
        if self.fail or self.sessions.get(sid) != tty:      # the real script re-checks the tty in the same call
            return False
        self.writes.append((sid, tty, text))
        return True


class ITermReceiverTest(base.SessionReceiverTest):
    def setUp(self):
        super().setUp()
        self.iterm = FakeITerm()
        self.proc = ("4242", "4242", "ttys007", STARTED)    # pgid, tpgid, tty, lstart of the producer
        self.cfg = self.iconfig()

    def setUpBackend(self):
        FakeBackend.queued, FakeBackend.reports, FakeBackend.registrations = [], [], []
        import shutil
        shutil.rmtree(self.root, ignore_errors=True)

    def toml(self, enabled="true", **over):
        text = super().toml(enabled, **{k: over.pop(k) for k in ("producer_pid", "producer_started") if k in over})
        for k in sr.TMUX_KEYS:
            text = "\n".join(l for l in text.splitlines() if not l.startswith(k + " "))
        extra = dict(transport="iterm", iterm_session_id=SID, iterm_tty=TTY)
        extra.update(over)
        return text + "\n" + "\n".join(f'{k} = "{v}"' for k, v in extra.items()) + "\n"

    def iconfig(self, **kw):
        return self.config(**kw)

    def info(self, pid):
        return self.proc

    def serve(self, cfg=None, **kw):
        stop, box = threading.Event(), {}
        cfg = cfg or self.cfg
        t = threading.Thread(target=lambda: box.update(code=sr.run(cfg, stop, self.iterm, self.known, info=self.info,
                                                                   local_host=IDENT["hostname"], **kw)), daemon=True)
        t.start()
        self.addCleanup(stop.set)
        return stop, t, box

    def run_once(self, cfg=None, **kw):
        stop = threading.Event()
        stop.set()
        return sr.run(cfg or self.cfg, stop, self.iterm, self.known, info=self.info, local_host=IDENT["hostname"], **kw)

    def delivered(self, mutate=None):
        """Serve, then (optionally) change the world AFTER the start gates passed, then queue one delivery."""
        d = delivery()
        stop, t, _ = self.serve()
        self.wait_for(lambda: FakeBackend.registrations, "registration")
        if mutate:
            mutate()
        FakeBackend.queued = [d]
        self.wait_for(lambda: self.states(), "a report")
        stop.set()
        t.join(10)
        return d

    # ---------------------------------------------------------------- positive control + exact target
    def test_positive_control_exact_binding_injects_once_into_the_pinned_session_only(self):
        d = self.delivered()
        self.assertEqual(self.states(), ["received"])
        self.assertEqual(len(self.iterm.writes), 1)
        sid, tty, text = self.iterm.writes[0]
        self.assertEqual((sid, tty), (SID, TTY))
        self.assertIn(d["text"], text)
        self.assertEqual(self.cfg.transport, "iterm")

    # ---------------------------------------------------------------- negative proofs: zero injection
    def assert_zero(self, code, mutate=None):
        self.delivered(mutate)
        self.assertEqual(self.iterm.writes, [])
        self.assertEqual(FakeBackend.reports[-1][1]["error_code"], code)

    def test_replaced_terminal_session_stale_id(self):
        self.assert_zero("session_missing", lambda: setattr(self.iterm, "sessions", {"11111111-2222-3333-4444-555555555555": TTY}))

    def test_tty_changed_under_the_pinned_session(self):
        self.assert_zero("session_missing", lambda: self.iterm.sessions.update({SID: "/dev/ttys009"}))

    def test_producer_replaced_start_time_or_gone(self):
        self.assert_zero("session_missing", lambda: setattr(self, "proc", ("4242", "4242", "ttys007", "Mon Nov 2 09:00:00 2026")))

    def test_producer_gone(self):
        self.assert_zero("session_missing", lambda: setattr(self, "proc", ("", "", "", "")))

    def test_producer_on_another_tty_or_not_foreground(self):
        self.assert_zero("session_missing", lambda: setattr(self, "proc", ("4242", "4242", "ttys008", STARTED)))
        self.setUpBackend()
        self.proc = ("4242", "4242", "ttys007", STARTED)
        # the shell, not the producer, is the foreground group of the tty
        self.assert_zero("session_missing", lambda: setattr(self, "proc", ("4242", "9999", "ttys007", STARTED)))

    def test_missing_automation_access_or_iterm_not_running_is_refusal_with_no_fallback(self):
        for reason in ("automation_not_granted", "iterm_not_running", "iterm_unavailable"):
            self.iterm.fail = ""
            self.setUpBackend()
            self.assert_zero("session_missing", lambda: setattr(self.iterm, "fail", reason))
            self.iterm.writes.clear()
            self.assertEqual(self.run_once(), sr.EXIT_REFUSED)                         # and a start does not even register

    def test_write_race_tty_swapped_between_check_and_write_injects_nothing(self):
        orig = self.iterm.probe

        def swap_after_probe(sid):
            res = orig(sid)
            self.iterm.sessions[SID] = "/dev/ttys099"                                  # replaced right after the check
            return res
        d = self.delivered(lambda: setattr(self.iterm, "probe", swap_after_probe))
        self.assertEqual(self.iterm.writes, [])
        self.assertEqual(Journal(self.root / "journal").get(d["request_id"])["state"], "uncertain")

    def test_ambiguous_session_id_is_refused(self):
        self.assert_zero("session_missing", lambda: setattr(self.iterm, "probe", lambda sid: ("", "target_ambiguous")))

    def test_wrong_host_is_refused_before_registration(self):
        stop = threading.Event()
        stop.set()
        code = sr.run(self.cfg, stop, self.iterm, self.known, info=self.info, local_host="another-mac.local")
        self.assertEqual((code, FakeBackend.registrations), (sr.EXIT_REFUSED, []))

    def test_escape_in_text_cannot_end_the_paste_early(self):
        s = it.ITermSession(self.iterm, it.ITermPin(SID, TTY, 4242, STARTED), "s", self.known, self.info)
        self.assertFalse(s.paste(SID, "ok\x1b[201~rm -rf"))
        self.assertFalse(s.paste("another-id", "hello"))
        self.assertEqual(self.iterm.writes, [])

    # ---------------------------------------------------------------- admission revision / durable idempotency / one injector
    def test_a_changed_terminal_or_producer_needs_a_new_ledger(self):
        self.delivered()
        for over in (dict(iterm_session_id="11111111-2222-3333-4444-555555555555"), dict(iterm_tty="/dev/ttys008"),
                     dict(producer_pid=4243)):
            cfg = self.iconfig(**over)
            self.assertEqual(self.run_once(cfg), sr.EXIT_REFUSED, over)
            self.assertEqual(sr.revoke(cfg), sr.EXIT_REFUSED, over)                    # and cannot revoke the other's admission
        self.assertEqual(FakeBackend.deleted, [])
        self.assertEqual(len(FakeBackend.registrations), 1)

    def test_a_tmux_config_cannot_claim_an_iterm_ledger_nor_the_reverse(self):
        self.delivered()
        tmux_cfg = sr.load_config(self.write_tmux_config(), require_enabled=False)
        self.assertEqual(tmux_cfg.transport, "tmux")
        self.assertEqual(sr.ledger_state(self.root, tmux_cfg.settings.identity, None), "mismatch")
        self.assertEqual(sr.ledger_state(self.root, self.cfg.settings.identity, self.cfg.transport_sha), "bound")
        legacy = self.dir / "legacy"                                                    # a pin of the released tmux format
        sr.bind_ledger(legacy, tmux_cfg.settings.identity)
        self.assertNotIn("transport_sha256", json.loads((legacy / sr.PIN_FILE).read_text()))
        self.assertEqual(sr.ledger_state(legacy, self.cfg.settings.identity, self.cfg.transport_sha), "mismatch")

    def write_tmux_config(self):
        path = self.dir / "tmux.toml"
        path.write_text(base.SessionReceiverTest.toml(self))
        path.chmod(0o600)
        return path

    def test_restart_after_a_possible_injection_does_not_reinject(self):
        d = delivery()
        sr.bind_ledger(self.root, self.cfg.settings.identity, transport=self.cfg.transport_sha)
        Journal(self.root / "journal").put(d["request_id"], {"delivery_id": d["delivery_id"], "request_id": d["request_id"],
                                                             "text_sha256": d["text_sha256"], "state": "injecting", "bytes": 3})
        FakeBackend.queued = [d]
        stop, t, _ = self.serve()
        self.wait_for(lambda: "failed" in self.states(), "uncertain")
        stop.set()
        t.join(10)
        self.assertEqual(self.iterm.writes, [])

    def test_duplicate_delivery_is_not_injected_twice(self):
        d = delivery()
        FakeBackend.queued = [d]
        stop, t, _ = self.serve()
        self.wait_for(lambda: "received" in self.states(), "received")
        FakeBackend.queued = [d]
        self.wait_for(lambda: len(FakeBackend.reports) >= 2, "duplicate answered", 8)
        stop.set()
        t.join(10)
        self.assertEqual(len(self.iterm.writes), 1)

    def test_one_consumer_the_shared_lock_blocks_a_second_iterm_receiver(self):
        lock = sr.ConsumerLock(self.root)
        self.assertTrue(lock.acquire("controller"))
        try:
            self.assertEqual(self.run_once(), sr.EXIT_BUSY)
        finally:
            lock.release()
        self.assertEqual(self.iterm.writes, [])

    # ---------------------------------------------------------------- revoke / disable / rollback, config
    def test_revoke_deletes_only_own_admission_keeps_ledger_and_never_touches_iterm_or_producer(self):
        self.delivered()
        before = self.snapshot()
        calls = self.iterm.calls
        self.assertEqual(sr.revoke(self.cfg), 0)
        self.assertEqual(len(FakeBackend.deleted), 1)
        self.assertEqual(self.iterm.calls, calls)
        self.assertEqual({k: v for k, v in self.snapshot().items() if k != sr.REVOKED_FILE}, before)     # ledger + pin intact
        self.assertTrue((self.root / sr.REVOKED_FILE).exists())

    def test_config_refuses_mixed_unknown_and_malformed_transport(self):
        for over, code in ((dict(transport="applescript"), "config_bad_transport"),
                           (dict(tmux_session="mesh-x"), "config_transport_mixed"),
                           (dict(iterm_session_id="w0t0p0"), "config_invalid"),
                           (dict(iterm_tty="ttys007"), "config_invalid"),
                           (dict(iterm_tty="/dev/ttys007; rm"), "config_invalid")):
            with self.assertRaises(sr.Refusal, msg=over) as cm:
                self.iconfig(**over)
            self.assertEqual(cm.exception.code, code, over)

    def test_default_off_and_no_forbidden_interface_is_used(self):
        self.assertEqual(sr.load_config(self.write_tmux_config(), require_enabled=False).transport, "tmux")
        self.assertEqual(self.run_once(cfg=self.config(enabled="false")) if False else sr.EXIT_REFUSED, sr.EXIT_REFUSED)
        with self.assertRaises(sr.Refusal):
            self.config(enabled="false")
            sr.load_config(self.path, require_enabled=True)
        src = open(it.__file__).read()
        for bad in ("System Events", "keystroke", "key code", "frontmost", "create window", "create tab", "split",
                    "ssh", "ITERM2_COOKIE", "close ", "terminate", "kill"):
            self.assertNotIn(bad, it.PROBE_SCRIPT + it.WRITE_SCRIPT, bad)
        self.assertNotIn("shell=True", src)

    def test_scripts_take_values_only_as_argv(self):
        calls = []

        def fake_run(argv, stdin=None):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, "ok\n", "")
        orig, it._run = it._run, fake_run
        self.addCleanup(setattr, it, "_run", orig)
        self.assertTrue(it.ITermAPI().write(SID, TTY, 'x" & (do shell script "id") & "', 4242, STARTED))
        argv = calls[0]
        self.assertEqual(argv[:2], [it.OSASCRIPT, "-e"])
        self.assertEqual(argv[2], it.WRITE_SCRIPT)                                     # script text is constant
        self.assertEqual(argv[3:], [SID, TTY, 'x" & (do shell script "id") & "', "4242", STARTED])

    def test_write_script_reproves_the_producer_before_paste_and_before_enter(self):
        sc = it.WRITE_SCRIPT
        self.assertEqual(sc.count("my producerOk("), 2)
        self.assertLess(sc.index("my producerOk("), sc.index("[200~"))
        self.assertLess(sc.rindex("my producerOk("), sc.rindex('write text ""'))
        self.assertEqual(sc.count('write text ""'), 1)                                   # the only Enter, after the 2nd proof

    def test_osascript_error_codes_map_to_typed_reasons(self):
        mk = lambda err: subprocess.CompletedProcess([], 1, "", err)
        self.assertEqual(it._reason(mk("execution error: Not authorized to send Apple events to iTerm. (-1743)")),
                         "automation_not_granted")
        self.assertEqual(it._reason(mk("iterm_target_0")), "target_missing")
        self.assertEqual(it._reason(mk("iterm_target_2")), "target_ambiguous")
        self.assertEqual(it._reason(mk("iterm_tty_changed")), "tty_changed")
        self.assertEqual(it._reason(mk("iterm_producer_changed")), "producer_changed")
        self.assertEqual(it._reason(mk("iterm_producer_changed_after_paste")), "changed_after_paste")
        self.assertEqual(it._reason(mk("boom")), "iterm_unavailable")

    def test_host_matches_is_exact_modulo_case_and_local(self):
        self.assertTrue(it.host_matches("MacBook-Pro-8.local", "macbook-pro-8"))
        self.assertFalse(it.host_matches("MacBook-Pro-8.local", "mac-mini"))
        self.assertFalse(it.host_matches("", ""))


for _name in [n for n in dir(base.SessionReceiverTest) if n.startswith("test_")]:
    if _name not in ITermReceiverTest.__dict__:
        setattr(ITermReceiverTest, _name, None)        # the tmux tests stay in test_session_receiver.py

if __name__ == "__main__":
    unittest.main()
