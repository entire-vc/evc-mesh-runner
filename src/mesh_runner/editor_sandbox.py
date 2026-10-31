"""OS-level launch boundary for the tgpeek editor harness (macOS ``sandbox-exec``, deny-by-default).

The supervisor wraps the fixed harness command in a profile that allows only: executing the harness binary itself, reading
the declared roots and the job's private directory, and writing only that directory. Every other ``exec`` (shell, git,
curl, a second interpreter) is denied by the kernel, so the harness cannot start a tool whatever it is told. Network is
denied unless the operator opts in; the profile language cannot pin a host, so an opt-in is outbound TCP/443 only and is
reported as such. The isolation attestation is issued here, from what this launcher applied, never copied from the
harness's own output.
"""
import hashlib
import hmac
import os
import re
import stat
from pathlib import Path

from mesh_runner.editor_job import SAFE_ATTESTATION, ProcessExecutor, canonical, sha256

SANDBOX_EXEC = "/usr/bin/sandbox-exec"
# Extra executables are an allowlist of file names, not a denylist: a copied or renamed shell cannot pass by choosing
# another name, and an operator who needs another runtime must change this reviewed constant. Each one is still ONE exact
# pinned file (never a directory or family); the node script it runs is an argv file that must be pinned as well.
ALLOWED_EXTRA_NAMES = frozenset({"node", "codex"})
BASE_READ = ("/usr/lib", "/System", "/private/var/db/dyld", "/private/var/select")
WORK = "@WORK@"
_SAFE_PATH = re.compile(r"[^\x00-\x1f\"\\]+")


def _lit(path: str) -> str:
    if not _SAFE_PATH.fullmatch(path):
        raise ValueError("sandbox path has a control, quote or backslash character")
    return f'"{path}"'


def profile_template(exe: str, read_roots: tuple, allow_network: bool, extra_exec: tuple = ()) -> str:
    """The profile with the per-job directory left as ``@WORK@`` (what the launch digest pins). ``extra_exec`` are further
    exact executables the composition needs (e.g. node then the native codex): each is one literal, never a directory,
    interpreter family or shell; with none, the text (and so every existing launch digest) is unchanged."""
    execs = " ".join(f"(literal {_lit(p)})" for p in (exe, *extra_exec))
    lines = ["(version 1)", "(deny default)", "(allow process-fork)", f"(allow process-exec {execs})",
             "(allow sysctl-read)", "(allow file-read-metadata)",   # stat only: names and sizes, never contents
             "(allow file-read* (literal \"/\") (literal \"/dev/null\") (literal \"/dev/urandom\") "
             + " ".join(f"(subpath {_lit(p)})" for p in (*BASE_READ, *read_roots, WORK))
             + "".join(f" (literal {_lit(p)})" for p in extra_exec) + ")",
             f"(allow file-write* (literal \"/dev/null\") (subpath {_lit(WORK)}))"]
    if allow_network:
        lines += ["(allow network-outbound (remote tcp \"*:443\"))", "(allow system-socket)"]
    return "\n".join(lines) + "\n"


def launch_digest_of(argv: tuple, read_roots: tuple, allow_network: bool, extra_exec: tuple = ()) -> str:
    """sha256 over argv + the sandbox profile template: the value the operator pins as ``launch_digest``."""
    exe = os.path.realpath(argv[0])
    return sha256(canonical({"argv": list(argv), "profile": profile_template(exe, read_roots, allow_network, extra_exec)}))


def file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def launch_inputs(exe: str, extra_exec: tuple, argv: tuple) -> tuple:
    """Every file the launch executes or loads by name: the interpreter, the extra executables and each absolute path
    argument that is an existing file (the harness script, the bundle, a node script). All of them must be pinned."""
    args = (os.path.realpath(a) for a in argv[1:] if os.path.isabs(a) and os.path.isfile(a))
    return tuple(dict.fromkeys((exe, *extra_exec, *args)))


def _immutable_for_runner(path: str, st) -> None:
    """The runner user cannot change this file or swap any directory above it: one link only, not owned by the runner,
    and every ancestor up to ``/`` is neither owned by nor writable for the runner. Point-in-time, like the hash.
    A root runner is refused: root can write everything, so immutability cannot be shown for it (unsupported, fail closed)."""
    me = os.geteuid()
    bad = None
    if me == 0:
        raise ValueError(f"pinned file {path}: immutable inputs are not supported for a runner running as root")
    if st.st_nlink != 1:
        bad = "has more than one hard link"
    elif st.st_uid == me:
        bad = "is owned by the runner user"
    else:
        d = os.path.dirname(path)
        while True:
            if os.stat(d).st_uid == me or os.access(d, os.W_OK):
                bad = f"sits under {d}, which the runner user owns or can write"
                break
            parent = os.path.dirname(d)
            if parent == d:
                break
            d = parent
    if bad:
        raise ValueError(f"pinned file {path} must be immutable for the runner user: it {bad}")


def verify_pinned(pinned: dict, required: tuple, immutable: bool = False) -> None:
    """Startup check of the immutable inputs: every required file must be pinned, and every pinned file must exist,
    be a regular file that nobody but its owner can write, and hash to its pin. With ``immutable`` (the production mode) the
    runner user must also be unable to replace it after this check: the file is owned by someone else (group/other write
    bits are already refused) and sits in a directory the runner user cannot write.)"""
    for path in required:
        if path not in pinned:
            raise ValueError(f"{path} has no pinned sha256")
    for path, want in pinned.items():
        try:
            st = os.stat(path)
        except OSError:
            raise ValueError(f"pinned file {path} is not readable")
        if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o022:
            raise ValueError(f"pinned file {path} must be a regular file not writable by group or others")
        if immutable:
            _immutable_for_runner(path, st)
        if not hmac.compare_digest(file_sha256(path), want):
            raise ValueError(f"pinned file {path} does not match its sha256")


class SandboxExecutor(ProcessExecutor):
    def __init__(self, argv: tuple, tmp_root: Path, read_roots: tuple = (), allow_network: bool = False,
                 extra_exec: tuple = (), pinned_files: dict | None = None, require_pins: bool = False,
                 immutable_pins: bool = False, allowed_extra_names: frozenset = ALLOWED_EXTRA_NAMES, **kw):
        if not argv or not os.path.isabs(argv[0]):
            raise ValueError("sandboxed harness argv[0] must be an absolute path")
        if not os.access(SANDBOX_EXEC, os.X_OK):
            raise ValueError("sandbox-exec is not available on this host")
        self.read_roots = tuple(os.path.realpath(p) for p in read_roots)
        self.allow_network = bool(allow_network)
        self.exe = os.path.realpath(argv[0])
        if any(not os.path.isabs(p) for p in extra_exec):
            raise ValueError("extra executables must be absolute paths")
        self.extra_exec = tuple(os.path.realpath(p) for p in extra_exec)
        for p in self.extra_exec:
            if os.path.basename(p) not in allowed_extra_names:
                raise ValueError(f"{p} is not an allowed extra executable name ({', '.join(sorted(allowed_extra_names))})")
        if len(set(self.extra_exec) | {self.exe}) != len(self.extra_exec) + 1 or any(os.path.isdir(p) for p in self.extra_exec):
            raise ValueError("extra executables must be distinct files")
        pins = {os.path.realpath(p): h for p, h in (pinned_files or {}).items()}
        if require_pins or pinned_files is not None or self.extra_exec:
            verify_pinned(pins, launch_inputs(self.exe, self.extra_exec, tuple(argv)), immutable_pins)
        super().__init__(argv, tmp_root, **kw)
        self.launch_digest = launch_digest_of(tuple(argv), self.read_roots, self.allow_network, self.extra_exec)

    def command(self, work: Path) -> list:
        prof = profile_template(self.exe, self.read_roots, self.allow_network, self.extra_exec).replace(WORK, os.path.realpath(work))
        args = [os.path.realpath(a) if os.path.isabs(a) and os.path.isfile(a) else a for a in self.argv[1:]]   # the pinned file, not a symlink to it
        return [SANDBOX_EXEC, "-p", prof, self.exe, *args]   # resolved binary: the profile allows that path, not a symlink to it

    def supervise(self, ob):
        ob.attestation = dict(SAFE_ATTESTATION, launch_digest=self.launch_digest)   # issued by the launcher, not the harness
        return ob
