"""Codex adapter: one non-interactive thread per task, resumed by thread id.

    codex [--profile P] -C <cwd> [--add-dir D] exec --json -             (prompt on stdin)
    codex [--profile P] -C <cwd> [--add-dir D] exec resume <thread> --json -

Profile, working directory and extra writable directories are root options:
``codex exec resume`` does not accept them itself. The thread id arrives in the
first event (``thread.started``); the runner stores it as the session id.
"""
from __future__ import annotations

import glob
import gzip
import json
import os
import re
import shutil
import tempfile
import tomllib
from pathlib import Path

from ..auth import is_codex_auth_error
from ..harness_hold import is_upgrade_required
from .base import Adapter, TurnResult, read_events

ROLLOUT_SCAN = 1 << 20          # how much of the rollout's end is searched for the latest turn_context
_MODEL_LINE = re.compile(r'^\s*model\s*=\s*"([^"]+)"', re.M)
_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")
# What the runner exports for a card turn (runner._writer_launch_env); a Claude MCP child inherits
# all of it, a Codex one only what env_vars names. evc-mesh-mcp reads TASK and CHECKOUT_SESSION
# (memory-read headers, claim-own-created) and CHECKOUT_GENERATION (idempotency keys).
RUNNER_MCP_ENV = ("MESH_RUNNER_TASK", "MESH_RUNNER_LANE", "MESH_RUNNER_CHECKOUT_GENERATION",
                  "MESH_RUNNER_CHECKOUT_REQUEST", "MESH_RUNNER_CHECKOUT_SESSION")
MESH_MCP_COMMANDS = ("mesh-mcp-agent", "mesh-mcp")


class CodexAdapter(Adapter):
    name = "codex"

    def __init__(self, binary: str = "codex", extra_args: list[str] | None = None, profile: str = ""):
        super().__init__(binary, extra_args)
        self.profile = profile

    def _root(self, cwd: Path, add_dirs: list[Path]) -> list[str]:
        argv = [self.binary]
        if self.profile:
            argv += ["--profile", self.profile]
        argv += ["-C", str(cwd)]
        for d in add_dirs:
            argv += ["--add-dir", str(d)]
        return argv

    # The session cwd is the agent's profile, which need not be a git repository
    # (code comes in through --add-dir worktrees); without this Codex refuses with
    # "Not inside a trusted directory" before reading the prompt.
    EXEC_OPTS = ("--json", "--skip-git-repo-check")

    def start_argv(self, session_id: str, cwd: Path, add_dirs: list[Path]) -> list[str]:
        return [*self._root(cwd, add_dirs), "exec", *self.EXEC_OPTS, *self.extra_args, "-"]

    def resume_argv(self, session_id: str, cwd: Path, add_dirs: list[Path]) -> list[str]:
        return [*self._root(cwd, add_dirs), "exec", "resume", session_id, *self.EXEC_OPTS, *self.extra_args, "-"]

    def interactive_argv(self, session_id: str, cwd: Path, add_dirs: list[Path], resume: bool) -> list[str]:
        """The interactive TUI (no exec): ``codex [root options] [resume <thread>]``."""
        argv = self._root(cwd, add_dirs)
        if resume and session_id:
            argv += ["resume", session_id]
        return argv + self.extra_args

    def prepare_resume(self, session_id: str, cwd: Path, env: dict) -> str:
        """Restore an idle rollout compressed by host retention before checking for it.

        Keep the archive and any existing native rollout. Only one exact thread in
        the selected home may be restored; an incomplete gzip is never published.
        """
        if not re.fullmatch(r"[0-9A-Za-z-]{8,64}", session_id):
            return ""
        home = Path(env.get("CODEX_HOME") or Path(env.get("HOME") or Path.home()) / ".codex")
        sessions = home / "sessions"
        if any(sessions.glob(f"**/rollout-*{session_id}*.jsonl")):
            return ""
        archives = list(sessions.glob(f"**/rollout-*-{session_id}.jsonl.gz"))
        if not archives:
            return ""
        if len(archives) != 1:
            return "compressed Codex rollout is ambiguous; not restored"
        archive = archives[0]
        try:
            with gzip.open(archive, "rb") as source:
                first = source.readline()
                meta = json.loads(first)
                if (not isinstance(meta, dict) or meta.get("type") != "session_meta"
                        or not isinstance(meta.get("payload"), dict)
                        or meta["payload"].get("id") != session_id):
                    raise ValueError("session metadata does not match")
                with tempfile.NamedTemporaryFile(dir=archive.parent, prefix=".codex-resume-") as restored:
                    restored.write(first)
                    shutil.copyfileobj(source, restored)
                    restored.flush()
                    os.fsync(restored.fileno())
                    try:
                        os.link(restored.name, archive.with_suffix(""))
                    except FileExistsError:
                        return ""              # a concurrent native writer wins; never replace it
        except (OSError, EOFError, ValueError) as exc:
            # Decoder errors may contain transcript bytes: only the error class leaves this function.
            raise OSError(f"compressed Codex rollout could not be restored ({type(exc).__name__})") from None
        return "Codex rollout restored from local gzip archive"

    def session_known(self, session_id: str, cwd: Path, env: dict) -> bool:
        """False only when the Codex home of ``env`` keeps rollouts and none is this thread's:
        ``resume <thread>`` would find nothing. A home without a sessions directory says True."""
        home = Path(env.get("CODEX_HOME") or Path(env.get("HOME") or Path.home()) / ".codex")
        sessions = home / "sessions"
        if not session_id or not sessions.is_dir():
            return True
        return any(sessions.glob(f"**/rollout-*{session_id}*.jsonl"))

    def with_read_only(self, argv: list[str]) -> list[str]:
        """Codex's own read-only sandbox; a root option, so it goes before any subcommand."""
        return self._sandboxed(argv, "read-only")

    def with_slot_confined(self, argv: list[str]) -> list[str]:
        """Codex's workspace-write sandbox: writes reach the session's cwd (the slot) and temp dirs only."""
        out, j = [], 0
        while j < len(argv):                   # no extra writable directory may survive: the slot is the whole boundary
            if argv[j] in ("--add-dir", "--writable-root"):
                j += 2
            elif argv[j].startswith(("--add-dir=", "--writable-root=")):
                j += 1
            else:
                out.append(argv[j])
                j += 1
        out = [out[0], "-c", "sandbox_workspace_write.writable_roots=[]", *out[1:]]   # and none from the profile
        return self._sandboxed(out, "workspace-write")

    def _sandboxed(self, argv: list[str], mode: str) -> list[str]:
        out, j = [], 0
        while j < len(argv):                   # drop a configured sandbox, then name ours
            if argv[j] in ("--sandbox", "-s"):
                j += 2
                continue
            if argv[j].startswith("--sandbox="):
                j += 1
                continue
            if argv[j] in ("--full-auto", "--dangerously-bypass-approvals-and-sandbox"):
                j += 1
                continue
            out.append(argv[j])
            j += 1
        # headless: before ``exec``; the interactive TUI has no subcommand (or ``resume``)
        i = next((k for k, a in enumerate(out) if a in ("exec", "resume")), len(out))
        return out[:i] + ["--sandbox", mode] + out[i:]

    def with_instructions(self, argv: list[str], text: str) -> list[str]:
        """A root ``-c developer_instructions`` override (a JSON string is a valid TOML value)."""
        return [argv[0], "-c", "developer_instructions=" + json.dumps(text), *argv[1:]]

    def with_mcp(self, argv: list[str], name: str, command: list[str], env: dict) -> list[str]:
        """Root ``-c`` overrides of ``mcp_servers.<name>``: command, args and the whole env table are
        replaced, so the profile's entry under that name loses its agent key. JSON strings and
        arrays are valid TOML values."""
        table = "{" + ", ".join(f"{k} = {json.dumps(str(v))}" for k, v in env.items()) + "}"
        prefix = f"mcp_servers.{name}."
        return [argv[0],
                "-c", prefix + "command=" + json.dumps(command[0]),
                "-c", prefix + "args=" + json.dumps(list(command[1:])),
                "-c", prefix + "env=" + table,
                *argv[1:]]

    def with_runner_env(self, argv: list[str], env: dict) -> list[str]:
        """Codex starts a stdio MCP server with an allowlisted environment, not its own: the
        Mesh MCP of a card turn lost MESH_RUNNER_TASK / MESH_RUNNER_CHECKOUT_*, so its memory
        reads carried no task or session. Root ``-c mcp_servers.<name>.env_vars``
        forwards the carrier by name (values stay in env) to every server of the profile Codex
        loads whose command is the Mesh MCP. Only a name that file defines: an override of an
        unknown name makes Codex refuse its config (``invalid transport``)."""
        carried = [k for k in RUNNER_MCP_ENV if env.get(k)]
        if not carried:
            return argv
        extra: list[str] = []
        for name, kept in self._mesh_servers(env):
            names = kept + [k for k in carried if k not in kept]
            extra += ["-c", f"mcp_servers.{name}.env_vars=" + json.dumps(names)]
        return [argv[0], *extra, *argv[1:]] if extra else argv

    def _mesh_servers(self, env: dict) -> list[tuple[str, list[str]]]:
        """(name, env_vars it already forwards) of each Mesh MCP server in the config file
        Codex reads for this profile and home: ``<home>/<profile>.config.toml``, else
        ``<home>/config.toml``. Unreadable or malformed: none (the launch goes on as before)."""
        home = Path(env.get("CODEX_HOME") or Path(env.get("HOME") or Path.home()) / ".codex")
        names = ([f"{self.profile}.config.toml"] if self.profile else []) + ["config.toml"]
        for name in names:
            try:
                data = tomllib.loads((home / name).read_text())
            except FileNotFoundError:
                continue
            except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
                return []
            servers = data.get("mcp_servers")
            out = []
            for server, spec in (servers.items() if isinstance(servers, dict) else ()):
                if (isinstance(spec, dict) and _BARE_KEY.fullmatch(server)
                        and Path(str(spec.get("command") or "")).name in MESH_MCP_COMMANDS):
                    kept = spec.get("env_vars")
                    out.append((server, [v for v in kept if isinstance(v, str)] if isinstance(kept, list) else []))
            return sorted(out)
        return []

    def profile_model(self) -> str:
        """The model the profile pins (``~/.codex/<profile>.config.toml``): Codex does
        not name it in its event stream, and spend reports need it."""
        if not self.profile:
            return ""
        home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        for name in (f"{self.profile}.config.toml", "config.toml"):
            try:
                text = (home / name).read_text(errors="replace")
            except OSError:
                continue
            m = _MODEL_LINE.search(text.split("\n[", 1)[0])     # top level only, not [profiles.*]
            if m:
                return m.group(1)
        return ""

    def observed_context(self, session_id: str, env: dict) -> dict:
        """The model and effort Codex wrote in the ``turn_context`` record of its latest turn (the
        session rollout). Only those two fields and the record's time are read: the record also holds
        the cwd, sandbox and permission policy, which never leave this function."""
        if not session_id or not re.fullmatch(r"[0-9A-Za-z-]{8,64}", session_id):
            return {}
        home = env.get("CODEX_HOME") or os.path.join(env.get("HOME") or str(Path.home()), ".codex")
        found = glob.glob(os.path.join(home, "sessions", "**", f"rollout-*{session_id}*.jsonl"), recursive=True)
        if not found:
            return {}
        try:
            path = max(found, key=os.path.getmtime)
            with open(path, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - ROLLOUT_SCAN))
                tail = fh.read().decode("utf-8", "replace")
        except OSError:
            return {}
        last = {}
        for line in tail.splitlines():
            if '"turn_context"' not in line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            payload = ev.get("payload") if isinstance(ev, dict) and ev.get("type") == "turn_context" else None
            if isinstance(payload, dict):
                last = {"model": payload.get("model"), "effort": payload.get("effort"),
                        "at": ev.get("timestamp")}
        return {k: v for k, v in last.items() if isinstance(v, str) and v}

    def with_effort(self, argv: list[str], effort: str) -> list[str]:
        return [argv[0], "-c", f'model_reasoning_effort="{effort}"', *argv[1:]]

    def stream_opened(self, events: list[dict]) -> bool:
        """False for an empty stream or one of typed top-level ``error`` events with a message and no
        thread: the CLI refused before it started a thread. Any other event (``thread.started``, a turn,
        an item, ``turn.failed``) reads as opened."""
        for ev in events:
            err = ev.get("error")
            text = ev.get("message") or (err.get("message") if isinstance(err, dict) else err)
            if ev.get("type") != "error" or ev.get("thread_id") or not (isinstance(text, str) and text.strip()):
                return True
        return False

    def parse(self, log_path: Path) -> TurnResult:
        r = TurnResult()
        r.model = self.profile_model()
        worked = False
        for ev in read_events(log_path):
            t = ev.get("type", "")
            if t == "thread.started" and ev.get("thread_id"):
                r.session_id = ev["thread_id"]
            elif t == "item.completed":
                item = ev.get("item") or {}
                if item.get("type") in ("agent_message", "assistant_message") and item.get("text"):
                    r.final_text = item["text"]
                if item.get("type") != "error":
                    worked = True
            elif t == "turn.completed":
                r.finished = True
                r.usage = ev.get("usage") or {}
                r.is_error, r.error, r.auth_failed, r.auth_only = False, "", False, False   # a transient error earlier in the turn did not end it
                r.upgrade_required = False
                # the interactive driver's record of a turn that ended without a reply; a typed
                # provider refusal of this CLI version is a lane fault, not an empty turn
                no_reply = str(ev.get("no_reply") or "")
                if is_upgrade_required(no_reply):
                    r.upgrade_required, r.error_details = True, no_reply[-2000:]
            elif t in ("turn.failed", "error"):
                r.finished = True
                r.is_error = True
                if isinstance(ev.get("usage"), dict):
                    r.usage = ev["usage"]
                err = ev.get("error") or {}
                r.error = (err.get("message") if isinstance(err, dict) else str(err)) or ev.get("message", "") or t
                # typed provider refusal only; never assistant prose or a quoted report
                r.auth_failed = is_codex_auth_error(str(r.error))
                r.upgrade_required = is_upgrade_required(str(r.error))
                r.auth_only = r.auth_failed and not worked
        return r
