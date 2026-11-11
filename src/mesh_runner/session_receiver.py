"""``mesh-runner session-receiver``: the strict existing-session metadata receiver as a process of its own.

Why it exists: the receiver of ``[session_metadata]`` starts only inside ``Runner.run_forever`` (once, at startup), so an
already running controller cannot take it up without a restart. This command runs the SAME receiver (``meta_delivery``:
frozen wire and security contract, unchanged) in a separate, opt-in process that a separate launchd job supervises. It is
neither a controller nor a producer launcher.

What it never does: start, resume, adopt or restore a native session, a controller or a tmux server/window; signal or
kill anything; register a runner identity (the registered runner token is only READ); fall back to a new session. Its only
tmux commands are ``list-panes`` (read) and the paste into the pinned window (``load-buffer``/``paste-buffer``/``send-keys``).

Fail closed (zero injection, nothing registered at startup): configuration missing, not 0600 or not enabled; identity,
runner id or credential mismatch; the pinned producer (tmux server pid, window id, pane pid, process start time) not exactly
as pinned; the native transcript unknown; the ledger bound to another identity; another consumer holding the lock.

One consumer: a ``flock`` on ``<ledger_root>/consumer.lock`` is held by the controller receiver and by this process alike,
over one durable ledger (``<ledger_root>/journal``) that is pinned to the identity it was first used for. A crash releases
the lock and keeps the ledger, so a restart never repeats a delivery that may have been pasted (``injecting`` -> ``uncertain``).
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import logging
import os
import signal
import stat
import subprocess
import sys
import threading
import tomllib
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import meta_delivery as md
from . import cmux_transport as ct
from . import iterm_transport as it
from . import qa_assignment as qa
from .interactive import Tmux

log = logging.getLogger("mesh_runner.session_receiver")

LABEL = "vc.entire.mesh-session-receiver"
SECTION = "session_receiver"
KEYS = ("enabled", "mesh_url", "ledger_root", "harness", "claude_config_dir", "runner_credential_file", "tmux_binary",
        "tmux_session", "tmux_window", "tmux_window_id", "tmux_server_pid", "producer_pid", "producer_started",
        "transport", "iterm_session_id", "iterm_tty", "cmux_workspace_id", "cmux_surface_id", "cmux_tty",
        "cmux_binary", "cmux_socket")
TRANSPORTS = ("tmux", "iterm", "cmux")                   # "tmux" is the default and the only one an old config can mean
TMUX_KEYS = ("tmux_session", "tmux_window", "tmux_window_id", "tmux_server_pid")
ITERM_KEYS = ("iterm_session_id", "iterm_tty")
CMUX_KEYS = ("cmux_workspace_id", "cmux_surface_id", "cmux_tty")
CMUX_CLIENT_KEYS = ("cmux_binary", "cmux_socket")        # how to reach cmux; not part of the binding
TRANSPORT_KEYS = {"tmux": TMUX_KEYS, "iterm": ITERM_KEYS, "cmux": CMUX_KEYS}
EXIT_REFUSED, EXIT_BUSY, EXIT_REVOKE_FAILED = 3, 4, 5
PIN_FILE, LOCK_FILE = "ledger-identity.json", "consumer.lock"
ADMISSION_FILE, REVOKED_FILE = "admission.json", "admission-revoked.json"   # this receiver's own server-side admission
TMUX_READ_ONLY = ("list-panes",)                         # what the receiver asks tmux, besides the paste of md.TmuxSession


class Refusal(Exception):
    """A fail-closed condition: ``code`` is stable and safe to print (never a secret)."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(code)
        self.code, self.detail = code, detail


# ---------------------------------------------------------------------- one consumer, one ledger
class ConsumerLock:
    """Exclusive, crash-safe right to inject into the identity's ledger. Held for the life of the process."""

    def __init__(self, root: Path):
        self.path = Path(root) / LOCK_FILE
        self._fd: int | None = None

    def acquire(self, role: str) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False
        os.ftruncate(fd, 0)
        os.write(fd, json.dumps({"pid": os.getpid(), "role": role}).encode())
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is not None:
            os.close(self._fd)                                # closing drops the flock
            self._fd = None

    def held_elsewhere(self) -> bool:
        """Read-only probe (status/preflight): True when another process holds the lock."""
        if not self.path.exists():
            return False
        fd = os.open(self.path, os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        except BlockingIOError:
            return True
        finally:
            os.close(fd)


def identity_digest(identity: md.Identity) -> str:
    f = identity.fields
    return hashlib.sha256(json.dumps({k: f[k] for k in md.IDENTITY_FIELDS}, sort_keys=True).encode()).hexdigest()


def ledger_state(root: Path, identity: md.Identity, transport: str | None = None) -> str:
    """``empty`` | ``bound`` | ``unbound`` (records, no pin) | ``mismatch`` (pinned to another identity or transport).

    ``transport`` is the digest of a non-tmux transport pin (None = tmux). A pin written before transports existed has no
    ``transport_sha256`` and therefore means tmux: it matches only ``transport=None``."""
    journal, pin = Path(root) / "journal", Path(root) / PIN_FILE
    records = list(journal.glob("*.json")) if journal.is_dir() else []
    if pin.exists():
        try:
            rec = json.loads(pin.read_text())
            same = rec.get("identity_sha256") == identity_digest(identity) and rec.get("transport_sha256") == transport
            return "bound" if same else "mismatch"
        except (OSError, ValueError):
            return "mismatch"
    return "unbound" if records else "empty"


def bind_ledger(root: Path, identity: md.Identity, adopt_existing: bool = False, transport: str | None = None) -> None:
    """Pin the ledger to this identity. A ledger that already holds records but no pin is adopted only on an explicit
    operator flag (its records carry no identity, so they cannot be checked); a pinned-elsewhere ledger never is."""
    state = ledger_state(root, identity, transport)
    if state == "mismatch":
        raise Refusal("ledger_identity_mismatch")
    if state == "unbound" and not adopt_existing:
        raise Refusal("ledger_unbound")
    if state == "bound":
        return
    Path(root).mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = Path(root) / (PIN_FILE + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        pin = {"identity_sha256": identity_digest(identity), "session_id": identity.fields["session_id"]}
        if transport:
            pin["transport_sha256"] = transport                 # tmux pins stay byte-identical to the released format
        fh.write(json.dumps(pin))
        fh.flush()
        os.fsync(fh.fileno())
    tmp.replace(Path(root) / PIN_FILE)


def _write_private(path: Path, obj: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(json.dumps(obj))
        fh.flush()
        os.fsync(fh.fileno())
    tmp.replace(path)


def read_admissions(root: Path, identity: md.Identity, missing_ok: bool = False, transport: str | None = None) -> list[dict]:
    """The admissions this receiver recorded for exactly this identity and runner, oldest first. Every start registers
    again, so the record is a list: a restart must not make the previous admission unrevokable."""
    try:
        rec = json.loads((Path(root) / ADMISSION_FILE).read_text())
    except FileNotFoundError:
        if missing_ok:
            return []
        raise Refusal("admission_unknown") from None
    except (OSError, ValueError):
        raise Refusal("admission_unknown") from None
    if (not isinstance(rec, dict) or rec.get("identity_sha256") != identity_digest(identity)
            or rec.get("runner_id") != identity.fields["runner_id"] or rec.get("transport_sha256") != transport):
        raise Refusal("admission_identity_mismatch")
    items = rec.get("receivers")
    if (not isinstance(items, list) or not items
            or not all(isinstance(i, dict) and md.is_uuid(i.get("receiver_id")) for i in items)):
        raise Refusal("admission_unknown")
    return items


def write_admissions(root: Path, identity: md.Identity, items: list[dict], transport: str | None = None) -> None:
    rec = {"identity_sha256": identity_digest(identity), "runner_id": identity.fields["runner_id"], "receivers": items}
    if transport:
        rec["transport_sha256"] = transport
    _write_private(Path(root) / ADMISSION_FILE, rec)


class RecordingClient(md.MetaClient):
    """The registration this process made is written down (under the consumer lock), so ``revoke`` can later name exactly
    that admission instead of guessing: the server-assigned receiver_id plus the identity it was made for."""

    def __init__(self, *a, ledger_root: Path, identity: md.Identity, stop: threading.Event | None = None,
                 transport: str | None = None, **kw):
        super().__init__(*a, **kw)
        self._root, self._identity, self._stop, self._transport = Path(ledger_root), identity, stop, transport
        self.record_failed = False

    def register(self, identity: md.Identity) -> dict:
        got = super().register(identity)
        try:
            rid = str(got.get("receiver_id"))
            items = [i for i in read_admissions(self._root, identity, True, self._transport) if i["receiver_id"] != rid]
            write_admissions(self._root, identity, items + [{"receiver_id": rid, "admission_revision": self.admission_revision}],
                             self._transport)
        except (OSError, Refusal) as e:
            # An admission nobody can name later is one nobody can revoke: undo it now, serve nothing, and stop the loop so
            # it does not register again on the next tick (each registration would add another unrecorded admission).
            self.record_failed, self.admission_revision = True, None
            try:
                self.revoke(str(got.get("receiver_id")))
                log.error("session receiver: admission record not writable (%s); the fresh admission was revoked", type(e).__name__)
            except (md.MeshError, OSError, ValueError):
                log.error("session receiver: admission record not writable (%s) AND the fresh admission %s was NOT revoked; "
                          "revoke it by hand", type(e).__name__, str(got.get("receiver_id"))[:8])
            if self._stop is not None:
                self._stop.set()
            raise md.MeshError(0, "admission record not written") from None
        return got


# ---------------------------------------------------------------------- the pinned producer
@dataclass(frozen=True)
class ProducerPin:
    tmux_session: str
    tmux_window: str
    tmux_window_id: str          # "@12": a renamed or recreated window gets another id
    tmux_server_pid: int
    producer_pid: int            # pid of the pane process of the native session
    producer_started: str        # `ps -o lstart=` of that pid, as baselined


def process_started(pid: int) -> str:
    res = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, timeout=10,
                         stdin=subprocess.DEVNULL)
    return " ".join(res.stdout.split()) if res.returncode == 0 else ""


def check_producer(tmux: Tmux, pin: ProducerPin, started=process_started) -> str:
    """"" when the producer is exactly the pinned one, else the typed reason. Read-only."""
    res = tmux.run("list-panes", "-t", f"={pin.tmux_session}:={pin.tmux_window}", "-F",
                   "#{window_id}\t#{pane_pid}\t#{pane_dead}\t#{pid}")
    if res.returncode != 0:
        return "target_missing"
    rows = [line.split("\t") for line in res.stdout.splitlines() if line.strip()]
    if len(rows) != 1 or len(rows[0]) != 4:
        return "target_ambiguous"
    window_id, pane_pid, dead, server_pid = rows[0]
    if dead == "1":
        return "pane_dead"
    if window_id != pin.tmux_window_id or server_pid != str(pin.tmux_server_pid):
        return "tmux_identity_changed"
    now = " ".join(started(pin.producer_pid).split())
    if not pin.producer_started or not now:
        return "producer_start_unknown"                     # an unknown start time never matches, not even an empty pin
    if pane_pid != str(pin.producer_pid) or " ".join(started(pin.producer_pid).split()) != pin.producer_started:
        return "producer_changed"
    return ""


class PinnedSession(md.TmuxSession):
    """The one existing native session, found by its PIN and never created. ``target`` is None (so zero injection) unless the
    producer is exactly the pinned one at the moment of the delivery."""

    def __init__(self, tmux: Tmux, pin: ProducerPin, tmp_dir: Path, session_id: str, known, started=process_started):
        Path(tmp_dir).mkdir(parents=True, exist_ok=True, mode=0o700)
        super().__init__(tmux, pin.tmux_session, tmp_dir, session_id, known)
        self.pin, self._started = pin, started

    def target(self) -> str | None:
        reason = check_producer(self.tmux, self.pin, self._started)
        if reason:
            log.warning("session receiver: producer not as pinned (%s); no injection", reason)
            return None
        return self.pin.tmux_window_id


# ---------------------------------------------------------------------- configuration
@dataclass
class ReceiverConfig:
    settings: md.Settings
    mesh_url: str
    ledger_root: Path
    harness: str
    claude_config_dir: str
    runner_credential_file: Path
    tmux_binary: str
    pin: "ProducerPin | it.ITermPin | ct.CmuxPin"
    enabled: bool
    transport: str = "tmux"
    qa: "qa.QASettings | None" = None                      # [host_local_qa_assignment_v1]; None = OFF (default)
    cmux_binary: str = ""                                  # transport cmux: the bundled CLI ("" = ct.DEFAULT_CLI)
    cmux_socket: str = ""                                  # transport cmux: explicit socket path ("" = the CLI's default)

    @property
    def transport_sha(self) -> str | None:
        return transport_digest(self)


def transport_digest(cfg: "ReceiverConfig") -> str | None:
    """None for tmux (the released ledger format). For iTerm: the whole terminal<->producer<->host<->runner binding, so a
    different terminal session, tty, producer, host or runner can never reuse this ledger or admission record."""
    if cfg.transport == "cmux":
        f, pin = cfg.settings.identity.fields, cfg.pin
        return hashlib.sha256(json.dumps({"transport": "cmux", "workspace_id": pin.workspace_id.lower(),
                                          "surface_id": pin.surface_id.lower(), "tty": pin.tty,
                                          "producer_pid": pin.producer_pid, "producer_started": pin.producer_started,
                                          "host_id": f["host_id"], "runner_id": f["runner_id"],
                                          "session_id": f["session_id"]}, sort_keys=True).encode()).hexdigest()
    if cfg.transport != "iterm":
        return None
    f, pin = cfg.settings.identity.fields, cfg.pin
    return hashlib.sha256(json.dumps({"transport": "iterm", "iterm_session_id": pin.iterm_session_id, "tty": pin.tty,
                                      "producer_pid": pin.producer_pid, "producer_started": pin.producer_started,
                                      "host_id": f["host_id"], "runner_id": f["runner_id"], "session_id": f["session_id"]},
                                     sort_keys=True).encode()).hexdigest()


def _secret(file, env, what):
    from .config import read_secret
    return read_secret(file, env, what)


def load_config(path: str | Path, require_enabled: bool = True, allow_expired_qa: bool = False) -> ReceiverConfig:
    p = Path(path).expanduser()
    try:
        mode = p.stat().st_mode
    except OSError:
        raise Refusal("config_missing") from None
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise Refusal("config_not_private")
    try:
        raw = tomllib.loads(p.read_text())
    except (OSError, ValueError):
        raise Refusal("config_unreadable") from None
    if set(raw) - {"session_metadata", SECTION, qa.SECTION}:
        raise Refusal("config_unknown_section")
    for name in ("session_metadata", SECTION, qa.SECTION):                  # an array of tables / scalar is malformed, never a traceback
        if name in raw and not isinstance(raw[name], dict):
            raise Refusal("config_invalid_section", name)
    sec = raw.get(SECTION) or {}
    unknown = set(sec) - set(KEYS)
    if unknown:
        raise Refusal("config_unknown_keys", ", ".join(sorted(unknown)))
    enabled = sec.get("enabled", False) is True
    if require_enabled and not enabled:
        raise Refusal("not_enabled")
    meta = dict(raw.get("session_metadata") or {})
    meta["enabled"] = True                                      # the receiver's own switch is [session_receiver] enabled
    try:
        settings = md.parse_settings(meta, _secret)
        transport = sec.get("transport", "tmux")
        if transport not in TRANSPORTS:
            raise Refusal("config_bad_transport")
        mine = TRANSPORT_KEYS[transport]
        other = [k for t, keys in TRANSPORT_KEYS.items() if t != transport for k in keys]
        if transport != "cmux":
            other += list(CMUX_CLIENT_KEYS)
        if any(sec.get(k) for k in other):
            raise Refusal("config_transport_mixed", ", ".join(k for k in other if sec.get(k)))
        missing = [k for k in ("mesh_url", "ledger_root", "harness", "runner_credential_file", *mine,
                               "producer_pid", "producer_started") if not sec.get(k)]
        if missing:
            raise Refusal("config_incomplete", ", ".join(missing))
        if sec["harness"] not in ("claude", "codex"):
            raise Refusal("config_bad_harness")
        if not " ".join(str(sec["producer_started"]).split()):
            raise Refusal("config_incomplete", "producer_started")
        started = " ".join(str(sec["producer_started"]).split())
        if transport == "cmux":
            pin = ct.CmuxPin(str(sec["cmux_workspace_id"]), str(sec["cmux_surface_id"]), str(sec["cmux_tty"]),
                             int(sec["producer_pid"]), started)
            if ct.valid_pin(pin):
                raise Refusal("config_invalid", ct.valid_pin(pin))
        elif transport == "iterm":
            pin = it.ITermPin(str(sec["iterm_session_id"]), str(sec["iterm_tty"]), int(sec["producer_pid"]), started)
            if it.valid_pin(pin):
                raise Refusal("config_invalid", it.valid_pin(pin))
        else:
            pin = ProducerPin(str(sec["tmux_session"]), str(sec["tmux_window"]), str(sec["tmux_window_id"]),
                              int(sec["tmux_server_pid"]), int(sec["producer_pid"]), started)
    except (ValueError, TypeError) as e:
        raise Refusal("config_invalid", type(e).__name__) from None
    except Exception as e:
        if isinstance(e, Refusal):
            raise
        raise Refusal("config_invalid", type(e).__name__) from None
    try:
        qa_settings = qa.parse_section(raw.get(qa.SECTION), _secret, settings.identity, settings.credential, pin, transport,
                                    allow_expired_qa)
    except qa.QARefusal as e:
        raise Refusal(e.code, e.detail) from None
    return ReceiverConfig(settings, str(sec["mesh_url"]), Path(str(sec["ledger_root"])).expanduser(), sec["harness"],
                          str(sec.get("claude_config_dir") or ""), Path(str(sec["runner_credential_file"])).expanduser(),
                          str(sec.get("tmux_binary") or "tmux"), pin, enabled, transport, qa_settings,
                          str(sec.get("cmux_binary") or ""), str(sec.get("cmux_socket") or ""))


def read_runner_token(path: Path, runner_id: str) -> str:
    """The token of the ALREADY registered runner (``chat-runner-<agent>.json``): read only, never registered here."""
    try:
        if path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise Refusal("runner_credential_not_private")
        cur = json.loads(path.read_text())
        rid, token = str(uuid.UUID(str(cur["runner_id"]))), str(cur["token"])
    except Refusal:
        raise
    except (OSError, ValueError, KeyError, TypeError):
        raise Refusal("runner_credential_missing") from None
    if rid != runner_id or not token:
        raise Refusal("runner_id_mismatch")
    return token


def _known(cfg: ReceiverConfig):
    """Does the harness keep this session's transcript on this machine (read only)."""
    if cfg.harness == "claude":
        from .adapters.claude import ClaudeAdapter as Adapter
    else:
        from .adapters.codex import CodexAdapter as Adapter
    adapter = Adapter(cfg.harness, [])
    env = dict(os.environ)
    if cfg.claude_config_dir:
        env["CLAUDE_CONFIG_DIR"] = cfg.claude_config_dir
    return lambda session_id: adapter.session_known(session_id, Path.home(), env)


def build_session(cfg: ReceiverConfig, tmux: Tmux | None = None, known=None, started=process_started, info=None):
    """The transport of the config. ``tmux`` carries the ``ITermAPI``/``CmuxAPI`` for transport iterm/cmux (the one
    transport client)."""
    if cfg.transport == "cmux":
        return ct.CmuxSession(tmux or transport_client(cfg), cfg.pin, cfg.settings.identity.fields["session_id"],
                              known or _known(cfg), info or it.process_info)
    if cfg.transport == "iterm":
        return it.ITermSession(tmux or it.ITermAPI(), cfg.pin, cfg.settings.identity.fields["session_id"],
                               known or _known(cfg), info or it.process_info)
    return PinnedSession(tmux or Tmux(cfg.tmux_binary), cfg.pin, cfg.ledger_root / "tmp",
                         cfg.settings.identity.fields["session_id"], known or _known(cfg), started)


def transport_client(cfg: ReceiverConfig):
    if cfg.transport == "cmux":
        return ct.CmuxAPI(cfg.cmux_binary, cfg.cmux_socket)
    return it.ITermAPI() if cfg.transport == "iterm" else Tmux(cfg.tmux_binary)


def preflight(cfg: ReceiverConfig, tmux: Tmux | None = None, known=None, started=None, info=None,
              local_host: str | None = None) -> list[tuple[str, str]]:
    """Read-only checks, ``[(check, "ok" | reason)]``. Takes no lock, registers nothing, touches no state."""
    out = []
    started = started or process_started
    ident = cfg.settings.identity
    iterm = cfg.transport == "iterm"
    tmux = tmux or transport_client(cfg)

    def add(name, reason):
        out.append((name, reason or "ok"))
    if iterm or cfg.transport == "cmux":
        add("host_binding", "" if it.host_matches(ident.fields["hostname"], local_host) else "host_mismatch")
        check = it.check_iterm if iterm else ct.check_cmux
        add("terminal_binding", check(tmux, cfg.pin, info or it.process_info))
    else:
        add("producer_pin", check_producer(tmux, cfg.pin, started))
    try:
        add("native_transcript", "" if (known or _known(cfg))(ident.fields["session_id"]) else "session_unknown")
    except OSError:
        add("native_transcript", "session_unknown")
    try:
        read_runner_token(cfg.runner_credential_file, ident.fields["runner_id"])
        add("runner_credential", "")
    except Refusal as e:
        add("runner_credential", e.code)
    state = ledger_state(cfg.ledger_root, ident, cfg.transport_sha)
    add("ledger", {"empty": "", "bound": ""}.get(state, "ledger_" + state))
    add("consumer_lock", "other_consumer_active" if ConsumerLock(cfg.ledger_root).held_elsewhere() else "")
    add("enabled", "" if cfg.enabled else "not_enabled")
    return out


# ---------------------------------------------------------------------- run
def run(cfg: ReceiverConfig, stop: threading.Event, tmux: Tmux | None = None, known=None, started=process_started,
        opener=None, adopt_existing_ledger: bool = False, info=None, local_host: str | None = None, qa_opener=None, argv_of=None) -> int:
    """Gate, then serve. Every gate is checked BEFORE registration, so a refused start leaves no admission side effect."""
    ident = cfg.settings.identity
    tmux = tmux or transport_client(cfg)
    lock = ConsumerLock(cfg.ledger_root)
    if not lock.acquire("session-receiver"):
        log.error("session receiver: another consumer owns %s", cfg.ledger_root)
        return EXIT_BUSY
    try:
        failed = [(n, r) for n, r in preflight_gates(cfg, tmux, known, started, info, local_host) if r != "ok"]
        if failed:
            log.error("session receiver refused: %s", ", ".join(f"{n}={r}" for n, r in failed))
            return EXIT_REFUSED
        try:
            bind_ledger(cfg.ledger_root, ident, adopt_existing_ledger, cfg.transport_sha)
            token = read_runner_token(cfg.runner_credential_file, ident.fields["runner_id"])
        except Refusal as e:
            log.error("session receiver refused: %s", e.code)
            return EXIT_REFUSED
        kwargs = {"opener": opener} if opener else {}
        client = RecordingClient(cfg.mesh_url, cfg.settings.credential, token, ledger_root=cfg.ledger_root, identity=ident,
                                 stop=stop, transport=cfg.transport_sha, **kwargs)
        receiver = md.MetaReceiver(ident, build_session(cfg, tmux, known, started, info), md.Journal(cfg.ledger_root / "journal"),
                                   client, cfg.ledger_root / "acks")
        log.info("session receiver on for session %s", ident.fields["session_id"][:8])
        receivers, qa_client = [receiver], None
        if cfg.qa is not None:                                    # opt-in, separate admission; same lock, journal and injector
            try:
                qa.pinned()
                qa_client = qa.QAClient(cfg.mesh_url, cfg.qa.credential, token, cfg.qa.identity, cfg.ledger_root,
                                        opener=qa_opener, stop=stop)
                observe = qa.make_observer(cfg.qa.identity, cfg.pin, lambda: _credential_runner_id(cfg.runner_credential_file),
                                           known or _known(cfg), tmux, info, argv_of, local_host)
                receivers.append(qa.QAReceiver(cfg.qa.identity, receiver.session, receiver.journal, qa_client, cfg.ledger_root,
                                               observe=observe))
                log.info("host-local QA assignment receiver on (separate admission)")
            except (qa.QARefusal, ValueError) as e:
                log.error("host-local QA assignment off: %s", getattr(e, "code", type(e).__name__))
        md.serve_loop(receivers, stop, cfg.settings.interval if qa_client is None else min(cfg.settings.interval,
                                                                                         cfg.qa.interval))
        return EXIT_REFUSED if client.record_failed or (qa_client is not None and qa_client.record_failed) else 0
    finally:
        lock.release()


def revoke(cfg: ReceiverConfig, opener=None) -> int:
    """Disable/rollback of the server side: revoke ONLY the admission this receiver recorded for its own identity.

    Needs the consumer lock (so the receiver is stopped and cannot register meanwhile), the recorded admission to match the
    configured identity and runner, and the already registered runner credential. Registers nothing, grants nothing, never
    touches tmux, the journal, the ledger pin or the producer. Any server answer but 204 is a failure and keeps the record:
    nothing claims a revoke that did not happen."""
    ident = cfg.settings.identity
    lock = ConsumerLock(cfg.ledger_root)
    if not lock.acquire("session-receiver-revoke"):
        log.error("session receiver revoke: the receiver still runs on %s; stop it first", cfg.ledger_root)
        return EXIT_BUSY
    try:
        try:
            if ledger_state(cfg.ledger_root, ident, cfg.transport_sha) == "mismatch":
                raise Refusal("ledger_identity_mismatch")
            items = read_admissions(cfg.ledger_root, ident, transport=cfg.transport_sha)
            token = read_runner_token(cfg.runner_credential_file, ident.fields["runner_id"])
        except Refusal as e:
            print(f"refused: {e.code}", file=sys.stderr)
            return EXIT_REFUSED
        kwargs = {"opener": opener} if opener else {}
        client = md.MetaClient(cfg.mesh_url, cfg.settings.credential, token, **kwargs)
        left = list(items)
        for item in reversed(items):                                  # newest first; every one of them is this receiver's own
            try:
                client.revoke(item["receiver_id"])
            except (md.MeshError, OSError, ValueError) as e:
                print(f"revoke_failed: {type(e).__name__} {getattr(e, 'status', '')}".strip()
                      + f" for receiver {item['receiver_id'][:8]} ({len(left)} admission(s) NOT revoked)", file=sys.stderr)
                return EXIT_REVOKE_FAILED
            left.remove(item)
            if left:
                write_admissions(cfg.ledger_root, ident, left, cfg.transport_sha)   # a retry asks only for what is still active
            print(f"revoked: receiver {item['receiver_id'][:8]}")
        (cfg.ledger_root / ADMISSION_FILE).replace(cfg.ledger_root / REVOKED_FILE)
        return 0
    finally:
        lock.release()


def preflight_gates(cfg, tmux, known, started, info=None, local_host=None):
    """``preflight`` minus the checks ``run`` makes itself (lock is ours; ledger is bound right after)."""
    return [(n, r) for n, r in preflight(cfg, tmux, known, started, info, local_host)
            if n not in ("consumer_lock", "ledger")] + [("ledger", "ok" if ledger_state(cfg.ledger_root, cfg.settings.identity,
                                                                                         cfg.transport_sha)
                                                         != "mismatch" else "ledger_mismatch")]


# ---------------------------------------------------------------------- host-local QA assignment: guard and disable
def qa_observer(cfg: ReceiverConfig, known=None, api=None, info=None, argv_of=None, local_host=None, clock=None):
    """The supported read-only proof of the ACTUAL target + producer for this config (see ``qa.observe_local``)."""
    if cfg.qa is None:
        raise Refusal("qa_not_enabled")
    runner_id = cfg.settings.identity.fields["runner_id"]
    return qa.make_observer(cfg.qa.identity, cfg.pin,
                            lambda: _credential_runner_id(cfg.runner_credential_file), known or _known(cfg), api, info,
                            argv_of, local_host, clock or qa.now_epoch)


def _credential_runner_id(path: Path) -> str:
    """The runner id of the ALREADY registered runner credential (read only; the token is never returned)."""
    try:
        if path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise Refusal("runner_credential_not_private")
        return str(uuid.UUID(str(json.loads(path.read_text())["runner_id"])))
    except Refusal:
        raise
    except (OSError, ValueError, KeyError, TypeError):
        raise Refusal("runner_credential_missing") from None


def _qa_client(cfg: ReceiverConfig, opener=None, clock=None):
    if cfg.qa is None:
        raise Refusal("qa_not_enabled")
    token = read_runner_token(cfg.runner_credential_file, cfg.settings.identity.fields["runner_id"])
    return qa.QAClient(cfg.mesh_url, cfg.qa.credential, token, cfg.qa.identity, cfg.ledger_root, opener=opener,
                       clock=clock or qa.now_epoch)


def qa_command(cfg: ReceiverConfig, action: str, delivery_id: str, boundary: str, opener=None, api=None, info=None,
               local_host: str | None = None, out=None, clock=None, known=None, argv_of=None) -> int:
    """``qa-check`` is the host-local guard the operation owner calls before EACH normal mutation: exit 0 and one JSON line
    only when the live backend and the live local binding both say yes right now; every other outcome is exit 3 with a
    typed reason and the mutation must not happen. It executes nothing. ``qa-revoke`` disables the QA scope only."""
    out = out or sys.stdout
    try:
        if action == "qa-observe":
            res = qa_observer(cfg, known, api, info, argv_of, local_host, clock)()
            print(json.dumps(res, sort_keys=True), file=out)
            return 0
        client = _qa_client(cfg, opener, clock)
        if action == "qa-revoke":
            ids = qa.revoke(cfg.qa, cfg.ledger_root, client)
            for rid in ids:
                print(f"revoked: qa receiver {rid[:8]}", file=out)
            return 0
        if not delivery_id or not boundary:
            raise Refusal("qa_check_needs_delivery_and_boundary")
        res = qa.guard(cfg.qa, cfg.ledger_root, cfg.ledger_root / "journal", client, delivery_id, boundary,
                       qa_observer(cfg, known, api, info, argv_of, local_host, clock), clock or qa.now_epoch)
    except (Refusal, qa.QARefusal) as e:
        print(f"refused: {e.code}", file=sys.stderr)
        return EXIT_REFUSED
    except (KeyError, TypeError, AttributeError) as e:          # untrusted state of an unexpected shape: typed refusal, no traceback
        print(f"refused: malformed_state ({type(e).__name__})", file=sys.stderr)
        return EXIT_REVOKE_FAILED if action == "qa-revoke" else EXIT_REFUSED
    except (md.MeshError, OSError, ValueError) as e:
        print(f"refused: {'revoke_failed' if action == 'qa-revoke' else 'check_unavailable'} ({type(e).__name__})", file=sys.stderr)
        return EXIT_REVOKE_FAILED if action == "qa-revoke" else EXIT_REFUSED
    print(json.dumps(res, sort_keys=True), file=out)
    return 0


# ---------------------------------------------------------------------- launchd job (the receiver only)
def plist(config: str, python: str, pythonpath: str, log_dir: str) -> str:
    """The supervisor job: it starts and stops THIS process and nothing else. A refused start is retried at the throttle."""
    from xml.sax.saxutils import escape as x
    args = "".join(f"<string>{x(a)}</string>" for a in (python, "-s", "-m", "mesh_runner.cli", "session-receiver", "run",
                                                         "--config", config))
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>{LABEL}</string>
<key>ProgramArguments</key><array>{args}</array>
<key>EnvironmentVariables</key><dict><key>PYTHONPATH</key><string>{x(pythonpath)}</string></dict>
<key>RunAtLoad</key><true/>
<key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
<key>ThrottleInterval</key><integer>60</integer>
<key>ProcessType</key><string>Background</string>
<key>StandardOutPath</key><string>{x(log_dir)}/session-receiver.log</string>
<key>StandardErrorPath</key><string>{x(log_dir)}/session-receiver.log</string>
</dict></plist>
"""


def status(cfg: ReceiverConfig, tmux=None, known=None, started=process_started) -> dict:
    ident = cfg.settings.identity
    journal = cfg.ledger_root / "journal"
    states: dict[str, int] = {}
    for p in journal.glob("*.json") if journal.is_dir() else []:
        rec = md.Journal(journal).get(p.stem) or {}
        states[str(rec.get("state"))] = states.get(str(rec.get("state")), 0) + 1
    qa_states: dict[str, int] = {}
    for p in journal.glob("*.json") if journal.is_dir() else []:
        rec = md.Journal(journal).get(p.stem) or {}
        if rec.get("capability") == qa.CAPABILITY:
            qa_states[str(rec.get("state"))] = qa_states.get(str(rec.get("state")), 0) + 1
            states[str(rec.get("state"))] -= 1                 # legacy counts stay legacy-only
            if not states[str(rec.get("state"))]:
                del states[str(rec.get("state"))]
    return {"session_id": ident.fields["session_id"], "enabled": cfg.enabled, "transport": cfg.transport,
            "checks": dict(preflight(cfg, tmux, known, started)),
            "ledger": states, "host_local_qa_assignment_v1": {"enabled": cfg.qa is not None, "ledger": qa_states}}


# ---------------------------------------------------------------------- CLI
def command(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="mesh-runner session-receiver")
    p.add_argument("action", choices=["preflight", "run", "status", "plist", "revoke", "qa-check", "qa-revoke", "qa-observe"])
    p.add_argument("--config", required=True)
    p.add_argument("--python", default=sys.executable)
    p.add_argument("--pythonpath", default="")
    p.add_argument("--log-dir", default="")
    p.add_argument("--bind-existing-ledger", action="store_true",
                   help="run: pin an existing unpinned ledger to this identity (explicit, once)")
    p.add_argument("--delivery-id", default="", help="qa-check: the delivered assignment this mutation belongs to")
    p.add_argument("--boundary", default="", choices=["", *qa.BOUNDARIES], help="qa-check: the mutation about to be made")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(message)s")
    try:
        cfg = load_config(args.config, require_enabled=args.action == "run",
                          allow_expired_qa=args.action in ("qa-revoke", "revoke", "status"))   # disable paths never get stuck
    except Refusal as e:
        print(f"refused: {e.code}" + (f" ({e.detail})" if e.detail else ""), file=sys.stderr)
        return EXIT_REFUSED
    if args.action == "plist":
        if not args.pythonpath or not args.log_dir:
            p.error("plist needs --pythonpath and --log-dir")
        sys.stdout.write(plist(str(Path(args.config).expanduser().resolve()), args.python, args.pythonpath, args.log_dir))
        return 0
    if args.action == "revoke":
        return revoke(cfg)
    if args.action in ("qa-check", "qa-revoke", "qa-observe"):
        return qa_command(cfg, args.action, args.delivery_id, args.boundary)
    if args.action == "status":
        json.dump(status(cfg), sys.stdout, indent=2)
        print()
        return 0
    if args.action == "preflight":
        res = preflight(cfg)
        for name, reason in res:
            print(f"{name}: {reason}" + (" (informational: preflight runs before the opt-in; `run` refuses until true)"
                                         if name == "enabled" and reason != "ok" else ""))
        return 0 if all(r == "ok" for n, r in res if n != "enabled") else EXIT_REFUSED
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: threading.Thread(target=stop.set, daemon=True).start())
    return run(cfg, stop, adopt_existing_ledger=args.bind_existing_ledger)
