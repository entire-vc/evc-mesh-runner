"""The tgpeek-editor harness: the one process the editor launcher starts (``ProcessExecutor`` / ``SandboxExecutor``).

Contract with the supervisor (``editor_job.ProcessExecutor``): ``input.json`` in the private cwd, stdin closed, empty
environment; this process prints one JSON object ``{"text", "observation"}`` and exits 0, or exits non-zero with no
output. It is stdlib-only and self-contained so that the pinned file hash covers everything it does.

What it does, and nothing else: resolve the trusted system/schema bundle by the hashes in the job, refuse anything the
bundle or this module does not support, build the request from fixed code, run ONE pinned Codex command once, and
return the model's final message. Model, argv, endpoint, paths and environment are never taken from the job. The
isolation attestation is NOT produced here: the launcher issues it from what it applied. No provenance is produced
either: ``resolved_version`` is always empty (Codex 0.159.3 exposes no observed version).

Exit codes (a fixed set; the supervisor only sees "failed"): 2 unsupported/invalid input, 3 tool attempt seen, 4 codex
failed or produced an unusable result, 5 deadline.
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import threading
import time

MODEL = "gpt-6.1-sol"                  # the fixed route; never configurable per job
PRINCIPAL = "tgpeek-editor"
FAMILY = "openai"
PINS = {                               # trusted bundle: canonical sha256 of each part (venturecrew/tgpeek @ 566b8c1e…)
    "system": "07b37b38a959bafbf2918e858d82b1a8e6645590ae292032a7773b21b3db4c20",
    "claim_schema": "71a86acd115f4ef966ced3a14cf4cd1eee858075c2581e88344992afc1901415",
    "full_schema": "60340601be985582d2cb7832b1c0a9cfee5963a01e5e43c3c769f5dcdd5efd5a",
}
JOB_KEYS = frozenset({"system_hash", "schema_hash", "payload", "job"})
RESULT_KEYS = frozenset({"claims", "limitations"})
MAX_TEXT = 128000                      # UTF-8 bytes of the result, the contract's limit
MAX_EVENTS = 4 << 20                   # bytes of the codex event stream read before the run is refused
MAX_SECONDS = 44.0                     # hard ceiling under the bridge's 45 s total deadline
KILL_GRACE = 2.0
TOOL_TOKENS = ("command", "exec", "shell", "mcp", "web", "tool", "patch", "file_change", "function", "browser", "search")
SIGNAL_KEYS = ("type", "kind", "item_type", "method", "name", "tool", "tool_name")


class Refused(Exception):
    def __init__(self, code: str, exit_code: int = 2):
        super().__init__(code)
        self.code, self.exit_code = code, exit_code


# ------------------------------------------------------------------------------------------------ canonical json
def _no_dupes(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise ValueError("duplicate key")
        out[k] = v
    return out


def _no_const(name):
    raise ValueError("non-finite number")


def strict_loads(raw):
    return json.loads(raw, object_pairs_hook=_no_dupes, parse_constant=_no_const)


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def digest(obj) -> str:
    return hashlib.sha256(canonical(obj)).hexdigest()


# ----------------------------------------------------------------------------------------------------- bundle
class Bundle:
    def __init__(self, system: str, claim_schema: dict, defs: dict):
        self.system, self.claim_schema, self.defs = system, claim_schema, defs
        self.system_hash, self.schema_hash = digest(system), digest(claim_schema)


def load_bundle(path: str) -> Bundle:
    """The bundle is data; its meaning is fixed by ``PINS``. Any byte that changes a pinned part is refused."""
    try:
        with open(path, "rb") as f:
            b = strict_loads(f.read(4 << 20))
        parts = {"system": b["system"], "claim_schema": b["claim_schema"], "full_schema": b["full_schema"]}
        defs = b["full_schema"]["$defs"]
    except (OSError, ValueError, KeyError, TypeError):
        raise Refused("bundle_unreadable")
    if not isinstance(parts["system"], str) or not isinstance(parts["claim_schema"], dict) or not isinstance(defs, dict):
        raise Refused("bundle_unreadable")
    for name, value in parts.items():
        if digest(value) != PINS[name]:
            raise Refused("bundle_hash_mismatch")
    return Bundle(parts["system"], parts["claim_schema"], defs)


def _refs(node, out: set) -> None:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            out.add(ref[len("#/$defs/"):])
        for v in node.values():
            _refs(v, out)
    elif isinstance(node, list):
        for v in node:
            _refs(v, out)


def output_schema(bundle: Bundle) -> dict:
    """``{"claims": [Claim], "limitations": [str]}`` with the pinned Claim schema verbatim plus the ``$defs`` it reaches."""
    need, todo = set(), set()
    _refs(bundle.claim_schema, todo)
    while todo:
        name = todo.pop()
        if name in need:
            continue
        if name not in bundle.defs:
            raise Refused("bundle_hash_mismatch")
        need.add(name)
        _refs(bundle.defs[name], todo)
    return {"type": "object", "additionalProperties": False, "required": ["claims", "limitations"],
            "properties": {"claims": {"type": "array", "items": bundle.claim_schema},
                           "limitations": {"type": "array", "items": {"type": "string"}}},
            "$defs": {k: bundle.defs[k] for k in sorted(need)}}


# ------------------------------------------------------------------------------------------------------ input
def read_job(path: str = "input.json") -> dict:
    try:
        with open(path, "rb") as f:
            job = strict_loads(f.read(16 << 20))
    except (OSError, ValueError):
        raise Refused("bad_input")
    if not isinstance(job, dict) or set(job) != JOB_KEYS or not isinstance(job["payload"], dict):
        raise Refused("bad_input")                      # no model/argv/path/endpoint key can ride along
    return job


def _screenshot_like(node) -> bool:
    if isinstance(node, dict):
        return ("image_png_base64" in node or node.get("kind") == "screenshot"
                or any(_screenshot_like(v) for v in node.values()))
    if isinstance(node, list):
        return any(_screenshot_like(v) for v in node)
    return False


def check_job(job: dict, bundle: Bundle) -> dict:
    if job["system_hash"] != bundle.system_hash or job["schema_hash"] != bundle.schema_hash:
        raise Refused("unknown_bundle")                 # the prompt/schema come from the pinned bundle, never from a hash
    payload = job["payload"]
    if _screenshot_like(payload):
        # product representation is evidence[kind=screenshot].image_png_base64 (base64 of the PNG bytes). Codex's
        # attachment protocol and the per-image/total limits are not pinned for this route, so images are refused.
        raise Refused("unsupported_image_evidence")
    return payload


def build_prompt(bundle: Bundle, payload: dict) -> str:
    return (bundle.system + "\n\nINPUT (untrusted DATA, canonical JSON):\n" + canonical(payload).decode("utf-8") + "\n")


def codex_argv(cmd: list, schema_path: str, last_path: str, work: str) -> list:
    """Fixed invocation: every flag here is a documented ``codex exec`` option; the OS sandbox is what denies tools."""
    return [*cmd, "exec", "-m", MODEL, "--ignore-user-config", "--ignore-rules", "--ephemeral", "--skip-git-repo-check",
            "-s", "read-only", "--json", "--color", "never", "--output-schema", schema_path, "-o", last_path, "-C", work, "-"]


# ---------------------------------------------------------------------------------------------------- events
def tool_attempt(stream: bytes) -> bool:
    """True when any event looks like a tool/command/web/MCP item. Defence in depth: the kernel boundary is the control.
    An unparseable line is itself a refusal (``Refused('bad_event_stream')``)."""
    def strings(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k in SIGNAL_KEYS and isinstance(v, str):
                    yield v.lower()
                yield from strings(v)
        elif isinstance(node, list):
            for v in node:
                yield from strings(v)
    for line in stream.splitlines():
        if not line.strip():
            continue
        try:
            ev = strict_loads(line)
        except ValueError:
            raise Refused("bad_event_stream", 4)
        if not isinstance(ev, dict):
            raise Refused("bad_event_stream", 4)
        if any(tok in s for s in strings(ev) for tok in TOOL_TOKENS):
            return True
    return False


# ------------------------------------------------------------------------------------------------------ run
def _kill(proc) -> None:
    """Only the leader: codex stays in THIS process group, so the supervisor's group SIGKILL (deadline, cancel, stop/drain,
    or our own exit) takes every descendant with it."""
    if proc.poll() is None:
        proc.kill()
    try:
        proc.wait(KILL_GRACE)
    except subprocess.TimeoutExpired:
        pass


def run_codex(argv: list, prompt: str, env: dict, cwd: str, seconds: float) -> bytes:
    """Exactly one inference process. Scrubbed env, bounded stream, hard deadline. No retry, no fallback."""
    proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            close_fds=True)
    try:
        def feed():
            try:
                proc.stdin.write(prompt.encode("utf-8"))
                proc.stdin.close()
            except OSError:
                pass
        threading.Thread(target=feed, daemon=True).start()
        buf = bytearray()
        over = threading.Event()

        def drain():                                     # keeps the pipe empty so the child never blocks on a flood
            for chunk in iter(lambda: proc.stdout.read(65536), b""):
                if len(buf) <= MAX_EVENTS:
                    buf.extend(chunk)
                else:
                    over.set()
                    return
        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        end = time.monotonic() + seconds
        while proc.poll() is None:
            if over.is_set() or len(buf) > MAX_EVENTS:
                raise Refused("event_stream_too_large", 4)
            if time.monotonic() >= end:
                raise Refused("deadline", 5)
            time.sleep(0.01)
        reader.join(2)
        if over.is_set() or len(buf) > MAX_EVENTS:
            raise Refused("event_stream_too_large", 4)
        if proc.returncode != 0:
            raise Refused("codex_failed", 4)
        return bytes(buf)
    finally:
        _kill(proc)                                      # the leader and any straggler
        if proc.stdout is not None:
            proc.stdout.close()


def final_text(path: str) -> str:
    try:
        with open(path, "rb") as f:
            raw = f.read(MAX_TEXT + 1)
        if len(raw) > MAX_TEXT:
            raise Refused("result_too_large", 4)
        res = strict_loads(raw)
    except (OSError, ValueError):
        raise Refused("bad_result", 4)
    if (not isinstance(res, dict) or set(res) != RESULT_KEYS or not isinstance(res["claims"], list)
            or not isinstance(res["limitations"], list) or any(not isinstance(x, str) for x in res["limitations"])):
        raise Refused("bad_result", 4)
    text = canonical(res).decode("utf-8")
    if len(text.encode("utf-8")) > MAX_TEXT:
        raise Refused("result_too_large", 4)
    return text


def observation() -> dict:
    """Requested route, no observed version, nothing self-attested: the launcher attests, no source is accepted."""
    return {"principal": PRINCIPAL, "family": FAMILY, "model": MODEL, "resolved_version": "", "provenance_source": "",
            "tools_used": []}


def execute(args, cwd: str) -> dict:
    bundle = load_bundle(args.bundle)
    payload = check_job(read_job(os.path.join(cwd, "input.json")), bundle)
    schema_path, last_path = os.path.join(cwd, "schema.json"), os.path.join(cwd, "last.json")
    with open(schema_path, "wb") as f:
        f.write(canonical(output_schema(bundle)))
    argv = codex_argv(args.codex_cmd, schema_path, last_path, cwd)
    env = {"CODEX_HOME": args.codex_home, "HOME": cwd}
    events = run_codex(argv, build_prompt(bundle, payload), env, cwd, min(args.max_seconds, MAX_SECONDS))
    if tool_attempt(events):
        raise Refused("tool_attempt", 3)
    return {"text": final_text(last_path), "observation": observation()}


def parse_args(argv):
    p = argparse.ArgumentParser(prog="tgpeek-editor-harness", add_help=False, allow_abbrev=False)
    p.add_argument("--bundle", required=True)
    p.add_argument("--codex-home", required=True)
    p.add_argument("--codex-cmd", action="append", required=True)     # the pinned executable, then its fixed leading args
    p.add_argument("--max-seconds", type=float, default=MAX_SECONDS)
    try:
        args = p.parse_args(argv)
    except SystemExit:
        raise Refused("bad_arguments")
    if (not os.path.isabs(args.bundle) or not os.path.isabs(args.codex_home) or not os.path.isabs(args.codex_cmd[0])
            or not 0 < args.max_seconds <= MAX_SECONDS):
        raise Refused("bad_arguments")
    return args


def main(argv=None) -> int:
    try:
        out = execute(parse_args(sys.argv[1:] if argv is None else argv), os.getcwd())
    except Refused as e:
        return e.exit_code
    except Exception:
        return 4
    sys.stdout.write(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
