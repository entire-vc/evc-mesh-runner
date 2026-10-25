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
ROUTING_KEYS = ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "MESH_RUNNER_PROFILE", "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL")
CRASH_WINDOW = 300.0          # a session gone within this after its start counts as a crash
CRASH_BACKOFF = 600.0
COMPARE_EVERY = 300.0         # how often a live session's routing is compared (builds the env)


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
        self.error = ""
        self._compared_at = -1e18

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

    def start(self, env: dict[str, str], digest: str) -> bool:
        env = dict(env)
        env.setdefault("TERM", "xterm-256color")    # env -i drops what tmux would have set
        self._write_env(env)
        f = shlex.quote(str(self.env_file))
        inner = f". {f}; rm -f {f}; exec {' '.join(shlex.quote(x) for x in self.spec.command)}"
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

    def check(self, env_of) -> str:
        """One check; env_of() builds the agent's environment (only called when
        needed). Returns started / restarted / kept / held / foreign / backoff / failed."""
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
            env, digest = env_of()
            if digest is None:                     # routed to a harness this session cannot run
                self.held_since = self.held_since or now
                return "held"
            ok = self.start(env, digest)
            if ok:
                self.error = ""
            return "started" if ok else "failed"
        self.env_file.unlink(missing_ok=True)       # the shell has read it (or never will)
        if state == "unknown":
            return "kept"                          # tmux did not answer: decide next time
        if state == "ours" and now - self._compared_at < COMPARE_EVERY and self.held_since is None:
            return "kept"                          # alive and recently verified: no env build
        if state == "foreign" and not self.spec.adopt:
            self.error = f"tmux session {self.spec.session} exists and is not the runner's (adopt = false)"
            return "foreign"
        env, digest = env_of()
        self._compared_at = now
        if digest is None:
            self.held_since = None
            return "kept"                          # routing says nothing about this session now
        recorded = self.option("@mr_digest") if state == "ours" else ""
        started = self.option("@mr_started") if state == "ours" else ""
        if recorded is None or started is None:
            return "kept"                          # unreadable now: not a reason to restart
        stale = recorded != digest
        old = (self.spec.max_age_hours > 0 and started.isdigit()
               and now - int(started) > self.spec.max_age_hours * 3600)
        if not (stale or old):
            self.held_since, self.error = None, ""
            return "kept"
        if not self.idle():
            self.held_since = self.held_since or now
            return "held"
        log.info("%s: chat session %s restarts (%s)", self.agent, self.spec.session,
                 "adopted" if state == "foreign" else "routing changed" if stale else "age")
        self.held_since = None
        self.stop()
        ok = self.start(env, digest)
        if ok:
            self.error = ""
        return "restarted" if ok else "failed"
