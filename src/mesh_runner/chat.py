"""A standing interactive session of an agent (e.g. a chat bridge pastes into it).

Cards run headless, one session each. Some agents also talk to a person through a
bridge that types into a terminal session (tmux). The runner keeps that session:

- alive: started in the agent's profile dir with ONLY the agent's environment
  (`env -i`: nothing leaks in from the tmux server, which may hold another
  account), restarted when gone or when its pane died, with a back-off when it
  keeps dying;
- current: when the agent's routing environment changes (another account or
  profile) or the session is older than `max_age_hours`, it is restarted — only
  while the operator's `idle_command` says it is idle, never mid-reply.

The session carries its own facts as tmux user options (@mr_owner, @mr_digest,
@mr_started), so a runner restart reads them instead of guessing. A session of
that name that the runner did not create is left alone unless `adopt` is set;
an adopted one counts as stale (restarted on the first idle moment).

Secrets never appear on a command line: they go through a 0600 file that the
session's shell reads and deletes before it execs the agent.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("mesh_runner")

NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")
ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# MESH_RUNNER_CHAT_HARNESS enters the digest only when the session runs another harness's
# command than its configured one, so the digest of a session on its own harness is unchanged
ROUTING_KEYS = ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "MESH_RUNNER_PROFILE", "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL",
                "MESH_RUNNER_CHAT_HARNESS")
CRASH_WINDOW = 300.0          # a session gone within this after its start counts as a crash
CRASH_BACKOFF = 600.0
COMPARE_EVERY = 300.0         # how often a live session's routing is compared (builds the env)
HOLD_ALERT_SECONDS = 1800.0   # a restart held this long is an operator signal (the runner raises it)


class ChatConfigError(ValueError):
    pass


@dataclass
class ChatSpec:
    session: str                                   # tmux session name the bridge expects
    command: list[str]                             # interactive agent, e.g. ["claude", "--model", "opus"]
    idle_command: list[str] = field(default_factory=list)   # exit 0 = idle, may restart now
    max_age_hours: float = 0.0                     # restart (while idle) after this; 0 = never
    adopt: bool = False                            # take over a same-named session the runner did not start
    tmux: list[str] = field(default_factory=lambda: ["tmux"])   # binary (+ e.g. -L socket)

    def __post_init__(self):
        if not NAME.fullmatch(self.session or ""):
            raise ChatConfigError(f"chat session name {self.session!r}: letters, digits, _ and - only")
        if not self.command:
            raise ChatConfigError("chat session needs a command")


def env_digest(env: dict[str, str]) -> str:
    """What decides a restart: the values that route the agent (account/profile)."""
    keys = sorted(k for k in env if k in ROUTING_KEYS)
    return hashlib.sha256("\n".join(f"{k}={env[k]}" for k in keys).encode()).hexdigest()[:16]


class ChatKeeper:
    def __init__(self, spec: ChatSpec, agent: str, profile_dir: Path, state_dir: Path, clock=time.time):
        self.spec, self.agent, self.cwd = spec, agent, profile_dir
        self.dir = state_dir / "chat"
        self.env_file = self.dir / f"{agent}.env"
        self.clock = clock
        self.last_start: float | None = None
        self.backoff_until = 0.0
        self.held_since: float | None = None
        self.hold_kind = ""                       # "idle": a restart waits for an idle moment;
                                                   # "route": routed to a harness without a command
        self.error = ""
        self._compared_at = -1e18
        self._env_failed = False                  # the last env build raised: no idle-driven rebuild before
                                                   # COMPARE_EVERY (a 60 s env timeout must not run each minute)
        self.hold_file = self.dir / f"{agent}.hold.json"
        self._load_hold()

    # ------------------------------------------------------------------ hold record
    def _load_hold(self) -> None:
        """A hold outlives a runner restart: its start stays the episode's start, so a chat held since the
        night is not shown (and not alerted) as held since the last restart."""
        try:
            rec = json.loads(self.hold_file.read_text())
            since = float(rec["since"])
        except (OSError, ValueError, TypeError, KeyError):
            return
        self.held_since, self.hold_kind = since, str(rec.get("kind") or "idle")
        log.info("%s: chat session %s held since %s (kept across the restart)", self.agent, self.spec.session,
                 time.strftime("%Y-%m-%d %H:%M", time.localtime(since)))

    def _save_hold(self) -> None:
        try:
            self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            tmp = self.hold_file.with_suffix(".tmp")
            tmp.write_text(json.dumps({"since": self.held_since, "kind": self.hold_kind}))
            os.replace(tmp, self.hold_file)
        except OSError as e:                      # the hold itself still works; only its memory is lost
            log.warning("%s: chat hold record not written: %s", self.agent, e)

    # ------------------------------------------------------------------ tmux
    def _tmux(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([*self.spec.tmux, *args], capture_output=True, text=True, timeout=15,
                              stdin=subprocess.DEVNULL)

    def _target(self) -> str:
        return f"={self.spec.session}:"      # exact name, accepted by every tmux command

    def state(self) -> str:
        """gone / dead (pane exited but kept) / ours / foreign."""
        if self._tmux("has-session", "-t", self._target()).returncode != 0:
            return "gone"
        panes = self._tmux("list-panes", "-s", "-t", self._target(), "-F", "#{pane_dead}")
        if panes.returncode == 0 and "1" in panes.stdout.split():
            return "dead"
        owner = self.option("@mr_owner")
        if owner is None:
            return "unknown"
        return "ours" if owner == self.agent else "foreign"

    def option(self, name: str) -> str | None:
        """The option's value, "" when unset, None when tmux could not answer."""
        res = self._tmux("show-options", "-v", "-q", "-t", self._target(), name)
        return res.stdout.strip() if res.returncode == 0 else None

    def _set(self, name: str, value: str) -> None:
        self._tmux("set-option", "-q", "-t", self._target(), name, value)

    # ------------------------------------------------------------------ actions
    def idle(self) -> bool:
        if not self.spec.idle_command:
            return False                           # without an idle probe: restart only when gone
        try:
            return subprocess.run(self.spec.idle_command, capture_output=True, timeout=30,
                                  stdin=subprocess.DEVNULL,
                                  env={**os.environ, "MESH_RUNNER_CHAT_SESSION": self.spec.session}
                                  ).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def _write_env(self, env: dict[str, str]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.dir, 0o700)
        self.env_file.unlink(missing_ok=True)
        fd = os.open(self.env_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            for k, v in env.items():
                if ENV_KEY.fullmatch(k):
                    fh.write(f"export {k}={shlex.quote(v)}\n")

    def start(self, env: dict[str, str], digest: str, command: list[str] | None = None) -> bool:
        env = dict(env)
        command = list(command or self.spec.command)
        env.setdefault("TERM", "xterm-256color")    # env -i drops what tmux would have set
        self._write_env(env)
        f = shlex.quote(str(self.env_file))
        inner = f". {f}; rm -f {f}; exec {' '.join(shlex.quote(x) for x in command)}"
        res = self._tmux("new-session", "-d", "-s", self.spec.session, "-c", str(self.cwd),
                         "/usr/bin/env", "-i", "/bin/sh", "-c", inner)
        if res.returncode != 0:
            self.env_file.unlink(missing_ok=True)
            self.error = f"chat session does not start: {res.stderr.strip()[:160]}"
            return False
        self.last_start = self.clock()
        self._set("@mr_owner", self.agent)
        self._set("@mr_digest", digest)
        self._set("@mr_started", str(int(self.clock())))
        self._set("remain-on-exit", "off")
        log.info("%s: chat session %s started", self.agent, self.spec.session)
        return True

    def stop(self) -> None:
        self._tmux("kill-session", "-t", self._target())
        self.env_file.unlink(missing_ok=True)

    def _hold(self, now: float, why: str, kind: str = "idle") -> str:
        if self.held_since is None:                # once per episode: the hold is visible in the log
            log.warning("%s: chat session %s held: %s", self.agent, self.spec.session, why)
        changed = self.held_since is None or self.hold_kind != kind
        self.held_since = self.held_since or now
        self.hold_kind = kind
        if changed:
            self._save_hold()
        return "held"

    def _release(self) -> None:
        if self.held_since is not None:
            self.hold_file.unlink(missing_ok=True)
        self.held_since, self.hold_kind = None, ""

    def hold_overdue(self, now: float | None = None) -> bool:
        """Held longer than HOLD_ALERT_SECONDS (counted from the episode's start, across restarts)."""
        return (self.held_since is not None
                and (self.clock() if now is None else now) - self.held_since >= HOLD_ALERT_SECONDS)

    def check(self, env_of) -> str:
        """One check; env_of() builds the agent's environment (only called when
        needed) and returns (env, digest) or (env, digest, command): digest None =
        routed to a harness this session has no command for; command None = the
        configured one. Returns started / restarted / kept / held / foreign / backoff / failed."""
        now = self.clock()
        state = self.state()
        if state in ("gone", "dead"):
            if self.last_start is not None and now - self.last_start < CRASH_WINDOW:
                self.backoff_until = now + CRASH_BACKOFF
                self.last_start = None
                self.error = "chat session keeps dying right after its start"
                log.warning("%s: chat session %s crash-loops; retry in %d min", self.agent,
                            self.spec.session, CRASH_BACKOFF // 60)
            if now < self.backoff_until:
                return "backoff"
            if state == "dead":
                self.stop()
            env, digest, command = _unpack(env_of())
            if digest is None:                     # routed to a harness this session cannot run
                self.error = "chat session is gone and routed to a harness it has no command for"
                return self._hold(now, "gone, routed to a harness without a chat command", "route")
            ok = self.start(env, digest, command)
            if ok:
                self.error = ""
                self._release()
            return "started" if ok else "failed"
        self.env_file.unlink(missing_ok=True)       # the shell has read it (or never will)
        if state == "unknown":
            return "kept"                          # tmux did not answer: decide next time
        if state == "foreign" and not self.spec.adopt:
            self.error = f"tmux session {self.spec.session} exists and is not the runner's (adopt = false)"
            return "foreign"
        if now - self._compared_at < COMPARE_EVERY:
            # alive and recently verified: no env build (a route hold changes only with the route). A restart
            # waiting for an idle moment asks the cheap idle probe first and builds the env (secrets, tool config)
            # only when the session is idle, so a busy chat costs one build per COMPARE_EVERY, not per check
            if self.hold_kind != "idle":
                return "held" if self.held_since is not None else "kept"
            if self.held_since is not None and (self._env_failed or not self.idle()):
                return "held"
        self._compared_at = now                    # stamped before the build: a failing build waits too
        self._env_failed = True
        env, digest, command = _unpack(env_of())
        self._env_failed = False
        if digest is None:                         # the live session stays on its old route, visibly
            return self._hold(now, "routed to a harness without a chat command; the live session "
                                   "keeps its current harness", "route")
        recorded = self.option("@mr_digest") if state == "ours" else ""
        started = self.option("@mr_started") if state == "ours" else ""
        if recorded is None or started is None:
            return "kept"                          # unreadable now: not a reason to restart
        stale = recorded != digest
        old = (self.spec.max_age_hours > 0 and started.isdigit()
               and now - int(started) > self.spec.max_age_hours * 3600)
        if not (stale or old):
            self._release()
            self.error = ""
            return "kept"
        if not self.idle():
            return self._hold(now, "routing changed or session aged, waiting for an idle moment")
        log.info("%s: chat session %s restarts (%s)", self.agent, self.spec.session,
                 "adopted" if state == "foreign" else "routing changed" if stale else "age")
        self._release()
        self.stop()
        ok = self.start(env, digest, command)
        if ok:
            self.error = ""
        return "restarted" if ok else "failed"


def _unpack(res) -> tuple:
    env, digest = res[0], res[1]
    return env, digest, (res[2] if len(res) > 2 else None)
