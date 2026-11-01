"""Editor jobs inside the common host cap, and the supported stop/drain of the editor bridge.

Real boundaries only: the real lock and lease files under a temp root, the real tui ``Driver.make_room`` for card windows,
real harmless child processes under ``ProcessExecutor``. The only fake is the tmux server (an external program): a list of
windows. The cap accounting itself is never replaced.
"""
import json
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path

from mesh_runner import editor_job as ej
from mesh_runner import host_admission as ha
from mesh_runner.editor_job import EditorBridge, ProcessExecutor
from mesh_runner.tui_driver import Driver

from test_editor_job import GOOD_RESULT, NOW_MS, Base, HmacSigner, JobAuthenticator, good_observation

SRC = str(Path(__file__).resolve().parent.parent / "src")


class FakeTmux:
    """The tmux server's window list; the only thing the cap reads from it."""

    def __init__(self, windows=()):
        self.mu = threading.Lock()
        self.windows = list(windows)

    def all_windows(self):
        with self.mu:
            return list(self.windows)

    def kill_window(self, session, window):
        with self.mu:
            self.windows.remove((session, window))

    def add(self, session, window):
        with self.mu:
            self.windows.append((session, window))

    def drop(self, session, window):
        with self.mu:
            if (session, window) in self.windows:
                self.windows.remove((session, window))


class Host(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "interactive"
        self.tmux = FakeTmux()

    def admission(self, cap):
        return ha.HostAdmission(self.root, cap, self.tmux)

    def driver(self, cap, key="card-1"):
        return Driver(harness="claude", agent="builder", state=self.root / "builder", tmux=self.tmux, max_sessions=cap,
                      cwd=Path(self.tmp.name), session_id=key, mode="start", key=key, argv=["true"])

    def leases(self):
        return sorted((self.root / ha.LEASE_DIR).glob("*.lease")) if (self.root / ha.LEASE_DIR).is_dir() else []

    def soon(self, seconds=0.3):
        return time.monotonic() + seconds


class Admission(Host):
    def test_editor_on_the_last_slot_refuses_a_card_start(self):
        # reciprocal 1: the editor holds the last slot, the card window start does not get it
        self.tmux.add("mr-builder", "other")
        adm = self.admission(2)
        lease = adm.acquire(self.soon(), threading.Event())
        d = self.driver(2)
        with d.host_lock():
            self.assertFalse(d.make_room())
        lease.release()
        with d.host_lock():
            self.assertTrue(d.make_room())

    def test_card_windows_on_every_slot_refuse_the_editor(self):
        # reciprocal 2: windows hold the cap, the editor waits for its deadline and takes nothing
        self.tmux.add("mr-builder", "a")
        self.tmux.add("mr-builder", "b")
        adm = self.admission(2)
        with self.assertRaises(ha.AdmissionTimeout):
            adm.acquire(self.soon(), threading.Event())
        self.assertEqual(self.leases(), [])
        self.tmux.drop("mr-builder", "b")
        adm.acquire(self.soon(), threading.Event()).release()

    def test_a_chat_window_counts_but_is_never_closed_for_the_editor(self):
        self.tmux.add("mr-builder", "chat-1")
        adm = self.admission(1)
        with self.assertRaises(ha.AdmissionTimeout):
            adm.acquire(self.soon(), threading.Event())
        self.assertEqual(self.tmux.all_windows(), [("mr-builder", "chat-1")])

    def test_an_idle_card_window_of_ours_is_closed_for_the_editor(self):
        from mesh_runner import interactive
        self.tmux.add("mr-builder", "idle-1")
        interactive.write_meta(self.root / "builder", "idle-1", {"busy_pid": 0, "last_end": 1})
        adm = self.admission(1)
        adm.acquire(self.soon(), threading.Event()).release()
        self.assertEqual(self.tmux.all_windows(), [])

    def test_release_is_exactly_once_and_removes_the_lease(self):
        adm = self.admission(1)
        lease = adm.acquire(self.soon(), threading.Event())
        self.assertEqual(len(self.leases()), 1)
        lease.release()
        lease.release()
        self.assertEqual((lease.released, self.leases()), (1, []))
        adm.acquire(self.soon(), threading.Event()).release()      # the slot is free again

    def test_cancel_deadline_and_close_end_a_waiter_without_a_lease(self):
        adm = self.admission(1)
        held = adm.acquire(self.soon(), threading.Event())
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(ha.AdmissionCancelled):
            adm.acquire(self.soon(5), cancel)
        with self.assertRaises(ha.AdmissionTimeout):
            adm.acquire(self.soon(0.2), threading.Event())
        woke = []
        t = threading.Thread(target=lambda: woke.append(self._catch(adm)))
        t.start()
        time.sleep(0.2)
        adm.close()
        t.join(3)
        self.assertEqual(woke, [ha.AdmissionClosed])
        self.assertEqual(len(self.leases()), 1)            # only the lease that was held before
        held.release()
        with self.assertRaises(ha.AdmissionClosed):
            adm.acquire(self.soon(), threading.Event())    # a closed admission never grants again

    @staticmethod
    def _catch(adm):
        try:
            adm.acquire(time.monotonic() + 10, threading.Event())
        except ha.AdmissionError as e:
            return type(e)

    def test_a_killed_holder_does_not_keep_its_slot(self):
        code = textwrap.dedent(f"""
            import sys, threading, time
            sys.path.insert(0, {SRC!r})
            from mesh_runner.host_admission import HostAdmission
            class T:
                def all_windows(self): return []
            lease = HostAdmission({str(self.root)!r}, 1, T()).acquire(time.monotonic() + 5, threading.Event())
            print("held", flush=True)
            time.sleep(60)
        """)
        p = subprocess.Popen([sys.executable, "-I", "-c", code], stdout=subprocess.PIPE, text=True)
        self.addCleanup(p.kill)
        self.assertEqual(p.stdout.readline().strip(), "held")
        adm = self.admission(1)
        with self.assertRaises(ha.AdmissionTimeout):       # the live holder of the only slot blocks us
            adm.acquire(self.soon(0.2), threading.Event())
        p.send_signal(signal.SIGKILL)
        p.wait(5)
        adm.acquire(self.soon(2), threading.Event()).release()   # the dead holder's lease is swept, not counted

    def test_concurrent_editors_and_card_starts_never_exceed_the_cap(self):
        cap, peak, bad = 3, [0], []
        adm = self.admission(cap)
        mu = threading.Lock()

        def observe():
            with mu:
                total = len(self.tmux.all_windows()) + len(self.leases())
                peak[0] = max(peak[0], total)
                if total > cap:
                    bad.append(total)

        def editor():
            try:
                lease = adm.acquire(time.monotonic() + 20, threading.Event())
            except ha.AdmissionError as e:
                bad.append(repr(e))
                return
            observe()
            time.sleep(0.05)
            lease.release()

        def card(i):
            d = self.driver(cap, key=f"card-{i}")
            end = time.monotonic() + 20
            while time.monotonic() < end:
                with d.host_lock():                          # what Driver.start_window does around make_room + new_window
                    if d.make_room():
                        self.tmux.add("mr-builder", f"card-{i}")
                        observe()
                        break
                time.sleep(0.01)
            time.sleep(0.05)
            self.tmux.drop("mr-builder", f"card-{i}")

        threads = [threading.Thread(target=editor) for _ in range(8)] + [threading.Thread(target=card, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(40)
        self.assertEqual(bad, [])
        self.assertEqual(peak[0], cap)                       # the cap was actually reached, so the check is not vacuous
        self.assertEqual((self.leases(), self.tmux.all_windows()), ([], []))


def write_harness(root: Path, body: str) -> tuple:
    script = root / "harness.py"
    script.write_text(textwrap.dedent(body))
    return (sys.executable, "-I", str(script))


class BridgeBase(Base):
    cap = 1

    def setUp(self):
        super().setUp()
        self.bridge.ledger.db.close()                        # Base built one without a host cap; rebuild with it
        self.tmux = FakeTmux()
        self.root = Path(self.dir.name) / "interactive"
        self.adm = ha.HostAdmission(self.root, self.cap, self.tmux)
        self.build()
        self.addCleanup(lambda: self.bridge.close(1))

    def build(self, executor=None, admission=None):
        def fetch(token):
            return (200, self.job) if token == "job-token-1" else (401, None)
        adm = admission if admission is not None else getattr(self, "adm", None)
        self.bridge = EditorBridge(self.cfg, JobAuthenticator(self.cfg, fetch, self.control), executor or self.executor, HmacSigner(),
                                   clock=lambda: self.clock[0], admission=adm)

    def leases(self):
        d = self.root / ha.LEASE_DIR
        return sorted(d.glob("*.lease")) if d.is_dir() else []

    def used(self, env):
        budget = ej.sha256(ej.canonical([env["project_id"], env["publication_id"], env["publication_version"]]))
        return self.bridge.ledger.used(budget)


class BridgeAdmission(BridgeBase):
    def test_a_full_host_expires_the_job_before_launch_and_spends_no_attempt(self):
        self.tmux.add("mr-builder", "a")
        env = self.envelope(deadline_at=NOW_MS + 1500)
        t0 = time.monotonic()
        status, out = self.post(env)
        self.assertEqual((status, out["status"], self.reason), (200, "expired", "queue_deadline"))
        self.assertLess(time.monotonic() - t0, 5)
        self.assertEqual((self.executor.calls, self.used(env), self.leases()), (0, 0, []))
        self.tmux.drop("mr-builder", "a")                    # unspent: the same key runs once there is room
        status, out = self.post(self.envelope(deadline_at=NOW_MS + 1500))
        self.assertEqual((status, out["status"]), (200, "completed"), out)
        self.assertEqual(self.leases(), [])

    def test_the_lease_is_held_while_the_job_runs_and_released_after(self):
        seen = []

        def run(job_input, deadline, cancel):
            seen.append((len(self.leases()), self.adm.held()))
            return good_observation()
        self.executor.fn = run
        self.assertEqual(self.post()[1]["status"], "completed")
        self.assertEqual((seen, self.leases()), ([(1, 1)], []))

    def test_every_error_path_releases_the_lease(self):
        def boom(job_input, deadline, cancel):
            raise OSError("exec failed")                     # a start exception of the executor
        self.executor.fn = boom
        self.assertEqual(self.post()[1]["status"], "failed")
        self.assertEqual(self.leases(), [])
        self.executor.fn = lambda i, d, c: good_observation(tools_used=["shell"])      # a rejected result
        self.assertEqual(self.post(self.envelope(publication_id="pub-2"))[1]["status"], "failed")
        self.assertEqual(self.leases(), [])

    def test_a_card_start_is_refused_while_the_editor_job_runs_and_admitted_after(self):
        d = Driver(harness="claude", agent="builder", state=self.root / "builder", tmux=self.tmux, max_sessions=self.cap,
                   cwd=Path(self.dir.name), session_id="c", mode="start", key="c", argv=["true"])
        seen = []

        def run(job_input, deadline, cancel):
            with d.host_lock():
                seen.append(d.make_room())
            return good_observation()
        self.executor.fn = run
        self.assertEqual(self.post()[1]["status"], "completed")
        with d.host_lock():
            seen.append(d.make_room())
        self.assertEqual(seen, [False, True])

    def test_concurrent_jobs_share_one_slot_and_a_cancel_wakes_the_queued_one(self):
        started, release = threading.Event(), threading.Event()

        def run(job_input, deadline, cancel):
            started.set()
            while not release.is_set() and not cancel.is_set():
                time.sleep(0.01)
            if cancel.is_set():
                raise ej.ExecutorFailed("cancelled")
            return good_observation()
        self.executor.fn = run
        env1, env2 = self.envelope(), self.envelope(publication_id="pub-2")
        out = {}
        t1 = threading.Thread(target=lambda: out.__setitem__(1, self.post(env1)))
        t1.start()
        self.assertTrue(started.wait(5))
        t2 = threading.Thread(target=lambda: out.__setitem__(2, self.post(env2)))
        t2.start()
        time.sleep(0.3)
        self.assertEqual(self.executor.calls, 1)             # the second is queued behind the first
        status, body = self.bridge.cancel({"JOB-TOKEN": "job-token-1"}, ej.job_id_of(env2))
        self.assertEqual((status, body["state"]), (200, "cancelling"))
        t2.join(5)
        self.assertEqual(out[2][1]["status"], "cancelled")
        self.assertEqual(self.executor.calls, 1)
        release.set()
        t1.join(5)
        self.assertEqual(out[1][1]["status"], "completed")
        self.assertEqual(self.leases(), [])


class ShutdownDrain(BridgeBase):
    def real_executor(self, body):
        return ProcessExecutor(write_harness(Path(self.dir.name), body), Path(self.dir.name) / "tmp")

    def test_close_kills_the_child_group_cleans_scratch_and_delivers_no_success(self):
        pidfile = Path(self.dir.name) / "child.pid"
        ex = self.real_executor(f"""
            import subprocess, sys, time
            c = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
            open({str(pidfile)!r}, "w").write(str(c.pid) + " " + str(__import__("os").getpid()))
            time.sleep(60)
        """)
        self.build(executor=ex)
        env = self.envelope(deadline_at=NOW_MS + 40_000)
        out = []
        t = threading.Thread(target=lambda: out.append(self.post(env)))
        t.start()
        end = time.monotonic() + 10
        while not pidfile.exists() and time.monotonic() < end:
            time.sleep(0.02)
        self.assertTrue(pidfile.exists())
        child, leader = (int(x) for x in pidfile.read_text().split())
        self.assertEqual(len(self.leases()), 1)
        t0 = time.monotonic()
        self.assertTrue(self.bridge.close(5))
        self.assertLess(time.monotonic() - t0, 5)
        t.join(5)
        self.assertEqual((out[0][0], out[0][1]["status"]), (200, "cancelled"))
        self.assertEqual(self.bridge.reasons[-1], "cancelled")
        for pid in (child, leader):
            self.assertDead(pid)
        self.assertEqual(list((Path(self.dir.name) / "tmp").iterdir()), [])
        self.assertEqual(self.leases(), [])
        self.assertTrue(self.bridge.close(1))                # idempotent
        self.assertEqual(self.post(self.envelope(publication_id="pub-9"))[0], 503)

    def assertDead(self, pid):
        end = time.monotonic() + 3
        while time.monotonic() < end:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.02)
        self.fail(f"pid {pid} is still alive")

    def test_close_wakes_queued_work_and_the_spent_budget_survives_a_restart(self):
        started = threading.Event()

        def run(job_input, deadline, cancel):
            started.set()
            cancel.wait(30)
            raise ej.ExecutorFailed("cancelled")
        self.executor.fn = run
        env1, env2 = self.envelope(), self.envelope(publication_id="pub-2")
        out = {}
        t1 = threading.Thread(target=lambda: out.__setitem__(1, self.post(env1)))
        t1.start()
        self.assertTrue(started.wait(5))
        t2 = threading.Thread(target=lambda: out.__setitem__(2, self.post(env2)))
        t2.start()
        time.sleep(0.3)
        self.assertTrue(self.bridge.close(5))
        t1.join(5)
        t2.join(5)
        self.assertEqual((out[1][1]["status"], out[2][1]["status"]), ("cancelled", "cancelled"))
        self.assertIn("shutting_down", self.bridge.reasons)  # the queued job never launched
        self.assertEqual((self.used(env1), self.used(env2)), (1, 0))   # launched = spent; queued = untouched
        self.bridge.ledger.db.close()
        # restart on the same state dir: the durable ledger still holds the spent attempt
        self.executor.fn = None
        self.adm = ha.HostAdmission(self.root, self.cap, self.tmux)
        self.build()
        self.assertEqual(self.used(env1), 1)
        status, body = self.post(self.envelope(attempt=2))
        self.assertEqual((status, body["status"]), (200, "completed"))
        status, body = self.post(self.envelope(publication_id="pub-2"))
        self.assertEqual((status, body["status"]), (200, "completed"))          # the unspent one runs normally

    def test_a_stuck_executor_is_bounded_by_the_close_timeout(self):
        gate = threading.Event()
        self.addCleanup(gate.set)
        self.executor.fn = lambda i, d, c: (gate.wait(30), good_observation())[1]   # ignores cancel
        out = []
        t = threading.Thread(target=lambda: out.append(self.post()))
        t.start()
        time.sleep(0.3)
        t0 = time.monotonic()
        self.assertFalse(self.bridge.close(0.5))             # reports that it did not drain
        self.assertLess(time.monotonic() - t0, 3)
        gate.set()
        t.join(5)
        self.assertEqual(out[0][1]["status"], "cancelled")   # and the late result is still never delivered as success
        self.assertEqual(self.reason, "late_result_discarded")

    def test_extension_service_shutdown_drains_the_editor_first(self):
        from mesh_runner.extension_service import ExtensionService
        svc = ExtensionService(Path(self.dir.name), threading.Event(), "ws", expected_secret="s")
        svc.editor = self.bridge
        svc.shutdown()
        svc.shutdown()                                       # idempotent
        self.assertTrue(self.bridge.closed)
        self.assertTrue(self.adm.closed)


if __name__ == "__main__":
    unittest.main()
