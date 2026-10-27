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

import logging
import os
import re
import shutil
import sqlite3
import subprocess
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


class WorkspacePool:
    def __init__(self, store: Store, root: Path, repos: list[RepoSpec], identity: tuple[str, str] | None = None,
                 env: dict[str, str] | None = None, owner: str = "default", min_free_gb: float = 0,
                 checkpoint_exclude_names: tuple | None = None):
        # env: the agent's environment, so fetches use its credentials (e.g. GIT_SSH_COMMAND)
        self.store, self.identity, self.env, self.owner = store, identity, env, owner
        self.min_free_gb = min_free_gb
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
    def lease(self, attempt_id: str, repo_name: str) -> Path:
        repo = self.repos[repo_name]
        with base_lock(repo.base):
            return self._lease(attempt_id, repo_name, repo)

    def _lease(self, attempt_id: str, repo_name: str, repo: RepoSpec) -> Path:
        self._check_disk()
        self._clean_staging(repo)
        self._fetch(repo)
        self._check_disk()
        start = f"{repo.remote}/{repo.branch}"
        for slot in self._rows(repo_name, "free"):
            path = Path(slot["path"])
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
        path = self._new_path(repo)
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
        self._configure(path)
        self._set(path, repo_name, "leased", attempt_id)
        return path

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

    def release(self, attempt_id: str) -> list[tuple[str, str]]:
        """Return the attempt's slots. Unsafe slots are held, never wiped.
        Returns [(path, reason)] for slots that were held."""
        kept = []
        rows = [dict(r) for r in self.store.db.execute(
            "SELECT * FROM slots WHERE attempt=? AND state='leased'", (attempt_id,))]
        for slot in rows:
            repo_name = slot["pool"].split("/", 1)[1]
            path, repo = Path(slot["path"]), self.repos.get(repo_name)
            reason = "repository no longer configured" if repo is None else self.unsafe_reason(path, repo)
            if not reason:
                try:
                    with base_lock(repo.base):
                        saved = self._save_detached_commits(path, repo, attempt_id)
                        self._free(path, repo)
                    if saved:
                        kept.append((str(path), f"commits saved as {saved}; slot returned"))
                    continue
                except WorkspaceError as e:
                    reason = f"cannot return: {e}"
            self._set(path, repo_name, "held", attempt_id, reason)
            kept.append((str(path), reason))
        return kept

    def suspend(self, attempt_id: str, metadata: dict, *, writer_stopped: bool = False) -> list[str]:
        """Checkpoint every owned lease before release. Caller proves the process group stopped.

        A durable journal makes release and restore retryable after any runner restart.
        Partial multi-repo suspension is safe: only independently verified slots are returned.
        """
        if not writer_stopped:
            raise WorkspaceError("checkpoint: writer stop not established")
        from .checkpoint import _clear_tree, base_objects_dir
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
                self._settle_row(attempt_id, row, checkpoints)      # same lock as the lease and the checkout
                # Restart recovery may already own a target lease, bypassing lease()'s
                # admission guard. Check before changing the journal or replaying bytes.
                # A completed restore is read-only and remains safe to re-enter.
                if row["phase"] != "restored":
                    self._check_disk()
                if row["phase"] == "released":
                    # lease is idempotent across a crash before the journal update.
                    owned = [r for r in self._rows(row["repo"], "leased") if r["attempt"] == attempt_id]
                    target = Path(owned[0]["path"]) if owned else self.lease(attempt_id, row["repo"])
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
                    self._fetch(repo)
                except WorkspaceError:
                    continue
                path = Path(slot["path"])
                if not self.unsafe_reason(path, repo):
                    try:
                        self._save_detached_commits(path, repo, slot["attempt"] or "held")
                        self._free(path, repo)
                        freed.append(slot["path"])
                    except WorkspaceError:
                        continue
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
        repo = self.repos.get(row["pool"].split("/", 1)[1])
        if repo is None:
            raise WorkspaceError("repository not configured")
        with base_lock(repo.base):
            if self._archive_candidate(path, expect_attempt) != row:      # changed while waiting for the lock
                raise WorkspaceError("slot changed before it could be archived")
            slot = Path(path)
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
        return ref

    def free_path(self, path: str, archive: bool = False) -> str:
        """Operator command: return a held slot if it is safe; never forces. With ``archive``, a
        slot held only for uncommitted changes is freed after its work is saved on a ref."""
        row = self.store.db.execute("SELECT * FROM slots WHERE path=?", (path,)).fetchone()
        if not row:
            return "unknown slot"
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
            saved = self._save_detached_commits(Path(path), repo, dict(row)["attempt"] or "held")
            self._free(Path(path), repo)
        return "freed" + (f" (commits saved as {saved})" if saved else "")

    def full(self, repo_name: str) -> bool:
        """No slot to lease: none free and the pool at its maximum. No git, no lock: a cheap
        pre-check before a task is checked out; lease() still decides."""
        return (not self._rows(repo_name, "free")
                and len(self._rows(repo_name)) >= self.repos[repo_name].max_slots)

    def leased_to(self, attempt_id: str) -> list[str]:
        return [r[0] for r in self.store.db.execute(
            "SELECT path FROM slots WHERE attempt=? AND state IN ('leased','held')", (attempt_id,))]

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

    def _archive_untracked(self, path: Path) -> None:
        """Move untracked, non-ignored files out of a slot before it is returned; nothing is deleted.
        Any failure is a WorkspaceError, so the caller holds the slot instead of crashing."""
        env = {**(self.env if self.env is not None else os.environ), "GIT_TERMINAL_PROMPT": "0"}
        try:
            res = subprocess.run(["git", "-C", str(path), "ls-files", "--others", "--exclude-standard",
                                  "-z", "--directory"], capture_output=True, timeout=600, env=env)
            if res.returncode != 0:
                raise WorkspaceError(f"ls-files in {path}: {res.stderr.decode(errors='replace').strip()[:200]}")
            names = [os.fsdecode(n) for n in res.stdout.split(b"\0") if n]    # raw: no strip of odd names
            if not names:
                return
            base = self.root.parent.parent / "slot-archive" / self.owner
            base.mkdir(parents=True, exist_ok=True)
            stamp = iso()[:19].replace(":", "")
            n = 0
            while True:                                # never reuse an archive dir: no overwrite
                dest = base / (f"{stamp}-{path.name}" + (f"-{n}" if n else ""))
                try:
                    dest.mkdir()
                    break
                except FileExistsError:
                    n += 1
            for name in names:
                tgt = dest / name.rstrip("/")
                tgt.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(path / name.rstrip("/")), str(tgt))    # moves symlinks as links
        except (OSError, subprocess.SubprocessError) as e:
            raise WorkspaceError(f"archive untracked in {path}: {e}") from None

    def _free(self, path: Path, repo: RepoSpec) -> None:
        self._archive_untracked(path)
        # Detach at the remote tip: frees any branch and pins no old commit.
        git(path, "checkout", "--quiet", "--detach", f"{repo.remote}/{repo.branch}", env=self.env)
        self._set(path, repo.name, "free")

    def _new_path(self, repo: RepoSpec) -> Path:
        base = self.root / repo.name
        base.mkdir(parents=True, exist_ok=True)
        taken = {Path(s["path"]).name for s in self._rows(repo.name)}
        n = 1
        while f"slot-{n}" in taken or (base / f"slot-{n}").exists():
            n += 1
        return base / f"slot-{n}"

    def _configure(self, path: Path) -> None:
        if not self.identity:
            return
        # Per-worktree identity: never touch the shared config other worktrees read.
        if git(path, "config", "--get", "extensions.worktreeConfig", check=False, env=self.env).strip() != "true":
            git(path, "config", "extensions.worktreeConfig", "true", env=self.env)   # shared config: once
        git(path, "config", "--worktree", "user.name", self.identity[0], env=self.env)
        git(path, "config", "--worktree", "user.email", self.identity[1], env=self.env)
