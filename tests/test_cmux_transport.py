"""Local cmux transport of the session receiver: exact native binding, no implicit resume, no queue as receipt.

The boundary is the real one the receiver uses: a ``cmux`` executable called as ``cmux --socket <path> rpc
surface.exact_input <json>``. The fake CLI below implements contract ``cmux.exact_input.v1`` over a JSON "world" (the
surfaces, their runtime, and the processes cmux would see) at the path passed as ``--socket``; tests change the world
under the receiver exactly where a real surface would be replaced, hibernated or re-spawned."""
import json
import os
import re
import subprocess
import sys
import threading
import unittest
from pathlib import Path

import test_session_receiver as base
from mesh_runner import cmux_transport as ct
from mesh_runner import session_receiver as sr
from mesh_runner.meta_delivery import Journal
from test_meta_delivery import IDENT, FakeBackend, delivery

WS = "11111111-2222-4333-8444-555555555555"
SURF = "66666666-7777-4888-9999-000000000000"
OTHER = "0D1E2F30-4152-4637-8899-AABBCCDDEEFF"
TTY = "/dev/ttys002"
STARTED = "Sun Nov 1 10:00:00 2026"
PID = 11875

FAKE_CLI = r'''
import json, os, sys
args = sys.argv[1:]
world_path = None
if args[:1] == ["--socket"]:
    world_path, args = args[1], args[2:]
w = json.load(open(world_path))
def save():
    json.dump(w, open(world_path, "w"))
def fail(line):
    save()
    sys.stderr.write(line + "\n")
    sys.exit(1)
if w.get("down"):
    fail("Error: Socket connection failed: No such file or directory")
if args[:1] != ["rpc"] or len(args) != 3:
    fail("Error: Usage: cmux rpc <method> [json-params]")
method, p = args[1], json.loads(args[2])
w.setdefault("calls", []).append({"method": method, "mode": p.get("mode"),
                                  "params": {k: v for k, v in p.items() if k != "text"},
                                  "env": sorted(k for k in os.environ if k.startswith("CMUX_"))})
if w.get("old") or method != "surface.exact_input":
    fail("Error: method_not_found: Unknown method")
mode = p.get("mode")
if (p.get("contract") != "cmux.exact_input.v1" or p.get("no_resume") is not True or p.get("no_queue") is not True
        or mode not in ("probe", "stage", "submit")):
    fail("Error: exact_invalid_params: refused")
s = w["surfaces"].get(str(p.get("surface_id", "")).upper())
if not s or s["workspace"] != str(p.get("workspace_id", "")).upper():
    fail("Error: exact_target_missing: no such surface")
for flag, code in (("replacing", "target_replacing"), ("hibernated", "target_hibernated")):
    if s.get(flag):
        fail("Error: exact_%s: refused" % code)
if not s.get("live"):
    fail("Error: exact_surface_unavailable: refused")
if not s.get("tty_reported"):
    fail("Error: exact_tty_unreported: refused")
if s["tty"] != p.get("expected_tty"):
    fail("Error: exact_tty_mismatch: refused")
pr = w["procs"].get(str(p.get("expected_pid")))
if not pr:
    fail("Error: exact_producer_gone: refused")
if pr["start"] != p.get("expected_pid_start"):
    fail("Error: exact_producer_changed: refused")
if pr["tty"] != s["tty"]:
    fail("Error: exact_producer_not_on_tty: refused")
if not pr["foreground"]:
    fail("Error: exact_producer_not_foreground: refused")
if (mode != "submit" and not s.get("bracketed", True)) or (mode == "stage" and w.get("stage_unbracketed")):
    fail("Error: exact_paste_unbracketed: refused")     # the witness refuses before (probe) or at (stage) the write
if mode == "submit" and p.get("expected_runtime_generation") != s["generation"]:
    fail("Error: exact_generation_changed: refused")
ans = {"contract": "cmux.exact_input.v1", "mode": mode, "delivery": "probed" if mode == "probe" else "received",
       "workspace_id": s["workspace"], "surface_id": str(p["surface_id"]).upper(), "tty": s["tty"],
       "producer_pid": p["expected_pid"], "producer_start": pr["start"], "runtime_generation": s["generation"],
       "resumed": False, "queued": False}
if mode == "stage":
    w.setdefault("writes", []).append({"surface": ans["surface_id"], "text": p.get("text"), "framed": True})
    for path, value in w.pop("after_stage", []):
        node = w
        for k in path[:-1]:
            node = node[k]
        node[path[-1]] = value
if mode == "submit":
    w.setdefault("writes", []).append({"surface": ans["surface_id"], "key": p.get("submit_key")})
ans.update(w.get("misbehave", {}).get(mode, {}))
save()
print(json.dumps(ans))
'''


class CmuxReceiverTest(base.SessionReceiverTest):
    def setUp(self):
        super().setUp()
        self.cli = self.dir / "cmux"
        self.cli.write_text(f"#!{sys.executable} -I\n" + FAKE_CLI)
        self.cli.chmod(0o755)
        self.world_path = self.dir / "cmux-world.json"
        self.born = ct.start_epoch(STARTED)
        self.write_world({"surfaces": {SURF: dict(workspace=WS, tty=TTY, tty_reported=True, live=True, generation="7")},
                          "procs": {str(PID): dict(tty=TTY, start=self.born, foreground=True)}})
        self.proc = (str(PID), str(PID), "ttys002", STARTED)       # pgid, tpgid, tty, lstart as this host's ps sees it
        self.cfg = self.cconfig()
        self.api = ct.CmuxAPI(str(self.cli), str(self.world_path))

    def setUpBackend(self):
        FakeBackend.queued, FakeBackend.reports, FakeBackend.registrations = [], [], []
        import shutil
        shutil.rmtree(self.root, ignore_errors=True)

    # ---------------------------------------------------------------- helpers
    def write_world(self, w):
        self.world_path.write_text(json.dumps(w))

    def world(self):
        return json.loads(self.world_path.read_text())

    def edit(self, fn):
        w = self.world()
        fn(w)
        self.write_world(w)

    def writes(self):
        return self.world().get("writes", [])

    def modes(self):
        return [c["mode"] for c in self.world().get("calls", [])]

    def toml(self, enabled="true", **over):
        text = super().toml(enabled, **{k: over.pop(k) for k in ("producer_pid", "producer_started") if k in over})
        text = text.replace("producer_pid = 4242", f"producer_pid = {PID}").replace(
            'producer_started = "Sun Nov 1 10:00:00 2026"', f'producer_started = "{STARTED}"')
        for k in sr.TMUX_KEYS:
            text = "\n".join(l for l in text.splitlines() if not l.startswith(k + " "))
        extra = dict(transport="cmux", cmux_workspace_id=WS, cmux_surface_id=SURF, cmux_tty=TTY,
                     cmux_binary=str(getattr(self, "cli", "/nonexistent/cmux")),
                     cmux_socket=str(getattr(self, "world_path", "/nonexistent/world")))   # base setUp runs first
        extra.update(over)
        return text + "\n" + "\n".join(f'{k} = "{v}"' if not isinstance(v, int) else f"{k} = {v}"
                                       for k, v in extra.items() if v is not None) + "\n"

    def cconfig(self, **kw):
        return self.config(**kw)

    def info(self, pid):
        if getattr(self, "proc_fn", None):
            return self.proc_fn(pid)
        return self.proc if pid == PID else ("", "", "", "")

    def serve(self, cfg=None, **kw):
        stop, box = threading.Event(), {}
        cfg = cfg or self.cfg
        t = threading.Thread(target=lambda: box.update(code=sr.run(cfg, stop, None, self.known, info=self.info,
                                                                   local_host=IDENT["hostname"], **kw)), daemon=True)
        t.start()
        self.addCleanup(stop.set)
        return stop, t, box

    def run_once(self, cfg=None, **kw):
        stop = threading.Event()
        stop.set()
        return sr.run(cfg or self.cfg, stop, None, self.known, info=self.info, local_host=IDENT["hostname"], **kw)

    def delivered(self, mutate=None, d=None):
        """Serve, then (optionally) change the world AFTER the start gates passed, then queue one delivery."""
        d = d or delivery()
        stop, t, _ = self.serve()
        self.wait_for(lambda: FakeBackend.registrations, "registration")
        if mutate:
            mutate()
        FakeBackend.queued = [d]
        self.wait_for(lambda: self.states(), "a report")
        stop.set()
        t.join(10)
        return d

    def assert_zero(self, code, mutate=None):
        self.delivered(mutate)
        self.assertEqual(self.writes(), [])
        self.assertNotIn("stage", self.modes())
        self.assertEqual(FakeBackend.reports[-1][1]["error_code"], code)

    # ---------------------------------------------------------------- AC2: positive control, received -> acknowledged
    def test_positive_control_exact_target_receives_once_then_the_producer_ack_acknowledges(self):
        d = self.delivered()
        self.assertEqual(self.states(), ["received"])
        stage, enter = self.writes()
        self.assertEqual(stage["surface"], SURF)
        self.assertIn(d["text"], stage["text"])
        self.assertIn(f"delivery_id={d['delivery_id']}", stage["text"])
        self.assertIn("\n", stage["text"])                   # multi-line: the reason the stage must be framed
        self.assertTrue(stage["framed"])
        self.assertEqual(enter, {"surface": SURF, "key": "return"})
        sub = [c for c in self.world()["calls"] if c["mode"] == "submit"][0]["params"]
        self.assertEqual(sub["expected_runtime_generation"], "7")
        for c in self.world()["calls"]:                     # every call names the exact target; focus is never asked
            self.assertEqual((c["params"]["workspace_id"], c["params"]["surface_id"], c["params"]["expected_pid"],
                              c["params"]["expected_pid_start"], c["params"]["no_resume"], c["params"]["no_queue"]),
                             (WS, SURF, PID, self.born, True, True))
        # the producer answers: the next poll turns `received` into `acknowledged`, nothing is pasted again
        ack_dir = self.root / "acks"
        ack_dir.mkdir(parents=True, exist_ok=True)
        (ack_dir / f"{d['delivery_id']}.json").write_text(json.dumps({
            "delivery_id": d["delivery_id"], "request_id": d["request_id"], "hostname": IDENT["hostname"],
            "session_id": IDENT["session_id"]}))
        stop, t, _ = self.serve()
        self.wait_for(lambda: "acknowledged" in self.states(), "acknowledged")
        stop.set()
        t.join(10)
        self.assertEqual(len(self.writes()), 2)

    # ---------------------------------------------------------------- AC1/AC3: replaced / changed target, zero input
    def test_surface_replaced_before_claim_injects_nothing(self):
        def replace(w):
            w["surfaces"] = {OTHER: dict(workspace=WS, tty=TTY, tty_reported=True, live=True, generation="1")}
        self.assert_zero("session_missing", lambda: self.edit(replace))

    def test_replacement_producer_on_the_surviving_surface_injects_nothing(self):
        def respawn(w):                                       # same surface and tty, a new native process
            w["procs"] = {"22222": dict(tty=TTY, start=self.born + 3600, foreground=True)}
        self.assert_zero("session_missing", lambda: (self.edit(respawn),
                                                     setattr(self, "proc", ("22222", "22222", "ttys002", "x"))))

    def test_native_pid_start_mismatch_seen_only_by_cmux_injects_nothing(self):
        # the local ps still shows the pinned start, but cmux's kernel read disagrees: the cmux refusal wins
        self.assert_zero("session_missing", lambda: self.edit(lambda w: w["procs"][str(PID)].update(start=self.born + 1)))

    def test_native_pid_start_mismatch_seen_locally_injects_nothing(self):
        self.assert_zero("session_missing", lambda: setattr(self, "proc", (str(PID), str(PID), "ttys002",
                                                                           "Mon Nov 2 09:00:00 2026")))

    def test_producer_on_another_tty_or_not_foreground(self):
        self.assert_zero("session_missing", lambda: self.edit(lambda w: w["procs"][str(PID)].update(foreground=False)))
        self.setUpBackend()
        self.edit(lambda w: (w.update(calls=[]), w["procs"][str(PID)].update(foreground=True)))
        self.assert_zero("session_missing", lambda: setattr(self, "proc", (str(PID), "9999", "ttys002", STARTED)))

    def test_tty_unreported_or_changed_injects_nothing(self):
        self.assert_zero("session_missing", lambda: self.edit(lambda w: w["surfaces"][SURF].update(tty_reported=False)))
        self.setUpBackend()
        self.edit(lambda w: w["surfaces"][SURF].update(tty_reported=True))
        self.assert_zero("session_missing", lambda: self.edit(lambda w: w["surfaces"][SURF].update(tty="/dev/ttys009")))

    def test_hibernated_surface_is_refused_and_never_resumed(self):
        self.assert_zero("session_missing", lambda: self.edit(lambda w: w["surfaces"][SURF].update(hibernated=True)))
        self.assertEqual(set(self.modes()), {"probe"})        # nothing but read-only probes reached cmux
        self.assertEqual(self.run_once(), sr.EXIT_REFUSED)    # and a start while hibernated does not register
        self.assertEqual(len(FakeBackend.registrations), 1)

    def test_a_terminal_without_bracketed_paste_is_refused_before_claim(self):
        # the block is multi-line: unframed, an interior newline would submit before the Enter check
        self.assert_zero("session_missing", lambda: self.edit(lambda w: w["surfaces"][SURF].update(bracketed=False)))

    def test_bracketed_paste_switched_off_between_probe_and_stage_writes_nothing(self):
        d = self.delivered(lambda: self.edit(lambda w: w.update(stage_unbracketed=True)))
        self.assertEqual(self.writes(), [])                  # the stage itself refused: zero bytes, no Enter
        self.assertEqual(Journal(self.root / "journal").get(d["request_id"])["state"], "uncertain")

    def test_surface_without_live_runtime_is_refused_not_queued(self):
        self.assert_zero("session_missing", lambda: self.edit(lambda w: w["surfaces"][SURF].update(live=False)))

    def test_replacement_between_stage_and_submit_sends_no_enter_and_is_uncertain(self):
        d = self.delivered(lambda: self.edit(lambda w: w.update(after_stage=[[["surfaces", SURF, "generation"], "8"]])))
        writes = self.writes()
        self.assertEqual(len(writes), 1)                      # the stage only: inert, unsubmitted text
        self.assertNotIn("key", writes[0])
        self.assertEqual(Journal(self.root / "journal").get(d["request_id"])["state"], "uncertain")
        self.assertEqual(self.states(), ["failed"])

    def test_producer_gone_between_stage_and_submit_sends_no_enter(self):
        calls = {"n": 0}

        def flaky(pid):                                       # alive for the target and pre-stage checks, then gone
            calls["n"] += 1
            return self.proc if calls["n"] <= 2 else ("", "", "", "")
        d = self.delivered(lambda: setattr(self, "proc_fn", flaky))
        self.assertEqual([w.get("key") for w in self.writes()], [None])
        self.assertEqual(Journal(self.root / "journal").get(d["request_id"])["state"], "uncertain")

    # ---------------------------------------------------------------- a queue/resume answer never masquerades as received
    def test_a_cmux_that_queues_or_resumes_is_never_received(self):
        for mode, bad in (("stage", {"queued": True, "delivery": "queued"}), ("stage", {"resumed": True}),
                          ("submit", {"delivery": "queued"}), ("stage", {"surface_id": OTHER}),
                          ("stage", {"producer_pid": 1})):
            self.setUpBackend()
            self.edit(lambda w: w.update(misbehave={mode: bad}, writes=[]))
            d = self.delivered()
            self.assertEqual(self.states(), ["failed"], (mode, bad))
            self.assertEqual(Journal(self.root / "journal").get(d["request_id"])["state"], "uncertain", (mode, bad))

    def test_red_control_a_probe_answering_resumed_is_refused_at_start(self):
        self.edit(lambda w: w.update(misbehave={"probe": {"resumed": True}}))
        self.assertEqual(self.run_once(), sr.EXIT_REFUSED)
        self.assertEqual(FakeBackend.registrations, [])
        self.assertEqual(ct.check_cmux(self.api, self.cfg.pin, self.info), "cmux_resumed")
        self.edit(lambda w: w.update(misbehave={}))
        self.assertEqual(ct.check_cmux(self.api, self.cfg.pin, self.info), "")       # positive control, same world

    # ---------------------------------------------------------------- unsupported / unavailable cmux
    def test_an_older_cmux_without_the_contract_refuses_before_registration(self):
        self.edit(lambda w: w.update(old=True))
        self.assertEqual(self.run_once(), sr.EXIT_REFUSED)
        self.assertEqual(FakeBackend.registrations, [])
        checks = dict(sr.preflight(self.cfg, None, self.known, info=self.info, local_host=IDENT["hostname"]))
        self.assertEqual(checks["terminal_binding"], "cmux_exact_input_unsupported")
        self.assertEqual(self.writes(), [])

    def test_cmux_down_or_missing_cli_is_refusal(self):
        self.edit(lambda w: w.update(down=True))
        self.assertEqual(ct.check_cmux(self.api, self.cfg.pin, self.info), "cmux_unavailable")
        missing = ct.CmuxAPI(str(self.dir / "no-such-cmux"), str(self.world_path))
        self.assertEqual(ct.check_cmux(missing, self.cfg.pin, self.info), "cmux_unavailable")

    def test_wrong_host_is_refused_before_registration(self):
        stop = threading.Event()
        stop.set()
        code = sr.run(self.cfg, stop, None, self.known, info=self.info, local_host="another-mac.local")
        self.assertEqual((code, FakeBackend.registrations), (sr.EXIT_REFUSED, []))

    # ---------------------------------------------------------------- duplicate / crash / uncertain / one injector
    def test_duplicate_delivery_is_not_injected_twice(self):
        d = delivery()
        FakeBackend.queued = [d]
        stop, t, _ = self.serve()
        self.wait_for(lambda: "received" in self.states(), "received")
        FakeBackend.queued = [d]
        self.wait_for(lambda: len(FakeBackend.reports) >= 2, "duplicate answered", 8)
        stop.set()
        t.join(10)
        self.assertEqual(len(self.writes()), 2)               # one stage + one Enter, once

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
        self.assertEqual(self.writes(), [])

    def test_one_consumer_the_shared_lock_blocks_a_second_receiver(self):
        lock = sr.ConsumerLock(self.root)
        self.assertTrue(lock.acquire("controller"))
        try:
            self.assertEqual(self.run_once(), sr.EXIT_BUSY)
        finally:
            lock.release()
        self.assertEqual(self.world().get("calls", []), [])

    # ---------------------------------------------------------------- wrong transport, binding digest, revocation
    def test_a_changed_surface_tty_or_producer_needs_a_new_ledger(self):
        self.delivered()
        for over in (dict(cmux_surface_id=OTHER), dict(cmux_workspace_id=OTHER), dict(cmux_tty="/dev/ttys008"),
                     dict(producer_pid=4243)):
            cfg = self.cconfig(**over)
            self.assertEqual(self.run_once(cfg), sr.EXIT_REFUSED, over)
            self.assertEqual(sr.revoke(cfg), sr.EXIT_REFUSED, over)
        self.assertEqual(FakeBackend.deleted, [])
        self.assertEqual(len(FakeBackend.registrations), 1)

    def test_client_location_is_not_part_of_the_binding(self):
        moved = self.cconfig(cmux_binary="/opt/cmux/bin/cmux", cmux_socket="/tmp/other.sock")
        self.assertEqual(moved.transport_sha, self.cfg.transport_sha)
        self.assertEqual(self.cconfig(cmux_surface_id=SURF.lower()).transport_sha, self.cfg.transport_sha)

    def test_other_transports_cannot_claim_a_cmux_ledger_nor_the_reverse(self):
        self.delivered()
        tmux_path = self.dir / "tmux.toml"
        tmux_path.write_text(base.SessionReceiverTest.toml(self))
        tmux_path.chmod(0o600)
        tmux_cfg = sr.load_config(tmux_path, require_enabled=False)
        self.assertEqual(sr.ledger_state(self.root, tmux_cfg.settings.identity, None), "mismatch")
        self.assertEqual(sr.ledger_state(self.root, self.cfg.settings.identity, self.cfg.transport_sha), "bound")
        legacy = self.dir / "legacy"
        sr.bind_ledger(legacy, tmux_cfg.settings.identity)
        self.assertEqual(sr.ledger_state(legacy, self.cfg.settings.identity, self.cfg.transport_sha), "mismatch")

    def test_config_refuses_mixed_unknown_malformed_and_qa_on_cmux(self):
        for over, code in ((dict(transport="cmux-queue"), "config_bad_transport"),
                           (dict(tmux_session="mesh-x"), "config_transport_mixed"),
                           (dict(iterm_tty=TTY), "config_transport_mixed"),
                           (dict(cmux_surface_id="surface:1000000003"), "config_invalid"),
                           (dict(cmux_workspace_id="workspace:1"), "config_invalid"),
                           (dict(cmux_tty="ttys002"), "config_invalid"),
                           (dict(cmux_tty=None), "config_incomplete")):
            with self.assertRaises(sr.Refusal, msg=over) as cm:
                self.cconfig(**over)
            self.assertEqual(cm.exception.code, code, over)
        path = self.dir / "receiver.toml"
        path.write_text(self.toml() + '[host_local_qa_assignment_v1]\nenabled = true\n')
        path.chmod(0o600)
        with self.assertRaises(sr.Refusal) as cm:
            sr.load_config(path, require_enabled=False)
        self.assertEqual(cm.exception.code, "qa_needs_iterm_transport")
        iterm_path = self.dir / "iterm.toml"                  # the client keys belong to cmux only
        iterm_path.write_text(base.SessionReceiverTest.toml(self) + 'transport = "iterm"\ncmux_socket = "/x"\n')
        iterm_path.chmod(0o600)
        with self.assertRaises(sr.Refusal) as cm:
            sr.load_config(iterm_path, require_enabled=False)
        self.assertEqual(cm.exception.code, "config_transport_mixed")

    def test_revoke_deletes_only_own_admission_and_never_calls_cmux(self):
        self.delivered()
        calls = len(self.world()["calls"])
        before = self.snapshot()
        self.assertEqual(sr.revoke(self.cfg), 0)
        self.assertEqual(len(FakeBackend.deleted), 1)
        self.assertEqual(len(self.world()["calls"]), calls)
        self.assertEqual({k: v for k, v in self.snapshot().items() if k != sr.REVOKED_FILE}, before)
        self.assertTrue((self.root / sr.REVOKED_FILE).exists())

    # ---------------------------------------------------------------- the client: argv, env, no forbidden verb
    def test_values_travel_as_one_json_argv_and_caller_surface_env_is_dropped(self):
        os.environ["CMUX_SURFACE_ID"], os.environ["CMUX_WORKSPACE_ID"] = OTHER, OTHER
        self.addCleanup(os.environ.pop, "CMUX_SURFACE_ID", None)
        self.addCleanup(os.environ.pop, "CMUX_WORKSPACE_ID", None)
        seen = []
        orig = subprocess.run

        def spy(argv, **kw):
            seen.append(argv)
            return orig(argv, **kw)
        ct.subprocess.run = spy
        self.addCleanup(setattr, ct.subprocess, "run", orig)
        s = ct.CmuxSession(self.api, self.cfg.pin, "s", self.known, self.info, pause=lambda _: None)
        self.assertTrue(s.paste(SURF, 'x"; $(id) `id` \n line2'))
        self.assertEqual(seen[0][:5], [str(self.cli), "--socket", str(self.world_path), "rpc", ct.METHOD])
        self.assertEqual(len(seen[0]), 6)
        self.assertEqual(json.loads(seen[0][5])["text"], 'x"; $(id) `id` \n line2')
        self.assertTrue(all(c["env"] == [] for c in self.world()["calls"]))

    def test_escape_nul_or_another_target_is_refused_without_a_call(self):
        s = ct.CmuxSession(self.api, self.cfg.pin, "s", self.known, self.info, pause=lambda _: None)
        self.assertFalse(s.paste(SURF, "ok\x1b[201~rm -rf"))
        self.assertFalse(s.paste(SURF, "a\x00b"))
        self.assertFalse(s.paste(OTHER, "hello"))
        self.assertEqual(self.world().get("calls", []), [])

    def test_no_resuming_or_queueing_cmux_verb_and_no_shell(self):
        src = Path(ct.__file__).read_text()
        code = src.split('"""', 2)[2]                          # the module body, without its contract docstring
        for bad in ("surface.send_text", "surface.send_key", "terminal.paste", "terminal.input", "agent.message",
                    "shell=True", "osascript", "ssh", "focus", "new_surface", "resumed\": True", "\"resume\""):
            self.assertFalse(bad in code, bad)
        self.assertEqual(code.count("resume"), code.count("no_resume") + code.count("resumed"))   # only the guard
        self.assertEqual(ct.METHOD, "surface.exact_input")

    def test_error_lines_map_to_typed_reasons(self):
        mk = lambda err: subprocess.CompletedProcess([], 1, "", err)
        self.assertEqual(ct._reason(mk("Error: method_not_found: Unknown method")), "cmux_exact_input_unsupported")
        self.assertEqual(ct._reason(mk("Error: exact_target_hibernated: refused")), "target_hibernated")
        self.assertEqual(ct._reason(mk("Error: exact_queued_not_received: maybe pending")), "queued_not_received")
        self.assertEqual(ct._reason(mk("Error: Socket connection failed")), "cmux_unavailable")
        self.assertIsNone(ct.start_epoch("not a date"))
        self.assertEqual(ct.valid_pin(ct.CmuxPin(WS, SURF, TTY, 0, STARTED)), "cmux_producer_pid_invalid")


for _name in [n for n in dir(base.SessionReceiverTest) if n.startswith("test_")]:
    if _name not in CmuxReceiverTest.__dict__:
        setattr(CmuxReceiverTest, _name, None)        # the tmux tests stay in test_session_receiver.py

if __name__ == "__main__":
    unittest.main()


def code_lines_of(added: str) -> str:
    return "\n".join(l for l in added.splitlines() if not l[1:].lstrip().startswith("//"))


class CmuxPatchContractTest(unittest.TestCase):
    """The shipped cmux patch and this adapter speak the same contract."""

    PATCH = Path(__file__).resolve().parents[1] / "contrib" / "cmux" / "0001-surface-exact-input.patch"

    def test_patch_speaks_the_adapters_contract(self):
        patch = self.PATCH.read_text()
        self.assertIn('case "%s":' % ct.METHOD, patch)
        self.assertIn('"%s"' % ct.CONTRACT, patch)
        for key in ("workspace_id", "surface_id", "expected_tty", "expected_pid", "expected_pid_start", "no_resume",
                    "no_queue", "text", "submit_key", "expected_runtime_generation", "runtime_generation",
                    "resumed", "queued", "delivery", "producer_pid", "producer_start", "tty"):
            self.assertIn('"%s"' % key, patch, key)
        added = "\n".join(l for l in patch.splitlines() if l.startswith("+"))
        codes = set(re.findall(r'refused\(code: "([a-z_]+)"\)', added)) | set(re.findall(r'return "(producer_[a-z_]+)"', added))
        self.assertGreaterEqual(len(codes), 12)
        for code in codes:
            self.assertIn("exact_" + code, ct.__doc__, code)       # every refusal the patch can give is documented here
        self.assertIn("ghostty_surface_text_bracketed_only(liveSurface", added)   # the stage is framed or refused
        self.assertNotIn("writeTextData(", code_lines_of(added))
        ghostty = (self.PATCH.parent / "0002-ghostty-text-bracketed-only.patch").read_text()
        for symbol in ("export fn ghostty_surface_text_bracketed_only", "export fn ghostty_surface_bracketed_paste_active",
                       "if (!opts.bracketed) return false;", "GHOSTTY_API bool ghostty_surface_text_bracketed_only"):
            self.assertIn(symbol, ghostty, symbol)
        code_lines = "\n".join(l for l in added.splitlines() if not l[1:].lstrip().startswith("//"))
        for verb in ("resumeForExplicitInputIfNeeded", "sendTextResult", "sendInputResult", "sendNamedKeyResult",
                     "requestInputDemandSurfaceStart", "enqueue"):
            self.assertNotIn(verb, code_lines, verb)                 # never the resuming/queueing paths
