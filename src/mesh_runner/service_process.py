"""Side services as processes of their own: ``mesh-runner service <chat|editor|gmail> --config runner.toml``.

Chat answers people and the editor answers CI jobs on a deadline; the Gmail observer may wait a minute on its
reader. None of them is a step in the life of a task, and none may stop working because the task loop is busy,
restarting or crashed, nor slow a lane's tick down. Listed in the top-level ``service_processes``, a service
leaves the runner process: the runner starts nothing of it in-process and its ``Supervisor`` runs
``mesh-runner service <name>`` instead. Unlisted (the default), everything stays as it was.

A service process works on the runner's state directory as the runner does:

- chat: the claim loops and turn workers of ``services.serve_chat`` over lanes built for the chat agents only
  (``chat_lane.chat_lanes``); tmux windows, the host cap and the chat files are shared through the filesystem;
- editor: the editor-job routes and their operator control (``services.attach_editor``) on a listener of its
  own, ``[extension] port + 1`` (``config.editor_port``); its ledger (``editor-jobs.sqlite``) is its own;
- gmail: ``gmail_watch.scan`` for every lane on its own schedule, writing the ``gmail_*`` rows and the outbox of
  the shared store; the lanes only read the wake rows.

The entry point needs no supervising parent: on a dedicated host it runs under its own launchd job. One
instance per service and state directory: ``<state_dir>/services/<name>.lock`` is held for the life of the
process (exit 3 when another holds it) and ``<name>.pid`` names the holder, so a runner that starts while a
service process is alive (its previous run crashed, or the operator's own job runs it) watches that process
instead of starting a second one.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import config as config_mod

log = logging.getLogger("mesh_runner")

NAMES = config_mod.SERVICE_NAMES
EXIT_OK, EXIT_CONFIG, EXIT_RUNNING = 0, 2, 3


def service_dir(state_dir: Path) -> Path:
    path = Path(state_dir) / "services"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


class InstanceLock:
    """The one-instance lock of a service: an flock the kernel drops when the holder dies, plus a pid file
    for whoever wants to watch the holder. The lock, not the pid, says whether the service runs: a pid can be
    reused, a dead process holds no flock."""

    def __init__(self, state_dir: Path, name: str):
        self.dir, self.name = service_dir(state_dir), name
        self.fd: int | None = None

    @property
    def pid_file(self) -> Path:
        return self.dir / f"{self.name}.pid"

    def acquire(self) -> bool:
        fd = os.open(self.dir / f"{self.name}.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False
        self.fd = fd
        return True

    def held_elsewhere(self) -> bool:
        """True while another process holds the lock (a probe: taken and given back at once when free)."""
        if self.fd is not None:
            return False
        if not self.acquire():
            return True
        self.release()
        return False

    def write_pid(self) -> None:
        tmp = self.pid_file.with_name(f"{self.name}.pid.{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"pid": os.getpid(), "started_at": time.time()}))
        os.replace(tmp, self.pid_file)

    def read_pid(self) -> dict:
        try:
            got = json.loads(self.pid_file.read_text())
            return got if isinstance(got, dict) else {}
        except (OSError, ValueError):
            return {}

    def release(self) -> None:
        if self.fd is None:
            return
        try:
            if self.read_pid().get("pid") == os.getpid():
                self.pid_file.unlink(missing_ok=True)
        finally:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None


# ---------------------------------------------------------------------- the services
def gmail_clients(cfg) -> list:
    """One Mesh client per lane for the Gmail service; ``[runner] idempotency_keys`` applies to them as
    to the lanes' own clients, so the rollback also covers the Gmail outbox comments."""
    from .mesh import MeshClient
    lanes = []
    for a in cfg.agents:
        mesh = MeshClient(cfg.mesh_url, a.key, basic_auth=cfg.mesh_basic_auth, bearer=cfg.extension_token)
        mesh.idempotency_keys = cfg.idempotency_keys
        lanes.append((a.name, mesh))
    return lanes


def run_gmail(cfg, stop: threading.Event) -> int:
    """The Gmail observer of every lane, every ``poll_seconds``: each due registration is read (one reader
    call at a time, each bounded by ``gmail_watch_timeout_seconds``) and committed, then pending comments are
    posted with the lane's key. A lane whose Mesh identity is unknown yet is retried on the next pass."""
    from . import gmail_watch
    from .mesh import MeshError
    from .store import Store
    store = Store(cfg.state_dir / "runner.sqlite")
    gmail_watch.ensure_schema(store.db)
    reader = gmail_watch.SubprocessReader(list(cfg.gmail_watch_reader), cfg.gmail_watch_timeout_seconds)
    lanes = gmail_clients(cfg)
    ids: dict[str, str] = {}
    busy: set[str] = set()
    log.info("gmail service: %d lane(s)", len(lanes))
    while not stop.is_set():
        for name, mesh in lanes:
            if stop.is_set():
                break
            if name not in ids:
                try:
                    ids[name] = str(mesh.me()["id"])
                except (MeshError, KeyError, TypeError) as e:
                    log.warning("gmail: %s has no Mesh identity yet: %s", name, type(e).__name__)
                    continue
            gmail_watch.scan(store, reader, mesh, ids[name], cfg.gmail_watch_interval_seconds, busy,
                             lambda reg, cls, name=name: _event(cfg, name, "gmail_alert", registration=reg,
                                                                error_class=cls), name)
        stop.wait(cfg.poll_seconds)
    return EXIT_OK


def _event(cfg, agent: str, kind: str, **detail) -> None:
    """One line of ``events.jsonl``, the file the lanes write their lifecycle events to (one short append)."""
    from .store import iso
    try:
        with open(cfg.state_dir / "events.jsonl", "a") as fh:
            fh.write(json.dumps({"at": iso(), "agent": agent, "kind": kind, **detail}, ensure_ascii=False) + "\n")
    except OSError as e:
        log.warning("event log: %s", e)


def run_editor(cfg, stop: threading.Event) -> int:
    """The editor-job routes and the operator editor control on ``[extension] host`` : ``editor_port``. The
    Mesh routes are off on this listener; on stop the editor drains first, then the listener closes."""
    from . import services
    from .extension_service import ExtensionService
    service = ExtensionService(cfg.state_dir, lambda kind, payload: [], cfg.extension_workspace_id,
                               tolerance=cfg.extension_tolerance_seconds, mesh_routes=False)
    services.attach_editor(service, cfg)
    port = config_mod.editor_port(cfg)
    service.serve(cfg.extension_host, port)
    log.info("editor service on %s:%s", cfg.extension_host, port)
    try:
        stop.wait()
    finally:
        service.shutdown()
    return EXIT_OK


def run_chat(cfg, stop: threading.Event) -> int:
    """Agent Chat over lanes built for the chat agents. A chat window left by a previous chat process is
    stopped on start (its session resumes on the next message), as a runner restart does today."""
    import types
    from . import services
    from .chat_lane import chat_lanes
    host = types.SimpleNamespace(cfg=cfg, agents=chat_lanes(cfg))
    while not services.serve_chat(host, stop):           # Mesh unreachable at start: try again, never give up
        if stop.wait(30):
            return EXIT_OK
    stop.wait()
    return EXIT_OK


RUN = {"chat": run_chat, "editor": run_editor, "gmail": run_gmail}


def command(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="mesh-runner service")
    p.add_argument("name", choices=NAMES)
    p.add_argument("--config", required=True)
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format=f"%(asctime)s %(levelname)s [{args.name}] %(message)s")
    try:
        cfg = config_mod.load(args.config)
    except (config_mod.ConfigError, OSError, ValueError) as e:
        print(f"service {args.name}: {e}", file=sys.stderr)
        return EXIT_CONFIG
    if args.name not in cfg.service_processes:
        # unlisted, the runner serves it in-process: a second copy would claim the same turns or jobs
        print(f"service {args.name}: not listed in service_processes of {args.config}", file=sys.stderr)
        return EXIT_CONFIG
    os.umask(0o077)                          # chat files, logs and tokens: the runner's own mode
    cfg.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = InstanceLock(cfg.state_dir, args.name)
    if not lock.acquire():
        print(f"service {args.name}: already running (pid {lock.read_pid().get('pid', '?')})", file=sys.stderr)
        return EXIT_RUNNING
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        # set from a helper thread: Event.set() inside a handler can deadlock on the Event's own lock
        signal.signal(sig, lambda *_: threading.Thread(target=stop.set, daemon=True).start())
    try:
        lock.write_pid()
        log.info("service %s started (pid %d)", args.name, os.getpid())
        return RUN[args.name](cfg, stop)
    finally:
        lock.release()
        log.info("service %s stopped", args.name)


# ---------------------------------------------------------------------- the supervisor
BACKOFF_FIRST, BACKOFF_CAP = 1.0, 300.0
HEALTHY_AFTER = 60.0           # a process that ran this long restarts at once, not after the backoff it had
STOP_GRACE = 15.0              # SIGTERM to SIGKILL; the editor drains within 10 s
LOG_ROTATE_BYTES = 10 * 1024 * 1024


def _iso(unix: float | None) -> str | None:
    if unix is None:
        return None
    import datetime as dt
    return dt.datetime.fromtimestamp(unix, dt.timezone.utc).isoformat(timespec="seconds")


class _Service:
    def __init__(self, name: str):
        self.name = name
        self.proc: subprocess.Popen | None = None      # ours
        self.adopted_pid: int | None = None            # alive before us: watched, never stopped
        self.started_at: float | None = None
        self.restarts = 0
        self.last_exit: dict | None = None
        self.next_start = 0.0                           # monotonic
        self.backoff = BACKOFF_FIRST


class Supervisor:
    """Runs every service of ``cfg.service_processes`` as ``mesh-runner service <name>`` in a process session
    of its own, from one thread: a dead one is started again after a backoff (1 s doubling to 300 s, back to
    1 s once a run lasted 60 s), ``stop()`` ends the ones it started (SIGTERM to the session, SIGKILL after
    15 s). A service already running when the supervisor starts (a previous runner crashed and left it, or the
    operator runs it under its own job) is watched, not started again and not stopped: it outlives runner
    restarts by design. ``snapshot()`` is what ``status.json`` shows; nothing here is ever waited on by a tick."""

    def __init__(self, cfg, *, argv=None, poll: float = 1.0, clock=time.monotonic):
        self.cfg, self.poll, self.clock = cfg, poll, clock
        self.argv = argv or (lambda name: [sys.executable, "-m", "mesh_runner.cli", "service", name,
                                           "--config", str(cfg.config_path)])
        self.services = {name: _Service(name) for name in getattr(cfg, "service_processes", ())}
        self.lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- lifecycle
    def start(self) -> None:
        if not self.services:
            return
        self._thread = threading.Thread(target=self._loop, name="service-supervisor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.terminate()
        self.wait()

    def terminate(self) -> None:
        """Stop supervising and ask every process this supervisor started to stop (SIGTERM, no wait): the runner
        does this first in its shutdown, so the services drain while its lanes finish their ticks."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        with self.lock:
            procs = [s.proc for s in self.services.values() if s.proc is not None and s.proc.poll() is None]
        for proc in procs:
            _signal_group(proc.pid, signal.SIGTERM)

    def wait(self) -> None:
        """Wait for the terminated processes, SIGKILL whatever is left after STOP_GRACE, record their exits."""
        with self.lock:
            procs = [(s, s.proc) for s in self.services.values() if s.proc is not None]
        deadline = time.monotonic() + STOP_GRACE
        for svc, proc in procs:
            try:
                proc.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                log.warning("service %s did not stop in %.0fs; killed", svc.name, STOP_GRACE)
                _signal_group(proc.pid, signal.SIGKILL)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
            with self.lock:
                self._exited(svc, proc.returncode, restart=False)

    # -- the loop
    def _loop(self) -> None:
        while not self._stop.is_set():
            for svc in list(self.services.values()):
                try:
                    self._check(svc)
                except Exception as e:                 # one service's trouble must not end the supervision
                    log.warning("service %s: supervisor: %s", svc.name, type(e).__name__)
            self._stop.wait(self.poll)

    def _check(self, svc: _Service) -> None:
        with self.lock:
            if svc.proc is not None:
                rc = svc.proc.poll()
                if rc is None:
                    return
                if rc == EXIT_RUNNING:                 # another instance won the lock: watch that one
                    svc.proc = None
                    self._adopt(svc)
                    return
                self._exited(svc, rc)
                return
            if svc.adopted_pid is not None:
                if InstanceLock(self.cfg.state_dir, svc.name).held_elsewhere():
                    return
                log.warning("service %s: watched process %d is gone", svc.name, svc.adopted_pid)
                svc.adopted_pid = None
                self._exited(svc, None)
                return
            if self.clock() < svc.next_start:
                return
            if InstanceLock(self.cfg.state_dir, svc.name).held_elsewhere():
                self._adopt(svc)
                return
            self._spawn(svc)

    def _adopt(self, svc: _Service) -> None:
        got = InstanceLock(self.cfg.state_dir, svc.name).read_pid()
        svc.adopted_pid = int(got.get("pid") or 0) or -1
        svc.started_at = float(got.get("started_at") or 0) or None
        log.info("service %s already runs (pid %s): watched, not started", svc.name, got.get("pid", "?"))

    def _spawn(self, svc: _Service) -> None:
        path = service_dir(self.cfg.state_dir) / f"{svc.name}.log"
        try:
            if path.exists() and path.stat().st_size > LOG_ROTATE_BYTES:
                path.replace(path.with_suffix(".log.1"))
        except OSError:
            pass
        env = dict(os.environ)
        # the child imports this very release, whatever the runner was started with
        src = str(Path(__file__).resolve().parents[1])
        env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        with open(path, "ab") as out:
            svc.proc = subprocess.Popen(self.argv(svc.name), stdin=subprocess.DEVNULL, stdout=out,
                                        stderr=subprocess.STDOUT, env=env, start_new_session=True, close_fds=True)
        svc.started_at = time.time()
        svc.spawned_mono = self.clock()
        log.info("service %s started (pid %d)", svc.name, svc.proc.pid)

    def _exited(self, svc: _Service, rc: int | None, restart: bool = True) -> None:
        """Record an exit (rc None: a watched process, code unknown) and, while supervising, schedule the next
        start. Called with the lock held."""
        ran = self.clock() - getattr(svc, "spawned_mono", self.clock()) if svc.proc is not None else 0.0
        svc.last_exit = {"code": rc if rc is None or rc >= 0 else None,
                         "signal": -rc if rc is not None and rc < 0 else None, "at": _iso(time.time())}
        svc.proc = None
        if not restart:
            return
        if ran >= HEALTHY_AFTER:
            svc.backoff = BACKOFF_FIRST
        svc.next_start = self.clock() + svc.backoff
        svc.restarts += 1
        log.warning("service %s exited (%s); next start in %.0fs", svc.name,
                    f"signal {-rc}" if rc is not None and rc < 0 else f"code {rc}", svc.backoff)
        svc.backoff = min(BACKOFF_CAP, svc.backoff * 2)

    # -- status
    def snapshot(self) -> dict:
        """``{name: {mode, state, pid, started_at, restarts, last_exit, next_start_in}}`` for status.json."""
        out = {}
        with self.lock:
            for name, svc in self.services.items():
                if svc.proc is not None:
                    state, pid = "running", svc.proc.pid
                elif svc.adopted_pid is not None:
                    state, pid = "watched", svc.adopted_pid
                else:
                    state, pid = ("stopped" if self._stop.is_set() else "waiting"), None
                out[name] = {"mode": "process", "state": state, "pid": pid,
                             "started_at": _iso(svc.started_at) if pid else None, "restarts": svc.restarts,
                             "last_exit": svc.last_exit,
                             "next_start_in": (round(max(0.0, svc.next_start - self.clock()), 1)
                                               if state == "waiting" else None)}
        return out


def _signal_group(pid: int, sig: int) -> None:
    try:
        os.killpg(pid, sig)                    # the service's own session: its reader or harness children too
    except (ProcessLookupError, PermissionError):
        pass
