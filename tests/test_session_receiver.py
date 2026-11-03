"""`mesh-runner session-receiver`: the existing-session receiver as a process of its own, over the real HTTP boundary
(the loopback FakeBackend of test_meta_delivery, strict about the frozen routes). The producer is a fake tmux that
records every command; its pane pid and start time are the baseline the receiver pins."""
import json
import subprocess
import tempfile
import threading
import time
import unittest
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path

from mesh_runner import session_receiver as sr
from mesh_runner.meta_delivery import Identity, Journal
from test_meta_delivery import AGENT_KEY, IDENT, REV, RUNNER_TOKEN, SENDER, FakeBackend, delivery

ALLOWED = {"list-panes", "load-buffer", "paste-buffer", "send-keys"}
FORBIDDEN = {"new-session", "new-window", "kill-window", "kill-session", "kill-server", "respawn-pane", "respawn-window",
             "attach-session", "detach-client", "run-shell", "select-window"}


class FakeTmux:
    """One producer window: its identity can change under the receiver, exactly like a restarted producer would."""

    def __init__(self):
        self.window_id, self.pane_pid, self.server_pid, self.dead = "@7", 4242, 900, False
        self.present = True
        self.commands: list = []
        self.started = "Sun Nov  1 10:00:00 2026"

    def run(self, *args, timeout=15):
        self.commands.append(args)
        if args[0] == "list-panes":
            if not self.present:
                return subprocess.CompletedProcess(args, 1, "", "can't find window")
            row = f"{self.window_id}\t{self.pane_pid}\t{int(self.dead)}\t{self.server_pid}\n"
            return subprocess.CompletedProcess(args, 0, row, "")
        return subprocess.CompletedProcess(args, 0, "", "")

    def pastes(self):
        return [c for c in self.commands if c[0] == "paste-buffer"]

    def targets(self):
        return {c[c.index("-t") + 1] for c in self.commands if "-t" in c and c[0] in ("paste-buffer", "send-keys")}

    def assert_hands_off(self, test):
        used = {c[0] for c in self.commands}
        test.assertTrue(used <= ALLOWED, used - ALLOWED)
        test.assertFalse(used & FORBIDDEN)


class SessionReceiverTest(unittest.TestCase):
    def setUp(self):
        FakeBackend.queued, FakeBackend.reports, FakeBackend.rejected = [], [], []
        FakeBackend.registrations, FakeBackend.states = [], {}
        FakeBackend.receivers, FakeBackend.deleted, FakeBackend.delete_status = [], [], 204
        FakeBackend.bad_receiver_id = None
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeBackend)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.dir = Path(tempfile.mkdtemp())
        self.tmux = FakeTmux()
        self.known = lambda sid: True
        (self.dir / "agent.key").write_text(AGENT_KEY)
        (self.dir / "agent.key").chmod(0o600)
        self.cred = self.dir / "chat-runner-x.json"
        self.cred.write_text(json.dumps({"runner_id": IDENT["runner_id"], "token": RUNNER_TOKEN}))
        self.cred.chmod(0o600)
        self.root = self.dir / "session_metadata"
        self.cfg = self.config()

    def toml(self, enabled="true", **over):
        meta = "\n".join(f'{k} = "{v}"' for k, v in IDENT.items())
        pin = dict(mesh_url=f"http://127.0.0.1:{self.srv.server_address[1]}", ledger_root=self.root,
                   harness="claude", runner_credential_file=self.cred, tmux_session="mesh-x", tmux_window="w",
                   tmux_window_id="@7", tmux_server_pid=900, producer_pid=4242,
                   producer_started="Sun Nov 1 10:00:00 2026")
        pin.update(over)
        pins = "\n".join(f'{k} = {v}' if isinstance(v, int) else f'{k} = "{v}"' for k, v in pin.items())
        return (f'[session_metadata]\nagent = "x"\ncredential_file = "{self.dir / "agent.key"}"\n'
                f'interval_seconds = 1\nallowed_sender_agent_ids = ["{SENDER}"]\n{meta}\n'
                f'[session_receiver]\nenabled = {enabled}\n{pins}\n')

    def config(self, mode=0o600, **kw):
        path = self.dir / "receiver.toml"
        path.write_text(self.toml(**kw))
        path.chmod(mode)
        self.path = path
        return sr.load_config(path, require_enabled=False)

    def serve(self, cfg=None, **kw):
        """Run the receiver in a thread; returns (stop event, thread, result box)."""
        stop, box = threading.Event(), {}
        cfg = cfg or self.cfg
        t = threading.Thread(target=lambda: box.update(code=sr.run(cfg, stop, self.tmux, self.known, lambda pid: self.tmux.started, **kw)),
                             daemon=True)
        t.start()
        self.addCleanup(stop.set)
        return stop, t, box

    def wait_for(self, cond, what, seconds=15):
        end = time.time() + seconds
        while time.time() < end:
            if cond():
                return
            time.sleep(0.05)
        self.fail("timed out: " + what)

    def states(self):
        return [s for s, _ in FakeBackend.reports]

    def run_once(self, **kw):
        """Start-up only: the code `run` returns when it refuses before serving."""
        stop = threading.Event()
        stop.set()                                       # a gate that passes still returns 0 at once
        return sr.run(self.cfg, stop, self.tmux, self.known, lambda pid: self.tmux.started, **kw)

    # ---------------------------------------------------------------- AC2: activation / replay / disable
    def test_activation_delivers_into_the_pinned_window_and_preserves_the_producer(self):
        d = delivery()
        FakeBackend.queued = [d]
        stop, t, box = self.serve()
        self.wait_for(lambda: "received" in self.states(), "received report")
        self.assertEqual(len(FakeBackend.registrations), 1)
        self.assertEqual(self.tmux.targets(), {"@7"})                # the pinned window id, nothing else
        self.assertEqual(FakeBackend.rejected, [])
        self.assertEqual((self.tmux.pane_pid, self.tmux.window_id, self.tmux.server_pid), (4242, "@7", 900))
        self.tmux.assert_hands_off(self)
        self.assertEqual(sr.ledger_state(self.root, self.cfg.settings.identity), "bound")
        stop.set()
        t.join(10)
        self.assertEqual(box["code"], 0)

    def test_replay_after_restart_pastes_nothing_and_keeps_the_identity_and_ledger(self):
        d = delivery()
        FakeBackend.queued = [d]
        stop, t, _ = self.serve()
        self.wait_for(lambda: "received" in self.states(), "first receipt")
        stop.set()
        t.join(10)
        pasted = len(self.tmux.pastes())
        before = (self.root / sr.PIN_FILE).read_text()
        FakeBackend.queued = [d]                                      # Mesh re-offers the same delivery
        FakeBackend.states[d["delivery_id"]] = "queued"
        stop2, t2, _ = self.serve()
        self.wait_for(lambda: len(FakeBackend.reports) >= 2, "second receipt")
        stop2.set()
        t2.join(10)
        self.assertEqual(len(self.tmux.pastes()), pasted)             # zero new injection
        self.assertEqual((self.root / sr.PIN_FILE).read_text(), before)
        self.assertEqual(len(list((self.root / "journal").glob("*.json"))), 1)

    def test_disable_stops_only_the_receiver_keeps_the_ledger_and_frees_the_lock(self):
        FakeBackend.queued = [delivery()]
        stop, t, box = self.serve()
        self.wait_for(lambda: "received" in self.states(), "receipt")
        self.assertTrue(sr.ConsumerLock(self.root).held_elsewhere())
        stop.set()
        t.join(10)
        self.assertEqual(box["code"], 0)
        self.assertFalse(sr.ConsumerLock(self.root).held_elsewhere())
        self.assertEqual(len(list((self.root / "journal").glob("*.json"))), 1)   # ledger kept
        self.tmux.assert_hands_off(self)
        self.assertEqual({c[0] for c in self.tmux.commands} & FORBIDDEN, set())

    # ---------------------------------------------------------------- disable/rollback revokes only the own admission
    def serve_then_stop(self):
        seen = len(FakeBackend.registrations)
        stop, t, box = self.serve()
        self.wait_for(lambda: len(FakeBackend.registrations) > seen and (self.root / sr.ADMISSION_FILE).exists()
                      and len(json.loads((self.root / sr.ADMISSION_FILE).read_text())["receivers"]) >= min(seen + 1, 2),
                      "registration")
        stop.set()
        t.join(10)
        self.assertEqual(box["code"], 0)

    def snapshot(self):
        return {p.name: p.read_text() for p in sorted(self.root.rglob("*.json")) if p.name != sr.ADMISSION_FILE}

    def test_revoke_deletes_exactly_the_recorded_admission_and_preserves_everything_else(self):
        FakeBackend.queued = [delivery()]
        stop, t, box = self.serve()
        self.wait_for(lambda: "received" in self.states(), "receipt")
        stop.set()
        t.join(10)
        rid = FakeBackend.receivers[0]
        self.assertEqual([i["receiver_id"] for i in json.loads((self.root / sr.ADMISSION_FILE).read_text())["receivers"]], [rid])
        self.assertEqual((self.root / sr.ADMISSION_FILE).stat().st_mode & 0o077, 0)
        before, cmds, regs = self.snapshot(), list(self.tmux.commands), len(FakeBackend.registrations)
        self.assertEqual(sr.revoke(self.cfg), 0)
        self.assertEqual(FakeBackend.deleted, [rid])                          # right path, own agent key + runner token
        self.assertEqual(FakeBackend.rejected, [])
        self.assertEqual(len(FakeBackend.registrations), regs)                # nothing new registered
        self.assertFalse((self.root / sr.ADMISSION_FILE).exists())
        self.assertTrue((self.root / sr.REVOKED_FILE).exists())
        self.assertEqual(self.snapshot() | {sr.REVOKED_FILE: ""}, before | {sr.REVOKED_FILE: ""})   # ledger, pin, journal
        self.assertEqual(self.tmux.commands, cmds)                            # the producer was not touched
        self.assertEqual(sr.revoke(self.cfg), sr.EXIT_REFUSED)                # a second revoke has nothing to revoke
        self.assertEqual(FakeBackend.deleted, [rid])

    def test_a_failed_revoke_is_not_reported_as_a_disable_and_keeps_the_record(self):
        self.serve_then_stop()
        for status in (500, 403, 409, 200, 202):   # 200/202 are successes, but not the frozen 204
            FakeBackend.delete_status = status
            self.assertEqual(sr.revoke(self.cfg), sr.EXIT_REVOKE_FAILED, status)
            self.assertTrue((self.root / sr.ADMISSION_FILE).exists())
            self.assertFalse((self.root / sr.REVOKED_FILE).exists())
        self.assertEqual(FakeBackend.deleted, [])
        FakeBackend.delete_status = 204                                       # retry works once the backend does
        self.assertEqual(sr.revoke(self.cfg), 0)
        self.assertEqual(len(FakeBackend.deleted), 1)

    def test_no_recorded_admission_or_another_identity_fails_closed_without_a_delete(self):
        self.assertEqual(sr.revoke(self.cfg), sr.EXIT_REFUSED)                # never registered here
        self.serve_then_stop()
        path = self.root / sr.ADMISSION_FILE
        good = json.loads(path.read_text())
        foreign = [{"receiver_id": str(uuid.uuid4())}]
        for bad in ({"identity_sha256": "0" * 64}, {"runner_id": str(uuid.uuid4())}, {"receivers": [{"receiver_id": "not-a-uuid"}]},
                    {"receivers": []}, {"receivers": foreign}, {"receivers": good["receivers"] + foreign}):
            path.write_text(json.dumps(good | bad))
            self.assertNotEqual(sr.revoke(self.cfg), 0, bad)
            self.assertEqual(FakeBackend.deleted, [], bad)                    # a foreign or unknown id is never deleted
            self.assertTrue(path.exists())
        path.write_text("{broken")
        self.assertEqual(sr.revoke(self.cfg), sr.EXIT_REFUSED)
        path.write_text(json.dumps(good))
        (self.root / sr.PIN_FILE).write_text(json.dumps({"identity_sha256": "f" * 64}))
        self.assertEqual(sr.revoke(self.cfg), sr.EXIT_REFUSED)                   # ledger pinned to another identity
        self.assertEqual(FakeBackend.deleted, [])

    def test_the_runner_credential_must_be_the_registered_one(self):
        self.serve_then_stop()
        self.cred.write_text(json.dumps({"runner_id": str(uuid.uuid4()), "token": RUNNER_TOKEN}))
        self.assertEqual(sr.revoke(self.cfg), sr.EXIT_REFUSED)
        self.cred.write_text(json.dumps({"runner_id": IDENT["runner_id"], "token": "wrong"}))
        self.assertEqual(sr.revoke(self.cfg), sr.EXIT_REVOKE_FAILED)          # the backend refuses a foreign token
        self.assertEqual(FakeBackend.deleted, [])

    def test_revoke_is_refused_while_a_consumer_holds_the_lock_and_cannot_race_a_registration(self):
        stop, t, box = self.serve()
        self.wait_for(lambda: (self.root / sr.ADMISSION_FILE).exists(), "registration")
        self.assertEqual(sr.revoke(self.cfg), sr.EXIT_BUSY)
        self.assertEqual(FakeBackend.deleted, [])
        stop.set()
        t.join(10)
        lock = sr.ConsumerLock(self.root)
        self.assertTrue(lock.acquire("test"))                                 # revoke holds the same lock the receiver needs
        try:
            self.assertEqual(self.run_once(), sr.EXIT_BUSY)
            self.assertEqual(sr.revoke(self.cfg), sr.EXIT_BUSY)
        finally:
            lock.release()

    def test_every_restart_registers_again_and_revoke_covers_all_of_the_own_admissions(self):
        self.serve_then_stop()
        self.serve_then_stop()
        old, new = FakeBackend.receivers[0], FakeBackend.receivers[-1]
        self.assertNotEqual(old, new)
        recorded = [i["receiver_id"] for i in json.loads((self.root / sr.ADMISSION_FILE).read_text())["receivers"]]
        self.assertEqual(recorded, [old, new])                                # the restart did not lose the first admission
        self.assertEqual(sr.revoke(self.cfg), 0)
        self.assertEqual(FakeBackend.deleted, [new, old])
        self.assertTrue((self.root / sr.REVOKED_FILE).exists())

    def test_a_partly_failed_revoke_keeps_only_what_is_still_active_and_is_not_a_success(self):
        self.serve_then_stop()
        self.serve_then_stop()
        old, new = FakeBackend.receivers[0], FakeBackend.receivers[-1]
        FakeBackend.receivers.remove(old)                                     # the backend no longer knows the older one: 404
        self.assertEqual(sr.revoke(self.cfg), sr.EXIT_REVOKE_FAILED)
        self.assertEqual(FakeBackend.deleted, [new])
        left = [i["receiver_id"] for i in json.loads((self.root / sr.ADMISSION_FILE).read_text())["receivers"]]
        self.assertEqual(left, [old])                                         # a retry asks only for the one still active
        self.assertFalse((self.root / sr.REVOKED_FILE).exists())

    def test_an_unwritable_admission_record_revokes_the_fresh_admission_and_never_registers_twice(self):
        (self.root).mkdir(parents=True, exist_ok=True)
        (self.root / sr.ADMISSION_FILE).mkdir()                               # replace() onto a directory fails
        stop, t, box = self.serve()
        t.join(10)
        self.assertFalse(t.is_alive())
        self.assertEqual(box["code"], sr.EXIT_REFUSED)
        self.assertEqual(len(FakeBackend.registrations), 1)                   # no retry storm
        self.assertEqual(FakeBackend.deleted, FakeBackend.receivers)          # the unrecorded admission was undone
        self.assertEqual(self.tmux.pastes(), [])

    def test_a_registration_answer_without_a_valid_receiver_id_is_never_served_or_recorded(self):
        FakeBackend.queued = [delivery()]
        for bad in ("None", "", "not-a-uuid", 7):
            FakeBackend.bad_receiver_id = bad
            stop, t, box = self.serve()
            time.sleep(0.6)
            stop.set()
            t.join(10)
            self.assertFalse((self.root / sr.ADMISSION_FILE).exists(), bad)
            self.assertEqual(self.states(), [], bad)
            self.assertEqual(self.tmux.pastes(), [], bad)

    def test_the_cli_revoke_action_and_exit_codes(self):
        self.serve_then_stop()
        FakeBackend.delete_status = 500
        self.assertEqual(sr.command(["revoke", "--config", str(self.path)]), sr.EXIT_REVOKE_FAILED)
        FakeBackend.delete_status = 204
        self.assertEqual(sr.command(["revoke", "--config", str(self.path)]), 0)

    def test_red_control_the_hands_off_check_catches_a_destructive_command(self):
        self.tmux.run("kill-window", "-t", "@7")
        with self.assertRaises(AssertionError):
            self.tmux.assert_hands_off(self)

    def test_restart_while_the_session_is_gone_creates_nothing_and_registers_nothing(self):
        self.tmux.present = False
        self.assertEqual(self.run_once(), sr.EXIT_REFUSED)
        self.assertEqual(FakeBackend.registrations, [])
        self.tmux.assert_hands_off(self)
        self.assertEqual({c[0] for c in self.tmux.commands}, {"list-panes"})

    # ---------------------------------------------------------------- AC2: one consumer
    def test_a_second_consumer_is_refused_without_registering_or_pasting(self):
        held = sr.ConsumerLock(self.root)
        self.assertTrue(held.acquire("controller"))
        self.addCleanup(held.release)
        FakeBackend.queued = [delivery()]
        self.assertEqual(self.run_once(), sr.EXIT_BUSY)
        self.assertEqual((FakeBackend.registrations, self.tmux.pastes(), self.states()), ([], [], []))

    def test_parallel_start_has_exactly_one_active_consumer(self):
        FakeBackend.queued = [delivery()]
        s1, t1, b1 = self.serve()
        s2, t2, b2 = self.serve()
        self.wait_for(lambda: "received" in self.states() and (b1 or b2), "one winner and one refusal")
        loser = b1 or b2
        self.assertEqual(loser["code"], sr.EXIT_BUSY)
        self.assertEqual(len(self.tmux.pastes()), 1)
        self.assertEqual(len(FakeBackend.registrations), 1)
        s1.set(), s2.set()
        t1.join(10), t2.join(10)

    def test_the_crash_leaves_the_lock_free_and_the_possible_injection_is_never_repeated(self):
        d = delivery()
        ident = self.cfg.settings.identity
        sr.bind_ledger(self.root, ident)
        Journal(self.root / "journal").put(d["request_id"], {"delivery_id": d["delivery_id"], "request_id": d["request_id"],
                                                             "text_sha256": d["text_sha256"], "state": "injecting", "bytes": 3})
        FakeBackend.queued = [d]                                      # the crashed process may have pasted this
        stop, t, _ = self.serve()
        self.wait_for(lambda: "failed" in self.states(), "uncertain reported as failed")
        stop.set()
        t.join(10)
        self.assertEqual(self.tmux.pastes(), [])                      # no automatic repeat
        self.assertEqual(FakeBackend.reports[-1][1]["error_code"], "delivery_uncertain")

    def test_uncertain_injection_is_not_repeated_by_the_next_start(self):
        d = delivery()
        FakeBackend.queued = [d]
        orig = self.tmux.run

        def refuse_paste(*a, **k):
            r = orig(*a, **k)
            return subprocess.CompletedProcess(a, 1, "", "") if a[0] == "paste-buffer" else r
        self.tmux.run = refuse_paste
        stop, t, _ = self.serve()
        self.wait_for(lambda: "failed" in self.states(), "paste failure reported")
        stop.set()
        t.join(10)
        n = len(self.tmux.pastes())
        self.tmux.run = orig
        FakeBackend.queued = [d]
        FakeBackend.states[d["delivery_id"]] = "queued"
        stop2, t2, _ = self.serve()
        self.wait_for(lambda: len(FakeBackend.reports) >= 2, "second report")
        stop2.set()
        t2.join(10)
        self.assertEqual(len(self.tmux.pastes()), n)

    # ---------------------------------------------------------------- AC2: identity / lifecycle mismatch, fail closed
    def refused(self, mutate, expect=sr.EXIT_REFUSED):
        mutate()
        FakeBackend.queued = [delivery()]
        self.assertEqual(self.run_once(), expect)
        self.assertEqual((FakeBackend.registrations, self.tmux.pastes(), self.states()), ([], [], []))
        self.tmux.assert_hands_off(self)

    def test_a_changed_producer_is_refused_at_start(self):
        self.refused(lambda: setattr(self.tmux, "pane_pid", 5555))             # restarted producer
        self.tmux.pane_pid = 4242
        self.refused(lambda: setattr(self.tmux, "started", "Sun Nov  1 11:11:11 2026"))   # pid reused
        self.tmux.started = "Sun Nov  1 10:00:00 2026"
        self.refused(lambda: setattr(self.tmux, "window_id", "@9"))            # window recreated
        self.tmux.window_id = "@7"
        self.refused(lambda: setattr(self.tmux, "server_pid", 901))            # tmux server restarted
        self.tmux.server_pid = 900
        self.refused(lambda: setattr(self.tmux, "dead", True))
        self.tmux.dead = False
        self.refused(lambda: setattr(self.tmux, "present", False))

    def test_an_empty_or_unreadable_start_time_never_passes(self):
        with self.assertRaises(sr.Refusal) as e:
            self.config(producer_started="   ")
        self.assertEqual(e.exception.code, "config_incomplete")
        self.assertEqual(sr.check_producer(self.tmux, self.cfg.pin, lambda pid: ""), "producer_start_unknown")
        empty = sr.ProducerPin("mesh-x", "w", "@7", 900, 4242, "")
        self.assertEqual(sr.check_producer(self.tmux, empty, lambda pid: ""), "producer_start_unknown")

    def test_positive_control_the_untouched_producer_passes_the_same_gate(self):
        stop, t, box = self.serve()
        self.wait_for(lambda: FakeBackend.registrations, "registration")
        stop.set()
        t.join(10)
        self.assertEqual((box["code"], len(FakeBackend.registrations)), (0, 1))

    def test_a_producer_that_changes_while_serving_gets_zero_injection(self):
        stop, t, _ = self.serve()
        self.wait_for(lambda: FakeBackend.registrations, "registration")
        self.tmux.pane_pid = 5555
        d = delivery()
        FakeBackend.queued = [d]
        self.wait_for(lambda: "failed" in self.states(), "typed failure")
        stop.set()
        t.join(10)
        self.assertEqual(self.tmux.pastes(), [])
        self.assertEqual(FakeBackend.reports[-1][1]["error_code"], "session_missing")

    def test_an_unknown_native_transcript_is_refused(self):
        self.refused(lambda: setattr(self, "known", lambda sid: False))

    def test_the_runner_credential_must_exist_match_and_is_never_created(self):
        self.refused(lambda: self.cred.write_text(json.dumps({"runner_id": str(uuid.uuid4()), "token": RUNNER_TOKEN})))
        self.refused(lambda: self.cred.unlink())
        self.assertFalse(self.cred.exists())                          # nothing registered one in its place
        self.assertEqual(FakeBackend.rejected, [])

    def test_a_ledger_bound_to_another_identity_or_unbound_with_records_is_refused(self):
        other = Identity([SENDER], **{**IDENT, "session_id": str(uuid.uuid4())})
        self.refused(lambda: sr.bind_ledger(self.root, other))                 # pinned elsewhere: no empty-ledger bypass
        fresh = self.dir / "fresh"
        self.cfg.ledger_root = fresh
        Journal(fresh / "journal").put(str(uuid.uuid4()), {"state": "uncertain"})
        self.refused(lambda: None)                                             # records, no pin
        stop, t, box = self.serve(adopt_existing_ledger=True)                  # explicit operator adoption only
        self.wait_for(lambda: FakeBackend.registrations, "registration after adoption")
        stop.set()
        t.join(10)
        self.assertEqual(box["code"], 0)

    def test_config_must_be_private_enabled_and_known(self):
        with self.assertRaises(sr.Refusal) as e:
            self.config(mode=0o644)
        self.assertEqual(e.exception.code, "config_not_private")
        cfg = self.config(enabled="false")
        with self.assertRaises(sr.Refusal) as e:
            sr.load_config(self.path)                                           # run needs enabled = true
        self.assertEqual(e.exception.code, "not_enabled")
        self.assertFalse(cfg.enabled)
        with self.assertRaises(sr.Refusal) as e:
            self.config(producer_pid=0)
        self.assertEqual(e.exception.code, "config_incomplete")
        self.path.write_text(self.toml() + "\n[chat]\nenabled = true\n")
        self.path.chmod(0o600)
        with self.assertRaises(sr.Refusal) as e:
            sr.load_config(self.path)
        self.assertEqual(e.exception.code, "config_unknown_section")

    def test_preflight_is_read_only_and_names_every_failing_check(self):
        self.tmux.pane_pid = 1
        res = dict(sr.preflight(self.cfg, self.tmux, self.known, lambda pid: self.tmux.started))
        self.assertEqual(res["producer_pin"], "producer_changed")
        self.assertEqual((FakeBackend.registrations, list(self.root.glob("*"))), ([], []))
        self.tmux.pane_pid = 4242
        res = dict(sr.preflight(self.cfg, self.tmux, self.known, lambda pid: self.tmux.started))
        self.assertEqual(set(res.values()), {"ok"})

    def test_preflight_exit_code_ignores_only_the_opt_in_switch(self):
        import contextlib
        import io

        def code():
            with contextlib.redirect_stdout(io.StringIO()) as out:
                c = sr.command(["preflight", "--config", str(self.path)])
            return c, out.getvalue()
        self.config(enabled="false")
        self.tmux_for_cli()
        c, out = code()
        self.assertEqual(c, 0)                                          # ready, opt-in pending
        self.assertIn("enabled: not_enabled (informational", out)
        self.tmux.pane_pid = 1                                          # red control: a real gate failure is non-zero
        self.assertEqual(code()[0], sr.EXIT_REFUSED)

    def tmux_for_cli(self):
        """The CLI builds its own Tmux and `ps`; route both to the fake producer."""
        orig_tmux, orig_started, orig_known = sr.Tmux, sr.process_started, sr._known
        sr.Tmux = lambda binary="tmux": self.tmux
        sr.process_started = lambda pid: self.tmux.started
        sr._known = lambda cfg: self.known
        self.addCleanup(lambda: (setattr(sr, "Tmux", orig_tmux), setattr(sr, "process_started", orig_started),
                                 setattr(sr, "_known", orig_known)))

    # ---------------------------------------------------------------- AC1/AC3: supervisor job
    def test_the_plist_supervises_only_the_receiver(self):
        text = sr.plist("/c/receiver.toml", "/usr/bin/python3", "/rel/src", "/logs")
        self.assertIn(f"<string>{sr.LABEL}</string>", text)
        self.assertIn("session-receiver", text)
        for word in ("tmux", "claude", "codex", "launchctl", "runner.toml", "mesh_runner.cli run"):
            self.assertNotIn(word, text)
        self.assertNotIn("<string>-I</string>", text)                  # isolated mode would ignore the release PYTHONPATH
        self.assertIn("<key>PYTHONPATH</key><string>/rel/src</string>", text)
        self.assertIn("<key>SuccessfulExit</key><false/>", text)       # a refusal is retried at the throttle, not in a loop
        self.assertIn("<integer>60</integer>", text)

    def test_controller_and_receiver_share_one_lock_and_one_pin(self):
        controller = sr.ConsumerLock(self.root)
        self.assertTrue(controller.acquire("controller"))
        self.assertFalse(sr.ConsumerLock(self.root).acquire("session-receiver"))
        controller.release()
        self.assertTrue(sr.ConsumerLock(self.root).acquire("session-receiver"))


if __name__ == "__main__":
    unittest.main()
