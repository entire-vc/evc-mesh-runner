"""Warm pool of git worktrees, one writable checkout per attempt.

Each configured repository has a base clone owned by the operator. The pool
adds worktrees under ``<root>/<repo>/slot-<n>`` and reuses them: ignored
directories (dependency installs, build caches) survive between attempts,
which is what makes a slot "warm".

Safety rules:
- a slot with uncommitted changes (or that git cannot inspect) is ``held`` with
  its reason and never wiped; held slots are re-checked and freed once clean;
- commits are never lost: branches are refs shared by all worktrees of the
  base clone and survive detaching; commits on a detached HEAD that the remote
  does not have are saved under ``refs/heads/runner-keep/<attempt>-<slot>``
  before the slot is returned;
- a held slot whose work can no longer resume (see ``archive_and_free``) is
  freed only after its uncommitted state is committed onto a never-pushed ref
  under ``refs/mesh-runner/archive/``;
- a returned slot is detached at the remote branch tip, so it pins nothing;
- two open attempts never share a slot; slots belong to one agent's pool;
- a slot is used only while its ``.git`` file leads to its own admin dir under the base clone's
  ``worktrees/`` (checked before any git command runs in it); new slots get an owner-prefixed admin
  id, so pools sharing one base clone never hand each other an admin id.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .store import OPEN, Store, iso

log = logging.getLogger("mesh_runner")

_BASE_LOCKS: dict[str, "TimedLock"] = {}
_BASE_LOCKS_GUARD = threading.Lock()


_COMMON_DIRS: dict[str, str] = {}

# A failed or interrupted eviction has an uncertain filesystem outcome. Keep
# this durable marker until an operator explicitly inspects the slot.
POOL_EVICTION_HOLD_NOTE = "pool eviction requires operator inspection"
POOL_INDEX_FLAGS_HOLD_NOTE = "index flags require operator inspection"
# The slot's link to its git admin dir (<common>/worktrees/<id>) is missing or points at an admin dir
# that belongs to another worktree: git in this slot would read and write someone else's HEAD and index.
POOL_ADMIN_HOLD_NOTE = "worktree admin link requires operator inspection"
DIRTY_HOLD_NOTE = "uncommitted changes"        # the hold reason archive_and_free can resolve
ARCHIVE_REF_PREFIX = "refs/mesh-runner/archive"  # outside refs/heads and refs/tags: no default push or fetch rule names it


def base_lock(base: Path, *, timeout: float = 600) -> "TimedLock":
    """One lock per repository for this process. Agents tick in parallel threads and
    two of them may pool the same repository — through one clone, or through two
    linked worktrees of it: git takes no retry on its config and ref locks, so
    fetch, worktree add, ref updates and config writes are serialised. The key is
    the repository's common git dir."""
    path = os.path.realpath(base)
    with _BASE_LOCKS_GUARD:
        key = _COMMON_DIRS.get(path)
    if key is None:
        try:
            common = git(Path(path), "rev-parse", "--git-common-dir", timeout=timeout)
            key = os.path.realpath(os.path.join(path, common))
            with _BASE_LOCKS_GUARD:
                _COMMON_DIRS[path] = key        # only a real resolution is remembered
        except WorkspaceError:
            key = path
    with _BASE_LOCKS_GUARD:
        return _BASE_LOCKS.setdefault(key, TimedLock(f"base:{os.path.basename(key)}"))


SCHEMA = """
CREATE TABLE IF NOT EXISTS slots (
    path TEXT PRIMARY KEY,
    pool TEXT NOT NULL,                    -- <agent>/<repo>
    attempt TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'free',    -- free | leased | held
    note TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
"""


class WorkspaceError(Exception):
    pass


class CheckpointSuspendError(WorkspaceError):
    """A failed suspension, bound to the exact generation it tried to release."""

    def __init__(self, cause: WorkspaceError, binding: dict):
        super().__init__(str(cause))
        self.binding = binding


class MaintenanceHeld(WorkspaceError):
    """An open maintenance window closes this operation; nothing was changed."""


class InventoryError(WorkspaceError):
    """The attempt's repository inventory cannot be proven; only a config/journal change heals it."""


class BranchInUse(WorkspaceError):
    """A checkpoint's branch is checked out in another worktree of the same base repository.

    ``live`` is True when a runner slot leased by a live attempt holds it: the restore must wait for
    that attempt (``holder_attempt``) to end or release the slot. It is not a failure of the restore.
    When False the holder is a stale slot that could not be cleared (uncommitted changes)."""

    def __init__(self, message: str, *, branch: str, holder_attempt: str = "", holder_path: str = "",
                 live: bool = True):
        super().__init__(message)
        self.branch, self.holder_attempt, self.holder_path, self.live = branch, holder_attempt, holder_path, live


class TimedLock:
    """A reentrant lock whose `with` form gives up instead of waiting forever. A slot stuck in
    filesystem IO (an unanswered access prompt, a hung mount) holds the lock for every other
    lane sharing the repository; a bounded wait turns that into a WorkspaceError for the one
    waiter, with the holder named, instead of a frozen tick."""

    def __init__(self, name: str, timeout: float = 900.0):
        self._lock = threading.RLock()
        self._name = name
        self._timeout = float(os.environ.get('MESH_RUNNER_LOCK_TIMEOUT', timeout))
        self._holder = ''

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        ok = self._lock.acquire(blocking, timeout)
        if ok:
            self._holder = threading.current_thread().name
        return ok

    def release(self) -> None:
        self._lock.release()

    def _is_owned(self) -> bool:
        return self._lock._is_owned()

    def __enter__(self):
        if not self._lock.acquire(timeout=self._timeout):
            raise WorkspaceError(f"lock {self._name} not acquired in {self._timeout:.0f}s; "
                                 f"last holder: {self._holder or 'unknown'}")
        self._holder = threading.current_thread().name
        return self

    def __exit__(self, *exc) -> None:
        self._lock.release()


@dataclass(frozen=True)
class RepoSpec:
    name: str
    base: Path               # operator's clone; only fetch and worktree metadata are touched
    remote: str = "origin"
    branch: str = "main"
    max_slots: int = 4
    # Exact, operator-approved public URL -> canonical internal fetch URL.
    # Initialized child remotes only: never the parent transport, Git config
    # files or the harness environment.
    fetch_url_overrides: tuple[tuple[str, str], ...] = ()


def _route_identity(url: str) -> tuple[str, int | None, str]:
    """Credential-free Git transport identity; never echo invalid input."""
    if re.search(r"[\s\x00-\x1f]", url):
        raise WorkspaceError("invalid fetch URL override")
    if re.fullmatch(r"git@[^/:]+:[^\s]+", url):
        url = "ssh://" + url.replace(":", "/", 1)
    try:
        u = urlsplit(url)
        if (u.scheme not in ("https", "ssh") or not u.hostname or u.password
                or u.username not in (None, "git") or u.query or u.fragment
                or not u.path.endswith(".git") or ".." in u.path.split("/")):
            raise ValueError()
        return u.hostname, u.port or (443 if u.scheme == "https" else 22), u.path
    except ValueError:
        raise WorkspaceError("invalid fetch URL override") from None


def _fetch_ref_cas_only(stderr: str) -> bool:
    """Recognise only a concurrent ref update, never generic lock/fetch failures.

    Use the full diagnostic before the caller truncates it. Mixed failures (for
    example an auth or missing-submodule-ref error) must retain their usual path.
    """
    found = False
    for line in stderr.splitlines():
        if not line.strip():
            continue
        match = re.fullmatch(r"error: cannot lock ref 'refs/[^'\s]+': is at "
                             r"([0-9a-f]{40}|[0-9a-f]{64}) but expected "
                             r"([0-9a-f]{40}|[0-9a-f]{64})", line)
        if match:
            if len(match[1]) != len(match[2]) or match[1] == match[2]:
                return False
            found = True
        elif not (line.startswith("From ") or re.fullmatch(
                r"\s*!\s+.+ -> \S+\s+\(unable to update local ref\)", line)):
            return False
    return found


def git(cwd: Path, *args: str, check: bool = True, env: dict[str, str] | None = None,
        timeout: int = 600) -> str:
    env = dict(env if env is not None else os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"            # never block the loop on a credential prompt
    deadline = time.monotonic() + timeout
    for attempt in range(2):
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired("git", timeout)
            res = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                                 errors="surrogateescape", timeout=remaining, env=env)
        except subprocess.TimeoutExpired:
            raise WorkspaceError(f"git {' '.join(args[:2])} in {cwd}: timed out") from None
        if (attempt == 0 and check and args and args[0] == "fetch"
                and res.returncode != 0 and _fetch_ref_cas_only(res.stderr)):
            # base_lock serialises our threads; an external writer can still win
            # ref CAS. Retry the identical fetch once, within the original budget.
            continue
        break
    if check and res.returncode != 0:
        raise WorkspaceError(f"git {' '.join(args[:3])} in {cwd}: {res.stderr.strip()[:300]}")
    return res.stdout.strip()



def _copy_file_fd(src: str, dfd: int, name: str) -> None:
    """Copy one regular file to name under the open directory dfd (exclusive create, no symlink follow)."""
    sfd = os.open(src, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        st = os.fstat(sfd)
        out = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=dfd)
        try:
            while chunk := os.read(sfd, 1 << 20):
                view = memoryview(chunk)
                while view:
                    view = view[os.write(out, view):]
            os.fchmod(out, stat.S_IMODE(st.st_mode))
            os.utime(out, ns=(st.st_atime_ns, st.st_mtime_ns))
        finally:
            os.close(out)
    finally:
        os.close(sfd)


def _rename_noreplace(src_fd: int, src: str, dst_fd: int, dst: str) -> None:
    """Atomic rename that fails with FileExistsError instead of replacing an existing destination
    (renameatx_np RENAME_EXCL on macOS, renameat2 RENAME_NOREPLACE on Linux). No such primitive -> refuse."""
    import ctypes
    import ctypes.util
    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    if sys.platform == "darwin":
        fn, flag = getattr(libc, "renameatx_np", None), 0x4
    else:
        fn, flag = getattr(libc, "renameat2", None), 1
    if fn is None:
        raise WorkspaceError("no atomic no-replace rename on this platform; restore refused")
    fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    if fn(src_fd, os.fsencode(src), dst_fd, os.fsencode(dst), flag) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), dst)


def _safe_rel(parts) -> Path:
    """A manifest path must be a non-empty list of plain names: no '', '.', '..', separators or NUL."""
    if (not isinstance(parts, list) or not parts
            or any(not isinstance(p, str) or p in ("", ".", "..") or "/" in p or "\0" in p for p in parts)):
        raise WorkspaceError(f"unsafe path in quarantine manifest: {parts!r}")
    return Path(*parts)


def _entry_digest(path: str) -> dict:
    """Type + content/symlink checksums of one entry, for the quarantine manifest."""
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode):
        return {"t": "symlink", "target": os.readlink(path)}
    if stat.S_ISDIR(st.st_mode):
        return {"t": "dir", "children": {e.name: _entry_digest(e.path) for e in sorted(os.scandir(path), key=lambda e: e.name)}}
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return {"t": "file", "sha256": h.hexdigest(), "size": st.st_size}


def _digest_ok(d) -> bool:
    """Shape of one _entry_digest() result, checked recursively."""
    if not isinstance(d, dict):
        return False
    t = d.get("t")
    if t == "file":
        sha = d.get("sha256")
        return isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{64}", sha) is not None \
            and isinstance(d.get("size"), int) and not isinstance(d["size"], bool) and d["size"] >= 0
    if t == "symlink":
        return isinstance(d.get("target"), str)
    if t == "dir":
        kids = d.get("children")
        return isinstance(kids, dict) and all(isinstance(n, str) and n not in ("", ".", "..") and "/" not in n
                                              and "\0" not in n and _digest_ok(c) for n, c in kids.items())
    return False


def _manifest_ok(doc) -> bool:
    """Full schema of a quarantine manifest; anything else is treated as unreadable."""
    if not isinstance(doc, dict) or doc.get("state") not in ("renaming", "complete", "recovered", "restored"):
        return False
    if not all(isinstance(doc.get(k), str) for k in ("slot", "owner", "attempt")):
        return False
    arch = doc.get("archive")
    if not (isinstance(arch, dict) and isinstance(arch.get("root"), str) and isinstance(arch.get("dir"), str)):
        return False
    entries = doc.get("entries")
    if not isinstance(entries, list):
        return False
    for e in entries:
        if not (isinstance(e, dict) and _digest_ok(e.get("digest"))):
            return False
        try:
            _safe_rel(e.get("path"))
        except WorkspaceError:
            return False
    return True


class WorkspacePool:
    def __init__(self, store: Store, root: Path, repos: list[RepoSpec], identity: tuple[str, str] | None = None,
                 env: dict[str, str] | None = None, owner: str = "default", min_free_gb: float = 0,
                 checkpoint_exclude_names: tuple | None = None,
                 archive_root: Path | None = None, archive_require_mount: Path | None = None):
        # env: the agent's environment, so fetches use its credentials (e.g. GIT_SSH_COMMAND)
        self.store, self.identity, self.env, self.owner = store, identity, env, owner
        self.min_free_gb = min_free_gb
        # archive_root None = legacy <pool root>/../slot-archive. archive_require_mount: the archive must
        # sit on that mounted volume (fail closed otherwise); only meaningful with archive_root.
        self.archive_root = Path(archive_root) if archive_root else None
        self.archive_require_mount = Path(archive_require_mount) if archive_require_mount else None
        self.checkpoint_exclude_names = checkpoint_exclude_names
        self.root = Path(root) / owner
        self.repos = {r.name: r for r in repos}
        store.db.executescript(SCHEMA)

    def disk_hold(self) -> str:
        """No mutation; OFF by default. Check the filesystem hosting the pool."""
        if self.min_free_gb <= 0 or not self.repos:
            return ""
        path = self.root
        while not path.exists() and path != path.parent:
            path = path.parent
        try:
            free = shutil.disk_usage(path).free / 1024**3
        except OSError:
            return "worktree disk availability unknown"
        return (f"worktree disk below floor ({free:.1f} GiB free; need {self.min_free_gb:g} GiB)"
                if free < self.min_free_gb else "")

    def _check_disk(self) -> None:
        if reason := self.disk_hold():
            raise WorkspaceError(reason)

    def _pool(self, repo: str) -> str:
        return f"{self.owner}/{repo}"

    # -- inspection ------------------------------------------------------------------
    def unsafe_reason(self, path: Path, repo: RepoSpec) -> str:
        """Why the slot may NOT be returned; empty when it is safe."""
        try:
            dotgit = path / ".git"
            if not dotgit.exists() and not dotgit.is_symlink():
                return "missing"
            # Before any git command in the slot: with a foreign admin dir git would succeed
            # through another worktree's HEAD and index.
            if why := self._admin_reason(path, repo):
                return f"{POOL_ADMIN_HOLD_NOTE}: {why}"
            if self.index_flags_reason(path):
                return "index flags may hide tracked changes"
            # Untracked scratch (evidence, logs) is archived on return, not a reason to keep
            # the slot: it held 11 of 12 slots of one lane forever. Tracked edits still hold.
            if git(path, "status", "--porcelain", "--untracked-files=no", env=self.env):
                return DIRTY_HOLD_NOTE
        except (WorkspaceError, UnicodeError) as e:
            return f"cannot verify: {e}"
        return ""

    def index_flags_reason(self, path: Path, *, timeout: float = 600) -> str:
        """Fail closed when Git index flags could hide tracked working-tree edits."""
        entries = git(path, "ls-files", "-v", "-z", timeout=timeout, env=self.env).split("\0")
        for entry in entries:
            if not entry or len(entry) < 2:
                continue
            tag = entry[0]
            if tag == "S" or tag.islower():  # skip-worktree or assume-unchanged
                return f"tracked path has {tag} index flag"
        return ""

    def _common_dir(self, repo: RepoSpec, *, timeout: float = 600) -> str:
        """Canonical git common dir of the repository's base clone. Read-only; runs in the base, never a slot.
        Resolved on every call rather than taken from base_lock's key cache: an integrity check must not
        trust a value remembered for another purpose."""
        base = os.path.realpath(repo.base)
        common = git(Path(base), "rev-parse", "--git-common-dir", timeout=timeout, env=self.env)
        return os.path.realpath(os.path.join(base, common))

    def _admin_check(self, path: Path, repo: RepoSpec, *, timeout: float = 600) -> tuple[str, str]:
        """(reason, admin dir): reason is '' when the slot's .git file names an admin dir directly under
        ``<common dir of repo.base>/worktrees`` whose ``commondir`` is that common dir and whose ``gitdir``
        back-pointer is this slot. Admin ids are derived from directory names and so may be reused by any
        other ``git worktree add`` on the same clone once an admin dir disappears: a slot left pointing at a
        reused id would share another worktree's HEAD and index while every git command still succeeds.

        Reads files only, plus one read-only git call in the base clone; never runs git in the slot.
        Paths are compared after realpath, so /var vs /private/var style aliases do not matter. Raises
        WorkspaceError only when the base clone's common dir cannot be resolved."""
        dotgit = path / ".git"
        if dotgit.is_symlink():
            return "slot .git is a symlink", ""
        if not dotgit.is_file():
            return "slot .git is not a regular file", ""
        try:
            lines = dotgit.read_text().splitlines()
        except (OSError, UnicodeError) as e:
            return f"slot .git is unreadable: {e}", ""
        if len(lines) != 1 or not lines[0].startswith("gitdir: ") or not lines[0][8:].strip():
            return "slot .git is not a single gitdir line", ""
        # Relative gitdir paths (worktree.useRelativePaths) are relative to the file's directory, as git reads them.
        admin = os.path.realpath(os.path.join(path, lines[0][8:].strip()))
        common = self._common_dir(repo, timeout=timeout)
        listing = os.path.realpath(os.path.join(common, "worktrees"))
        if os.path.dirname(admin) != listing:
            return f"admin dir {admin} is not under {listing}", admin
        if not os.path.isdir(admin):
            return f"admin dir {admin} is missing", admin
        try:
            commondir = Path(admin, "commondir").read_text().strip()
            back = Path(admin, "gitdir").read_text().strip()
        except (OSError, UnicodeError) as e:
            return f"admin dir {admin} is unreadable: {e}", admin
        if not commondir or os.path.realpath(os.path.join(admin, commondir)) != common:
            return f"admin dir {admin} does not belong to {common}", admin
        if not back or os.path.realpath(os.path.join(admin, back)) != os.path.realpath(dotgit):
            return f"admin dir {admin} belongs to another worktree ({back or 'no gitdir'})", admin
        return "", admin

    def _admin_reason(self, path: Path, repo: RepoSpec, *, timeout: float = 600) -> str | None:
        """Why git must not be run in this slot because of its admin link; None when the link is sound."""
        return self._admin_check(path, repo, timeout=timeout)[0] or None

    def _warn_admin(self, path: Path, repo: RepoSpec) -> None:
        """One WARNING for a slot held for its admin link, with the timestamps that help find who removed
        or replaced the admin dir: the admin dir itself and the ``worktrees`` listing that contained it."""
        def mtime(p: str) -> str:
            try:
                return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(os.stat(p).st_mtime))
            except OSError:
                return "absent"
        try:
            why, admin = self._admin_check(path, repo, timeout=30)
            listing = os.path.join(self._common_dir(repo, timeout=30), "worktrees")
        except WorkspaceError as e:
            why, admin, listing = f"cannot verify: {e}", "", ""
        log.warning("%s: worktree %s held (%s): %s; admin dir %s (mtime %s); admin listing %s (mtime %s)",
                    self.owner, path, POOL_ADMIN_HOLD_NOTE, why or "link sound again",
                    admin or "unknown", mtime(admin) if admin else "n/a",
                    listing or "unknown", mtime(listing) if listing else "n/a")

    def audit_admin(self) -> list[str]:
        """Periodic pass: hold every free slot whose admin link fails ``_admin_reason``, before a lease
        would hand it out. Reads files only (plus one read-only git call per base clone). The rule is the one
        unsafe_reason applies: any present ``.git`` (a symlink too, even dangling) is checked; a slot whose
        ``.git`` is absent is left to lease(), which holds it as missing. Returns the newly held paths."""
        held = []
        for name, repo in self.repos.items():
            if not self._rows(name, "free"):
                continue
            with base_lock(repo.base):
                for row in self._rows(name, "free"):
                    path = Path(row["path"])
                    if not os.path.lexists(path / ".git"):
                        continue
                    try:
                        why = self._admin_reason(path, repo, timeout=30)
                    except WorkspaceError as e:
                        log.warning("%s: worktree admin audit of %s skipped: %s", self.owner, name, e)
                        break
                    if not why:
                        continue
                    claim = self.store.db.execute(
                        "UPDATE slots SET state='held', note=?, updated_at=? "
                        "WHERE path=? AND state='free' AND attempt='' AND updated_at=?",
                        (POOL_ADMIN_HOLD_NOTE, iso(), str(path), row["updated_at"]))
                    if claim.rowcount == 1:
                        self._warn_admin(path, repo)
                        held.append(str(path))
        return held

    def _save_detached_commits(self, path: Path, repo: RepoSpec, attempt_id: str) -> str:
        """Keep commits of a detached HEAD that the remote lacks. Returns the ref or ''."""
        if git(path, "symbolic-ref", "-q", "HEAD", check=False, env=self.env):
            return ""                              # on a branch: the branch ref keeps them
        ahead = git(path, "rev-list", "--count", "HEAD", "--not", f"--remotes={repo.remote}", env=self.env)
        if ahead == "0":
            return ""
        ref = f"refs/heads/runner-keep/{attempt_id[:8]}-{path.name}"
        git(path, "update-ref", ref, "HEAD", env=self.env)
        return ref

    def _rows(self, repo: str, state: str | None = None) -> list[dict]:
        q, args = "SELECT * FROM slots WHERE pool=?", [self._pool(repo)]
        if state:
            q, args = q + " AND state=?", args + [state]
        return [dict(r) for r in self.store.db.execute(q + " ORDER BY path", args)]

    def _set(self, path: Path, repo: str, state: str, attempt: str = "", note: str = "") -> None:
        self.store.db.execute(
            "INSERT INTO slots(path,pool,attempt,state,note,updated_at) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(path) DO UPDATE SET attempt=excluded.attempt, state=excluded.state, "
            "note=excluded.note, updated_at=excluded.updated_at",
            (str(path), self._pool(repo), attempt, state, note, iso()))

    # -- lease / release ---------------------------------------------------------------
    def lease(self, attempt_id: str, repo_name: str, *, preferred_path: str | None = None) -> Path:
        repo = self.repos[repo_name]
        with base_lock(repo.base):
            return self._lease(attempt_id, repo_name, repo, preferred_path=preferred_path)

    def _suspended_sources(self, repo_name: str, attempt_id: str) -> set[str]:
        """Slots that other attempts of this pool were suspended from and have not been restored to."""
        if not self.store.db.execute("SELECT 1 FROM sqlite_master WHERE name='workspace_checkpoints'").fetchone():
            return set()                  # nothing was ever suspended in this registry
        return {r[0] for r in self.store.db.execute(
            "SELECT source FROM workspace_checkpoints WHERE owner=? AND repo=? AND attempt<>? "
            "AND phase IN ('releasing','released')", (self.owner, repo_name, attempt_id))}

    RETURNING_HOURS = 3         # an ownership-loss note older than this no longer keeps its slots aside

    def _returning_sources(self) -> set[str]:
        """Working copies named by this lane's ownership-loss notes (``runner.OWNERSHIP_LOSS`` marks that
        no return has spent yet): the paths on which a card that left the lane (to a review, to another
        agent) continues its session if it comes back. A card that was closed (done, cancelled) when it left
        is not expected back and names nothing: on a busy lane those notes cover nearly every slot and the
        order would say nothing. Unreadable notes and notes older than ``RETURNING_HOURS`` name nothing."""
        since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=self.RETURNING_HOURS)).isoformat()
        out: set[str] = set()
        try:
            rows = self.store.db.execute(
                "SELECT value FROM marks WHERE agent=? AND key='ownership_loss' AND value<>'' AND at>=?",
                (self.owner, since)).fetchall()
        except Exception:                                # noqa: BLE001 - an ordering hint never fails a lease
            return out
        for (value,) in rows:
            try:
                note = json.loads(value)
                paths = note.get("workspaces") or []
                closed = note.get("observed_status") in ("done", "cancelled")
            except (ValueError, AttributeError):
                continue
            if not closed:
                out.update(p for p in paths if isinstance(p, str))
        return out

    def _lease(self, attempt_id: str, repo_name: str, repo: RepoSpec, *,
               preferred_path: str | None = None) -> Path:
        self._check_disk()
        self._keep_base_bare(repo)
        self._clean_staging(repo)
        self._fetch(repo)
        self._check_disk()
        start = f"{repo.remote}/{repo.branch}"
        slots = self._rows(repo_name, "free")
        # Preference only reorders this pool's free inventory. Normal admission checks
        # and the base lock still apply; held, foreign and unknown paths cannot enter.
        # A slot that a suspended attempt of this pool left is the one path where that attempt's
        # session can continue (a restore to any other path starts the session anew), so other
        # work takes such a slot last. The attempt's own old slot stays first for its restore.
        waited = self._suspended_sources(repo_name, attempt_id)
        # The same holds, one step weaker, for a slot a card of this lane left when it went away and may
        # come back to (``_returning_sources``): other work takes it after the plain free slots.
        returning = self._returning_sources()
        slots.sort(key=lambda slot: (slot["path"] != preferred_path, slot["path"] in waited,
                                     slot["path"] in returning))
        # While the pool may still grow, such a kept-aside slot is not taken at all: a new slot costs one
        # checkout, taking the old one costs its owner the session (a returning card starts fresh on another
        # path; a restore that has to move keeps the session only where the harness is confined). At the
        # maximum it is taken as before, and a caller that names the path gets it.
        room = len(self._rows(repo_name)) < repo.max_slots
        aside = {slot["path"] for slot in slots if room and slot["path"] != preferred_path
                 and (slot["path"] in returning or slot["path"] in waited)}
        # The slot a returning card names may be gone from the inventory: the slot pruner removes free slots
        # of long waits. While the pool may grow, the new slot is made at that very path, so the card finds
        # its paths and keeps its session; the free slots are left for other work.
        again = self._gone_preferred(repo, repo_name, preferred_path) if room else None
        for slot in ([] if again else slots):
            if slot["path"] in aside:
                continue
            path = Path(slot["path"])
            # Only this pool's free inventory is eligible for config repair. Validate
            # the admin link before writing anything; never follow a foreign HEAD/index.
            if path.is_dir() and not self._admin_reason(path, repo):
                self._configure(path)
            reason = self.unsafe_reason(path, repo)
            if reason:
                self._set(path, repo_name, "held", note=f"found while free: {reason}")
                if reason.startswith(POOL_ADMIN_HOLD_NOTE):
                    self._warn_admin(path, repo)
                continue
            git(path, "checkout", "--quiet", "--detach", start, env=self.env)
            self._set(path, repo_name, "leased", attempt_id)
            return path
        if len(self._rows(repo_name)) >= repo.max_slots:
            raise WorkspaceError(f"no free worktree for {repo_name} (max {repo.max_slots}; see `mesh-runner slots`)")
        self._check_disk()
        # a new slot never takes the name of a slot somebody may come back to: the pruner removes such
        # slots while their owner is parked, and the lowest free number would then be exactly that path
        path = again or self._new_path(repo, avoid=waited | returning)
        # git names the admin dir after the basename it is given, and slot-N names repeat across the pools
        # sharing one base clone. Adding under an owner-prefixed name and moving it into place keeps an
        # owner-unique admin id while the directory stays slot-N.
        staging = self._staging_path(path)
        git(repo.base, "worktree", "add", "--quiet", "--detach", str(staging), start, env=self.env)
        git(repo.base, "worktree", "move", str(staging), str(path), env=self.env)
        if why := self._admin_reason(path, repo):
            self._set(path, repo_name, "held", note=f"{POOL_ADMIN_HOLD_NOTE}: {why}")
            self._warn_admin(path, repo)
            raise WorkspaceError(f"new worktree {path}: {why}")
        # Inventory before configuration: a refused config write (another writer's config.lock) must
        # leave a known free slot that the next lease repairs and reuses, not an unregistered directory
        # that every later lease skips while failed retries pile up more of them.
        self._set(path, repo_name, "free", note="new; configuration pending")
        self._configure(path)
        self._keep_base_bare(repo)       # _configure may have just switched per-worktree config on
        self._set(path, repo_name, "leased", attempt_id)
        return path

    def _gone_preferred(self, repo: RepoSpec, repo_name: str, preferred_path: str | None) -> Path | None:
        """``preferred_path`` when it is a slot path of this pool that neither the inventory nor the disk
        has any more; otherwise None (the ordinary order applies)."""
        if not preferred_path:
            return None
        path = Path(preferred_path)
        if path.parent != self.root / repo.name or not re.fullmatch(r"slot-[1-9]\d*", path.name):
            return None
        if path.exists() or path.is_symlink() or any(r["path"] == preferred_path for r in self._rows(repo_name)):
            return None
        return path

    def _keep_base_bare(self, repo: RepoSpec) -> None:
        """With per-worktree config on, a bare base is bare for itself only.

        ``extensions.worktreeConfig`` (switched on by ``_configure`` for the per-worktree identity) makes git
        apply the shared ``core.bare`` to every linked worktree as well. On a bare base each slot then answers
        "this operation must be run in a work tree": ``git status`` fails and its checkpoint cannot list files.
        git's rule for this extension is to keep ``core.bare`` in the main worktree's ``config.worktree``.

        It is written there first and removed from the shared file second, so the base is bare at every
        moment. A base that is not bare, or has the extension off, is left alone; repeating the call changes
        nothing, which also repairs a base an earlier runner left in the broken form."""
        if git(repo.base, "config", "--get", "extensions.worktreeConfig", check=False, env=self.env) != "true":
            return
        common = Path(git(repo.base, "rev-parse", "--path-format=absolute", "--git-common-dir", env=self.env))
        shared = str(common / "config")
        if git(repo.base, "config", "--file", shared, "--get", "core.bare", check=False, env=self.env) != "true":
            return
        git(repo.base, "config", "--file", str(common / "config.worktree"), "core.bare", "true", env=self.env)
        git(repo.base, "config", "--file", shared, "--unset-all", "core.bare", env=self.env)
        log.info("%s: base %s: core.bare moved to config.worktree (per-worktree config is on)", self.owner, repo.base)

    def _owner_tag(self) -> str:
        """The owner as a git-safe admin id prefix."""
        return re.sub(r"[^A-Za-z0-9._-]", "-", self.owner).strip(".-") or "pool"

    def _staging_path(self, path: Path) -> Path:
        """Where a new slot is added before it is moved to ``path``: same parent, owner-prefixed name."""
        staging = path.with_name(f"{self._owner_tag()}-{path.name}")
        n = 0
        while os.path.lexists(staging):            # a leftover that could not be removed: never reuse it
            n += 1
            staging = path.with_name(f"{self._owner_tag()}-{path.name}.{n}")
        return staging

    def _clean_staging(self, repo: RepoSpec) -> None:
        """Remove staging worktrees left by a crash between ``worktree add`` and ``worktree move``.

        A staging worktree never had a slot row (the row is written after the move), so nothing was ever
        leased in it: it is a fresh detached checkout. It is removed only when its admin link is sound and
        git agrees it is clean (no --force); anything else is kept for the operator."""
        parent = self.root / repo.name
        if not parent.is_dir():
            return
        pattern = re.compile(rf"{re.escape(self._owner_tag())}-slot-[1-9][0-9]*(\.[1-9][0-9]*)?")
        known = {Path(r["path"]).name for r in self._rows(repo.name)}
        for leftover in sorted(parent.iterdir()):
            if (not pattern.fullmatch(leftover.name) or leftover.name in known
                    or leftover.is_symlink() or not leftover.is_dir()):
                continue
            try:
                if why := self._admin_reason(leftover, repo):
                    raise WorkspaceError(why)
                git(repo.base, "worktree", "remove", str(leftover), env=self.env)
            except WorkspaceError as e:
                log.warning("%s: leftover staging worktree %s kept for operator inspection: %s",
                            self.owner, leftover, e)
                continue
            log.info("%s: removed leftover staging worktree %s", self.owner, leftover)

    def release(self, attempt_id: str, *, paths: list[str] | None = None) -> list[tuple[str, str]]:
        """Return owned slots, optionally limited to exact paths. Unsafe slots are held, never wiped.
        Returns [(path, reason)] for slots that were held."""
        self._maintenance_guard(attempt_id)
        kept = []
        rows = [dict(r) for r in self.store.db.execute(
            "SELECT * FROM slots WHERE attempt=? AND state='leased'", (attempt_id,))]
        if paths is not None:
            allowed = {str(Path(path)) for path in paths}
            rows = [row for row in rows if str(Path(row["path"])) in allowed]
        for slot in rows:
            repo_name = slot["pool"].split("/", 1)[1]
            path, repo = Path(slot["path"]), self.repos.get(repo_name)
            reason = "repository no longer configured" if repo is None else self.unsafe_reason(path, repo)
            if not reason:
                try:
                    with base_lock(repo.base):
                        self._maintenance_guard(attempt_id, str(path))   # a window opened since the precheck
                        saved = self._save_detached_commits(path, repo, attempt_id)
                        self._free(path, repo)
                    if saved:
                        kept.append((str(path), f"commits saved as {saved}; slot returned"))
                    continue
                except MaintenanceHeld:
                    raise                    # not a reason to hold the slot: leave it exactly as it is
                except WorkspaceError as e:
                    reason = f"cannot return: {e}"
            if repo is None:
                self._maintenance_guard(attempt_id, str(path))
                self._set(path, repo_name, "held", attempt_id, reason)
            else:
                with base_lock(repo.base):
                    # holding a slot rewrites the pinned lease row: never under an opened window
                    self._maintenance_guard(attempt_id, str(path))
                    self._set(path, repo_name, "held", attempt_id, reason)
            kept.append((str(path), reason))
        return kept

    def suspend(self, attempt_id: str, metadata: dict, *, writer_stopped: bool = False) -> list[str]:
        try:
            return self._suspend(attempt_id, metadata, writer_stopped=writer_stopped)
        except MaintenanceHeld:
            raise
        except WorkspaceError as exc:
            # A capture can fail before creating its first journal. Keep both the
            # intended sources and any partial generation, never infer recovery
            # from a later arbitrary comment or from a different suspension.
            binding = {"attempt": attempt_id, "generation": metadata.get("checkpoint_generation"),
                       "sources": sorted(metadata.get("workspaces", [])),
                       "checkpoints": {r["repo"]: r["checkpoint"] for r in self._checkpoint_rows(attempt_id)
                                       if r["phase"] not in ("active", "restored")}}
            raise CheckpointSuspendError(exc, binding) from exc

    def _suspend(self, attempt_id: str, metadata: dict, *, writer_stopped: bool = False) -> list[str]:
        """Checkpoint every owned lease before release. Caller proves the process group stopped.

        A durable journal makes release and restore retryable after any runner restart.
        Partial multi-repo suspension is safe: only independently verified slots are returned.
        """
        if not writer_stopped:
            raise WorkspaceError("checkpoint: writer stop not established")
        from .checkpoint import _clear_tree, base_objects_dir
        self._maintenance_guard(attempt_id)
        checkpoints = self._checkpoints()
        result = []
        for row in self._checkpoint_rows(attempt_id):
            if row["phase"] in ("released", "restoring", "restored"):
                result.append(row["checkpoint"])
        rows = [dict(r) for r in self.store.db.execute(
            "SELECT * FROM slots WHERE attempt=? AND state='leased' AND pool LIKE ?",
            (attempt_id, f"{self.owner}/%"))]
        for slot in rows:
            repo_name = slot["pool"].split("/", 1)[1]
            repo, path = self.repos[repo_name], Path(slot["path"])
            with base_lock(repo.base):
                self._maintenance_guard(attempt_id, slot["path"])
                # Snapshot, reset and clear below all run git in the slot: never through a foreign admin dir.
                if why := self._admin_reason(path, repo):
                    raise WorkspaceError(f"checkpoint: {path}: {POOL_ADMIN_HOLD_NOTE}: {why}; lease retained")
                journal = next((r for r in self._checkpoint_rows(attempt_id) if r["repo"] == repo_name), None)
                if journal and journal["phase"] in ("restored", "active"):
                    # A later WAIT needs a new generation; old snapshots remain recoverable.
                    self.store.db.execute("DELETE FROM workspace_checkpoints WHERE owner=? AND attempt=? AND repo=?",
                                          (self.owner, attempt_id, repo_name))
                    journal = None
                if journal is None:
                    import uuid
                    cp = checkpoints.create(uuid.uuid4().hex, path,
                        {**metadata, "repo": repo_name, "attempt": attempt_id,
                         "branch": git(path, "symbolic-ref", "-q", "HEAD", check=False, env=self.env),
                         "base_sha": git(path, "rev-parse", f"{repo.remote}/{repo.branch}", env=self.env)})
                    self.store.db.execute(
                        "INSERT INTO workspace_checkpoints VALUES(?,?,?,?,?,?,?)",
                        (self.owner, attempt_id, repo_name, str(cp), str(path), "verified", ""))
                else:
                    cp = Path(journal["checkpoint"])
                    try:
                        # old-format snapshot: shed regenerable caches, verified swap
                        checkpoints.compact(cp, base_objects=base_objects_dir(repo.base, self.env))
                    except (WorkspaceError, OSError) as e:
                        log.warning("checkpoint compact %s skipped: %s", cp.name, e)
                    checkpoints.verify(cp)
                # Every destructive action below has a durable, replay-verified copy.
                # Mark first: a retry must finish returning this lease, never recapture it.
                self.store.db.execute("UPDATE workspace_checkpoints SET phase='releasing' "
                    "WHERE owner=? AND attempt=? AND repo=?", (self.owner, attempt_id, repo_name))
                git(path, "reset", "--hard", "--quiet", env=self.env)
                _clear_tree(path)
                git(path, "checkout", "--quiet", "--force", "--detach",
                    f"{repo.remote}/{repo.branch}", env=self.env)
                self._set(path, repo_name, "free")
                self.store.db.execute("UPDATE workspace_checkpoints SET phase='released' "
                    "WHERE owner=? AND attempt=? AND repo=?", (self.owner, attempt_id, repo_name))
                result.append(str(cp))
        # Crash after slot free but before journal advance: never touch its new occupant.
        for row in self._checkpoint_rows(attempt_id):
            if row["phase"] == "releasing" and not self.store.db.execute(
                "SELECT 1 FROM slots WHERE path=? AND attempt=? AND state='leased'",
                (row["source"], attempt_id)).fetchone():
                self.store.db.execute("UPDATE workspace_checkpoints SET phase='released' "
                    "WHERE owner=? AND attempt=? AND repo=?", (self.owner, attempt_id, row["repo"]))
        return result

    def _maintenance_guard(self, attempt_id: str = "", path: str = "") -> None:
        """An open maintenance window closes every capture, restore, release and free of its attempt
        and slot. Checked before and, under the repository lock, during each operation."""
        from . import maintenance
        if maintenance.held(self.store, attempt=attempt_id, path=path):
            raise MaintenanceHeld("maintenance window held; workspace untouched")

    def _checkpoints(self):
        from .checkpoint import Checkpoints
        self.store.db.execute("CREATE TABLE IF NOT EXISTS workspace_checkpoints ("
            "owner TEXT, attempt TEXT, repo TEXT, checkpoint TEXT, source TEXT, phase TEXT, target TEXT, "
            "PRIMARY KEY(owner,attempt,repo))")
        return Checkpoints(self.root.parent / "checkpoints" / self.owner,
                           capture_min_free_bytes=int(max(0, self.min_free_gb) * 1024**3),
                           **({} if self.checkpoint_exclude_names is None
                              else {'exclude_names': self.checkpoint_exclude_names}))

    def _checkpoint_rows(self, attempt_id: str) -> list[dict]:
        self._checkpoints()
        return [dict(r) for r in self.store.db.execute(
            "SELECT * FROM workspace_checkpoints WHERE owner=? AND attempt=? ORDER BY repo",
            (self.owner, attempt_id))]

    # -- branch ownership ----------------------------------------------------------------
    def _worktree_branches(self, repo: RepoSpec) -> list[tuple[Path, str]]:
        """(path, full branch ref) of every worktree of the base repository that has a branch checked out."""
        out = git(repo.base, "worktree", "list", "--porcelain", env=self.env)   # fails closed: ownership unknown
        found, path = [], None
        for line in out.splitlines():
            if line.startswith("worktree "):
                path = Path(line[len("worktree "):])
            elif line.startswith("branch ") and path is not None:
                found.append((path, line[len("branch "):].strip()))
        return found

    def _attempt_live(self, attempt_id: str, state: str) -> bool:
        """The attempt holding a slot is still alive. An attempt the store does not know, holding a
        leased slot, counts as live: a checkout is never taken from an owner that cannot be disproved."""
        a = self.store.get(attempt_id) if attempt_id else None
        if a is not None:
            return a.state in OPEN
        return state == "leased"

    def attempt_holds_slot(self, attempt_id: str, path: str = "") -> bool:
        """The live attempt still has a leased or held slot (at ``path`` when given)."""
        q, args = "SELECT state FROM slots WHERE attempt=? AND state IN ('leased','held')", [attempt_id]
        if path:
            q, args = q + " AND path=?", args + [path]
        row = self.store.db.execute(q, args).fetchone()
        return bool(row) and self._attempt_live(attempt_id, row["state"])

    def _settle_branch(self, attempt_id: str, repo: RepoSpec, branch: str, target: Path | None) -> None:
        """Make ``branch`` free to check out for ``attempt_id`` or raise why it is not.

        Held by a slot of a live attempt: BranchInUse (wait, no failure). Held by a runner slot nobody
        lives in: detached when clean, kept held (BranchInUse, not live) when it carries uncommitted
        changes. Held by anything else: logged, git decides as before. Caller holds the base lock."""
        name = branch[len("refs/heads/"):]
        slots = {os.path.realpath(r["path"]): dict(r) for r in self.store.db.execute("SELECT * FROM slots")}
        skip = os.path.realpath(target) if target else None
        for path, ref in self._worktree_branches(repo):
            real = os.path.realpath(path)
            if ref != branch or real == skip:
                continue
            row = slots.get(real)
            if row is None:
                log.warning("%s: branch %s is checked out in %s, which is not a runner slot; "
                            "restore for attempt %s goes on and git may refuse", self.owner, name, path,
                            attempt_id[:8])
                continue
            if row["attempt"] == attempt_id and row["state"] == "leased":
                continue                                  # the attempt's own lease: not a conflict
            slot_name = Path(row["path"]).name
            holder = self.store.get(row["attempt"]) if row["attempt"] else None
            who = holder.task8 if holder else (row["attempt"][:8] or "unknown")
            if row["state"] in ("leased", "held") and self._attempt_live(row["attempt"], row["state"]):
                raise BranchInUse(f"branch {name} in use by #{who} ({slot_name})", branch=name,
                                  holder_attempt=row["attempt"], holder_path=row["path"])
            holder_repo = self.repos.get(row["pool"].split("/", 1)[1], repo)
            reason = self.unsafe_reason(Path(row["path"]), holder_repo)
            if reason:
                if row["state"] != "held":
                    self._set(Path(row["path"]), holder_repo.name, "held", row["attempt"],
                              f"branch {name} checked out: {reason}")
                log.warning("%s: branch %s is checked out in stale slot %s (%s); slot held for inspection",
                            self.owner, name, row["path"], reason)
                raise BranchInUse(f"branch {name} checked out in {slot_name} ({reason}); slot held",
                                  branch=name, holder_path=row["path"], live=False)
            git(Path(row["path"]), "checkout", "--quiet", "--detach", env=self.env)
            log.info("%s: detached stale slot %s (state %s) to free branch %s for attempt %s",
                     self.owner, row["path"], row["state"], name, attempt_id[:8])

    def _settle_row(self, attempt_id: str, row: dict, checkpoints) -> None:
        """Settle the branch one journal row will restore. Caller holds the base lock."""
        if row["phase"] not in ("released", "restoring"):
            return
        try:
            branch = checkpoints.read(Path(row["checkpoint"])).get("branch")
        except WorkspaceError:
            return                                        # restore itself reports an unreadable snapshot
        if isinstance(branch, str) and branch.startswith("refs/heads/"):
            self._settle_branch(attempt_id, self.repos[row["repo"]], branch,
                                Path(row["target"]) if row["target"] else None)

    def _settle_branches(self, attempt_id: str, rows: list[dict], checkpoints) -> None:
        """Before any lease or replay of any repository: every branch to restore must be free, so a
        multi-repo restore does not stop half way. restore() settles each row again under the lock it
        leases and replays with: nothing can take the branch between that check and the checkout."""
        for row in rows:
            with base_lock(self.repos[row["repo"]].base):
                self._settle_row(attempt_id, row, checkpoints)

    def restore(self, attempt_id: str, *, expected_repos: set[str] | None = None) -> tuple[list[str], list[dict]]:
        """Fresh leases; failed replay retains its lease and cannot admit a writer."""
        self._maintenance_guard(attempt_id)
        checkpoints = self._checkpoints()
        paths, manifests = [], []
        rows = self._checkpoint_rows(attempt_id)
        known = {r["repo"] for r in rows}
        leased = {r["pool"].split("/", 1)[1] for r in self.rows()
                  if r["attempt"] == attempt_id and r["state"] == "leased"}
        if leased - known or (expected_repos is not None and known != expected_repos):
            raise WorkspaceError("checkpoint: incomplete repository inventory; leases retained")
        self._settle_branches(attempt_id, rows, checkpoints)
        for row in rows:
            if row["phase"] not in ("released", "restoring", "restored"):
                raise WorkspaceError("checkpoint: suspension incomplete; lease retained")
            repo = self.repos[row["repo"]]
            with base_lock(repo.base):
                self._maintenance_guard(attempt_id)
                self._settle_row(attempt_id, row, checkpoints)      # same lock as the lease and the checkout
                # Restart recovery may already own a target lease, bypassing lease()'s
                # admission guard. Check before changing the journal or replaying bytes.
                # A completed restore is read-only and remains safe to re-enter.
                if row["phase"] != "restored":
                    self._check_disk()
                if row["phase"] == "released":
                    # lease is idempotent across a crash before the journal update.
                    owned = [r for r in self._rows(row["repo"], "leased") if r["attempt"] == attempt_id]
                    target = (Path(owned[0]["path"]) if owned else
                              self.lease(attempt_id, row["repo"], preferred_path=row["source"]))
                    self.store.db.execute("UPDATE workspace_checkpoints SET phase='restoring', target=? "
                        "WHERE owner=? AND attempt=? AND repo=?",
                        (str(target), self.owner, attempt_id, row["repo"]))
                else:
                    target = Path(row["target"])
                owned = self.store.db.execute("SELECT 1 FROM slots WHERE path=? AND attempt=? AND state='leased'",
                                              (str(target), attempt_id)).fetchone()
                if not owned:
                    raise WorkspaceError("checkpoint: restore lease ownership lost")
                if row["phase"] == "restored":
                    # Re-entry after persisting completion must not overwrite later edits.
                    manifest = checkpoints.read(Path(row["checkpoint"]))
                else:
                    # A target already owned (restart recovery) did not pass lease()'s admission checks.
                    if why := self._admin_reason(target, repo):
                        raise WorkspaceError(f"checkpoint: restore target {target}: {POOL_ADMIN_HOLD_NOTE}: "
                                             f"{why}; lease retained")
                    manifest = checkpoints.restore(Path(row["checkpoint"]), target)
                    self.store.db.execute("UPDATE workspace_checkpoints SET phase='restored' "
                        "WHERE owner=? AND attempt=? AND repo=?", (self.owner, attempt_id, row["repo"]))
                paths.append(str(target))
                manifests.append(manifest)
        return paths, manifests

    def recheck_held(self, open_attempts: set[str]) -> list[str]:
        """Free held slots that became safe (e.g. their branch was pushed), unless
        their attempt is still open. Returns freed paths."""
        freed = []
        rows = [dict(r) for r in self.store.db.execute(
            "SELECT * FROM slots WHERE state='held' AND pool LIKE ?", (f"{self.owner}/%",))]
        for slot in rows:
            if slot["note"] in (POOL_EVICTION_HOLD_NOTE, POOL_INDEX_FLAGS_HOLD_NOTE) \
                    or POOL_ADMIN_HOLD_NOTE in slot["note"]:
                continue
            if slot["attempt"] and slot["attempt"] in open_attempts:
                continue
            repo = self.repos.get(slot["pool"].split("/", 1)[1])
            if repo is None:
                continue
            with base_lock(repo.base):
                try:
                    self._maintenance_guard(slot["attempt"], slot["path"])
                except WorkspaceError:
                    continue
                try:
                    self._fetch(repo)
                except WorkspaceError:
                    continue
                path = Path(slot["path"])
                if not self.unsafe_reason(path, repo):
                    if not self._claim(slot):         # `free-slot` (another process) or a re-lease won the row
                        continue
                    try:
                        self._save_detached_commits(path, repo, slot["attempt"] or "held")
                        self._free(path, repo)
                        freed.append(slot["path"])
                    except WorkspaceError:
                        continue
                    finally:
                        self._unclaim(slot)           # no-op once the slot is free
        return freed

    def _snapshot_tree(self, path: Path, env: dict[str, str], index: Path) -> str:
        """Tree of the slot's current state (tracked edits, deletions and untracked files not
        ignored), built in a scratch index: the slot's own index and files are not touched."""
        env = {**env, "GIT_INDEX_FILE": str(index)}
        if index.exists():
            index.unlink()
        git(path, "read-tree", "HEAD", env=env)
        git(path, "add", "-A", "--", ".", env=env)
        return git(path, "write-tree", env=env)

    def _archive_dirty(self, path: Path, attempt_id: str) -> str:
        """Commit the slot's uncommitted state onto a ref that is never pushed and return it.

        The commit's parent is the slot's HEAD, so commits ahead of the remote stay reachable too.
        The ref is verified (it resolves to a commit holding the snapshot tree, and the slot did not
        change while it was written) before the caller may discard anything."""
        base_env = dict(self.env if self.env is not None else os.environ)
        name, email = self.identity or ("mesh-runner", "mesh-runner@localhost")
        env = {**base_env, "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
               "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email}
        gitdir = Path(git(path, "rev-parse", "--absolute-git-dir", env=self.env))
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        stem = f"{ARCHIVE_REF_PREFIX}/{(attempt_id or 'held')[:8]}-{path.name}-{stamp}"
        with tempfile.TemporaryDirectory(prefix="archive-index-", dir=gitdir) as scratch:
            index = Path(scratch) / "index"
            tree = self._snapshot_tree(path, env, index)
            head = git(path, "rev-parse", "HEAD", env=self.env)
            commit = git(path, "commit-tree", tree, "-p", head, "-m",
                         f"mesh-runner archive of uncommitted work\n\nslot: {path.name}\nattempt: {attempt_id}",
                         env=env)
            ref, n = stem, 0
            while git(path, "rev-parse", "-q", "--verify", ref, check=False, env=self.env):
                n += 1
                ref = f"{stem}-{n}"
            git(path, "update-ref", ref, commit, "", env=self.env)      # "" = the ref must not exist
            if git(path, "rev-parse", f"{ref}^{{tree}}", env=self.env) != tree:
                raise WorkspaceError(f"archive ref {ref} does not hold the snapshot")
            if self._snapshot_tree(path, env, index) != tree:
                raise WorkspaceError(f"slot changed while archiving; ref {ref} kept")
        try:
            self.store.event(attempt_id or "held", "slot-archived", f"{path} -> {ref}")
        except sqlite3.Error as e:
            # the verified ref is the real record; a failed bookkeeping row must not strand the slot
            log.warning("%s: worktree %s: archive ref %s is saved but its event was not recorded: %s",
                        self.owner, path, ref, e)
        return ref

    def _archive_candidate(self, path: str, expect_attempt: str | None) -> dict:
        """The slot row if it is held for the dirty-hold reason (and for ``expect_attempt``); else a
        WorkspaceError. Reads the database only, so a refused slot costs no git call and no lock."""
        try:
            row = self.store.db.execute("SELECT * FROM slots WHERE path=?", (path,)).fetchone()
        except sqlite3.Error as e:
            raise WorkspaceError(f"archive failed: {type(e).__name__}: {e}") from None
        if not row:
            raise WorkspaceError("unknown slot")
        row = dict(row)
        if row["state"] != "held" or (expect_attempt is not None and row["attempt"] != expect_attempt):
            raise WorkspaceError("slot is not held for that attempt")
        if row["note"] != DIRTY_HOLD_NOTE:       # allow-list: any other hold reason is not ours to resolve
            raise WorkspaceError(f"still held for another reason: {row['note'] or 'no note'}")
        return row

    def archive_and_free(self, path: str, expect_attempt: str | None = None) -> str:
        """Free a held slot whose only obstacle is uncommitted work, after preserving that work.

        Returns the archive ref ('' when the slot had become clean and was freed as usual).
        Raises WorkspaceError, leaving the slot held, when the slot is not held (or is held for
        another attempt than ``expect_attempt``), has any other obstacle, or saving fails."""
        row = self._archive_candidate(path, expect_attempt)       # database only: no git, no lock
        self._maintenance_guard(row["attempt"], path)
        repo = self.repos.get(row["pool"].split("/", 1)[1])
        if repo is None:
            raise WorkspaceError("repository not configured")
        with base_lock(repo.base):
            self._maintenance_guard(row["attempt"], path)
            if self._archive_candidate(path, expect_attempt) != row:      # changed while waiting for the lock
                raise WorkspaceError("slot changed before it could be archived")
            if not self._claim(row):
                raise WorkspaceError("slot changed before it could be archived")
            slot = Path(path)
            try:
                reason = self.unsafe_reason(slot, repo)
                if reason and reason != DIRTY_HOLD_NOTE:
                    raise WorkspaceError(f"still unsafe: {reason}")
                ref = ""
                try:
                    if reason:
                        ref = self._archive_dirty(slot, row["attempt"])
                        git(slot, "reset", "--hard", "--quiet", env=self.env)
                    self._save_detached_commits(slot, repo, row["attempt"] or "held")
                    self._free(slot, repo)
                except (OSError, UnicodeError, sqlite3.Error, subprocess.SubprocessError) as e:
                    # anything before the verified ref leaves the slot untouched; later, the work is saved
                    # and a later recheck frees the (then clean) held slot
                    raise WorkspaceError(f"archive failed: {type(e).__name__}: {e}") from None
            finally:
                self._unclaim(row)               # no-op once the slot is free
        return ref

    def _claim(self, row: dict) -> bool:
        """Compare-and-swap held -> freeing on the row as it was read. Another process (``free-slot``) and
        the Runner share no lock, only this row: whoever loses the swap leaves the slot alone."""
        cur = self.store.db.execute(
            "UPDATE slots SET state='freeing' WHERE path=? AND state='held' AND attempt=? AND updated_at=?",
            (row["path"], row["attempt"], row["updated_at"]))
        return cur.rowcount == 1

    def _unclaim(self, row: dict) -> None:
        self.store.db.execute("UPDATE slots SET state='held' WHERE path=? AND state='freeing' AND attempt=?",
                              (row["path"], row["attempt"]))

    def free_path(self, path: str, archive: bool = False) -> str:
        """Operator command: return a held slot if it is safe; never forces. With ``archive``, a
        slot held only for uncommitted changes is freed after its work is saved on a ref."""
        row = self.store.db.execute("SELECT * FROM slots WHERE path=?", (path,)).fetchone()
        if not row:
            return "unknown slot"
        if dict(row)["state"] != "held":
            # `free-slot` runs in another process, beside the service: the process-local repository lock
            # cannot order it against the Runner, so it never touches a leased slot (a maintenance
            # window's target is always leased) and the state check is one atomic row read.
            return f"still unsafe: slot is not held (state {dict(row)['state']})"
        try:
            self._maintenance_guard(dict(row)["attempt"], path)
        except WorkspaceError as e:
            return f"still unsafe: {e}"
        repo = self.repos.get(dict(row)["pool"].split("/", 1)[1])
        reason = self.unsafe_reason(Path(path), repo) if repo else "repository not configured"
        if reason == DIRTY_HOLD_NOTE and archive:
            attempt = dict(row)["attempt"]
            found = self.store.db.execute("SELECT task_id FROM attempts WHERE id=?", (attempt,)).fetchone() \
                if attempt else None
            who = f"attempt {attempt[:8] or 'unknown'}, task " + (f"#{found[0][:8]}" if found else "unknown")
            try:
                ref = self.archive_and_free(path, expect_attempt=attempt or None)
            except WorkspaceError as e:
                log.warning("%s: worktree %s (%s) stays held: %s", self.owner, path, who, e)
                return f"still unsafe: {e}"
            log.info("%s: worktree %s returned to the pool (operator --archive): %s, uncommitted work saved as %s",
                     self.owner, path, who, ref or "(nothing to save)")
            return "freed" + (f" (uncommitted work saved as {ref}; never pushed)" if ref else "")
        if reason:
            return f"still unsafe: {reason}"
        with base_lock(repo.base):
            try:
                self._maintenance_guard(dict(row)["attempt"], path)      # a window opened since the precheck
            except MaintenanceHeld as e:
                return f"still unsafe: {e}"
            if not self._claim(dict(row)):
                return "still unsafe: slot changed while it was being freed"
            try:
                saved = self._save_detached_commits(Path(path), repo, dict(row)["attempt"] or "held")
                self._free(Path(path), repo)
            finally:
                self._unclaim(dict(row))         # no-op once the slot is free
        return "freed" + (f" (commits saved as {saved})" if saved else "")

    def full(self, repo_name: str) -> bool:
        """No slot to lease: none free and the pool at its maximum. No git, no lock: a cheap
        pre-check before a task is checked out; lease() still decides."""
        return (not self._rows(repo_name, "free")
                and len(self._rows(repo_name)) >= self.repos[repo_name].max_slots)

    def issue(self, attempt_id: str, path: str) -> str:
        """The path a session is told to write in for the slot recorded as ``path``.

        Inventory and leases keep the recorded identity. When it is a symlink alias of the real directory
        (a parent such as ``worktrees/<lane>`` pointing at a volume), the harness policy of the lane may
        allow only the physical path, so that one is issued, once the slot is proven to be this attempt's
        own leased worktree. A plain path is returned unchanged only after the same lease check. Raises
        WorkspaceError (fail closed, nothing is modified) for a foreign or stale slot, an unsafe target
        or a changed admin link."""
        rows = [r for r in self.rows() if r["path"] == path]
        if len(rows) != 1 or rows[0]["state"] != "leased" or rows[0]["attempt"] != attempt_id:
            raise WorkspaceError(f"issue {path}: slot is not leased to this attempt")
        real = os.path.realpath(path)
        if real == path:
            return path
        # every owner's slots count: the physical target must belong to this one row and nobody else's
        if any(r[0] != path and os.path.realpath(r[0]) == real
               for r in self.store.db.execute("SELECT path FROM slots")):
            raise WorkspaceError(f"issue {path}: physical target {real} is shared with another slot")
        repo = self.repos.get(rows[0]["pool"].split("/", 1)[1])
        if repo is None:
            raise WorkspaceError(f"issue {path}: repository is not configured")
        # The target must be this slot relocated, not another worktree: the same owner/repo/slot tail ...
        tail = lambda p: Path(p).parts[-3:]
        if (not os.path.isdir(real) or tail(real) != tail(path) or real == os.path.realpath(repo.base)):
            raise WorkspaceError(f"issue {path}: unsafe physical target {real}")
        why, admin = self._admin_check(Path(real), repo)
        if why:
            raise WorkspaceError(f"issue {path}: {why}")
        # ... and an admin dir created for it. Legacy worktree add appended a decimal collision
        # counter directly to the slot basename (slot-1 -> slot-11). Only unprefixed legacy ids
        # get that spelling; exact reciprocal ownership was proved above, not inferred from this name.
        slot = re.escape(os.path.basename(path))
        modern = rf"(?:{re.escape(self._owner_tag())}-)?{slot}(?:\.\d+)?"
        legacy = rf"{slot}[1-9][0-9]*"
        if not re.fullmatch(rf"(?:{modern}|{legacy})", os.path.basename(admin)):
            raise WorkspaceError(f"issue {path}: admin dir {admin} was not created for this slot")
        top = os.path.realpath(git(Path(real), "rev-parse", "--show-toplevel", env=self.env))
        if top != real:
            raise WorkspaceError(f"issue {path}: git toplevel {top} is not {real}")
        return real

    def leased_to(self, attempt_id: str) -> list[str]:
        return [r[0] for r in self.store.db.execute(
            "SELECT path FROM slots WHERE attempt=? AND state IN ('leased','held')", (attempt_id,))]

    def leased_paths(self, attempt_id: str) -> list[str]:
        """Paths this attempt may still issue; held slots remain evidence only."""
        return [r[0] for r in self.store.db.execute(
            "SELECT path FROM slots WHERE attempt=? AND state='leased' ORDER BY path", (attempt_id,))]

    def leased_path(self, attempt_id: str) -> Path | None:
        """The slot ``attempt_id`` holds in the leased state (a held one is no longer its to use)."""
        row = self.store.db.execute(
            "SELECT path FROM slots WHERE attempt=? AND state='leased' ORDER BY path", (attempt_id,)).fetchone()
        return Path(row[0]) if row else None

    def rows(self) -> list[dict]:
        return [dict(r) for r in self.store.db.execute(
            "SELECT * FROM slots WHERE pool LIKE ? ORDER BY path", (f"{self.owner}/%",))]

    # -- helpers -------------------------------------------------------------------------
    def _fetch(self, repo: RepoSpec) -> None:
        env = self.env
        if repo.fetch_url_overrides:
            env = dict(self.env if self.env is not None else os.environ)
            parent = git(repo.base, "config", "--get", f"remote.{repo.remote}.url", env=env)
            parent_host, parent_port, parent_path = _route_identity(parent)
            # Git inherits insteadOf into the parent fetch too. A matching
            # child must never make a rule eligible to rewrite that parent.
            if any(parent.startswith(source) for source, _ in repo.fetch_url_overrides):
                raise WorkspaceError("fetch URL override may not rewrite parent remote")
            # insteadOf is a PREFIX. Inspect every initialized descendant's
            # remotes so an external fork with a matching suffix cannot route.
            raw = git(repo.base, "submodule", "foreach", "--quiet", "--recursive",
                      "git config --get-regexp '^remote\\..*\\.url$' || test $? -eq 1", env=env)
            urls = [line.split(None, 1)[1] for line in raw.splitlines() if len(line.split(None, 1)) == 2]
            routes = []
            for source, target in repo.fetch_url_overrides:
                source_host, _, source_path = _route_identity(source)
                target_host, target_port, target_path = _route_identity(target)
                if (source_host == target_host or target_host != parent_host or target_port != parent_port
                        or target_path != source_path
                        or target_path.rsplit("/", 1)[0] != parent_path.rsplit("/", 1)[0]):
                    raise WorkspaceError("fetch URL override is outside canonical repository namespace")
                if any(url.startswith(source) and url != source for url in urls):
                    raise WorkspaceError("fetch URL override has a prefix collision")
                if source in urls:
                    routes.append((source, target))
            if routes:
                count = env.get("GIT_CONFIG_COUNT", "0")
                if not re.fullmatch(r"[0-9]{1,3}", count) or int(count) + len(routes) > 128:
                    raise WorkspaceError("cannot append fetch URL overrides to Git environment")
                n = int(count)
                for source, target in routes:
                    env[f"GIT_CONFIG_KEY_{n}"] = f"url.{target}.insteadOf"
                    env[f"GIT_CONFIG_VALUE_{n}"] = source
                    n += 1
                env["GIT_CONFIG_COUNT"] = str(n)
        git(repo.base, "fetch", repo.remote, "--prune", "--quiet", env=env)

    # Archive writes are bound to directory fds opened under the verified volume: once the destination
    # fd is open, every mkdir/create/verify is relative to it, so a lost or substituted mount can only
    # make an operation fail, never resolve the path again through the internal disk.
    _DIR = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    _archive_hook = None            # test seam: called once the destination fd is open
    _verified_hook = None           # test seam: called after all copies verified, before quarantine
    _pre_rename_hook = None         # test seam: called after the last read-back, before each rename
    _dest_name = ""
    _dest_parent = -1
    _moved_any = False              # a source entry has been renamed into the quarantine: the archive is now the only copy
    _mount_ident = None             # (mount path, its (dev, ino), archive root path, its (dev, ino)) of the open dest

    def _open_archive_dest(self, slot_name: str) -> tuple[int, int | None]:
        """Open (creating) the unique archive directory of this run; return (fd, required device or None)."""
        flags, want = self._DIR, None
        self._mount_ident = None
        if self.archive_root is None or self.archive_require_mount is None:
            base = (self.root.parent.parent / "slot-archive" / self.owner if self.archive_root is None
                    else self.archive_root / self.owner)
            base.mkdir(parents=True, exist_ok=True)
            cur = os.open(base, flags)
        else:
            mount = self.archive_require_mount
            mfd = os.open(mount, flags)                  # fails when the volume path is not there
            try:
                st, up = os.fstat(mfd), os.stat("..", dir_fd=mfd)
                if st.st_dev == up.st_dev:
                    raise WorkspaceError(f"archive volume {mount} is not mounted")
                if st.st_dev == os.stat(self.root.parent.parent).st_dev:
                    raise WorkspaceError(f"archive volume {mount} is on the same device as the runner state dir")
                want = st.st_dev
                real_mount = Path(os.path.realpath(mount))      # names only: every step below is fd-relative
                real_root = Path(os.path.realpath(self.archive_root))
                if real_mount != real_root and real_mount not in real_root.parents:
                    raise WorkspaceError(f"archive root {self.archive_root} resolves outside volume {mount}")
                cur = mfd
                steps = [(p, False) for p in real_root.relative_to(real_mount).parts] + [(self.owner, True)]
                mid = (st.st_dev, st.st_ino)
                rid = mid
                for part, create in steps:
                    if create:                               # the owner step: cur is the archive root fd
                        rst = os.fstat(cur)
                        rid = (rst.st_dev, rst.st_ino)
                    nxt = self._open_child(cur, part, want, create=create)
                    if cur != mfd:
                        os.close(cur)
                    cur = nxt
            except BaseException:
                os.close(mfd)
                raise
            if cur != mfd:
                os.close(mfd)
            self._mount_ident = (mount, mid, real_root, rid)
        try:
            stamp = iso()[:19].replace(":", "")
            n = 0
            while True:                                  # never reuse an archive dir: no overwrite
                name = f"{stamp}-{slot_name}" + (f"-{n}" if n else "")
                try:
                    os.mkdir(name, 0o700, dir_fd=cur)
                    break
                except FileExistsError:
                    n += 1
            dest = self._open_child(cur, name, want)
            self._dest_name = name
        except BaseException:
            os.close(cur)
            raise
        self._dest_parent = cur                  # stays open: the entry's liveness is checked against it
        return dest, want

    def _mount_same(self) -> bool:
        """The configured mount and archive root still name the volume and directories whose fds we hold:
        still a mount point (device differs from its parent), same (dev, ino). Unmounted, replaced or any
        check error => False (the caller holds the slot)."""
        mount, mid, root, rid = self._mount_ident
        try:
            m, up = os.stat(mount), os.stat(Path(mount).parent)
            r = os.stat(root)
        except OSError:
            return False
        return ((m.st_dev, m.st_ino) == mid and m.st_dev != up.st_dev and (r.st_dev, r.st_ino) == rid
                and Path(os.path.realpath(root)) == Path(root))

    @staticmethod
    def _open_child(parent: int, name: str, want: int | None, create: bool = False) -> int:
        if create:
            try:
                os.mkdir(name, 0o700, dir_fd=parent)
            except FileExistsError:
                pass
        try:
            fd = os.open(name, WorkspacePool._DIR, dir_fd=parent)
        except FileNotFoundError:
            raise WorkspaceError(f"archive directory {name} does not exist on the required volume") from None
        if want is not None and os.fstat(fd).st_dev != want:
            os.close(fd)
            raise WorkspaceError(f"archive directory {name} is not on the required volume")
        return fd

    def _ensure_dirs(self, fd: int, parts: tuple, want: int | None) -> int:
        """Return a new fd for fd/parts..., creating each level relative to the open parent."""
        if any(p in ("", ".", "..") or "/" in p for p in parts):
            raise WorkspaceError(f"unsafe path component in {parts!r}")
        cur = os.dup(fd)
        try:
            for part in parts:
                nxt = self._open_child(cur, part, want, create=True)
                os.close(cur)
                cur = nxt
        except BaseException:
            os.close(cur)
            raise
        return cur

    def _copy_entry(self, src: str, dfd: int, name: str, want: int | None) -> None:
        st = os.lstat(src)
        if stat.S_ISLNK(st.st_mode):
            os.symlink(os.readlink(src), name, dir_fd=dfd)
        elif stat.S_ISDIR(st.st_mode):
            sub = self._ensure_dirs(dfd, (name,), want)
            try:
                for entry in os.scandir(src):
                    self._copy_entry(entry.path, sub, entry.name, want)
                os.fchmod(sub, stat.S_IMODE(st.st_mode))
            finally:
                os.close(sub)
        elif stat.S_ISREG(st.st_mode):
            _copy_file_fd(src, dfd, name)
        else:
            raise WorkspaceError(f"cannot archive special file {src}")

    def _verify_entry(self, src: str, dfd: int, name: str) -> None:
        """The archived copy, read back through the open fd, must equal the source before the source goes."""
        st = os.lstat(src)
        if stat.S_ISLNK(st.st_mode):
            if os.readlink(src) != os.readlink(name, dir_fd=dfd):
                raise WorkspaceError(f"archived symlink differs: {name}")
        elif stat.S_ISDIR(st.st_mode):
            sub = os.open(name, self._DIR, dir_fd=dfd)
            try:
                have = {e.name for e in os.scandir(sub)}
                if {e.name for e in os.scandir(src)} != have:
                    raise WorkspaceError(f"archived directory differs: {name}")
                for entry in os.scandir(src):
                    self._verify_entry(entry.path, sub, entry.name)
            finally:
                os.close(sub)
        else:
            with open(src, "rb") as a:
                cfd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=dfd)
                with os.fdopen(cfd, "rb") as b:
                    while True:
                        x, y = a.read(1 << 20), b.read(1 << 20)
                        if x != y:
                            raise WorkspaceError(f"archived file differs: {name}")
                        if not x:
                            break

    def _quarantine(self, path: Path, srcs: list, dest: int, want: int | None) -> None:
        """Replace destruction of the archived sources by an atomic rename of each entry into a unique
        quarantine directory next to the REAL slot directory (same device as the slot, never through an
        internal logical pool path). Nothing is deleted here: on any error the data is in the slot or in
        the quarantine and the caller holds the slot. A manifest (types, checksums, archive reference) is
        committed first and finished last; `quarantine_restore` puts entries back. The runner never
        purges a quarantine."""
        real = Path(os.path.realpath(path))
        sfd, pfd = os.open(real, self._DIR), os.open(real.parent, self._DIR)
        try:
            slot_dev = os.fstat(sfd).st_dev
            qfd = self._open_child(pfd, f".quarantine-{real.name}", slot_dev, create=True)
            try:
                stamp, n = iso()[:19].replace(":", ""), 0
                while True:
                    name = f"{stamp}-{n}"
                    try:
                        os.mkdir(name, 0o700, dir_fd=qfd)
                        break
                    except FileExistsError:
                        n += 1
                run = self._open_child(qfd, name, slot_dev)
                moved = []
                try:
                    os.fsync(qfd)                   # the run directory entry (and the quarantine dir) must outlive a crash
                    os.fsync(pfd)
                    row = self.store.db.execute("SELECT attempt FROM slots WHERE path=?", (str(path),)).fetchone()
                    doc = {"state": "renaming", "slot": real.name, "owner": self.owner,
                           "attempt": (row["attempt"] if row else "") or "",
                           "archive": {"root": str(self.archive_root or "slot-archive"), "dir": self._dest_name},
                           "entries": [{"path": list(rel), "digest": _entry_digest(str(src))} for src, rel in srcs]}
                    self._write_manifest(run, doc)
                    def alive() -> bool:
                        """The archive directory we hold is still the entry its parent (held open) names: not
                        removed, replaced or swapped for a symlink. An unlinked directory keeps its fd, and on some
                        systems a nonzero link count, so the entry is looked up."""
                        st = os.fstat(dest)
                        if st.st_nlink == 0 or (want is not None and st.st_dev != want):
                            return False
                        if self._mount_ident is not None and not self._mount_same():
                            return False
                        try:
                            now = os.stat(self._dest_name, dir_fd=self._dest_parent, follow_symlinks=False)
                        except OSError:
                            return False
                        return stat.S_ISDIR(now.st_mode) and (now.st_dev, now.st_ino) == (st.st_dev, st.st_ino)
                    for src, rel in srcs:
                        parent = self._ensure_dirs(dest, rel[:-1], want)
                        try:
                            self._verify_entry(str(src), parent, rel[-1])      # archive still there, equal
                            if not alive():
                                raise WorkspaceError("archive destination was removed")
                        finally:
                            os.close(parent)
                        if self._pre_rename_hook:
                            self._pre_rename_hook()
                        if not alive():                                        # lost after the last read-back
                            raise WorkspaceError("archive destination was removed")
                        sparent, qparent = self._walk(sfd, rel[:-1]), self._ensure_dirs(run, rel[:-1], slot_dev)
                        try:
                            os.rename(rel[-1], rel[-1], src_dir_fd=sparent, dst_dir_fd=qparent)
                            moved.append(rel)
                            self._moved_any = True
                            os.fsync(sparent)       # both directory entries of the rename are durable
                            os.fsync(qparent)       # before the next entry and before 'complete'
                        finally:
                            os.close(sparent)
                            os.close(qparent)
                    for _r, _d, _f, dfd in os.fwalk(".", dir_fd=run):    # every directory created inside the run
                        os.fsync(dfd)
                    if not alive():
                        # the archive vanished after the renames: never free the slot on a missing archive,
                        # put every entry back (no replace) and hold
                        for rel in reversed(moved):
                            sparent, qparent = self._ensure_dirs(sfd, rel[:-1], None), self._walk(run, rel[:-1])
                            try:
                                _rename_noreplace(qparent, rel[-1], sparent, rel[-1])
                                os.fsync(sparent)
                            finally:
                                os.close(sparent)
                                os.close(qparent)
                        self._write_manifest(run, {**doc, "state": "restored"})
                        raise WorkspaceError("archive destination was removed; sources returned to the slot")
                    self._write_manifest(run, {**doc, "state": "complete"})
                except BaseException:
                    if not moved:                   # nothing left the slot: the run is an empty shell with a manifest
                        self._discard_dir(qfd, name, run)
                    raise
                finally:
                    os.close(run)
            finally:
                os.close(qfd)
        finally:
            os.close(sfd)
            os.close(pfd)

    def _walk(self, fd: int, parts: tuple) -> int:
        """fd of fd/parts... without creating anything (the entries exist in the slot)."""
        if any(p in ("", ".", "..") or "/" in p for p in parts):
            raise WorkspaceError(f"unsafe path component in {parts!r}")
        cur = os.dup(fd)
        try:
            for part in parts:
                nxt = os.open(part, self._DIR, dir_fd=cur)
                os.close(cur)
                cur = nxt
        except BaseException:
            os.close(cur)
            raise
        return cur

    @staticmethod
    def _write_manifest(run: int, doc: dict) -> None:
        """Crash-consistent: write a temp file, fsync it, rename over manifest.json, fsync the directory."""
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        fd = os.open("manifest.json.tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC | nofollow, 0o600, dir_fd=run)
        try:
            view = memoryview(json.dumps(doc, sort_keys=True).encode())
            while view:                                  # os.write may be short: finish or fail
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.rename("manifest.json.tmp", "manifest.json", src_dir_fd=run, dst_dir_fd=run)
        os.fsync(run)

    def _quarantine_runs(self, path: Path):
        """(run dir, manifest) of every quarantine run of this slot, oldest first."""
        real = Path(os.path.realpath(path))
        qdir = real.parent / f".quarantine-{real.name}"
        if not qdir.is_dir() or qdir.is_symlink():
            return
        for run in sorted(qdir.iterdir()):
            try:
                doc = json.loads((run / "manifest.json").read_text())
            except (OSError, ValueError):
                doc = None
            ok = _manifest_ok(doc)
            yield run, (doc if ok else None)          # a manifest of the wrong shape is as unreadable as broken JSON

    def _reconcile_quarantine(self, path: Path) -> None:
        """Recovery receipt: a run left in 'renaming' is accounted for entry by entry (moved to the
        quarantine, or still in the slot to be archived by this run). An entry found in neither place,
        or an unreadable manifest, holds the slot."""
        real = Path(os.path.realpath(path))
        for run, doc in self._quarantine_runs(path):
            if doc is None:
                raise WorkspaceError(f"quarantine {run} has no readable manifest")
            if doc.get("state") != "renaming":
                continue
            for entry in doc["entries"]:
                rel = _safe_rel(entry.get("path"))
                if not (os.path.lexists(run / rel) or os.path.lexists(real / rel)):
                    raise WorkspaceError(f"quarantine {run}: entry {rel} is neither in the slot nor quarantined")
            rfd = os.open(run, self._DIR)
            try:
                self._write_manifest(rfd, {**doc, "state": "recovered"})
            finally:
                os.close(rfd)

    def _known_slot(self, path: Path | str) -> Path:
        """Operator-supplied slot path: no '..', and it must be a slot of this pool (exact registered path)."""
        p = Path(path)
        if not p.is_absolute() or ".." in p.parts:
            raise WorkspaceError(f"slot path must be absolute without '..': {path}")
        row = self.store.db.execute("SELECT path FROM slots WHERE path=? AND pool LIKE ?",
                                    (str(p), f"{self.owner}/%")).fetchone()
        if row is None:
            raise WorkspaceError(f"not a slot of this pool: {path}")
        return p

    def quarantine_inventory(self, path: Path | str, verify: bool = False) -> list[dict]:
        """List the quarantine runs of a slot: state, archive reference, where each entry is now
        ('quarantine' / 'slot' / 'missing'), and with verify=True whether its checksums still match."""
        path = self._known_slot(path)
        real, out = Path(os.path.realpath(path)), []
        for run, doc in self._quarantine_runs(path):
            if doc is None:
                out.append({"run": str(run), "state": "unreadable", "entries": []})
                continue
            entries = []
            for entry in doc["entries"]:
                rel = _safe_rel(entry.get("path"))
                where = "quarantine" if os.path.lexists(run / rel) else "slot" if os.path.lexists(real / rel) else "missing"
                item = {"path": str(rel), "where": where}
                if verify and where == "quarantine":
                    item["intact"] = _entry_digest(str(run / rel)) == entry["digest"]
                entries.append(item)
            out.append({"run": str(run), "state": doc["state"], "attempt": doc.get("attempt", ""),
                        "archive": doc.get("archive"), "entries": entries})
        return out

    def quarantine_restore(self, path: Path | str, run_name: str) -> list[str]:
        """Move a run's entries back to their original place in the slot (atomic same-device rename).
        Every entry is checksum-verified first; an occupied target or a mismatch refuses the whole run.
        Only a held or free slot is touched, and only while this process owns it through a row
        compare-and-swap (the same row `free-slot` and the Runner use): a leased slot has an agent writing."""
        path = self._known_slot(path)
        row = self.store.db.execute("SELECT * FROM slots WHERE path=?", (str(path),)).fetchone()
        row = dict(row) if row else {}
        if row.get("state") not in ("held", "free"):
            raise WorkspaceError(f"cannot restore into {path}: slot is {row.get('state', 'unknown')}, not held or free")
        if row["state"] == "held":
            self._maintenance_guard(row["attempt"], str(path))
        cur = self.store.db.execute(
            "UPDATE slots SET state='freeing' WHERE path=? AND state=? AND attempt=? AND updated_at=?",
            (str(path), row["state"], row["attempt"], row["updated_at"]))
        if cur.rowcount != 1:
            raise WorkspaceError(f"cannot restore into {path}: the slot changed state")
        try:
            return self._restore_run(path, run_name)
        finally:                                     # back to what it was; no-op if nothing else touched it
            self.store.db.execute("UPDATE slots SET state=? WHERE path=? AND state='freeing' AND attempt=?",
                                  (row["state"], str(path), row["attempt"]))

    def _restore_run(self, path: Path, run_name: str) -> list[str]:
        real = Path(os.path.realpath(path))
        for run, doc in self._quarantine_runs(path):
            if run.name != run_name:
                continue
            if doc is None:
                raise WorkspaceError(f"quarantine {run} has no readable manifest")
            entries = [(e, _safe_rel(e.get("path"))) for e in doc["entries"]]   # validate before touching anything
            todo = [(e, rel) for e, rel in entries if os.path.lexists(run / rel)]
            for e, rel in entries:                     # an entry not quarantined must already be intact in the slot
                if (e, rel) not in todo and (not os.path.lexists(real / rel)
                                              or _entry_digest(str(real / rel)) != e["digest"]):
                    raise WorkspaceError(f"cannot restore {rel}: missing from the quarantine and not intact in the slot")
            for e, rel in todo:
                if _entry_digest(str(run / rel)) != e["digest"]:
                    raise WorkspaceError(f"quarantined {rel} does not match its manifest checksum")
                if os.path.lexists(real / rel):
                    raise WorkspaceError(f"cannot restore {rel}: the slot already has it")
            sfd, rfd = os.open(real, self._DIR), os.open(run, self._DIR)
            restored = []
            try:
                for e, rel in todo:
                    sparent, qparent = self._ensure_dirs(sfd, rel.parts[:-1], None), self._walk(rfd, rel.parts[:-1])
                    try:
                        _rename_noreplace(qparent, rel.name, sparent, rel.name)
                    except FileExistsError:
                        raise WorkspaceError(f"cannot restore {rel}: the slot already has it") from None
                    finally:
                        os.close(sparent)
                        os.close(qparent)
                    restored.append(str(rel))
                self._write_manifest(rfd, {**doc, "state": "restored"})
            finally:
                os.close(sfd)
                os.close(rfd)
            return restored
        raise WorkspaceError(f"no quarantine run {run_name} for {path}")

    def _archive_untracked(self, path: Path) -> None:
        """Move untracked, non-ignored files out of a slot before it is returned; nothing is deleted
        until every archived copy has been read back equal. Any failure is a WorkspaceError, so the
        caller holds the slot instead of crashing."""
        env = {**(self.env if self.env is not None else os.environ), "GIT_TERMINAL_PROMPT": "0"}
        dest = -1
        done = False
        self._moved_any = False
        try:
            res = subprocess.run(["git", "-C", str(path), "ls-files", "--others", "--exclude-standard",
                                  "-z", "--directory"], capture_output=True, timeout=600, env=env)
            if res.returncode != 0:
                raise WorkspaceError(f"ls-files in {path}: {res.stderr.decode(errors='replace').strip()[:200]}")
            names = [os.fsdecode(n) for n in res.stdout.split(b"\0") if n]    # raw: no strip of odd names
            self._reconcile_quarantine(path)
            if not names:
                return
            dest, want = self._open_archive_dest(path.name)
            if self._archive_hook:
                self._archive_hook()
            srcs = []
            for name in names:                         # copy everything first; sources stay until verified
                rel = Path(name.rstrip("/")).parts
                parent = self._ensure_dirs(dest, rel[:-1], want)
                try:
                    self._copy_entry(str(path / name.rstrip("/")), parent, rel[-1], want)
                    srcs.append((path / name.rstrip("/"), rel))
                finally:
                    os.close(parent)
            for src, rel in srcs:
                parent = self._ensure_dirs(dest, rel[:-1], want)
                try:
                    self._verify_entry(str(src), parent, rel[-1])
                finally:
                    os.close(parent)
            if self._verified_hook:
                self._verified_hook()
            self._quarantine(path, srcs, dest, want)
            done = True
        except (OSError, subprocess.SubprocessError) as e:
            raise WorkspaceError(f"archive untracked in {path}: {e}") from None
        finally:
            if not done and dest >= 0 and not self._moved_any:
                self._discard_archive_dest(dest)
            if dest >= 0:
                os.close(dest)
            if self._dest_parent >= 0:
                os.close(self._dest_parent)
                self._dest_parent = -1

    def _discard_archive_dest(self, dest: int) -> None:
        """A failed run removes the archive directory it created itself, so a held slot retried by every
        sweep does not leave one full copy per retry. Only called while no source entry has left the slot
        (the sources are intact, the copy is redundant). Everything is relative to the held fds; the entry
        is removed only if its parent still names the directory we created. Best effort: an error leaves
        the directory in place (a duplicate, never a loss)."""
        try:
            st = os.fstat(dest)
            now = os.stat(self._dest_name, dir_fd=self._dest_parent, follow_symlinks=False)
            if (now.st_dev, now.st_ino) != (st.st_dev, st.st_ino) or not stat.S_ISDIR(now.st_mode):
                return
            os.fchmod(dest, 0o700)
            for _r, dirs, files, dfd in os.fwalk(".", dir_fd=dest, topdown=False):
                os.fchmod(dfd, 0o700)                # copied directories keep the source mode (e.g. 0555)
                for f in files:
                    os.unlink(f, dir_fd=dfd)
                for d in dirs:
                    try:
                        os.rmdir(d, dir_fd=dfd)
                    except FileNotFoundError:
                        pass
                    except OSError:                  # a symlink to a directory is listed in dirs by fwalk
                        os.unlink(d, dir_fd=dfd)
            if self._same_entry(self._dest_parent, self._dest_name, dest):   # recheck after the walk
                os.rmdir(self._dest_name, dir_fd=self._dest_parent)
        except OSError:
            pass

    @staticmethod
    def _discard_dir(parent: int, name: str, fd: int) -> None:
        """Remove directory `name` of `parent` (open as `fd`) with its contents, fd-relative, only if the
        entry still is that directory. Best effort: an error leaves it in place."""
        try:
            st = os.fstat(fd)
            now = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if (now.st_dev, now.st_ino) != (st.st_dev, st.st_ino) or not stat.S_ISDIR(now.st_mode):
                return
            for _r, dirs, files, dfd in os.fwalk(".", dir_fd=fd, topdown=False):
                for f in files:
                    os.unlink(f, dir_fd=dfd)
                for d in dirs:
                    try:
                        os.rmdir(d, dir_fd=dfd)
                    except OSError:
                        os.unlink(d, dir_fd=dfd)
            if WorkspacePool._same_entry(parent, name, fd):          # recheck after the walk
                os.rmdir(name, dir_fd=parent)
        except OSError:
            pass

    @staticmethod
    def _same_entry(parent: int, name: str, fd: int) -> bool:
        """`name` in `parent` still is the directory open as `fd` (rmdir only ever removes an empty one)."""
        st, now = os.fstat(fd), os.stat(name, dir_fd=parent, follow_symlinks=False)
        return stat.S_ISDIR(now.st_mode) and (now.st_dev, now.st_ino) == (st.st_dev, st.st_ino)

    def _free(self, path: Path, repo: RepoSpec) -> None:
        self._archive_untracked(path)
        # Detach at the remote tip: frees any branch and pins no old commit.
        git(path, "checkout", "--quiet", "--detach", f"{repo.remote}/{repo.branch}", env=self.env)
        self._set(path, repo.name, "free")

    def _new_path(self, repo: RepoSpec, avoid=()) -> Path:
        base = self.root / repo.name
        base.mkdir(parents=True, exist_ok=True)
        taken = {Path(s["path"]).name for s in self._rows(repo.name)}
        taken |= {Path(p).name for p in avoid if Path(p).parent == base}
        n = 1
        while f"slot-{n}" in taken or (base / f"slot-{n}").exists():
            n += 1
        return base / f"slot-{n}"

    def _enable_worktree_config(self, path: Path) -> None:
        """Enable per-worktree config without changing existing worktrees' meaning.

        With the extension off, Git treats shared core.bare/core.worktree as
        main-only settings. Enabling it removes that exception. Move those keys
        to the main config.worktree before atomically publishing the extension.
        The caller holds base_lock; config.lock also excludes external Git writers.
        """
        common = Path(git(path, "rev-parse", "--path-format=absolute", "--git-common-dir", env=self.env))
        config, pending = common / "config", common / "config.lock"
        try:
            fd = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            raise WorkspaceError("worktree config: shared config lock already exists") from None
        published = False
        try:
            with os.fdopen(fd, "wb") as out:
                original = config.read_bytes()
                out.write(original)
            for key in ("core.bare", "core.worktree"):
                value = git(path, "config", "--file", str(config), "--get", key, check=False, env=self.env)
                if key == "core.bare":
                    value = git(path, "config", "--file", str(config), "--type=bool", "--get", key,
                                check=False, env=self.env)
                    if value != "true":
                        continue
                elif not value:
                    continue
                git(path, "config", "--file", str(common / "config.worktree"), key, value, env=self.env)
                git(path, "config", "--file", str(pending), "--unset-all", key, env=self.env)
            git(path, "config", "--file", str(pending), "extensions.worktreeConfig", "true", env=self.env)
            if config.read_bytes() != original:
                raise WorkspaceError("worktree config: shared config changed during migration")
            pending.chmod(stat.S_IMODE(config.stat().st_mode))
            os.replace(pending, config)
            published = True
        finally:
            if not published:
                pending.unlink(missing_ok=True)

    def _configure(self, path: Path) -> None:
        enabled = git(path, "config", "--type=bool", "--get", "extensions.worktreeConfig",
                      check=False, env=self.env) == "true"
        if not enabled:
            if not self.identity:
                return
            self._enable_worktree_config(path)
        # Legacy bases may already have the extension and shared core.bare=true.
        # Repair only this owned slot; the shared base and other slots stay untouched.
        git(path, "config", "--worktree", "core.bare", "false", env=self.env)
        if not self.identity:
            return
        git(path, "config", "--worktree", "user.name", self.identity[0], env=self.env)
        git(path, "config", "--worktree", "user.email", self.identity[1], env=self.env)
