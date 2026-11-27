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
usage is the sum of its own steps. The context one model call carried is that step's ``input_tokens``
plus its ``cache_read_tokens`` (the cached part is not inside the input); the largest of the turn is
``peak_context``, which the runner's context control reads (``agy_context_rotate_tokens``). A headless permission the settings do not grant is soft-denied:
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
adapter writes before each start from the lane's ``.mcp.json``: the Mesh servers, plus the stdio servers
the lane names in ``mcp_allow`` (exact names; none by default). A read-only turn denies the tools of
those named servers in its own ``--settings``: the workspace file is shared by every session of the lane.

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
# a server name a lane may allow (exact, no pattern): also safe inside a permission rule mcp(<name>/*)
MCP_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
ADMISSION_TIMEOUT = 30
# Every action a lane turn takes; deny stays explicit. Read-only turns deny commands and writes.
LANE_PERMISSIONS = {"allow": ["command(*)", "read_file(*)", "write_file(*)", "read_url(*)",
                              "download_url(*)", "mcp(*)"], "deny": []}
READ_ONLY_PERMISSIONS = {"allow": ["read_file(*)", "read_url(*)", "mcp(*)"],
                         "deny": ["command(*)", "write_file(*)", "download_url(*)"]}
# output_tokens already counts thinking_tokens (on every recorded step total = input + output and
# thinking <= output), which matches Gemini billing thinking as output; thinking is kept for display only
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


def _stdio_servers(path: Path) -> dict:
    """The enabled stdio servers of an MCP config file (``mcpServers``), each reduced to ``command``,
    ``args``, ``env``, ``cwd``; {} when none or unreadable. A remote (``url``) server is not one."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    out = {}
    for name, spec in (servers.items() if isinstance(servers, dict) else ()):
        if (isinstance(spec, dict) and isinstance(spec.get("command"), str) and spec["command"]
                and not spec.get("disabled") and not spec.get("url") and not spec.get("serverUrl")):
            out[str(name)] = {k: spec[k] for k in ("command", "args", "env", "cwd") if k in spec}
    return out


def mesh_servers(path: Path) -> dict:
    """The Mesh MCP servers (stdio) of an MCP config file (``mcpServers``), {} when none or unreadable."""
    return {name: spec for name, spec in _stdio_servers(path).items() if _is_mesh(spec)}


def allowed_servers(path: Path, allow) -> dict:
    """The stdio servers of ``path`` whose names ``allow`` lists exactly (no pattern, no reading of the
    executable: the name in the lane's own ``.mcp.json`` is what the lane config allows)."""
    names = {n for n in allow or () if isinstance(n, str) and MCP_NAME_RE.fullmatch(n)}
    return {name: spec for name, spec in _stdio_servers(path).items() if name in names}


def _spare_mesh(argv: list[str], mesh) -> list[str]:
    """``argv`` whose ``--settings`` deny no Mesh server: a read-only turn denies the tools of the
    ``mcp_allow`` servers, and a name there that is a Mesh server of this workspace must stay usable."""
    spared = {f"mcp({name}/*)" for name in mesh}
    out = list(argv)
    for i, a in enumerate(out):
        value, prefix = ((out[i + 1], "") if a == "--settings" and i + 1 < len(out) else
                         (a[len("--settings="):], "--settings=") if a.startswith("--settings=") else (None, ""))
        if value is None:
            continue
        try:
            settings = json.loads(value)
            deny = settings["permissions"]["deny"]
        except (ValueError, TypeError, KeyError):
            continue
        if not isinstance(deny, list) or not spared.intersection(d for d in deny if isinstance(d, str)):
            continue
        settings["permissions"]["deny"] = [d for d in deny if d not in spared]
        body = prefix + json.dumps(settings, separators=(",", ":"))
        if prefix:
            out[i] = body
        else:
            out[i + 1] = body
    return out


def write_workspace_mcp(cwd: Path, allow=()) -> bool:
    """``<cwd>/.agents/mcp_config.json`` holds the Mesh servers of the lane's ``<cwd>/.mcp.json`` plus
    the stdio servers of that ``.mcp.json`` named in ``allow`` (written 0600, atomically, only when it
    differs). Without a Mesh server there, the Mesh servers of an existing workspace file are kept; an
    allowed server comes from ``.mcp.json`` only, never from the existing file, and every other server is
    dropped: the session never sees a server the filter refused. True when the workspace has a Mesh
    server; an allowed server alone is not one."""
    target = cwd / ".agents" / "mcp_config.json"
    source = cwd / ".mcp.json"
    mesh = mesh_servers(source) or mesh_servers(target)
    if not mesh:
        return False
    servers = {**allowed_servers(source, allow), **mesh}
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
                 admission_commands: list[list[str]] | None = None, mcp_allow: list[str] | None = None):
        super().__init__(binary, extra_args)
        self.shell = shell
        self.admission_commands = [[str(x) for x in c] for c in admission_commands or [] if c]
        bad = [n for n in mcp_allow or [] if not isinstance(n, str) or not MCP_NAME_RE.fullmatch(n)]
        if bad:
            raise ValueError(f"mcp_allow: not a server name: {bad[0]!r}")
        self.mcp_allow = sorted(set(mcp_allow or []))

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
        allow, and a configured ``--settings`` is replaced); Mesh MCP and reads stay allowed. The tools
        of the lane's ``mcp_allow`` servers are denied too: the shared workspace file still lists them.
        A listed name that is a Mesh server of the workspace is spared again at ``spawn``."""
        rules = {"allow": list(READ_ONLY_PERMISSIONS["allow"]),
                 "deny": list(READ_ONLY_PERMISSIONS["deny"]) + [f"mcp({n}/*)" for n in self.mcp_allow]}
        return _drop(argv, ("--settings",)) + ["--settings", _settings(rules)]

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
            if not write_workspace_mcp(Path(launch.cwd), self.mcp_allow):
                raise LaunchProbeDenied("agy-mesh-mcp-missing")
        except OSError:
            raise LaunchProbeDenied("agy-mesh-mcp-unwritable") from None
        if self.mcp_allow:
            # a read-only turn denies the allowed servers by name; never the Mesh server it requires
            mesh = mesh_servers(Path(launch.cwd) / ".agents" / "mcp_config.json")
            argv = _spare_mesh(launch.argv, mesh)
            if argv != launch.argv:
                launch = Launch(argv, launch.cwd, launch.env, launch.prompt, launch.on_created, launch.gate_path)
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
                    # the context this one call carried: its fresh input plus the implicit cache read
                    # (input_tokens does not include the cached part)
                    context = sum(v for v in (step["usage"].get("input_tokens"),
                                              step["usage"].get("cache_read_tokens")) if type(v) is int)
                    r.peak_context = max(r.peak_context, context)
            elif kind == "result" and isinstance(ev.get("result"), dict):
                result = ev["result"]
        try:
            stderr = log_path.with_suffix(".stderr").read_text(errors="replace")
        except OSError:
            stderr = ""
        if "cache_read_input_tokens" in usage:
            # Gemini caches implicitly only: no explicit cache, no write price, so the part of the
            # input written to the cache at a price of its own is 0 (an unknown read stays unknown)
            usage["cache_creation_input_tokens"] = 0
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
