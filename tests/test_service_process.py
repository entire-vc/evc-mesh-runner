"""Side services as their own processes: the config key, the entry point, the supervisor and each service's split."""
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
from types import SimpleNamespace as NS
from unittest import mock

from mesh_runner import config as config_mod
from mesh_runner import service_process as sp

ROOT = Path(__file__).resolve().parent.parent


def write_config(tmp: Path, top: str = "", runner_extra: str = "", sections: str = "") -> Path:
    (tmp / "k").write_text("key")
    os.chmod(tmp / "k", 0o600)
    path = tmp / "runner.toml"
    path.write_text(textwrap.dedent(f"""\
        {top}
        [runner]
        mesh_url = "https://mesh.test"
        state_dir = "{tmp / 'state'}"
        {runner_extra}

        [[agents]]
        name = "a"
        key_file = "{tmp / 'k'}"
        profile_dir = "{tmp}"
        """) + textwrap.dedent(sections))
    return path


CHAT = """
[chat]
enabled = true
agents = ["a"]
credential_file = "{k}"
"""


class ConfigKey(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def load(self, **kw):
        return config_mod.load(write_config(self.tmp, **kw))

    def test_default_is_empty_and_inert(self):
        cfg = self.load()
        self.assertEqual(cfg.service_processes, ())
        self.assertEqual(config_mod.editor_port(cfg), cfg.extension_port)

    def test_a_listed_service_must_be_known_unique_and_top_level(self):
        for top in ('service_processes = ["chat", "chat"]', 'service_processes = ["announce"]',
                    'service_processes = "gmail"'):
            with self.assertRaisesRegex(config_mod.ConfigError, "distinct names"):
                self.load(top=top)
        with self.assertRaisesRegex(config_mod.ConfigError, "top-level key"):
            self.load(runner_extra='service_processes = ["gmail"]')

    def test_a_listed_service_must_be_on(self):
        with self.assertRaisesRegex(config_mod.ConfigError, r"\[chat\] enabled"):
            self.load(top='service_processes = ["chat"]')
        with self.assertRaisesRegex(config_mod.ConfigError, "gmail_watch_v1"):
            self.load(top='service_processes = ["gmail"]')
        with self.assertRaisesRegex(config_mod.ConfigError, r"\[extension\] enabled"):
            self.load(top='service_processes = ["editor"]')

    def test_a_write_chat_is_refused_as_a_process(self):
        sections = CHAT.format(k=self.tmp / "k") + 'chat_workspace = "write"\n'
        with self.assertRaisesRegex(config_mod.ConfigError, "not supported"):
            self.load(top='service_processes = ["chat"]', sections=sections)
        cfg = self.load(sections=sections)                      # in-process a write chat stays as it was
        self.assertEqual(cfg.chat_workspace, "write")

    def test_listed_services_load(self):
        cfg = self.load(top='service_processes = ["chat", "gmail"]', sections=CHAT.format(k=self.tmp / "k"),
                        runner_extra='gmail_watch_v1 = true\ngmail_watch_reader = ["/bin/true"]')
        self.assertEqual(cfg.service_processes, ("chat", "gmail"))

    def test_the_editor_port_is_the_next_one_only_as_a_process(self):
        cfg = NS(extension_port=8787, service_processes=("editor",))
        self.assertEqual(config_mod.editor_port(cfg), 8788)
        self.assertEqual(config_mod.editor_port(NS(extension_port=8787, service_processes=())), 8787)


class EntryPoint(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def run_cli(self, *args, timeout=30):
        env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
        return subprocess.run([sys.executable, "-m", "mesh_runner.cli", "service", *args], capture_output=True,
                              text=True, env=env, timeout=timeout)

    def test_an_unlisted_service_refuses_to_run(self):
        res = self.run_cli("gmail", "--config", str(write_config(self.tmp)))
        self.assertEqual(res.returncode, sp.EXIT_CONFIG)
        self.assertIn("not listed in service_processes", res.stderr)

    def gmail_config(self):
        return write_config(self.tmp, top='service_processes = ["gmail"]',
                            runner_extra='gmail_watch_v1 = true\ngmail_watch_reader = ["/bin/true"]\npoll_seconds = 1')

    def test_a_second_instance_exits_already_running_and_sigterm_stops_cleanly(self):
        path = self.gmail_config()
        env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
        # no parent supervises it: started as a launchd job would start it
        first = subprocess.Popen([sys.executable, "-m", "mesh_runner.cli", "service", "gmail", "--config", str(path)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, start_new_session=True)
        try:
            pid_file = self.tmp / "state" / "services" / "gmail.pid"
            for _ in range(200):
                if pid_file.exists():
                    break
                time.sleep(0.05)
            self.assertEqual(json.loads(pid_file.read_text())["pid"], first.pid)
            second = self.run_cli("gmail", "--config", str(path))
            self.assertEqual(second.returncode, sp.EXIT_RUNNING)
            self.assertIn(f"already running (pid {first.pid})", second.stderr)
        finally:
            first.send_signal(signal.SIGTERM)
            out, err = first.communicate(timeout=30)
        self.assertEqual(first.returncode, 0, err.decode()[-500:])
        self.assertIn(b"service gmail stopped", err)
        self.assertFalse(pid_file.exists())
        self.assertFalse(sp.InstanceLock(self.tmp / "state", "gmail").held_elsewhere())


class Lock(unittest.TestCase):
    def test_a_lock_is_free_again_once_the_holder_is_gone(self):
        state = Path(tempfile.mkdtemp())
        holder = sp.InstanceLock(state, "chat")
        self.assertTrue(holder.acquire())
        self.assertTrue(sp.InstanceLock(state, "chat").held_elsewhere())
        self.assertFalse(sp.InstanceLock(state, "chat").acquire())
        holder.release()
        self.assertFalse(sp.InstanceLock(state, "chat").held_elsewhere())


SLEEPER = textwrap.dedent("""\
    import fcntl, json, os, signal, sys, time
    state, name, code = sys.argv[1], sys.argv[2], int(sys.argv[3])
    d = os.path.join(state, "services")
    os.makedirs(d, exist_ok=True)
    fd = os.open(os.path.join(d, name + ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(3)
    open(os.path.join(d, name + ".pid"), "w").write(json.dumps({"pid": os.getpid(), "started_at": time.time()}))
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
    if code >= 0:
        sys.exit(code)
    while True:
        time.sleep(0.05)
""")


class SupervisorTest(unittest.TestCase):
    def setUp(self):
        self.state = Path(tempfile.mkdtemp())
        self.script = self.state / "svc.py"
        self.script.write_text(SLEEPER)

    def supervisor(self, names=("chat",), code=-1, clock=time.monotonic):
        cfg = NS(state_dir=self.state, service_processes=tuple(names), config_path="unused")
        return sp.Supervisor(cfg, poll=0.05, clock=clock,
                             argv=lambda name: [sys.executable, str(self.script), str(self.state), name, str(code)])

    def wait_for(self, cond, timeout=10):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if cond():
                return True
            time.sleep(0.02)
        return False

    def test_nothing_listed_starts_nothing(self):
        sup = self.supervisor(names=())
        sup.start()
        self.assertIsNone(sup._thread)
        self.assertEqual(sup.snapshot(), {})
        sup.stop()

    def test_starts_in_its_own_session_reports_and_stops_what_it_started(self):
        sup = self.supervisor()
        sup.start()
        try:
            self.assertTrue(self.wait_for(lambda: sup.snapshot()["chat"]["state"] == "running"))
            self.assertTrue(self.wait_for(lambda: (self.state / "services" / "chat.pid").exists()))   # its handler is set
            snap = sup.snapshot()["chat"]
            self.assertEqual((snap["mode"], snap["restarts"], snap["last_exit"]), ("process", 0, None))
            self.assertEqual(os.getpgid(snap["pid"]), snap["pid"])          # its own process session
            self.assertNotEqual(os.getsid(snap["pid"]), os.getsid(0))
            self.assertIsNotNone(snap["started_at"])
            json.dumps(sup.snapshot())                                      # status.json can carry it as it is
        finally:
            sup.stop()
        snap = sup.snapshot()["chat"]
        self.assertEqual((snap["state"], snap["pid"], snap["last_exit"]["code"]), ("stopped", None, 0))

    def test_a_dead_service_is_restarted_with_a_growing_backoff(self):
        sup = self.supervisor(code=7)
        sup.start()
        try:
            self.assertTrue(self.wait_for(lambda: sup.snapshot()["chat"]["restarts"] >= 2, timeout=15))
            snap = sup.snapshot()["chat"]
            self.assertEqual(snap["last_exit"]["code"], 7)
            self.assertGreaterEqual(sup.services["chat"].backoff, 4.0)      # 1 s, then 2 s, doubling
        finally:
            sup.stop()

    def test_a_killed_service_reports_its_signal_and_comes_back(self):
        sup = self.supervisor()
        sup.start()
        try:
            self.assertTrue(self.wait_for(lambda: sup.snapshot()["chat"]["state"] == "running"))
            first = sup.snapshot()["chat"]["pid"]
            os.kill(first, signal.SIGKILL)
            self.assertTrue(self.wait_for(lambda: sup.snapshot()["chat"]["restarts"] == 1))
            self.assertEqual(sup.snapshot()["chat"]["last_exit"]["signal"], signal.SIGKILL)
            self.assertTrue(self.wait_for(lambda: sup.snapshot()["chat"]["state"] == "running"
                                          and sup.snapshot()["chat"]["pid"] != first, timeout=10))
        finally:
            sup.stop()

    def test_a_service_already_running_is_watched_not_doubled_and_not_stopped(self):
        # a previous runner crashed (or the operator's own job runs it): the instance lock is held
        other = subprocess.Popen([sys.executable, str(self.script), str(self.state), "chat", "-1"],
                                 start_new_session=True)
        try:
            self.assertTrue(self.wait_for(lambda: sp.InstanceLock(self.state, "chat").held_elsewhere()))
            sup = self.supervisor()
            sup.start()
            self.assertTrue(self.wait_for(lambda: sup.snapshot()["chat"]["state"] == "watched"))
            self.assertEqual(sup.snapshot()["chat"]["pid"], other.pid)
            sup.stop()
            self.assertIsNone(other.poll())                                # outlives the runner's stop
            # once it is gone a new supervisor starts its own
            other.terminate()
            other.wait(timeout=10)
            sup = self.supervisor()
            sup.start()
            try:
                self.assertTrue(self.wait_for(lambda: sup.snapshot()["chat"]["state"] == "running"))
            finally:
                sup.stop()
        finally:
            if other.poll() is None:
                other.kill()

    def test_a_watched_service_that_dies_is_replaced(self):
        other = subprocess.Popen([sys.executable, str(self.script), str(self.state), "chat", "-1"],
                                 start_new_session=True)
        sup = self.supervisor()
        try:
            self.assertTrue(self.wait_for(lambda: sp.InstanceLock(self.state, "chat").held_elsewhere()))
            sup.start()
            self.assertTrue(self.wait_for(lambda: sup.snapshot()["chat"]["state"] == "watched"))
            other.kill()
            other.wait(timeout=10)
            self.assertTrue(self.wait_for(lambda: sup.snapshot()["chat"]["state"] == "running"
                                          and sup.snapshot()["chat"]["pid"] != other.pid, timeout=10))
            self.assertEqual(sup.snapshot()["chat"]["last_exit"]["code"], None)   # not ours: code unknown
        finally:
            sup.stop()

    def test_snapshot_never_waits_on_a_service(self):
        sup = self.supervisor()
        with sup.lock:                                                        # held briefly by the loop only
            pass
        t0 = time.monotonic()
        sup.snapshot()
        self.assertLess(time.monotonic() - t0, 0.5)


class GmailSplit(unittest.TestCase):
    """With gmail as a process the lane spawns no reader; the gmail process's pass is the lane's own pass."""

    def test_the_lane_does_not_read_mail_when_gmail_is_a_process(self):
        from mesh_runner.runner import AgentRunner
        ar = AgentRunner.__new__(AgentRunner)
        ar.cfg = NS(gmail_watch_v1=True, service_processes=("gmail",), gmail_watch_reader=["/bin/false"])
        with mock.patch("mesh_runner.gmail_watch.scan") as scan, \
                mock.patch.object(AgentRunner, "_gmail_reader", side_effect=AssertionError("reader built")):
            ar._scan_gmail()
        scan.assert_not_called()

    def test_in_process_the_lane_runs_the_shared_scan(self):
        from mesh_runner.runner import AgentRunner
        ar = AgentRunner.__new__(AgentRunner)
        ar.cfg = NS(gmail_watch_v1=True, service_processes=(), gmail_watch_interval_seconds=120.0)
        ar.store, ar.mesh, ar.me, ar.agent = "STORE", "MESH", {"id": "agent-1"}, NS(name="a")
        with mock.patch("mesh_runner.gmail_watch.scan") as scan, \
                mock.patch.object(AgentRunner, "_gmail_reader", return_value="READER"):
            ar._scan_gmail()
        args = scan.call_args.args
        self.assertEqual(args[:5], ("STORE", "READER", "MESH", "agent-1", 120.0))

    def test_the_gmail_service_scans_every_lane_with_its_identity(self):
        cfg = NS(state_dir=Path(tempfile.mkdtemp()), gmail_watch_reader=["/bin/true"], gmail_watch_timeout_seconds=5,
                 gmail_watch_interval_seconds=120.0, poll_seconds=0, mesh_url="https://mesh.test", mesh_basic_auth="",
                 extension_token=None, idempotency_keys=True, agents=[NS(name="a", key="ka"), NS(name="b", key="kb")])
        stop = threading.Event()
        seen = []

        class Mesh:
            def __init__(self, url, key, **kw):
                self.key = key

            def me(self):
                if self.key == "kb" and not any(s[1] == "id-a" for s in seen[:1]):
                    raise KeyError("id")                                   # b unknown on the first pass
                return {"id": "id-" + self.key[1]}

        def scan(store, reader, mesh, agent_id, interval, busy, on_alert, label):
            seen.append((label, agent_id))
            if len(seen) >= 3:
                stop.set()
        with mock.patch("mesh_runner.mesh.MeshClient", Mesh), mock.patch("mesh_runner.gmail_watch.scan", scan):
            self.assertEqual(sp.run_gmail(cfg, stop), sp.EXIT_OK)
        self.assertEqual(seen[0], ("a", "id-a"))
        self.assertIn(("b", "id-b"), seen)


class EditorSplit(unittest.TestCase):
    def test_the_runner_listener_keeps_mesh_routes_and_drops_the_editor_as_a_process(self):
        from mesh_runner import services
        cfg = NS(extension_enabled=True, state_dir=Path(tempfile.mkdtemp()), extension_workspace_id="w",
                 extension_secret="", extension_trust_first_install=True, extension_allow_rekey=False,
                 extension_tolerance_seconds=300, native_result_adoption=None, extension_host="127.0.0.1",
                 extension_port=0, extension_token=None, extension_rotate_days=0, service_processes=("editor",))
        runner = NS(cfg=cfg, wake_event=lambda *a: [], agents=[])
        with mock.patch.object(services, "attach_editor") as attach:
            service = services.start_extension(runner, threading.Event())
        try:
            attach.assert_not_called()
            self.assertTrue(service.mesh_routes)
        finally:
            service.shutdown()
        cfg.service_processes = ()
        with mock.patch.object(services, "attach_editor") as attach:
            service = services.start_extension(runner, threading.Event())
        service.shutdown()
        attach.assert_called_once()

    def test_the_editor_listener_refuses_mesh_routes(self):
        import urllib.error
        import urllib.request
        from mesh_runner.extension_service import ExtensionService
        svc = ExtensionService(Path(tempfile.mkdtemp()), lambda *a: [], "w", trust_first_install=True,
                               mesh_routes=False)
        server = svc.serve("127.0.0.1", 0)
        port = server.server_address[1]
        try:
            for path in ("/mesh/install", "/mesh/events"):
                req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=b"{}", method="POST")
                with self.assertRaises(urllib.error.HTTPError) as cm:
                    urllib.request.urlopen(req, timeout=5)
                self.assertEqual(cm.exception.code, 404)
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5) as resp:
                self.assertEqual(resp.status, 200)
        finally:
            svc.shutdown()

    def test_run_editor_serves_on_the_editor_port_and_drains_on_stop(self):
        cfg = NS(state_dir=Path(tempfile.mkdtemp()), extension_workspace_id="w", extension_tolerance_seconds=300,
                 extension_host="127.0.0.1", extension_port=0, service_processes=("editor",))
        stop = threading.Event()
        calls = []
        real = __import__("mesh_runner.extension_service", fromlist=["ExtensionService"]).ExtensionService

        class Spy(real):
            def serve(self, host, port):
                calls.append(("serve", host, port, self.mesh_routes))
                return super().serve(host, 0)

            def shutdown(self):
                calls.append(("shutdown",))
                super().shutdown()
        with mock.patch("mesh_runner.extension_service.ExtensionService", Spy), \
                mock.patch("mesh_runner.services.attach_editor") as attach:
            t = threading.Thread(target=sp.run_editor, args=(cfg, stop))
            t.start()
            for _ in range(100):
                if calls:
                    break
                time.sleep(0.02)
            stop.set()
            t.join(timeout=10)
        self.assertEqual(calls, [("serve", "127.0.0.1", 1, False), ("shutdown",)])
        attach.assert_called_once()


class ChatSplit(unittest.TestCase):
    def test_the_runner_does_not_start_chat_when_it_is_a_process(self):
        from mesh_runner import services
        cfg = NS(chat_enabled=True, service_processes=("chat",))
        with mock.patch.object(services, "serve_chat") as serve:
            services.start_chat(NS(cfg=cfg, agents=[]), threading.Event())
        serve.assert_not_called()
        cfg.service_processes = ()
        with mock.patch.object(services, "serve_chat") as serve:
            services.start_chat(NS(cfg=cfg, agents=[]), threading.Event())
        serve.assert_called_once()

    def test_run_chat_serves_lanes_built_for_the_chat_agents_and_retries_an_unresolved_start(self):
        cfg = NS(chat_agents=("a",))
        stop = threading.Event()
        hosts = []

        def serve(host, stop_):
            hosts.append(host)
            if len(hosts) == 2:
                stop.set()
                return True
            return False
        with mock.patch("mesh_runner.chat_lane.chat_lanes", return_value=["LANE"]) as lanes, \
                mock.patch("mesh_runner.services.serve_chat", serve), \
                mock.patch.object(threading.Event, "wait", lambda self, t=None: self.is_set()):
            self.assertEqual(sp.run_chat(cfg, stop), sp.EXIT_OK)
        lanes.assert_called_once_with(cfg)
        self.assertEqual([h.agents for h in hosts], [["LANE"], ["LANE"]])
        self.assertIs(hosts[0].cfg, cfg)

    def test_serve_chat_gives_an_unticked_lane_its_identity(self):
        from mesh_runner import services
        lane = NS(agent=NS(name="a"), mesh=NS(me=lambda: {"id": "agent-1"}), me={})
        cfg = NS(chat_agents=("a",), state_dir=Path(tempfile.mkdtemp()), announce_controllers=True,
                 mesh_url="https://mesh.test", chat_credential="c", chat_max_per_agent=1, chat_host_cap=2,
                 chat_idle_minutes=10, chat_workspace="read", tmux_binary="tmux")
        with mock.patch("mesh_runner.chat_protocol.registered_client", return_value="CLIENT"), \
                mock.patch("mesh_runner.chat_host.ChatHost") as host, \
                mock.patch("mesh_runner.chat_protocol.load_runner_id", return_value="rid"), \
                mock.patch("threading.Thread"):
            self.assertTrue(services.serve_chat(NS(cfg=cfg, agents=[lane]), threading.Event()))
        self.assertEqual(lane.me, {"id": "agent-1"})
        self.assertEqual(host.call_args.args[2], {"agent-1": "a"})


class RunnerStatus(unittest.TestCase):
    def test_status_json_carries_the_services_only_when_some_run_as_processes(self):
        from mesh_runner.runner import Runner
        state = Path(tempfile.mkdtemp())
        r = Runner.__new__(Runner)
        r.cfg = NS(state_dir=state, pool_root=state / "pool", metrics_file=None, checkpoint_budget_gb=1.0)
        r.store = NS()
        with mock.patch("mesh_runner.queue_wait.publish"), mock.patch("mesh_runner.pool_ops.summary", return_value={}), \
                mock.patch("mesh_runner.config_receipt.build", return_value={}):
            r.write_status([])
            self.assertNotIn("services", json.loads((state / "status.json").read_text()))
            r.supervisor = NS(services={"chat": 1}, snapshot=lambda: {"chat": {"mode": "process", "pid": 42}})
            r.write_status([])
        self.assertEqual(json.loads((state / "status.json").read_text())["services"]["chat"]["pid"], 42)


if __name__ == "__main__":
    unittest.main()
