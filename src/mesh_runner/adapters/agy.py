"""Google Antigravity CLI adapter (binary ``agy``): one print-mode conversation per task, resumed by
its id.

    agy --output-format stream-json --settings <json> [--add-dir D] ... --print=<prompt>
    agy --output-format stream-json --settings <json> [--add-dir D] ... --conversation <id> --print=<prompt>

Print mode takes the prompt as an argument (``--input-format stream-json`` would read it from stdin,
but only wrapped in an NDJSON envelope), so a POSIX shell reads the runner's stdin and hands it over
in one ``--print=`` argument; the ``=`` form keeps a prompt that starts with a dash a value.

Like the Kimi adapter it has no ``interactive_argv``: the subscription quota is the same in the TUI
and in print mode, so ``interactive.wrap`` keeps it headless in every launch mode.

Stream (NDJSON on stdout, recorded from agy 1.5.14):
    {"event": "init", "conversation_id": "<uuid>", "init": {"model": "...", "cwd": "...", "tools": [...],
     "permission_mode": "..."}}
    {"event": "step_update", "step_update": {"conversation_id", "step_index", "state": "ACTIVE"|"DONE",
     "step_type": "user_input"|"agent_response"|"tool"|"system_message"|..., "text_delta", "tool_name",
     "tool_info", "usage": {...}}}                                   # usage on a DONE model step
    {"event": "result", "result": {"conversation_id", "status": "SUCCESS"|"ERROR"|"CANCELED"|
     "INTERRUPTED"|..., "response", "error", "num_turns", "usage", "denied_actions": [...]}}
Exit 0 is not success: ``status`` is. ``result.usage`` counts the whole conversation, so a turn's
usage is the sum of its own steps. A headless permission the settings do not grant is soft-denied:
the run goes on, ``denied_actions`` names it and stderr explains.

Resume: ``--conversation <id>`` of an unknown id does NOT fail: agy warns on stderr and starts a new
conversation. ``session_known`` checks the conversation store first; ``parse`` reports a returned id
that differs from the requested one as a missing session (the runner starts the task over once).

The fleet's ``agy`` is a wrapper that runs the native CLI only through the Google egress tunnel; when
the tunnel is down it refuses the launch with ``Google egress: <reason>; launch refused ...`` on
stderr and exit 69, before any stream. That is reported as ``egress_unavailable``: neither an account
limit nor an authentication failure.

Permissions are explicit per session (``--settings``, never saved): every action a lane turn needs is
allowed and nothing asks, since print mode cannot answer a question. Mesh MCP comes from the
workspace file ``<cwd>/.agents/mcp_config.json`` (``--settings`` does not take MCP servers), which the
adapter writes before each start from the lane's ``.mcp.json``: the Mesh servers only.

Admission: before every start each configured ``admission_commands`` entry (an argv; ``{gemini_dir}``
= the Gemini home of the launch environment) must exit 0, e.g. the secret guard check of that home and
the egress tunnel check; anything else refuses the start before any process runs (fail closed, the
pending prompt is kept, never a quota). Without admission commands every start is refused.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

from .base import Adapter, Launch, LaunchProbeDenied, TurnResult, read_events

_SHELL = 'prompt=$(cat); exec "$0" "$@" "--print=$prompt"'
_CONVERSATION_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
EGRESS_RE = re.compile(r"^Google egress: (.*)$", re.M)
ERROR_RE = re.compile(r"^(?:Error|error): (.*)$", re.M)
NOT_FOUND_RE = re.compile(r'^warning: conversation "([^"]+)" not found', re.M)
# agy's own classification of a provider limit (the CLI's QUOTA_RE), plus the runner's words
_LIMIT_RE = re.compile(r"RESOURCE_EXHAUSTED|quota|\b429\b|exhausted your capacity|rate.?limit|usage limit", re.I)
_AUTH_RE = re.compile(r"authentication required|unauthenticated|not (?:logged|signed) in|\b401\b|"
                      r"invalid[_ ]grant|UNAUTHENTICATED", re.I)
EFFORTS = ("low", "medium", "high", "xhigh", "max")
MESH_MCP_COMMANDS = ("mesh-mcp-agent", "mesh-mcp")
# exec wrappers that run their first argument as the server (stderr capture), nothing else
MCP_WRAPPERS = ("mcp-wrap",)
ADMISSION_TIMEOUT = 30
# Every action a lane turn takes; deny stays explicit. Read-only turns deny commands and writes.
LANE_PERMISSIONS = {"allow": ["command(*)", "read_file(*)", "write_file(*)", "read_url(*)",
                              "download_url(*)", "mcp(*)"], "deny": []}
READ_ONLY_PERMISSIONS = {"allow": ["read_file(*)", "read_url(*)", "mcp(*)"],
                         "deny": ["command(*)", "write_file(*)", "download_url(*)"]}
_USAGE_KEYS = (("input_tokens", "input_tokens"), ("output_tokens", "output_tokens"),
               ("cache_read_tokens", "cache_read_input_tokens"), ("thinking_tokens", "thinking_tokens"))


def _settings(permissions: dict) -> str:
    return json.dumps({"permissions": permissions}, separators=(",", ":"))


def _typed(detail: str) -> str:
    """A failure in the runner's words: a provider limit says ``quota``."""
    if _LIMIT_RE.search(detail) and not re.search(r"quota|rate.?limit", detail, re.I):
        return "quota: " + detail
    return detail


def gemini_dir(env: dict) -> Path:
    return Path(env.get("HOME") or Path.home()) / ".gemini"


def _is_mesh(spec: dict) -> bool:
    """A server whose executable is Mesh's MCP binary: the command itself, or the first argument of a
    known exec wrapper (``mcp-wrap <binary> <args>``). Any other argument naming it proves nothing:
    ``python3 other.py mesh-mcp`` runs ``other.py``."""
    command = spec.get("command")
    args = spec.get("args") or []
    if not isinstance(command, str) or not isinstance(args, list):
        return False
    executable = command
    if Path(command).name in MCP_WRAPPERS:
        executable = args[0] if args and isinstance(args[0], str) else ""
    return Path(executable).name in MESH_MCP_COMMANDS


def mesh_servers(path: Path) -> dict:
    """The Mesh MCP servers (stdio) of an MCP config file (``mcpServers``), {} when none or unreadable."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    out = {}
    for name, spec in (servers.items() if isinstance(servers, dict) else ()):
        if isinstance(spec, dict) and spec.get("command") and _is_mesh(spec) and not spec.get("disabled"):
            out[str(name)] = {k: spec[k] for k in ("command", "args", "env", "cwd") if k in spec}
    return out


def write_workspace_mcp(cwd: Path) -> bool:
    """``<cwd>/.agents/mcp_config.json`` holds the Mesh servers of the lane's ``<cwd>/.mcp.json``
    (written 0600, atomically, only when it differs). Without a Mesh server there, the Mesh servers of an
    existing workspace file are kept and every other server is dropped from it: the session never sees a
    server the filter refused. True when the workspace has a Mesh server."""
    target = cwd / ".agents" / "mcp_config.json"
    servers = mesh_servers(cwd / ".mcp.json") or mesh_servers(target)
    if not servers:
        return False
    body = json.dumps({"mcpServers": servers}, indent=2, sort_keys=True) + "\n"
    try:
        if target.read_text() == body and target.stat().st_mode & 0o777 == 0o600:
            return True
    except OSError:
        pass
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".mcp_config.", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(body)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return True


def _drop(argv: list[str], flags: tuple[str, ...]) -> list[str]:
    out, i = [], 0
    while i < len(argv):
        if argv[i] in flags:
            i += 2
            continue
        if not any(argv[i].startswith(f + "=") for f in flags):
            out.append(argv[i])
        i += 1
    return out


class AgyAdapter(Adapter):
    name = "agy"

    def __init__(self, binary: str = "agy", extra_args: list[str] | None = None, shell: str = "/bin/sh",
                 admission_commands: list[list[str]] | None = None):
        super().__init__(binary, extra_args)
        self.shell = shell
        self.admission_commands = [[str(x) for x in c] for c in admission_commands or [] if c]

    def _argv(self, add_dirs: list[Path], conversation: str = "") -> list[str]:
        argv = [self.shell, "-c", _SHELL, self.binary, "--output-format", "stream-json"]
        if not any(a == "--settings" or a.startswith("--settings=") for a in self.extra_args):
            argv += ["--settings", _settings(LANE_PERMISSIONS)]
        for d in add_dirs:
            argv += ["--add-dir", str(d)]
        if conversation:
            argv += ["--conversation", conversation]
        return argv + self.extra_args

    def start_argv(self, session_id: str, cwd: Path, add_dirs: list[Path]) -> list[str]:
        return self._argv(add_dirs)

    def resume_argv(self, session_id: str, cwd: Path, add_dirs: list[Path]) -> list[str]:
        return self._argv(add_dirs, session_id)

    def session_known(self, session_id: str, cwd: Path, env: dict) -> bool:
        """False when the Gemini home of ``env`` keeps conversations and none is ``session_id``
        (``antigravity-cli/conversations/<id>.db``): agy would silently start a new one."""
        store = gemini_dir(env) / "antigravity-cli" / "conversations"
        if not session_id or not store.is_dir():
            return True
        if not _CONVERSATION_RE.fullmatch(session_id):
            return False
        return (store / f"{session_id}.db").is_file()

    def with_model(self, argv: list[str], model: str) -> list[str]:
        return _drop(argv, ("--model",)) + ["--model", model]

    def with_effort(self, argv: list[str], effort: str) -> list[str]:
        if effort not in EFFORTS:
            return argv
        return _drop(argv, ("--effort",)) + ["--effort", effort]

    def with_read_only(self, argv: list[str]) -> list[str]:
        """Commands, file writes and downloads are denied by the session's own rules (deny wins over
        allow, and a configured ``--settings`` is replaced); Mesh MCP and reads stay allowed."""
        return _drop(argv, ("--settings",)) + ["--settings", _settings(READ_ONLY_PERMISSIONS)]

    def with_slot_confined(self, argv: list[str]) -> list[str]:
        raise NotImplementedError("writes are not confined to the slot by this adapter")

    # -- launch -----------------------------------------------------------------------
    def admit(self, env: dict) -> str:
        """'' when every admission command exits 0 under the launch env, else the refusal."""
        if not self.admission_commands:
            return "agy-admission-unconfigured"
        home = str(gemini_dir(env))
        for n, command in enumerate(self.admission_commands, 1):
            argv = [a.replace("{gemini_dir}", home) for a in command]
            try:
                rc = subprocess.run(argv, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, timeout=ADMISSION_TIMEOUT).returncode
            except (OSError, subprocess.TimeoutExpired):
                return f"agy-admission-{n}-unavailable"
            if rc != 0:
                return f"agy-admission-{n}-refused:{rc}"
        return ""

    def spawn(self, launch: Launch, log_path: Path):
        refused = self.admit(launch.env)
        if refused:
            raise LaunchProbeDenied(refused[:48])
        try:
            if not write_workspace_mcp(Path(launch.cwd)):
                raise LaunchProbeDenied("agy-mesh-mcp-missing")
        except OSError:
            raise LaunchProbeDenied("agy-mesh-mcp-unwritable") from None
        conversation = next((launch.argv[i + 1] for i, a in enumerate(launch.argv[:-1])
                             if a == "--conversation"), "")
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.with_suffix(".agy").write_text(json.dumps({"conversation": conversation}))
        except OSError:
            pass
        return super().spawn(launch, log_path)

    # -- result -----------------------------------------------------------------------
    @staticmethod
    def _requested(log_path: Path) -> str:
        try:
            mark = json.loads(log_path.with_suffix(".agy").read_text())
        except (OSError, ValueError):
            return ""
        return str(mark.get("conversation") or "") if isinstance(mark, dict) else ""

    def parse(self, log_path: Path) -> TurnResult:
        r = TurnResult()
        worked = False
        usage: dict = {}
        result = None
        for ev in read_events(log_path):
            kind = ev.get("event")
            if kind == "init":
                r.session_id = str(ev.get("conversation_id") or r.session_id)
                init = ev.get("init") if isinstance(ev.get("init"), dict) else {}
                r.model = str(init.get("model") or r.model)
            elif kind == "step_update" and isinstance(ev.get("step_update"), dict):
                step = ev["step_update"]
                if step.get("step_type") == "tool":
                    worked = True
                elif step.get("step_type") == "agent_response" and step.get("text_delta"):
                    worked = True
                if step.get("state") == "DONE" and isinstance(step.get("usage"), dict):
                    for src, dst in _USAGE_KEYS:
                        v = step["usage"].get(src)
                        if type(v) is int:
                            usage[dst] = usage.get(dst, 0) + v
            elif kind == "result" and isinstance(ev.get("result"), dict):
                result = ev["result"]
        try:
            stderr = log_path.with_suffix(".stderr").read_text(errors="replace")
        except OSError:
            stderr = ""
        r.usage = usage
        if result is not None:
            r.finished = True
            r.session_id = str(result.get("conversation_id") or r.session_id)
            if isinstance(result.get("response"), str):
                r.final_text = result["response"]
            status = str(result.get("status") or "")
            if status != "SUCCESS":
                r.is_error = True
                r.error_details = str(result.get("error") or "").strip() or f"turn ended with status {status or 'unknown'}"
            denied = [str(d.get("action")) for d in result.get("denied_actions") or [] if isinstance(d, dict)]
            if denied and not r.is_error:
                # not a failed turn, but the lane's permissions lacked something it tried
                r.error_details = "headless permission denied: " + ", ".join(sorted(set(denied)))
        else:
            egress = EGRESS_RE.findall(stderr)
            failed = ERROR_RE.findall(stderr)
            if egress and not r.session_id:
                r.finished = r.is_error = True
                r.error_details = "egress_unavailable: Google egress: " + egress[-1].strip()
                r.error = r.error_details[:300]
                return r                             # neither a quota nor a login: the tunnel
            if failed:
                r.finished = r.is_error = True
                r.error_details = failed[-1].strip()
        requested = self._requested(log_path)
        if requested and (NOT_FOUND_RE.search(stderr) or (r.session_id and r.session_id != requested)):
            # agy started a new conversation instead of resuming: not the task's session
            r.is_error = True
            r.error_details = f"session not found: conversation {requested} not found by agy"
            r.session_id = ""
        if r.is_error:
            detail = _typed(r.error_details or "turn failed")
            r.error_details = detail
            r.error = detail[:300]
            r.auth_failed = bool(_AUTH_RE.search(detail)) and not _LIMIT_RE.search(detail)
            r.auth_only = r.auth_failed and not worked
        return r
