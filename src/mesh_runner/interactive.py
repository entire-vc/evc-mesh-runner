"""Interactive launch mode: every turn goes into a live interactive harness session.

A subscription covers the interactive terminal programs (Claude Code, Codex TUI);
their non-interactive modes (``claude -p``, ``codex exec``) are billed per token. In
the interactive mode the runner keeps the model it always had: one native session per
task, resumed by id, several tasks of an agent in parallel. Only the process changes:

- the harness runs as its interactive program in a tmux window, one window per
  attempt, in a tmux session per agent (``mr-<agent>``);
- the turn's process is a small driver (``tui_driver.py``): it finds or starts the
  window, pastes the prompt, waits for the harness's end-of-turn hook (Claude Code
  ``Stop``, Codex ``notify``), writes the turn's events to the turn log in the same
  format the headless adapters produce, and exits;
- the window outlives the turn, so the next turn of the task goes into a warm session.
  ``reap`` closes windows whose attempt is gone, idle too long, or waiting long.

The headless mode stays available: ``launch_mode`` in ``[runner]`` (default
``interactive``) and an optional per-agent ``launch_mode`` choose between them.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .adapters.base import Adapter, Launch, TurnResult

log = logging.getLogger("mesh_runner")

LAUNCH_MODES = ("interactive", "headless")
SESSION_PREFIX = "mr-"
DRIVER = Path(__file__).with_name("tui_driver.py")
HOOK = Path(__file__).with_name("tui_hook.py")
REAP_EVERY_SECONDS = 60
CHAT_PREFIX = "chat-"                  # windows of chat sessions (see chat_delivery.window_key)
# how long a turn's prompt waits for the session's running background agents (see tui_driver)
BACKGROUND_WAIT_MINUTES = 45.0
# waits a warm session is not kept for: someone else's decision, a human, a timer
LONG_WAIT_KINDS = ("decision", "blocking", "human", "gate")


def session_name(agent: str) -> str:
    return SESSION_PREFIX + re.sub(r"[^A-Za-z0-9_-]", "_", agent)


def state_root(state_dir: Path) -> Path:
    return Path(state_dir) / "interactive"


def agent_dir(state_dir: Path, agent: str) -> Path:
    return state_root(state_dir) / re.sub(r"[^A-Za-z0-9_-]", "_", agent)


# ---------------------------------------------------------------------- window metadata
def meta_path(directory: Path, key: str) -> Path:
    return directory / f"{key}.json"


def read_meta(directory: Path, key: str) -> dict:
    try:
        data = json.loads(meta_path(directory, key).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_meta(directory: Path, key: str, meta: dict) -> None:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = meta_path(directory, key).with_suffix(".json.tmp")
    tmp.write_text(json.dumps(meta))
    tmp.replace(meta_path(directory, key))


def forget(directory: Path, key: str) -> None:
    """Drop everything the runner kept for one window."""
    for suffix in (".json", ".events.jsonl", ".settings.json", ".env", ".prompt", ".progress.json"):
        (directory / f"{key}{suffix}").unlink(missing_ok=True)


# ---------------------------------------------------------------------- turn progress
# The driver is the only process that sees an interactive turn: its own turn log stays empty
# until the turn ends, so the log's size says nothing about a live turn. It reports where the
# turn stands in <key>.progress.json (same directory as the window metadata), rewritten at
# least every few seconds while it polls. Phases, in order:
#   launcher  the driver runs; the harness window is not ready yet
#   ready     the harness is up and takes input; our prompt is not accepted yet
#   sent      the harness accepted the prompt; it has written nothing in reply yet
#   output    the harness wrote a reply: first output seen (progress follows)
#   done      the driver finished the turn (or gave up)
PHASES = ("launcher", "ready", "sent", "output", "done")


def progress_path(directory: Path, key: str) -> Path:
    return directory / f"{key}.progress.json"


def read_progress(directory: Path, key: str) -> dict:
    try:
        data = json.loads(progress_path(directory, key).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_progress(directory: Path, key: str, progress: dict) -> None:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = progress_path(directory, key).with_suffix(".json.tmp")
    tmp.write_text(json.dumps(progress))
    tmp.replace(progress_path(directory, key))


def pid_alive(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


# ---------------------------------------------------------------------- tmux
class Tmux:
    def __init__(self, binary: str | list[str] = "tmux"):
        self.cmd = list(binary) if isinstance(binary, (list, tuple)) else [binary]

    def run(self, *args: str, timeout: float = 15) -> subprocess.CompletedProcess:
        try:
            return subprocess.run([*self.cmd, *args], capture_output=True, text=True, timeout=timeout,
                                  stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError) as e:
            return subprocess.CompletedProcess([*self.cmd, *args], 1, "", f"{type(e).__name__}: {e}")

    def windows(self, session: str) -> dict[str, bool]:
        """window name -> pane alive, for one session ({} when the session is absent)."""
        res = self.run("list-windows", "-t", f"={session}:", "-F", "#{window_name}\t#{pane_dead}")
        if res.returncode != 0:
            return {}
        out = {}
        for line in res.stdout.splitlines():
            name, _, dead = line.partition("\t")
            if name:
                out[name] = dead.strip() != "1"
        return out

    def all_windows(self) -> list[tuple[str, str]]:
        """(session, window) of every runner window on this server."""
        res = self.run("list-windows", "-a", "-F", "#{session_name}\t#{window_name}")
        if res.returncode != 0:
            return []
        out = []
        for line in res.stdout.splitlines():
            s, _, w = line.partition("\t")
            if s.startswith(SESSION_PREFIX) and w:
                out.append((s, w))
        return out

    def kill_window(self, session: str, window: str) -> None:
        self.run("kill-window", "-t", f"={session}:={window}")


# ---------------------------------------------------------------------- adapter
class InteractiveAdapter(Adapter):
    """Wraps a headless adapter: same session ids and turn-log format, but the turn's
    process is the driver and the harness runs interactively."""

    def __init__(self, base: Adapter, agent: str, state_dir: Path, tmux: str = "tmux",
                 max_sessions: int = 20, python: str | None = None,
                 background_wait_minutes: float = BACKGROUND_WAIT_MINUTES):
        super().__init__(base.binary, base.extra_args)
        self.base = base
        self.name = base.name
        self.agent = agent
        self.dir = agent_dir(state_dir, agent)
        self.tmux = shutil.which(tmux) or tmux
        self.max_sessions = max_sessions
        self.python = python or sys.executable
        self.background_wait_minutes = background_wait_minutes

    # the runner routes a Codex lane to another profile by setting `profile` on its adapter:
    # it must reach the wrapped adapter, which builds the command line and names the model
    @property
    def profile(self) -> str:
        return getattr(self.base, "profile", "")

    @profile.setter
    def profile(self, value: str) -> None:
        if hasattr(self.base, "profile"):
            self.base.profile = value

    def __getattr__(self, name):
        # attributes the runner keeps on an adapter (e.g. _base_profile) live on the wrapped one
        if name.startswith("__") or name == "base":
            raise AttributeError(name)
        return getattr(self.base, name)

    def __setattr__(self, name, value):
        if name == "_base_profile":
            setattr(self.base, name, value)
        else:
            super().__setattr__(name, value)

    @property
    def session_id_known_upfront(self) -> bool:
        return self.base.session_id_known_upfront

    def _argv(self, session_id: str, cwd: Path, add_dirs: list[Path], resume: bool) -> list[str]:
        harness = self.base.interactive_argv(session_id, cwd, add_dirs, resume)
        return [self.python, str(DRIVER), "--harness", self.name, "--agent", self.agent,
                "--state", str(self.dir), "--tmux", self.tmux, "--max-sessions", str(self.max_sessions),
                "--cwd", str(cwd), "--session", session_id or "", "--mode", "resume" if resume else "start",
                "--background-wait-minutes", "%g" % self.background_wait_minutes, "--", *harness]

    def start_argv(self, session_id: str, cwd: Path, add_dirs: list[Path]) -> list[str]:
        return self._argv(session_id, cwd, add_dirs, False)

    def resume_argv(self, session_id: str, cwd: Path, add_dirs: list[Path]) -> list[str]:
        return self._argv(session_id, cwd, add_dirs, True)

    @staticmethod
    def _split(argv: list[str]) -> tuple[list[str], list[str]]:
        i = argv.index("--")
        return argv[:i + 1], argv[i + 1:]

    def with_model(self, argv: list[str], model: str) -> list[str]:
        head, tail = self._split(argv)
        return head + self.base.with_model(tail, model)

    def with_read_only(self, argv: list[str]) -> list[str]:
        head, tail = self._split(argv)
        return head + self.base.with_read_only(tail)

    def with_effort(self, argv: list[str], effort: str) -> list[str]:
        head, tail = self._split(argv)
        return head + self.base.with_effort(tail, effort)

    def prepare_resume(self, session_id: str, cwd: Path, env: dict) -> str:
        return self.base.prepare_resume(session_id, cwd, env)

    def parse(self, log_path: Path) -> TurnResult:
        return self.base.parse(log_path)

    def spawn(self, launch: Launch, log_path: Path):
        # the attempt id names the window: the turn log is <attempt>.turn<n>.<harness>.jsonl
        key = log_path.name.split(".")[0]
        head, tail = self._split(launch.argv)
        argv = head[:-1] + ["--key", key, "--"] + tail
        return super().spawn(Launch(argv, launch.cwd, launch.env, launch.prompt), log_path)


def wrap(base: Adapter, agent: str, mode: str, state_dir: Path, tmux: str, max_sessions: int,
         background_wait_minutes: float = BACKGROUND_WAIT_MINUTES) -> Adapter:
    if mode == "headless" or not hasattr(base, "interactive_argv"):
        return base
    return InteractiveAdapter(base, agent, state_dir, tmux, max_sessions,
                              background_wait_minutes=background_wait_minutes)


# ---------------------------------------------------------------------- reaper
def _wait_is_long(wait: dict, now: float, idle_seconds: float) -> bool:
    kind = str(wait.get("kind") or "")
    if kind in LONG_WAIT_KINDS:
        return True
    if kind == "time":
        until = wait.get("until") or ""
        try:
            from datetime import datetime
            at = datetime.fromisoformat(str(until).replace("Z", "+00:00")).timestamp()
        except ValueError:
            return True
        return at - now > idle_seconds
    return False


def reap(tmux: Tmux, store, state_dir: Path, agent: str, idle_minutes: float,
         open_states: tuple[str, ...], active_states: tuple[str, ...], now: float | None = None) -> list[tuple[str, str]]:
    """Close the agent's windows nobody will use warm: the attempt is gone or closed,
    the window sat idle longer than ``idle_minutes``, or the attempt waits on something
    slow (a person, a decision, a timer beyond the idle limit). A window whose driver is
    delivering a turn, or whose attempt is about to run, is never closed."""
    now = time.time() if now is None else now
    directory = agent_dir(state_dir, agent)
    session = session_name(agent)
    idle_seconds = idle_minutes * 60
    closed = []
    for window, alive in tmux.windows(session).items():
        if window.startswith(CHAT_PREFIX):              # a chat's window: the chat pool ends it, not a card
            continue
        meta = read_meta(directory, window)
        if pid_alive(int(meta.get("busy_pid") or 0)):
            continue
        a = store.get(window)
        if not alive:
            reason = "session exited"
        elif a is None or a.state not in open_states:
            reason = "attempt closed"
        elif a.state in active_states:
            continue
        else:
            last = float(meta.get("last_end") or meta.get("started") or 0)
            try:
                wait = json.loads(a.wait or "{}") if isinstance(a.wait, str) else dict(a.wait or {})
            except ValueError:
                wait = {}
            if now - last > idle_seconds:
                reason = f"idle {int((now - last) // 60)} min"
            elif a.state == "waiting" and _wait_is_long(wait, now, idle_seconds):
                reason = f"long wait ({wait.get('kind', '?')})"
            else:
                continue
        tmux.kill_window(session, window)
        forget(directory, window)
        closed.append((window, reason))
    # metadata of windows that no longer exist (a killed tmux server, a reboot)
    live = set(tmux.windows(session))
    if directory.is_dir():
        for f in directory.glob("*.json"):
            key = f.name[:-len(".json")]
            if "." in key or key.startswith(CHAT_PREFIX):   # settings files; a chat keeps its session id to resume
                continue
            if key not in live and not pid_alive(int(read_meta(directory, key).get("busy_pid") or 0)):
                forget(directory, key)
        for f in directory.glob("*.progress.json"):          # a driver that died without cleaning up
            key = f.name[:-len(".progress.json")]
            if key not in live and not pid_alive(int(read_progress(directory, key).get("pid") or 0)):
                f.unlink(missing_ok=True)
    return closed
