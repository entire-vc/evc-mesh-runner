"""One turn in an interactive harness session (see interactive.py).

    python3 tui_driver.py --harness claude|codex --agent A --state DIR --tmux BIN
        --max-sessions N --cwd DIR --session ID --mode start|resume --key ATTEMPT -- <harness argv>

The prompt arrives on stdin; the turn's events go to stdout (the runner points stdout at
the turn log), in the format of the headless adapters, so the runner parses them
unchanged. Exit 0 when the harness ended its turn, 1 otherwise (the events say why).
SIGTERM/SIGINT interrupt the turn (Escape in the session); the session itself stays and
no result is written, so the runner treats the turn as cut off.
"""
from __future__ import annotations

import contextlib
import fcntl
import glob
import hashlib
import json
import os
import re
import shlex
import signal
import sys
import time
from pathlib import Path

if __package__ in (None, ""):                         # run as a file by the runner
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mesh_runner.interactive import (BACKGROUND_WAIT_MINUTES, CHAT_PREFIX, HOOK, Tmux, forget, pid_alive,  # noqa: E402
                                     read_meta, session_name, write_meta, write_progress)

ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# markers of a parent Claude Code session: inherited, they make the child think it is a
# nested session (no transcript); never part of a lane's own configuration
SCRUB_ENV = re.compile(r"^(CLAUDECODE|CLAUDE_CODE_(ENTRYPOINT|CHILD_SESSION\w*|SSE_PORT|SESSION_ID|PARENT\w*))$")
# banners the harnesses show when a turn cannot run; matched only on the last lines of the
# visible screen of an idle session, never on history or on assistant text above them
LIMIT_RE = re.compile(r"(usage limit|hit your (usage )?limit|limit reached|limit will reset|resets at|"
                      r"rate limit(ed)?\b|api error:? ?\d{3}|overloaded_error)", re.I)
AUTH_RE = re.compile(r"(not logged in|run /login|please log in|oauth token (has )?expired|invalid api key|"
                     r"authentication_error|please run codex login)", re.I)
# a running turn: the interrupt hint, a running tool, or an active spinner ("✽ Forging… (1m 2s · …)";
# a finished one reads "✻ Sautéed for 24s")
BUSY_RE = re.compile(r"(esc to interrupt|esc to cancel|working \(|ctrl\+b to run in background|…\s*\(\d+[ms])", re.I)
CODEX_READY_RE = re.compile(r"(ask codex to do anything|\? for shortcuts)", re.I)
TRUST_RE = re.compile(r"trust this folder|do you trust the files|one you trust", re.I)
UPDATE_RE = re.compile(r"update available|update now|new version", re.I)
# Codex exits with status 1 when its own account check times out during start-up
# ("account/read failed during TUI bootstrap: ... workspace routing discovery timed out");
# measured 21.10 on the Mini: about one fresh start in three, with or without a proxy
BOOTSTRAP_RE = re.compile(r"^Error: .*failed during TUI bootstrap", re.I)
PANE_DEAD_RE = re.compile(r"^Pane is dead\b")
BOOTSTRAP_RETRIES = 2
CURSOR_RE = re.compile(r"^\s*[❯›>]\s*(.*)$")
SESSION_ARGS = ("--session-id", "--resume")
# Claude Code lists the main thread and its background agents in rows under the prompt box
# while any agent is listed:
#     ──────────────────────
#     ❯
#     ──────────────────────
#       ⏵⏵ bypass permissions on (shift+tab to cycle) · ↓ to manage
#       ⏺ main
#       ◯ reviewer-a  Running the checks                     8m 48s
# A row is [selection pointer] [tree connector] bullet name  activity  status. The bullet is ⏺
# (● off macOS) for the row being viewed and ◯ otherwise; the main row is listed only together
# with agent rows. A finished background agent leaves the list; a teammate or agent that sits
# idle shows "idle" or "waiting" as its status, and several idle ones collapse into
# "N idle agents". Codex renders no such rows.
PROMPT_RULE_RE = re.compile(r"^\s*─{8,}\s*$")
TASK_ROW_RE = re.compile(r"^\s*(?:❯\s*)?[│├└─\s]*[⏺●◯]\s+(\S.*?)\s*$")
MAIN_ROW_RE = re.compile(r"^main(?:\s+↑\s*\d+\s+more)?$")
IDLE_ROW_RE = re.compile(r"(?:^\d+\s+idle\s+agents?$|\s(?:idle|waiting)$)")


def background_agents(screen: str) -> list[str]:
    """Names of the background agents a Claude Code screen lists as running: the rows under the
    main-thread row (``⏺ main`` / ``◯ main``) below the prompt box's last rule, except idle ones.
    Text above that rule (the conversation) never counts, and a screen without the main row (no
    agent listed, or Codex) has none."""
    lines = screen.splitlines()
    rules = [i for i, line in enumerate(lines) if PROMPT_RULE_RE.match(line)]
    if not rules:
        return []
    agents, listed = [], False
    for line in lines[rules[-1] + 1:]:
        m = TASK_ROW_RE.match(line)
        if not m:
            continue
        row = m.group(1)
        if not listed:
            listed = bool(MAIN_ROW_RE.match(row))
            continue
        if MAIN_ROW_RE.match(row) or IDLE_ROW_RE.search(row):
            continue
        agents.append(row.split()[0])
    return agents


PASTE_WRAP_RE = re.compile(r"</?pasted_content[^>]*>")
SCAN_CHUNK = 1 << 20
# Codex rollout response_item types written by the model (a "message" counts only with role assistant)
CODEX_MODEL_ITEMS = ("agent_message", "reasoning", "function_call", "custom_tool_call", "local_shell_call",
                     "web_search_call")


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _said(text: str) -> str:
    """The prompt as the harness records it, comparable to ours: Claude Code wraps pasted
    text in <pasted_content id="…">…</pasted_content>; whitespace may differ."""
    return _squash(PASTE_WRAP_RE.sub("", text or ""))


class DriverError(Exception):
    def __init__(self, message: str, close: bool = False):
        super().__init__(message)
        self.close = close                     # the window is unusable: close it


class Cancelled(Exception):
    pass


class Driver:
    def __init__(self, harness: str, agent: str, state: Path, tmux: Tmux, max_sessions: int, cwd: Path,
                 session_id: str, mode: str, key: str, argv: list[str], out=None, env: dict | None = None,
                 python: str | None = None, clock=time.time, sleep=time.sleep, timeouts: dict | None = None):
        self.harness, self.agent, self.dir = harness, agent, Path(state)
        self.tmux, self.max_sessions, self.cwd = tmux, max_sessions, Path(cwd)
        self.sid, self.mode, self.key, self.argv = session_id, mode, key, list(argv)
        self.out = out or sys.stdout
        self.env = {k: v for k, v in dict(os.environ if env is None else env).items() if not SCRUB_ENV.match(k)}
        self.python = python or sys.executable
        self.clock, self.sleep = clock, sleep
        self.t = {"ready": 90, "accept": 30, "poll": 1.0, "scan": 15, "interrupt": 6, "settle": 1.5,
                  "idle": 30, "stop_wait": 45, "beat": 5,
                  "cap_wait": 300, "cap_poll": 5, "busy_wait": 1800, "paste": 5,
                  # before a prompt goes into a session whose background agents still run (0 = do not wait)
                  "background_wait": BACKGROUND_WAIT_MINUTES * 60, "background_poll": 2.0, "background_settle": 10,
                  **(timeouts or {})}
        self.session = session_name(agent)
        self.target = f"={self.session}:={key}"
        self.events = self.dir / f"{key}.events.jsonl"
        self.cancelled = False
        self.prompt = ""
        self.prompt_id = ""
        self.turn_offset = 0
        self.meta: dict = {}
        # where the turn stands, for the runner's watchdog (see interactive.PHASES)
        self.progress = {"pid": os.getpid(), "phase": "launcher", "started": self.clock(), "progress_at": self.clock(),
                         "updated": 0.0, "size": 0, "external_wait": False,
                         # the prompt is held while the session's background agents run (wait_background);
                         # released_at: when that hold ended (0 = never held)
                         "held": False, "released_at": 0.0}
        self.beat_at = 0.0
        self.sent_size = -1            # the transcript's size when the prompt was accepted; -1 = not accepted yet
        self.scan_from = 0             # first transcript byte not yet searched for a reply
        self.skipping = False          # scan_from is inside a line longer than one read
        self.rollout = ""
        self.replied = False           # Codex: a model record was seen since the prompt was accepted
        self.turn_end: dict | None = None   # Codex: the task_complete of our turn, read from the rollout
        self.turn_id = ""              # Codex: the turn our prompt started, from the rollout's task_started

    # ------------------------------------------------------------------ helpers
    def emit(self, event: dict) -> None:
        self.out.write(json.dumps(event, ensure_ascii=False) + "\n")
        self.out.flush()

    def save(self) -> None:
        write_meta(self.dir, self.key, self.meta)

    def check_cancel(self) -> None:
        if self.cancelled:
            raise Cancelled()

    def read_events(self) -> list[dict]:
        out = []
        try:
            with open(self.events, "rb") as fh:
                for raw in fh:
                    try:
                        out.append(json.loads(raw))
                    except ValueError:
                        continue
        except FileNotFoundError:
            pass
        return out

    def pane(self, lines: int = 60) -> str:
        res = self.tmux.run("capture-pane", "-p", "-t", self.target, "-S", f"-{lines}")
        return res.stdout if res.returncode == 0 else ""

    def screen(self) -> str:
        res = self.tmux.run("capture-pane", "-p", "-t", self.target)
        return res.stdout if res.returncode == 0 else ""

    def busy(self) -> bool:
        return bool(BUSY_RE.search(self.screen()))

    def background(self, screen: str | None = None) -> list[str]:
        """Background agents the session shows as running (Claude Code only)."""
        if self.harness != "claude":
            return []
        return background_agents(self.screen() if screen is None else screen)

    def window_state(self) -> str:
        """absent / dead / alive"""
        res = self.tmux.run("list-panes", "-t", self.target, "-F", "#{pane_dead}")
        if res.returncode != 0:
            return "absent"
        return "dead" if "1" in res.stdout.split() else "alive"

    def digest(self) -> str:
        """What decides that a live window cannot take this turn: the whole environment
        (account, tokens, home) and the harness command line (model, effort, flags),
        never the session id. Values are hashed, never stored."""
        argv, skip = [], False
        for x in self.argv:
            if skip:
                skip = False
                continue
            if x in SESSION_ARGS:
                skip = True
                continue
            argv.append(x)
        if self.harness == "codex" and "resume" in argv:     # codex: `resume <thread>`
            i = argv.index("resume")
            argv = argv[:i] + argv[i + 2:]
        env = [f"{k}={self.env[k]}" for k in sorted(self.env)]
        return hashlib.sha256("\0".join(env + ["--"] + argv).encode()).hexdigest()[:16]

    def died_in_bootstrap(self) -> str:
        """The harness's start-up error line when the window died before taking our prompt
        ("" otherwise). Only then is a new window safe: the prompt was never accepted, the
        process exited non-zero and its LAST output line is the start-up error, so the
        phrase inside a prompt or in later output does not count."""
        if self.prompt_taken:
            return ""
        res = self.tmux.run("list-panes", "-t", self.target, "-F", "#{pane_dead} #{pane_dead_status}")
        dead, _, status = (res.stdout.split("\n")[0] if res.returncode == 0 else "").partition(" ")
        if dead != "1" or status.strip() in ("", "0"):
            return ""
        lines = [x.strip() for x in self.pane(200).splitlines() if x.strip()]
        lines = [x for x in lines if not PANE_DEAD_RE.match(x)]
        return lines[-1] if lines and BOOTSTRAP_RE.search(lines[-1]) else ""

    def tail(self, lines: int = 8) -> str:
        text = [x for x in self.pane(40).splitlines() if x.strip()]
        return " | ".join(x.strip() for x in text[-lines:])[:400]

    def wait_idle(self, seconds: float) -> bool:
        """The session shows no running turn and its transcript stopped growing."""
        deadline = self.clock() + seconds
        last = None
        while self.clock() < deadline:
            self.check_cancel()
            self.beat()
            size = self.transcript_size()
            if not self.busy() and size == last:
                return True
            last = size
            self.sleep(self.t["settle"])
        return False

    # ------------------------------------------------------------------ progress
    def progress_size(self) -> int:
        """Size of the file the harness appends to while it works: Claude's transcript, Codex's rollout."""
        if self.harness == "claude":
            return self.transcript_size()
        if not self.rollout or not os.path.exists(self.rollout):
            thread = self.meta.get("session_id") or self.sid
            self.rollout = self.codex_rollout(thread) if thread else self.fresh_rollout()
        try:
            return os.path.getsize(self.rollout) if self.rollout else 0
        except OSError:
            return 0

    def reply_started(self, size: int) -> bool:
        """The harness wrote a reply after it accepted our prompt: Claude, an assistant record in
        the transcript (``claude_reply``); Codex, a model-written record in the rollout
        (``codex_reply``). The echo of our prompt, session and context records never count.
        Only complete lines appended since the last look are parsed, at most SCAN_CHUNK bytes per
        call; a line longer than that counts as output and is skipped through, never parsed."""
        path = self.meta.get("transcript") if self.harness == "claude" else self.rollout
        if not path or size <= self.scan_from:
            return False
        try:
            with open(path, "rb") as fh:
                while True:
                    fh.seek(self.scan_from)
                    chunk = fh.read(min(SCAN_CHUNK, size - self.scan_from))
                    if not self.skipping:
                        break
                    nl = chunk.find(b"\n")        # inside an over-long line: find where it ends
                    if nl < 0:
                        self.scan_from += len(chunk)
                        if self.scan_from >= size or not chunk:
                            return False
                        continue
                    self.scan_from += nl + 1
                    self.skipping = False
                    chunk = chunk[nl + 1:]
                    break
        except OSError:
            return False
        if not chunk:
            return False
        end = chunk.rfind(b"\n") + 1                    # a trailing partial line waits for its newline
        if end == 0 and len(chunk) >= SCAN_CHUNK:
            # A record longer than one read. Its fragment is not parsed, and parsing does not resume in
            # the middle of it (self.skipping carries on to its newline). It counts as output: only model
            # text or tool results that follow a model reply grow that large, our prompt echo is small, and
            # a false "no first output" would cut a working turn, the dangerous direction for a watchdog.
            self.scan_from += len(chunk)
            self.skipping = True
            return True
        self.scan_from += end
        lines = chunk[:end].splitlines()
        if self.harness == "claude":
            return any(self.claude_reply(line) for line in lines)
        replied = False
        for line in lines:                                # in file order; the first task end closes our turn
            if self.codex_reply(line):
                replied = True
                continue
            started = self.codex_task_start(line)
            if started is not None:
                self.turn_id = self.turn_id or started    # the first task after our prompt is our turn
                continue
            ended = self.codex_task_end(line)
            if ended is not None:
                ended["output"] = replied or self.replied
                self.turn_end = ended
                break
        self.replied = self.replied or replied
        return replied

    @staticmethod
    def claude_reply(line: bytes) -> bool:
        """A transcript line whose top-level ``type`` is ``assistant``. The record is parsed, never
        searched for text: a user record may quote the marker."""
        if b"assistant" not in line:
            return False
        try:
            rec = json.loads(line)
        except ValueError:
            return False
        return isinstance(rec, dict) and rec.get("type") == "assistant"

    @staticmethod
    def codex_reply(line: bytes) -> bool:
        """A rollout line the model wrote: an assistant message, reasoning, or a tool call. The user and
        developer messages (our prompt and the harness context), session_meta, event_msg, turn_context
        and token records are not."""
        if b'"response_item"' not in line:
            return False
        try:
            rec = json.loads(line)
        except ValueError:
            return False
        item = rec.get("payload") if rec.get("type") == "response_item" else None
        if not isinstance(item, dict):
            return False
        kind = item.get("type")
        if kind == "message":
            return item.get("role") == "assistant"
        return kind in CODEX_MODEL_ITEMS

    @staticmethod
    def codex_task_start(line: bytes) -> str | None:
        """The rollout's ``task_started``: the id of the turn it opens ('' when it names none). None for
        any other line."""
        if b'"task_started"' not in line:
            return None
        try:
            rec = json.loads(line)
        except ValueError:
            return None
        item = rec.get("payload") if rec.get("type") == "event_msg" else None
        if not isinstance(item, dict) or item.get("type") != "task_started":
            return None
        return str(item.get("turn_id") or "")

    @staticmethod
    def codex_task_end(line: bytes) -> dict | None:
        """The rollout's ``task_complete``, which Codex writes when a task ends, however it ends: with
        ``message`` (its final agent message, may be empty) and ``error`` (a short description, or
        ''). None for any other line. The caller reads only lines after our prompt was accepted, so an
        earlier task's end is never seen here."""
        if b'"task_complete"' not in line:
            return None
        try:
            rec = json.loads(line)
        except ValueError:
            return None
        item = rec.get("payload") if rec.get("type") == "event_msg" else None
        if not isinstance(item, dict) or item.get("type") != "task_complete":
            return None
        err = item.get("error")
        text = "%s: %s" % (str(err.get("codex_error_info") or "?")[:60], str(err.get("message") or "")[:200]) \
            if isinstance(err, dict) else ""
        return {"message": str(item.get("last_agent_message") or ""), "error": text}

    def beat(self, phase: str = "", force: bool = False, moved: bool = False) -> None:
        """Tell the runner where the turn stands (interactive.PHASES). ``progress_at`` moves when the
        phase changes, the harness's file grows or the caller says so (``moved``); ``external_wait``
        says the screen shows a running tool, a background task or agent, or a spinner, or the prompt
        is held for background agents, i.e. the silence is the agent waiting on something.
        Never raises: reporting must not end a turn."""
        now = self.clock()
        changed = bool(phase) and phase != self.progress["phase"]
        if changed:
            self.progress["phase"] = phase
        changed = changed or moved
        if not (force or changed) and now - self.beat_at < self.t["beat"]:
            return
        self.beat_at = now
        try:
            size = self.progress_size()
            if size != self.progress["size"]:
                changed = changed or size > self.progress["size"]
                self.progress["size"] = size
            if self.progress["phase"] == "sent" or (self.harness == "codex" and self.progress["phase"] == "output"):
                # Codex's rollout is read to the end of the turn too: its task_complete is the end signal
                if self.reply_started(size) and self.progress["phase"] == "sent":
                    self.progress["phase"], changed = "output", True
                if self.turn_end is not None:            # the turn is over: nothing left for the watchdog to judge
                    self.progress["phase"], changed = "done", True
            if changed:
                self.progress["progress_at"] = now
            self.progress["external_wait"] = self.progress["phase"] != "done" and (
                self.progress["held"] or self.busy() or bool(self.background()))
            self.progress["updated"] = now
            write_progress(self.dir, self.key, self.progress)
        except Exception:                                # noqa: BLE001 - see the docstring
            pass

    # ------------------------------------------------------------------ session
    def harness_argv(self) -> list[str]:
        hook = f"{shlex.quote(self.python)} {shlex.quote(str(HOOK))} {shlex.quote(str(self.events))}"
        if self.harness == "claude":
            settings = self.dir / f"{self.key}.settings.json"
            entry = [{"hooks": [{"type": "command", "command": hook}]}]
            hooks = {name: entry for name in ("SessionStart", "UserPromptSubmit", "Stop", "StopFailure")}
            settings.write_text(json.dumps({"hooks": hooks, "skipDangerousModePermissionPrompt": True}))
            return [*self.argv, "--settings", str(settings)]
        notify = json.dumps([self.python, str(HOOK), str(self.events)])
        return [self.argv[0], "-c", f"notify={notify}",
                "-c", f'projects."{self.cwd}".trust_level="trusted"',
                "-c", "check_for_update_on_startup=false", *self.argv[1:]]

    @contextlib.contextmanager
    def host_lock(self):
        """One window start at a time on this host (all agents of this runner): the session
        cap is checked and the window created under it, so parallel starts cannot exceed it."""
        path = self.dir.parent / ".start.lock"
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def make_room(self) -> bool:
        """At most max_sessions runner windows on this host: close the one idle longest.
        False when the cap is reached and every window is running a turn."""
        # chat windows are the chat pool's: neither counted against this cap nor closed from here
        windows = [(se, w) for se, w in self.tmux.all_windows() if not w.startswith(CHAT_PREFIX)]
        if self.max_sessions <= 0 or len(windows) < self.max_sessions:
            return True
        root = self.dir.parent
        idle = []
        for session, window in windows:
            directory = root / session[len("mr-"):]
            meta = read_meta(directory, window)
            if pid_alive(int(meta.get("busy_pid") or 0)) or window == self.key:
                continue
            idle.append((float(meta.get("last_end") or 0), session, window, directory))
        need = len(windows) - self.max_sessions + 1
        if len(idle) < need:
            return False
        for _, session, window, directory in sorted(idle)[:need]:
            self.tmux.kill_window(session, window)
            forget(directory, window)
        return True

    def new_window(self, command: list[str]) -> None:
        """Create the window; a session another driver creates or kills meanwhile is retried."""
        # remain-on-exit in the same tmux command: a harness that dies at once leaves its
        # last screen behind for the error, instead of a window that is simply gone
        keep = [";", "set-option", "-w", "-t", self.target, "remain-on-exit", "on"]
        err = ""
        for _ in range(6):
            if self.tmux.run("has-session", "-t", f"={self.session}").returncode == 0:
                res = self.tmux.run("new-window", "-d", "-t", f"={self.session}:", "-n", self.key,
                                    "-c", str(self.cwd), *command, *keep)
            else:
                res = self.tmux.run("new-session", "-d", "-s", self.session, "-n", self.key, "-x", "220",
                                    "-y", "60", "-c", str(self.cwd), *command, *keep)
            if res.returncode == 0:
                return
            err = res.stderr.strip()
            if not re.search(r"duplicate session|can't find session|no such session|session not found", err, re.I):
                break
            self.sleep(0.2)
        raise DriverError(f"interactive session does not start: {err[:200]}")

    def private_dir(self) -> None:
        """The state directory holds the session env file: 0700, also when it existed."""
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.dir, 0o700)

    def start_window(self) -> None:
        self.private_dir()
        self.events.unlink(missing_ok=True)
        argv = self.harness_argv()
        env = {k: v for k, v in self.env.items() if ENV_KEY.fullmatch(k)}
        env.setdefault("TERM", "xterm-256color")
        env_file = self.dir / f"{self.key}.env"
        env_file.unlink(missing_ok=True)
        fd = os.open(env_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            for k, v in env.items():
                fh.write(f"export {k}={shlex.quote(v)}\n")
        f = shlex.quote(str(env_file))
        inner = f". {f}; rm -f {f}; exec {' '.join(shlex.quote(x) for x in argv)}"
        try:
            deadline = self.clock() + self.t["cap_wait"]
            while True:
                with self.host_lock():                   # count, evict and create as one step
                    if self.make_room():
                        self.new_window(["/usr/bin/env", "-i", "/bin/sh", "-c", inner])
                        self.save()                      # busy before the lock lets anyone count it
                        break
                if self.clock() > deadline:              # every live window runs a turn
                    raise DriverError(f"host session cap reached ({self.max_sessions} live sessions, all busy)")
                self.check_cancel()
                self.beat()
                self.sleep(self.t["cap_poll"])
        except (DriverError, Cancelled):
            env_file.unlink(missing_ok=True)
            raise
        self.meta.update({"harness": self.harness, "digest": self.digest(), "session_id": self.sid,
                          "started": self.clock(), "last_end": self.clock(), "transcript": ""})
        self.save()
        self.wait_ready()

    def wait_ready(self) -> None:
        deadline = self.clock() + self.t["ready"]
        answered = 0
        last_screen = None
        while self.clock() < deadline:
            self.check_cancel()
            self.beat()
            if self.window_state() != "alive":
                raise DriverError("interactive session exited at start: " + self.tail(), close=True)
            if self.harness == "claude":
                for ev in self.read_events():
                    if ev.get("hook_event_name") == "SessionStart":
                        if ev.get("transcript_path"):
                            self.meta["transcript"] = ev["transcript_path"]
                            self.save()                  # a killed driver must not lose it
                        return
            text = self.screen()                         # what is on screen now, not history
            menu = TRUST_RE.search(text) or (self.harness == "codex" and UPDATE_RE.search(text)
                                             and not CODEX_READY_RE.search(text))
            if self.harness == "codex" and CODEX_READY_RE.search(text) and not TRUST_RE.search(text):
                if text == last_screen:                  # the marker shows before start-up is done:
                    return                               # ready when the screen also stopped changing
                last_screen = text
                self.sleep(self.t["poll"])
                continue
            if menu and answered < 6:                    # startup questions: trust yes, update skip
                self.answer_menu(text)
                answered += 1
            self.sleep(self.t["poll"])
        raise DriverError("interactive session did not become ready: " + self.tail(), close=True)

    def answer_menu(self, text: str) -> None:
        """Move the cursor to the wanted option, then confirm: trust the folder (Claude
        preselects "No, exit", Codex lists "Trust and continue" first); skip an update."""
        update = UPDATE_RE.search(text) and not TRUST_RE.search(text)
        want = re.compile(r"skip|later|not now", re.I) if update else re.compile(r"\btrust\b|\byes\b", re.I)
        selected = ""
        for line in text.splitlines():
            m = CURSOR_RE.match(line)
            if m:
                selected = m.group(1)
        if selected and want.search(selected) and not re.search(r"\bno,|update now", selected, re.I):
            self.tmux.run("send-keys", "-t", self.target, "Enter")
        else:
            self.tmux.run("send-keys", "-t", self.target, "Down")
        self.sleep(0.5)

    def ensure_session(self) -> None:
        state = self.window_state()
        reuse = (state == "alive" and self.mode == "resume" and self.meta.get("harness") == self.harness
                 and self.meta.get("digest") == self.digest()
                 and (not self.sid or self.meta.get("session_id") in ("", self.sid)))
        why = ("reuse" if reuse else "start" if state == "absent" else "restart: " + ", ".join(
            r for r, bad in (("mode start", self.mode != "resume"), ("harness", self.meta.get("harness") != self.harness),
                             ("environment or command", self.meta.get("digest") != self.digest()),
                             ("session", bool(self.sid) and self.meta.get("session_id") not in ("", self.sid)),
                             ("window dead", state == "dead")) if bad))
        print(f"interactive: {why}", file=sys.stderr, flush=True)
        if reuse:
            # a busy session (it goes on after a background task, or a cut-off turn still
            # runs) is not interrupted: turn() waits for it to become idle before pasting
            return
        if state != "absent":
            self.tmux.run("kill-window", "-t", self.target)
        busy = self.meta.get("busy_pid")
        forget(self.dir, self.key)
        self.meta = {"busy_pid": busy}
        self.save()                                      # still claimed while the window is rebuilt
        self.beat(force=True)                            # forget() dropped the progress file too
        self.start_window()

    # ------------------------------------------------------------------ turn
    def paste(self, prompt: str) -> None:
        buf = self.dir / f"{self.key}.prompt"
        fd = os.open(buf, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(prompt)
        name = f"mr-{self.key}"
        try:
            for attempt in (1, 2):
                if self.tmux.run("load-buffer", "-b", name, str(buf)).returncode != 0:
                    raise DriverError("prompt could not be loaded into tmux")
                if self.tmux.run("paste-buffer", "-p", "-d", "-b", name, "-t", self.target).returncode != 0:
                    raise DriverError("prompt could not be pasted into the session", close=True)
                if self.pasted(prompt):
                    break
        finally:
            buf.unlink(missing_ok=True)
        self.tmux.run("send-keys", "-t", self.target, "Enter")

    def pasted(self, prompt: str) -> bool:
        """The paste shows in the input box: a collapsed "[Pasted …" chip or the prompt's start."""
        head = _squash(prompt)[:24]
        deadline = self.clock() + self.t["paste"]
        while True:
            self.sleep(0.5)
            screen = self.screen()
            if re.search(r"\[Pasted (Content|text)", screen) or (head and head in _squash(screen)):
                return True
            if self.clock() > deadline:
                return False

    def said_ours(self, text: str) -> bool:
        mine, said = _said(self.prompt), _said(text)
        # the record must hold our prompt's start: a short or unrelated record (a "y",
        # a side task's message) is not ours even if it occurs inside our text
        return bool(said) and bool(mine) and mine[:200] in said

    def opens_with_ours(self, text: str) -> bool:
        """The message IS our prompt: it starts with the whole of it (whitespace aside). A message that
        only quotes it, or shares its opening and then goes on differently, does not count. Codex
        lists each user message in full (a 17.5k-character prompt came back at its full length), so
        no shorter prefix is compared."""
        mine, said = _said(self.prompt), _said(text)
        return bool(said) and bool(mine) and said.startswith(mine)

    def ours(self, ev: dict) -> bool:
        """Codex also notifies for its own side tasks, e.g. the title it generates for a new thread,
        which runs in a thread and turn of its own and whose single input message quotes our prompt
        after its own instructions. A notify is ours when it names the turn our prompt started (once
        the rollout told us that turn's id) and its last input message is our prompt itself, not a
        quote of it. A notify without input messages carries no evidence of our prompt: it is ours
        only when it names our turn's id, its thread alone is not enough. Until that id is known such
        a notify waits (done() looks at it again on every poll), and the rollout's task_complete
        still ends the turn."""
        turn = str(ev.get("turn-id") or "")
        if self.turn_id and turn and turn != self.turn_id:
            return False
        said = [str(x) for x in ev.get("input-messages") or [] if str(x).strip()]
        if said:                                  # every user message of the thread so far
            return self.opens_with_ours(said[-1])
        return bool(self.turn_id) and turn == self.turn_id

    def accepted(self, base: int, busy_before: bool) -> bool:
        """The harness took our prompt: Claude's UserPromptSubmit naming it (its prompt_id
        then identifies our Stop); Codex: its notify, or a turn that started running."""
        evs = self.read_events()[base:]
        if self.harness == "claude":
            for e in evs:
                if e.get("hook_event_name") == "UserPromptSubmit" and self.said_ours(str(e.get("prompt") or "")):
                    self.prompt_id = str(e.get("prompt_id") or "")
                    return True
            return False
        return any(e.get("type") == "agent-turn-complete" and self.ours(e) for e in evs) or \
            (not busy_before and self.busy())

    def done(self, base: int) -> dict | None:
        """The last end-of-turn event of OUR prompt. Claude: Stop/StopFailure carrying our
        prompt_id (an earlier, cut-off turn's Stop does not count)."""
        found = None
        for ev in self.read_events()[base:]:
            if self.harness == "claude" and ev.get("hook_event_name") in ("Stop", "StopFailure"):
                pid = str(ev.get("prompt_id") or "")
                # a Stop must name our prompt (both unnamed only on a harness without ids);
                # a failure may not name it: after ours was taken, it is this turn's
                if (not pid and ev.get("hook_event_name") == "StopFailure") or \
                        (pid and pid == self.prompt_id) or \
                        (not pid and not self.prompt_id and self.reply_in_turn(ev)):
                    found = ev
            elif self.harness == "codex" and ev.get("type") == "agent-turn-complete" and self.ours(ev):
                found = ev
        return found

    def reply_in_turn(self, ev: dict) -> bool:
        """A Stop that names no prompt (a harness without prompt ids) is ours only if its
        reply stands in the transcript after our prompt."""
        reply = _squash(str(ev.get("last_assistant_message") or ""))
        if not reply:
            return False
        try:
            with open(self.meta.get("transcript") or ev.get("transcript_path") or "", "rb") as fh:
                fh.seek(self.turn_offset)
                entries = [json.loads(x) for x in fh if x.strip().startswith(b"{")]
        except (OSError, ValueError):
            return False
        mine = self.slice_turn(entries)
        if mine is entries and not any(e.get("type") == "user" and self.said_ours(self._user_text(e))
                                       for e in entries):
            return False
        return any(e.get("type") == "assistant" and reply in _squash(" ".join(
            str(b.get("text") or "") for b in (e.get("message") or {}).get("content") or [] if isinstance(b, dict)))
            for e in mine)

    def interrupt(self, base: int) -> None:
        self.tmux.run("send-keys", "-t", self.target, "Escape")
        stop = self.clock() + self.t["interrupt"]
        while self.clock() < stop and self.done(base) is None:
            self.sleep(self.t["poll"])
        raise Cancelled()

    def wait_background(self) -> bool:
        """Hold the prompt while the session's background agents run. Its prompt box is free then,
        so a pasted prompt would go in at once and get a "still waiting" turn; when an agent
        finishes, Claude Code resumes the main thread by itself. Returns once no agent is listed,
        no turn runs and the transcript stayed unchanged for ``background_settle`` seconds (that
        resumed turn has ended too); False when ``background_wait`` ran out first (the prompt then
        goes in as before). The progress report marks the wait as external meanwhile."""
        limit = self.t["background_wait"]
        if limit <= 0:
            return True
        agents = self.background()
        if not agents:
            return True
        print("interactive: %d background agent(s) running (%s): the prompt waits up to %g min" % (
            len(agents), ", ".join(agents)[:120], limit / 60), file=sys.stderr, flush=True)
        deadline = self.clock() + limit
        quiet_since, last = None, None
        self.progress["held"] = True
        self.beat(force=True)
        try:
            while self.clock() < deadline:
                self.check_cancel()
                self.beat()
                screen, size = self.screen(), self.transcript_size()
                if self.background(screen) or BUSY_RE.search(screen) or size != last:
                    quiet_since = None
                elif quiet_since is None:
                    quiet_since = self.clock()
                elif self.clock() - quiet_since >= self.t["background_settle"]:
                    return True
                last = size
                self.sleep(self.t["background_poll"])
            print("interactive: background agents still running after %g min, the prompt goes in anyway: %s" % (
                limit / 60, self.tail(4)), file=sys.stderr, flush=True)
            return False
        finally:
            # the session's own work is over (or no longer waited for): the turn's clocks start here
            self.progress["held"], self.progress["released_at"] = False, self.clock()
            self.beat(force=True, moved=True)

    def turn(self, prompt: str) -> dict:
        self.prompt, self.prompt_id = prompt, ""
        self.prompt_taken = False
        self.turn_end, self.replied, self.turn_id = None, False, ""
        # a prompt pasted while background agents run only gets a "still waiting" turn: wait for
        # them (and the turn Claude Code starts by itself when one finishes) before anything else
        self.wait_background()
        base = len(self.read_events())
        offset = self.turn_offset = self.transcript_size()
        totals = self.codex_totals()
        # the session may still be working although its last turn ended (it continues on
        # its own when a background task finishes): paste into an idle session
        if not self.wait_idle(self.t["busy_wait"]):     # no busy marker AND a settled transcript
            raise DriverError("the session stayed busy: " + self.tail())
        busy_before = self.busy()
        # Codex: read the rollout from where it stands before the prompt goes in, never from where it stands
        # once the prompt is accepted: a task that fails fast may already have written its end by then.
        # Records before our prompt belong to turns that ended (the session is idle). Unknown only for a
        # known thread whose rollout file is not found yet; then the cursor is taken after acceptance.
        before = self.progress_size() if self.harness == "codex" else 0
        cursor = before if self.harness == "codex" and (
            self.rollout or not (self.meta.get("session_id") or self.sid)) else None
        self.paste(prompt)
        deadline = self.clock() + self.t["accept"]
        retried = False
        while not self.accepted(base, busy_before):
            self.beat()
            if self.cancelled:
                self.interrupt(base)
            if self.window_state() != "alive":
                raise DriverError("interactive session exited: " + self.tail(), close=True)
            if self.busy():                              # queued behind running work: not refused
                deadline = max(deadline, self.clock() + self.t["accept"])
            if self.clock() > deadline:
                if retried:
                    raise DriverError("the session did not take the prompt: " + self.tail(), close=True)
                self.tmux.run("send-keys", "-t", self.target, "Enter")
                retried, deadline = True, self.clock() + self.t["accept"]
            self.sleep(self.t["poll"])
        self.prompt_taken = True                         # from here a retry could deliver it twice
        self.marks["taken"] = self.clock()
        self.sent_size = self.progress_size()
        self.scan_from = self.sent_size if cursor is None else cursor
        self.skipping = False
        self.beat("sent", force=True)
        scan_at, banner_seen, stop_seen = self.clock() + self.t["scan"], "", None
        end_seen = None
        while True:
            self.beat()
            if self.cancelled:
                self.interrupt(base)
            ev = self.done(base)
            if ev is not None:
                # another Stop hook may block the stop and Claude goes on: the turn is over
                # when the session is idle, its transcript settled and no newer Stop came —
                # or, so a status line that never clears cannot hold the turn, after stop_wait
                stop_seen = stop_seen if stop_seen is not None else self.clock()
                if self.harness == "codex" or ev.get("hook_event_name") == "StopFailure" or \
                        (self.wait_idle(self.t["settle"] * 3) and self.done(base) == ev) or \
                        self.clock() - stop_seen > self.t["stop_wait"]:
                    return {"event": self.done(base) or ev, "offset": offset, "totals": totals}
                continue
            if self.turn_end is not None:
                # The rollout says the task ended after our prompt. Codex calls its notify only for a task
                # that finished with a message, so a task that ended without one (an error, no model output)
                # would otherwise hang until the watchdog. With a message the notify is given a moment, to be
                # consumed here and not taken for the next turn's.
                end = self.turn_end
                end_seen = end_seen if end_seen is not None else self.clock()
                if not end["message"] or self.clock() - end_seen > self.t["settle"] * 2:
                    if not end["message"]:
                        print("interactive: codex ended the turn with no final message (%s%s)" % (
                            "after model output" if end["output"] else "no model output",
                            "; " + end["error"] if end["error"] else ""), file=sys.stderr, flush=True)
                    thread = self.meta.get("session_id") or self.sid or self.rollout_thread()
                    event = {"type": "agent-turn-complete", "thread-id": thread,
                             "last-assistant-message": end["message"]}
                    return {"event": event, "offset": offset, "totals": totals}
            if self.window_state() != "alive":
                raise DriverError("interactive session exited: " + self.tail(), close=True)
            if self.clock() >= scan_at:
                scan_at = self.clock() + self.t["scan"]
                text = self.screen()
                hit = "" if BUSY_RE.search(text) else self.banner(text)
                if hit and hit == banner_seen:          # the same banner on an idle screen twice
                    raise DriverError(hit, close=True)
                banner_seen = hit
            self.sleep(self.t["poll"])

    @staticmethod
    def banner(text: str) -> str:
        for line in reversed([x.strip() for x in text.splitlines() if x.strip()][-6:]):
            if LIMIT_RE.search(line) or AUTH_RE.search(line):
                return line[:300]
        return ""

    # ------------------------------------------------------------------ transcripts
    def transcript_size(self) -> int:
        path = self.meta.get("transcript")
        try:
            return os.path.getsize(path) if path else 0
        except OSError:
            return 0

    def rollout_thread(self) -> str:
        """The thread id in the rollout's file name (rollout-<time>-<thread>.jsonl), or ''."""
        m = re.search(r"([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})\.jsonl$", self.rollout or "")
        return m.group(1) if m else ""

    def codex_rollout(self, thread: str) -> str:
        if not thread:
            return ""
        home = self.env.get("CODEX_HOME") or os.path.join(self.env.get("HOME") or os.path.expanduser("~"), ".codex")
        found = glob.glob(os.path.join(home, "sessions", "**", f"rollout-*{thread}*.jsonl"), recursive=True)
        return max(found, key=os.path.getmtime) if found else ""

    def fresh_rollout(self) -> str:
        """A new Codex session's rollout, before its thread id is known: the newest file created since
        this driver started whose session_meta names our working directory."""
        home = self.env.get("CODEX_HOME") or os.path.join(self.env.get("HOME") or os.path.expanduser("~"), ".codex")
        since = self.progress["started"] - 2
        found = []
        for path in glob.glob(os.path.join(home, "sessions", "*", "*", "*", "rollout-*.jsonl")):
            try:
                if os.path.getmtime(path) >= since:
                    found.append(path)
            except OSError:
                continue
        for path in sorted(found, key=os.path.getmtime, reverse=True):
            try:
                with open(path, "rb") as fh:
                    meta = json.loads(fh.readline(SCAN_CHUNK))
                if os.path.realpath(str((meta.get("payload") or {}).get("cwd") or "")) == os.path.realpath(str(self.cwd)):
                    return path
            except (OSError, ValueError, AttributeError):
                continue
        return ""

    def codex_totals(self) -> dict:
        if self.harness != "codex":
            return {}
        path = self.codex_rollout(self.meta.get("session_id") or self.sid)
        last = {}
        try:
            with open(path) as fh:
                for line in fh:
                    if "token_count" not in line:
                        continue
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    info = (e.get("payload") or {}).get("info") or {}
                    if isinstance(info.get("total_token_usage"), dict):
                        last = info["total_token_usage"]
        except (OSError, TypeError):
            pass
        return last

    @staticmethod
    def _user_text(entry: dict) -> str:
        content = (entry.get("message") or {}).get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return " ".join(str(b.get("text") or "") for b in content
                            if isinstance(b, dict) and b.get("type") == "text")
        return ""

    def slice_turn(self, entries: list[dict]) -> list[dict]:
        """This turn's entries: from the last user entry that is our prompt (the offset
        only says where to start looking)."""
        for i in range(len(entries) - 1, -1, -1):
            e = entries[i]
            if e.get("type") == "user" and self.said_ours(self._user_text(e)):
                return entries[i:]
        return entries

    def emit_claude(self, done: dict) -> bool:
        ev, offset = done["event"], done["offset"]
        sid = ev.get("session_id") or self.sid
        path = ev.get("transcript_path") or self.meta.get("transcript") or ""
        if path != self.meta.get("transcript"):
            offset = 0                                   # another file: search it from the start
            self.meta["transcript"] = path
        entries = []
        try:
            with open(path, "rb") as fh:
                fh.seek(offset)
                for raw in fh:
                    try:
                        entries.append(json.loads(raw))
                    except ValueError:
                        continue
        except (OSError, TypeError):
            pass
        entries = self.slice_turn(entries)
        failure = ev.get("hook_event_name") == "StopFailure"
        if not failure and not any(e.get("type") == "assistant" for e in entries):
            raise DriverError("the session ended the turn but its transcript has no reply ("
                              + ("transcript missing" if not entries else "no assistant message") + ")", close=True)
        model, usage, error = "", {}, ""
        for e in entries:
            if e.get("type") == "assistant":
                msg = e.get("message") or {}
                model = model or str(msg.get("model") or "")
                if msg.get("id"):
                    usage[msg["id"]] = msg.get("usage") or {}
                if e.get("isApiErrorMessage"):
                    error = " ".join(str(b.get("text") or "") for b in msg.get("content") or []
                                     if isinstance(b, dict)).strip() or "API error"
        if failure:
            error = str(ev.get("error") or ev.get("message") or ev.get("reason") or "") or error or "turn failed"
        self.emit({"type": "system", "subtype": "init", "session_id": sid, "model": model,
                   "launch_mode": "interactive"})
        for e in entries:
            t = e.get("type")
            if t in ("user", "assistant"):
                self.emit({"type": t, "session_id": sid, "message": e.get("message") or {}})
            elif t == "system" and e.get("subtype") == "compact_boundary":
                self.emit({"type": "system", "subtype": "compact_boundary", "session_id": sid})
        total = {}
        for u in usage.values():
            for k, v in u.items():
                if isinstance(v, (int, float)):
                    total[k] = total.get(k, 0) + v
        self.emit({"type": "result", "subtype": "error_during_execution" if error else "success",
                   "is_error": bool(error), "result": error or ev.get("last_assistant_message") or "",
                   "session_id": sid, "usage": total, "total_cost_usd": None})
        self.meta["session_id"] = sid
        return not error

    def emit_codex(self, done: dict) -> bool:
        ev, before = done["event"], done["totals"] or {}
        thread = str(ev.get("thread-id") or self.meta.get("session_id") or self.sid)
        self.meta["session_id"] = thread
        after = self.codex_totals()
        usage = {k: max(0, int(after.get(k) or 0) - int(before.get(k) or 0))
                 for k in ("input_tokens", "cached_input_tokens", "output_tokens")} if after else {}
        self.emit({"type": "thread.started", "thread_id": thread, "launch_mode": "interactive"})
        self.emit({"type": "item.completed", "item": {"type": "agent_message",
                                                      "text": ev.get("last-assistant-message") or ""}})
        self.emit({"type": "turn.completed", "usage": usage})
        return True

    def emit_start(self) -> None:
        """Session id only, no result: the runner treats the turn as cut off."""
        sid = self.meta.get("session_id") or self.sid
        if not sid:
            return
        if self.harness == "codex":
            self.emit({"type": "thread.started", "thread_id": sid, "launch_mode": "interactive"})
        else:
            self.emit({"type": "system", "subtype": "init", "session_id": sid, "launch_mode": "interactive"})

    def emit_error(self, message: str) -> None:
        if self.harness == "codex":
            thread = self.meta.get("session_id") or self.sid
            if thread:
                self.emit({"type": "thread.started", "thread_id": thread, "launch_mode": "interactive"})
            self.emit({"type": "turn.failed", "error": {"message": message}})
        else:
            sid = self.meta.get("session_id") or self.sid
            self.emit({"type": "system", "subtype": "init", "session_id": sid, "launch_mode": "interactive"})
            self.emit({"type": "result", "subtype": "error_during_execution", "is_error": True,
                       "result": message, "session_id": sid, "usage": {}, "total_cost_usd": None})

    # ------------------------------------------------------------------ main
    def print_timing(self, marks: dict) -> None:
        """One stderr line of where a turn's seconds went (the chat delivery logs it)."""
        order = (("session", "start", "ready"), ("idle_paste", "ready", "taken"),
                 ("model_stop", "taken", "stop"), ("emit", "stop", "emit"))
        parts = ["%s=%.1f" % (n, marks[b] - marks[a]) for n, a, b in order if a in marks and b in marks]
        print("timing: " + " ".join(parts), file=sys.stderr, flush=True)

    def run(self, prompt: str) -> int:
        # claim the window first: the reaper and other drivers' make_room skip a busy one
        self.private_dir()
        self.meta = read_meta(self.dir, self.key)
        self.meta["busy_pid"] = os.getpid()
        self.save()
        self.beat(force=True)
        ok, close = False, False
        self.prompt_taken = False
        marks = self.marks = {"start": self.clock()}
        try:
            for start in range(BOOTSTRAP_RETRIES + 1):
                try:
                    self.ensure_session()
                    self.beat("ready", force=True)
                    marks["ready"] = self.clock()
                    done = self.turn(prompt)
                    marks["stop"] = self.clock()
                    break
                except DriverError:
                    line = self.died_in_bootstrap()
                    if not line:
                        raise
                    if start == BOOTSTRAP_RETRIES:
                        raise DriverError(f"{self.harness} exited during start-up {start + 1} times: {line[:300]}",
                                          close=True) from None
                    print(f"interactive: {self.harness} exited during start-up, starting again "
                          f"({start + 1}/{BOOTSTRAP_RETRIES})", file=sys.stderr, flush=True)
            ok = self.emit_claude(done) if self.harness == "claude" else self.emit_codex(done)
            marks["emit"] = self.clock()
            self.print_timing(marks)
            # a failed turn (StopFailure, an API error) leaves the session in that state
            close = not ok
        except Cancelled:
            self.emit_start()
        except DriverError as e:
            close = e.close
            self.emit_error(str(e))
        except Exception as e:                           # never leave a turn without a result
            close = True
            self.emit_error(f"driver failed: {type(e).__name__}: {str(e)[:200]}")
        finally:
            self.beat("done", force=True)
            self.meta["busy_pid"] = 0
            self.meta["last_end"] = self.clock()
            if not close and self.window_state() == "alive":
                self.save()
            else:
                self.tmux.run("kill-window", "-t", self.target)
                forget(self.dir, self.key)
        return 0 if ok else 1


def parse_args(argv: list[str]) -> tuple[dict, list[str]]:
    if "--" not in argv:
        raise SystemExit("usage: tui_driver.py <options> -- <harness argv>")
    i = argv.index("--")
    opts, harness = argv[:i], argv[i + 1:]
    out = {}
    j = 0
    while j < len(opts):
        if not opts[j].startswith("--") or j + 1 >= len(opts):
            raise SystemExit(f"bad option {opts[j]}")
        out[opts[j][2:].replace("-", "_")] = opts[j + 1]
        j += 2
    return out, harness


def timeouts_from(opts: dict) -> dict:
    """Driver timeouts named on the command line; ``--poll`` / ``--settle`` (seconds) are the chat's."""
    return {"background_wait": 60 * float(opts.get("background_wait_minutes", BACKGROUND_WAIT_MINUTES)),
            **{k: float(opts[k]) for k in ("poll", "settle") if k in opts}}


def main(argv: list[str] | None = None) -> int:
    opts, harness = parse_args(list(sys.argv[1:] if argv is None else argv))
    driver = Driver(harness=opts["harness"], agent=opts["agent"], state=Path(opts["state"]),
                    tmux=Tmux(opts.get("tmux", "tmux")), max_sessions=int(opts.get("max_sessions", 20)),
                    cwd=Path(opts["cwd"]), session_id=opts.get("session", ""), mode=opts.get("mode", "start"),
                    key=opts["key"], argv=harness,
                    timeouts=timeouts_from(opts))

    def interrupt(signum, frame):
        driver.cancelled = True
    signal.signal(signal.SIGTERM, interrupt)
    signal.signal(signal.SIGINT, interrupt)
    prompt = sys.stdin.read()
    return driver.run(prompt)


if __name__ == "__main__":
    sys.exit(main())
