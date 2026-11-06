"""Runner side of the Codex launch-context probe (private stdin protocol, version 1).

The runner hands the exact post-shell launch context of a native Codex child to one
helper process (``scripts/codex_launch_probe.py`` in the fleet repository) over a
private anonymous pipe: one JSON document (<= 131072 bytes) then EOF. Nothing of the
context is ever placed in arguments, logs, the store or the reply. The helper answers
with one small JSON line holding only opaque identity (account_ref, quota_bucket).

An observation is evidence, never authority: it does not mint a grant and does not
make ACCOUNT_CAPACITY_READY true. The start gate decides; the runner only carries the
opaque facts to it, and re-observes the exact final context right before native exec.
Every malformed, late, foreign or unclean answer is ``account_unknown`` (fail closed).
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import secrets
import select
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

VERSION = 1                     # default protocol
VERSIONS = (1, 2)               # v2 (opt-in) adds primary/secondary used percent to the reply
MAX_REQUEST = 131072
MAX_REPLY = 4096
DEADLINE_SECONDS = 8.0          # one shared deadline: spawn + write + read + cleanup
CLEANUP_SECONDS = 1.5           # reserved inside the deadline for the group kill

STATUSES = ("available", "quota_blocked", "account_unknown")
# Fixed ProbeReason enum: equals bob scripts/codex_account_probe.py ProbeReason (helper e54bad9).
REASONS = frozenset({
    "unspecified", "available", "quota_blocked", "invalid_request", "scope_invalid", "scope_changed",
    "spawn_failed", "transport_error", "timeout", "rpc_error", "malformed_response",
    "account_type_unknown", "account_id_invalid", "target_limit_missing", "target_limit_invalid",
    "target_limit_ambiguous", "usage_permission_unknown", "window_percent_invalid",
    "spend_control_invalid", "snapshot_identity_mismatch", "cleanup_failed",
})
# Reasons the runner itself assigns when it never got a valid answer.
LOCAL = {"timeout": "timeout", "error": "transport_error", "invalid": "invalid_request",
         "cleanup": "cleanup_failed"}
_REPLY_KEYS = {"version", "nonce", "status", "reason", "account_ref", "quota_bucket",
               "observed_at", "cleanup_ok"}
_REF = re.compile(r"[0-9a-f]{64}\Z")
_NONCE = re.compile(r"[0-9a-f]{32}")


def _pct(value: int | None) -> str:
    return "" if value is None else str(value)


@dataclasses.dataclass(frozen=True)
class Observation:
    status: str
    reason: str
    account_ref: str = ""
    quota_bucket: str = ""
    observed_at: str = ""
    cleanup_ok: bool = False
    fingerprint: str = ""        # sha256 of the observed context (opaque; never the context)
    primary_used_percent: int | None = None      # v2 only; None = not measured, never a synthesized 0
    secondary_used_percent: int | None = None

    @property
    def available(self) -> bool:
        return self.status == "available" and self.cleanup_ok

    def gate_env(self) -> dict[str, str]:
        """Opaque, non-secret facts for the start gate. Carries no grant."""
        return {"MESH_RUNNER_LAUNCH_STATUS": self.status, "MESH_RUNNER_LAUNCH_REASON": self.reason,
                "MESH_RUNNER_LAUNCH_ACCOUNT_REF": self.account_ref,
                "MESH_RUNNER_LAUNCH_QUOTA_BUCKET": self.quota_bucket,
                "MESH_RUNNER_LAUNCH_OBSERVED_AT": self.observed_at,
                "MESH_RUNNER_LAUNCH_CLEANUP_OK": "1" if self.cleanup_ok else "0",
                "MESH_RUNNER_LAUNCH_FINGERPRINT": self.fingerprint,
                "MESH_RUNNER_LAUNCH_PRIMARY_USED_PERCENT": _pct(self.primary_used_percent),
                "MESH_RUNNER_LAUNCH_SECONDARY_USED_PERCENT": _pct(self.secondary_used_percent)}


def unknown(reason: str, fingerprint: str = "") -> Observation:
    return Observation("account_unknown", reason, fingerprint=fingerprint)


def profile_of(argv: list[str]) -> str:
    """The Codex ``--profile`` the argv selects ("" = none)."""
    for i, item in enumerate(argv):
        if item in ("--profile", "-p") and i + 1 < len(argv):
            return argv[i + 1]
        if item.startswith("--profile="):
            return item.split("=", 1)[1]
    return ""


def codex_home_of(env: dict[str, str]) -> str:
    home = env.get("CODEX_HOME") or os.path.join(env.get("HOME") or str(Path.home()), ".codex")
    return os.path.abspath(home)


def build_context(argv: list[str], env: dict[str, str], cwd: str | os.PathLike) -> dict:
    """The exact context of a native child: what Popen will receive, nothing constructed.

    ``executable`` is argv[0] resolved the way exec resolves it (PATH of that env)."""
    if not argv or not all(isinstance(x, str) for x in argv):
        raise ValueError("argv")
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
        raise ValueError("env")
    exe = argv[0]
    if os.sep not in exe:
        exe = shutil.which(exe, path=env.get("PATH") or os.defpath) or ""
    elif not os.path.isabs(exe):
        exe = os.path.abspath(os.path.join(str(cwd), exe))
    if not exe or not os.path.isabs(exe):
        raise ValueError("executable")
    return {"executable": exe, "argv": list(argv), "codex_home": codex_home_of(env),
            "cwd": os.path.abspath(str(cwd)), "profile": profile_of(argv), "env": dict(env)}


def fingerprint(context: dict) -> str:
    """Opaque identity of the context, also binding the executable's file identity so a
    swapped binary or edited config is a different launch."""
    parts = [json.dumps(context, sort_keys=True, separators=(",", ":"))]
    for path in (context["executable"], os.path.join(context["codex_home"], "config.toml")):
        try:
            st = os.stat(path)
            parts.append(f"{os.path.realpath(path)}:{st.st_dev}:{st.st_ino}:{st.st_size}:{st.st_mtime_ns}")
        except OSError:
            parts.append(f"{path}:absent")
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()


def _validate(reply: object, nonce: str, version: int = VERSION) -> Observation | None:
    keys = _REPLY_KEYS | ({"primary_used_percent", "secondary_used_percent"} if version == 2 else set())
    if (not isinstance(reply, dict) or set(reply) != keys or type(reply["version"]) is not int
            or reply["version"] != version or reply["nonce"] != nonce
            or type(reply["status"]) is not str or reply["status"] not in STATUSES
            or type(reply["reason"]) is not str or reply["reason"] not in REASONS
            or not (reply["cleanup_ok"] is None or type(reply["cleanup_ok"]) is bool)):
        return None
    ref, bucket, seen = reply["account_ref"], reply["quota_bucket"], reply["observed_at"]
    if type(seen) is not int or not 0 <= seen < 2**63:
        return None
    pct = (None, None)
    if version == 2:
        pct = (reply["primary_used_percent"], reply["secondary_used_percent"])
        if any(v is not None and (type(v) is not int or not 0 <= v <= 100) for v in pct):
            return None
    if reply["status"] == "account_unknown":
        if ref is not None or bucket is not None or pct != (None, None):
            return None            # an unknown account carries no identity and no measurement
        return Observation("account_unknown", reply["reason"], "", "", str(seen), reply["cleanup_ok"] is True)
    expected = {"available": "available", "quota_blocked": "quota_blocked"}[reply["status"]]
    if (reply["reason"] != expected or reply["cleanup_ok"] is not True or not isinstance(ref, str)
            or not _REF.fullmatch(ref) or bucket != f"openai:{ref}:codex"):
        return None
    return Observation(reply["status"], reply["reason"], ref, bucket, str(seen), True,
                       primary_used_percent=pct[0], secondary_used_percent=pct[1])


def _kill_group(proc: subprocess.Popen) -> bool:
    """Kill the helper's whole group and reap it; False when anything stays alive."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=CLEANUP_SECONDS)
    except subprocess.TimeoutExpired:
        return False
    try:
        os.killpg(proc.pid, 0)
        return False               # a descendant of the group is still there
    except ProcessLookupError:
        return True
    except PermissionError:
        return False


def observe(helper: list[str], context: dict, *, deadline: float = DEADLINE_SECONDS,
            version: int = VERSION) -> Observation:
    """One bounded helper run. Never raises; the context never leaves the pipe."""
    if version not in VERSIONS:
        return unknown(LOCAL["invalid"])
    try:
        fp = fingerprint(context)
        nonce = secrets.token_hex(16)
        body = json.dumps({"version": version, "nonce": nonce, "context": context},
                          separators=(",", ":"), ensure_ascii=False).encode()
    except (ValueError, TypeError, KeyError, OSError):
        return unknown(LOCAL["invalid"])
    if not helper or len(body) > MAX_REQUEST:
        return unknown(LOCAL["invalid"], fp)
    end = time.monotonic() + deadline
    try:
        proc = subprocess.Popen(helper, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, start_new_session=True,
                                env=_helper_env(), close_fds=True)
    except (OSError, ValueError):
        return unknown(LOCAL["error"], fp)
    assert proc.stdin is not None and proc.stdout is not None
    reason, reply = "", b""
    try:
        try:
            _write(proc, body, end - CLEANUP_SECONDS)
        except TimeoutError:
            reason = LOCAL["timeout"]
        except OSError:
            reason = LOCAL["error"]
        if not reason:
            try:
                reply = _read(proc, end - CLEANUP_SECONDS)
            except TimeoutError:
                reason = LOCAL["timeout"]
            except ValueError:
                reason = LOCAL["invalid"]
        if not reason:
            try:
                code = proc.wait(timeout=max(0.05, end - CLEANUP_SECONDS - time.monotonic()))
            except subprocess.TimeoutExpired:
                reason = LOCAL["timeout"]
            else:
                if code != 0:
                    reason = LOCAL["error"]
    finally:
        clean = _kill_group(proc)
        for pipe in (proc.stdin, proc.stdout):
            try:
                pipe.close()
            except OSError:
                pass
    if not clean:
        return unknown(LOCAL["cleanup"], fp)
    if reason:
        return unknown(reason, fp)
    try:
        obs = _validate(json.loads(reply, object_pairs_hook=_unique), nonce, version)
    except (ValueError, TypeError, KeyError, RecursionError):
        obs = None
    if obs is None:
        return unknown(LOCAL["invalid"], fp)
    return dataclasses.replace(obs, fingerprint=fp)


def _unique(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate key")
        out[key] = value
    return out


def _helper_env() -> dict[str, str]:
    """The helper gets no lane secrets in its own environment: the context is its input."""
    return {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "TMPDIR")}


def _write(proc: subprocess.Popen, body: bytes, until: float) -> None:
    fd = proc.stdin.fileno()
    os.set_blocking(fd, False)
    view = memoryview(body)
    while view:
        left = until - time.monotonic()
        if left <= 0:
            raise TimeoutError
        _, ready, _ = select.select([], [fd], [], left)
        if not ready:
            raise TimeoutError
        try:
            view = view[os.write(fd, view[:65536]):]
        except BlockingIOError:
            continue
    proc.stdin.close()               # EOF ends the request


def _read(proc: subprocess.Popen, until: float) -> bytes:
    fd = proc.stdout.fileno()
    chunks, size = [], 0
    while True:
        left = until - time.monotonic()
        if left <= 0:
            raise TimeoutError
        ready, _, _ = select.select([fd], [], [], left)
        if not ready:
            raise TimeoutError
        data = os.read(fd, 1024)
        if not data:
            break
        size += len(data)
        if size > MAX_REPLY:
            raise ValueError("oversize")
        chunks.append(data)
    return b"".join(chunks)


def helper_command(raw: list[str] | None, version: int = VERSION) -> list[str]:
    """Configured helper argv; the interpreter is the runner's own when it is a script path.
    Protocol v2 is selected by the helper's ``--measurements-v2`` flag (v1 stays flagless)."""
    raw = list(raw or [])
    if raw and raw[0].endswith(".py") and os.path.isabs(raw[0]):
        raw = [sys.executable, *raw]     # the helper imports its sibling scripts; needs Python >= 3.11
    if version == 2 and raw and "--measurements-v2" not in raw:
        raw.append("--measurements-v2")
    return raw
