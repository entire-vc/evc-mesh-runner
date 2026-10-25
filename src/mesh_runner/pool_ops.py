"""Bounded pool metrics and conservative, explicit operator eviction.

Eviction apply requires the exclusive Runner lock (CLI); stop the runner first.
No force removal or git clean.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import logging
import math
import itertools
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from .checkpoint import CheckpointBaseMissing, base_objects_dir
from .store import Store, iso
from .workspace import (POOL_ADMIN_HOLD_NOTE, POOL_EVICTION_HOLD_NOTE, POOL_INDEX_FLAGS_HOLD_NOTE,
                        WorkspacePool, WorkspaceError, _COMMON_DIRS, base_lock, git)

_SIZE_CACHE: dict[str, tuple[float, int | None]] = {}


def summary(store: Store, root: Path) -> dict:
    counts = {state: 0 for state in ("free", "leased", "held")}
    if store.db.execute("SELECT 1 FROM sqlite_master WHERE name='slots'").fetchone():
        for row in store.db.execute("SELECT state, COUNT(*) FROM slots GROUP BY state"):
            counts[row[0]] = row[1]
    cached = _SIZE_CACHE.get(str(root))
    if cached and time.monotonic() - cached[0] < 60:
        size = cached[1]
    else:
        size = None
        if not root.exists():
            size = 0
        else:
            try:
                result = subprocess.run(["du", "-sk", str(root)], capture_output=True,
                                        text=True, timeout=3)
                if result.returncode == 0:
                    size = int(result.stdout.split()[0]) / 1024
            except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
                pass
        _SIZE_CACHE[str(root)] = (time.monotonic(), size)
    path = root
    while not path.exists() and path != path.parent:
        path = path.parent
    try:
        free = shutil.disk_usage(path).free / 1024**3
    except OSError:
        free = None
    # Read a legacy eviction count if present; status never creates this table.
    exists = store.db.execute("SELECT 1 FROM sqlite_master WHERE name='pool_evictions'").fetchone()
    evicted = store.db.execute("SELECT COUNT(*) FROM pool_evictions").fetchone()[0] if exists else 0
    return {"pool_size_mb": size, "size_sample_seconds": 60,
            "slots_total": sum(counts.values()), **{f"slots_{k}": v for k, v in counts.items()},
            "disk_free_gb": free, "evicted_total": evicted}


def eviction_candidates(pool: WorkspacePool, *, idle_hours: float, keep: int, deadline: float | None = None) -> list[dict]:
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=idle_hours)
    candidates = []
    deadline = min(deadline if deadline is not None else float("inf"), time.monotonic() + 10)
    inspected = 0
    def inspect_git(path, *args, **kwargs):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise WorkspaceError("pool inspection budget exhausted")
        return git(path, *args, timeout=min(3, remaining), env=pool.env, **kwargs)
    for name, repo in pool.repos.items():
        # Keep the lowest numbered known slots, irrespective of state.
        rows = pool._rows(name)
        def number(row):
            m = re.fullmatch(r"slot-([1-9][0-9]*)", Path(row["path"]).name)
            return int(m[1]) if m else 0
        rows.sort(key=number)
        for row in rows[keep:]:
            if inspected >= 64 or time.monotonic() >= deadline:
                return candidates
            inspected += 1
            if row["state"] != "free" or row["attempt"]:
                continue
            try:
                updated = dt.datetime.fromisoformat(row["updated_at"])
                if updated.tzinfo is None or updated > cutoff:
                    continue
                path = Path(row["path"])
                expected = pool.root / name / path.name
                if not re.fullmatch(r"slot-[1-9][0-9]*", path.name) or path != expected:
                    continue
                if path.resolve() != pool.root.resolve() / name / path.name or not os.path.lexists(path / ".git"):
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise WorkspaceError("pool inspection budget exhausted")
                if pool._admin_reason(path, repo, timeout=min(3, remaining)):
                    # Any present .git that is not a sound link to this slot's own admin dir (a symlink,
                    # dangling or not, a directory, a foreign or reused admin dir): git in this slot would
                    # act on another worktree, and `worktree remove` must never run on it. Apply
                    # quarantines it, dry-run does not offer it.
                    candidates.append({**row, "_blocked_reason": POOL_ADMIN_HOLD_NOTE})
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise WorkspaceError("pool inspection budget exhausted")
                if pool.index_flags_reason(path, timeout=min(3, remaining)):
                    # Apply will durably quarantine this exact slot. Dry-run
                    # must remain read-only and must not advertise it as safe.
                    candidates.append({**row, "_blocked_reason": POOL_INDEX_FLAGS_HOLD_NOTE})
                    continue
                # Also ignored data stays intact: no assumptions about its value.
                if inspect_git(path, "status", "--porcelain", "--untracked-files=all", "--ignored"):
                    continue
                if inspect_git(path, "rev-list", "--count", "HEAD", "--not", "--remotes") != "0":
                    continue
                # Only detached worktrees returned by Runner, no operator branches.
                if inspect_git(path, "symbolic-ref", "-q", "HEAD", check=False):
                    continue
                registered = inspect_git(repo.base, "worktree", "list", "--porcelain")
                registered_paths = [Path(line[9:]).resolve() for line in registered.splitlines()
                                    if line.startswith("worktree ")]
                if path.resolve() not in registered_paths:
                    continue
                candidates.append(row)
            except (ValueError, OSError, WorkspaceError):
                continue
    return candidates


@contextlib.contextmanager
def _bounded_base_lock(base: Path, deadline: float):
    """base_lock whose resolution and acquisition both stay inside the eviction deadline."""
    try:
        lock = base_lock(base, timeout=max(0.1, min(30, deadline - time.monotonic())))
    except WorkspaceError:
        yield False
        return
    if os.path.realpath(base) not in _COMMON_DIRS:
        # base_lock fell back to a path-keyed lock (common dir unresolved in time): not the
        # repository lock other writers use, so fail closed.
        yield False
        return
    if not lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
        yield False
        return
    try:
        yield True
    finally:
        lock.release()


def evict(pool: WorkspacePool, *, idle_hours: float, keep: int, limit: int, apply: bool = False,
          deadline: float | None = None) -> list[str]:
    """Remove at most limit slots; apply only under CLI's exclusive runner lock.

    Claiming as held before git removal keeps a crash from returning the slot to
    another writer. A failed removal remains held for explicit operator inspection.
    """
    deadline = deadline if deadline is not None else time.monotonic() + 30
    removed = []
    for row in eviction_candidates(pool, idle_hours=idle_hours, keep=keep, deadline=deadline)[:limit]:
        if time.monotonic() >= deadline:
            break
        path = Path(row["path"])
        if not apply:
            if not row.get("_blocked_reason"):
                removed.append(str(path))
            continue
        name = row["pool"].split("/", 1)[1]
        repo = pool.repos[name]
        with _bounded_base_lock(repo.base, deadline) as locked:
            if not locked:
                break
            # Reinspect inside the repository lock; no force fallback.
            if row not in eviction_candidates(pool, idle_hours=idle_hours, keep=keep, deadline=deadline):
                continue
            if row.get("_blocked_reason"):
                pool._set(path, name, "held", note=row["_blocked_reason"])
                if row["_blocked_reason"] == POOL_ADMIN_HOLD_NOTE:
                    pool._warn_admin(path, repo)
                continue
            if time.monotonic() >= deadline:
                break
            claim = pool.store.db.execute("UPDATE slots SET state='held',note=? "
                "WHERE path=? AND state='free' AND attempt='' AND updated_at=?",
                (POOL_EVICTION_HOLD_NOTE, str(path), row["updated_at"]))
            if claim.rowcount != 1:
                continue
            pool.store.db.execute("CREATE TABLE IF NOT EXISTS pool_evictions (path TEXT, evicted_at TEXT)")
            try:
                git(repo.base, "worktree", "remove", str(path), timeout=max(0.1, min(30, deadline - time.monotonic())), env=pool.env)
            except WorkspaceError:
                # No deletion fallback. Retain row/held even after uncertain subprocess outcome.
                continue
            pool.store.db.execute("INSERT INTO pool_evictions VALUES(?,?)", (str(path), iso()))
            pool.store.db.execute("DELETE FROM slots WHERE path=? AND state='held' AND note=?",
                                  (str(path), POOL_EVICTION_HOLD_NOTE))
            removed.append(str(path))
            _SIZE_CACHE.pop(str(pool.root.parent), None)
    return removed


# -- regenerable ignored directories in idle free slots ---------------------------------
IGNORED_ALLOWLIST = frozenset({"node_modules", "target", ".next", "dist", ".venv"})
log = logging.getLogger("mesh_runner")


def _budget(deadline: float, cap: float) -> float:
    """Seconds a single step may take: its own cap, never more than what is left of the pass."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise WorkspaceError("ignored cleanup budget exhausted")
    return min(cap, remaining)


def _dir_bytes(path: Path, deadline: float) -> int:
    """du estimate; an expired budget raises instead of reporting a bogus 0."""
    try:
        result = subprocess.run(["du", "-sk", str(path)], capture_output=True, text=True,
                                timeout=_budget(deadline, 60))
        return int(result.stdout.split()[0]) * 1024 if result.returncode == 0 else 0
    except subprocess.TimeoutExpired:
        raise WorkspaceError("ignored cleanup budget exhausted") from None
    except (OSError, ValueError, IndexError):
        return 0


def _bounded_rmtree(target: Path, deadline: float, anchor: Path | None = None) -> None:
    """rmtree that never follows symlinks and checks the budget before every single scan,
    unlink and rmdir (WorkspaceError once spent), so it cannot keep holding the base lock;
    a half-removed regenerable dir is harmless. Directories are read in bounded batches.
    The walk is anchored: `anchor` (the pool root, trusted config) is opened once, every
    component down to `target` and below is then opened O_NOFOLLOW relative to the previous
    fd, so a slot, an ancestor or a subdirectory swapped for a symlink after the checks is
    refused (ELOOP) instead of followed out of the pool."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    anchor = anchor if anchor is not None else target.parent
    parts = target.relative_to(anchor).parts
    if not parts:
        raise WorkspaceError("ignored cleanup target is the anchor itself")
    opened = []
    try:
        # canonical path of the anchor, taken once; then walked from "/" component by component
        # with O_NOFOLLOW, so a symlink planted in any ancestor afterwards is refused too
        cur = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        opened.append(cur)
        for part in Path(os.path.realpath(anchor)).parts[1:]:
            cur = os.open(part, flags, dir_fd=cur)
            opened.append(cur)
        for part in parts[:-1]:
            cur = os.open(part, flags, dir_fd=cur)
            opened.append(cur)
        parent_fd = cur
        stack = [(parts[-1], os.open(parts[-1], flags, dir_fd=parent_fd))]
        opened.append(stack[0][1])
        while stack:
            _budget(deadline, 1)
            name, fd = stack[-1]
            with os.scandir(fd) as it:
                batch = list(itertools.islice(it, 256))
            if not batch:
                _budget(deadline, 1)                    # the scan itself may have outlived the budget
                stack.pop()
                up = stack[-1][1] if stack else parent_fd
                os.rmdir(name, dir_fd=up)
                os.close(fd)
                opened.remove(fd)
                continue
            for entry in batch:
                _budget(deadline, 1)
                if entry.is_dir(follow_symlinks=False):
                    child = os.open(entry.name, flags, dir_fd=fd)   # ELOOP if swapped for a symlink
                    opened.append(child)
                    stack.append((entry.name, child))
                    break                               # descend first; siblings are rescanned later
                os.unlink(entry.name, dir_fd=fd)
    finally:
        for fd in opened:
            os.close(fd)


def _slot_inside_pool(pool: WorkspacePool, name: str, path: Path) -> bool:
    """The slot and every component below the pool root are real directories, and its canonical
    location is exactly <pool root>/<repo>/<slot>: a symlinked slot or ancestor is refused."""
    if pool.root.is_symlink() or path.parent.is_symlink() or path.is_symlink() or not path.is_dir():
        return False
    return path.resolve() == pool.root.resolve() / name / path.name


def _slot_skip_reason(pool: WorkspacePool, repo, path: Path, deadline: float) -> str:
    """Why this slot's ignored data must stay; empty when the slot is clean and fully pushed."""
    def run(*args, **kwargs):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise WorkspaceError("ignored cleanup budget exhausted")
        return git(path, *args, timeout=min(30, remaining), env=pool.env, **kwargs)
    if not os.path.lexists(path / ".git"):
        return "no worktree .git file"
    if pool._admin_reason(path, repo, timeout=_budget(deadline, 30)):
        return "admin link"      # a symlinked or foreign link: git here would act on another worktree
    if (path / ".gitmodules").exists() or Path(run("rev-parse", "--path-format=absolute",
                                                   "--git-path", "modules")).exists():
        return "submodule metadata"
    if pool.index_flags_reason(path, timeout=_budget(deadline, 30)):
        return "index flags"
    if run("status", "--porcelain", "--untracked-files=all"):
        return "dirty tree"
    if run("rev-list", "--count", "HEAD", "--not", "--remotes") != "0":
        return "unpushed commits"
    if run("rev-parse", "-q", "--verify", "refs/stash", check=False):
        return "stash"           # refs/stash is shared by every worktree of the repository
    return ""


def _prunable_dirs(pool: WorkspacePool, path: Path, deadline: float) -> list[Path]:
    """Allowlisted git-ignored directories inside the slot, never symlinks, never tracked."""
    root = path.resolve()
    _budget(deadline, 1)
    out = git(path, "ls-files", "--others", "--ignored", "--exclude-standard", "--directory", "-z",
              timeout=_budget(deadline, 60), env=pool.env)
    found = []
    for entry in out.split("\0"):
        if not entry.endswith("/"):
            continue
        rel = entry.rstrip("/")
        if os.path.basename(rel) not in IGNORED_ALLOWLIST:
            continue
        target = path / rel
        try:
            if target.is_symlink() or not target.is_dir():
                continue
            if root not in target.resolve().parents:       # never outside the slot root
                continue
            if git(path, "ls-files", "--", rel, timeout=_budget(deadline, 30), env=pool.env):
                continue                                   # tracked content inside
            if subprocess.run(["git", "-C", str(path), "check-ignore", "-q", "--", rel],
                              env=pool.env, timeout=_budget(deadline, 30)).returncode != 0:
                continue
        except (OSError, WorkspaceError, subprocess.TimeoutExpired):
            continue
        found.append(target)
    return found


def prune_ignored(pool: WorkspacePool, *, idle_hours: float, apply: bool = False,
                  deadline: float | None = None) -> dict:
    """Delete regenerable ignored dirs (node_modules, target, ...) from idle free slots.

    A slot qualifies only when free with no attempt, idle (slots.updated_at) longer than
    idle_hours, clean, fully pushed, without stash or submodules. State and deletion happen
    under the repository base_lock, the one lease() takes, so a slot cannot be handed out
    mid-removal. Dry run reports the same set without touching anything."""
    if isinstance(idle_hours, bool) or not isinstance(idle_hours, (int, float)):
        raise ValueError(f"idle_hours must be a number, got {idle_hours!r}")
    if not math.isfinite(idle_hours) or idle_hours < 0:
        raise ValueError(f"idle_hours must be >= 0, got {idle_hours!r}")   # never a cutoff in the future
    deadline = deadline if deadline is not None else time.monotonic() + 120
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=idle_hours)
    result = {"applied": apply, "freed_bytes": 0, "eligible_bytes": 0, "eligible_slots": 0,
              "errors": 0, "skipped": {}, "dirs": []}
    def skip(reason):
        result["skipped"][reason] = result["skipped"].get(reason, 0) + 1
    for name, repo in pool.repos.items():
        for row in pool._rows(name):
            if row["state"] != "free" or row["attempt"]:
                skip("leased" if row["state"] == "leased" else row["state"])
                continue
            if time.monotonic() >= deadline:
                return result
            path = Path(row["path"])
            try:
                updated = dt.datetime.fromisoformat(row["updated_at"])
            except (ValueError, TypeError):
                skip("bad-updated-at")
                continue
            if updated.tzinfo is None or not re.fullmatch(r"slot-[1-9][0-9]*", path.name) \
                    or path != pool.root / name / path.name:
                skip("unexpected-path")
                continue
            if updated > cutoff:
                skip("fresh-released")
                continue
            if not _slot_inside_pool(pool, name, path):
                skip("slot-outside-pool")
                continue
            with _bounded_base_lock(repo.base, deadline) as locked:
                if not locked:
                    return result
                fresh = pool.store.db.execute(
                    "SELECT 1 FROM slots WHERE path=? AND state='free' AND attempt='' AND updated_at=?",
                    (str(path), row["updated_at"])).fetchone()
                if not fresh:
                    skip("changed-under-lock")
                    continue
                try:
                    if not _slot_inside_pool(pool, name, path):      # re-checked under the lock
                        skip("slot-outside-pool")
                        continue
                    reason = _slot_skip_reason(pool, repo, path, deadline)
                    if reason:
                        skip(reason)
                        continue
                    targets = _prunable_dirs(pool, path, deadline)
                except (OSError, WorkspaceError):
                    skip("cannot-inspect")
                    continue
                if not targets:
                    skip("nothing-to-prune")
                    continue
                result["eligible_slots"] += 1
                for target in targets:
                    if time.monotonic() >= deadline:
                        return result
                    try:
                        size = _dir_bytes(target, deadline)
                    except WorkspaceError:
                        return result                  # budget spent: start no deletion
                    result["eligible_bytes"] += size
                    if apply:
                        if time.monotonic() >= deadline:
                            return result
                        try:
                            _bounded_rmtree(target, deadline, anchor=pool.root)
                        except WorkspaceError:
                            log.warning("ignored cleanup %s: budget spent mid-removal", target)
                            result["errors"] += 1
                            return result
                        except OSError as e:
                            log.warning("ignored cleanup %s: %s", target, e)
                            result["errors"] += 1
                            continue
                        log.info("ignored cleanup: removed %s slot=%s/%s bytes=%d", target.relative_to(path),
                                 name, path.name, size)
                        result["freed_bytes"] += size
                    result["dirs"].append({"slot": str(path), "dir": str(target.relative_to(path)), "bytes": size})
    return result


_LIVE_ATTEMPT_STATES = frozenset({"starting", "running", "unknown", "waiting", "ready"})
_CHECKPOINT_REF_PREFIXES = ("refs/runner-checkpoints/", "refs/runner-checkpoint-bases/")


def _ended_before(updated_at: str | None, cutoff: float) -> bool:
    """True only for a parseable timestamp older than cutoff; anything else keeps the checkpoint."""
    try:
        stamp = dt.datetime.fromisoformat((updated_at or "").replace("Z", "+00:00"))
    except ValueError:
        return False
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=dt.timezone.utc)
    return stamp.timestamp() < cutoff


def _checkpoint_repo(path: Path) -> str | None:
    try:
        import json
        return json.loads((path / "manifest.json").read_text())["metadata"]["repo"]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def prune_checkpoints(pool: WorkspacePool, *, grace_hours: float, apply: bool = False,
                      deadline: float | None = None) -> dict:
    """Remove workspace checkpoints that nothing can restore any more, and the refs pinning them.

    Garbage is: the checkpoint of an attempt that ended (not starting/running/unknown/waiting/
    ready) more than grace_hours ago; a checkpoint directory no journal row names (the generation
    a later suspend replaced) older than grace_hours; a .pending- capture directory while no
    capture runs in this process. A checkpoint of a live attempt is never touched, and without
    an attempts table nothing is. Each decision and the rename into .trash- happen under the
    repository base_lock that suspend() holds while it captures and journals, so a capture in
    flight is never mistaken for garbage. Removal is bounded by the deadline; what is left in
    .trash- is finished on the next pass. Dry run reports the same set without touching anything."""
    if isinstance(grace_hours, bool) or not isinstance(grace_hours, (int, float)):
        raise ValueError(f"grace_hours must be a number, got {grace_hours!r}")
    if not math.isfinite(grace_hours) or grace_hours < 0:
        raise ValueError(f"grace_hours must be >= 0, got {grace_hours!r}")
    deadline = deadline if deadline is not None else time.monotonic() + 120
    result = {"owner": pool.owner, "checkpoints": [], "rows": 0, "refs": 0, "freed_bytes": 0, "errors": 0}
    all_roots = pool.root.parent / "checkpoints"
    root = all_roots / pool.owner
    db = pool.store.db
    if not root.is_dir() or not db.execute("SELECT 1 FROM sqlite_master WHERE name='attempts'").fetchone():
        return result
    pool._checkpoints()
    cutoff = time.time() - grace_hours * 3600

    def victim(path: Path, reason: str, row: dict | None) -> None:
        entry = {"checkpoint": str(path), "reason": reason, "bytes": 0}
        if path.exists() and not apply:
            entry["bytes"] = _dir_bytes(path, deadline)
        if apply:
            if path.exists():
                os.rename(path, root / f".trash-{path.name}")
            if row is not None:
                db.execute("DELETE FROM workspace_checkpoints WHERE owner=? AND attempt=? AND repo=?",
                           (pool.owner, row["attempt"], row["repo"]))
                result["rows"] += 1
        result["checkpoints"].append(entry)

    try:
        for repo_name, repo in pool.repos.items():
            if time.monotonic() >= deadline:
                break
            with _bounded_base_lock(repo.base, deadline) as locked:
                if not locked:
                    result["errors"] += 1
                    continue
                rows = [dict(r) for r in db.execute(
                    "SELECT w.*, a.state AS attempt_state, a.updated_at AS attempt_updated "
                    "FROM workspace_checkpoints w LEFT JOIN attempts a ON a.id = w.attempt "
                    "WHERE w.owner=?", (pool.owner,))]
                named = {r["checkpoint"] for r in rows}
                for row in rows:
                    if (row["repo"] == repo_name and row["attempt_state"] is not None
                            and row["attempt_state"] not in _LIVE_ATTEMPT_STATES
                            and _ended_before(row["attempt_updated"], cutoff)):
                        victim(Path(row["checkpoint"]), f"attempt {row['attempt_state']}", row)
                for path in sorted(root.iterdir()):
                    if (path.name.startswith(".") or str(path) in named or path.is_symlink()
                            or not path.is_dir() or path.stat().st_mtime >= cutoff
                            or _checkpoint_repo(path) != repo_name):
                        continue
                    victim(path, "superseded", None)
                if apply:
                    refs = git(repo.base, "for-each-ref", "--format=%(refname)", *_CHECKPOINT_REF_PREFIXES,
                               check=False, env=pool.env).split()
                    for ref in refs:
                        key = ref.rsplit("/", 1)[-1]
                        if not any(all_roots.glob(f"*/{key}")):
                            git(repo.base, "update-ref", "-d", ref, check=False, env=pool.env)
                            result["refs"] += 1
        from .checkpoint import _IO_LOCK
        if _IO_LOCK.acquire(blocking=False):            # no capture is running in this process
            try:
                for path in sorted(root.glob(".pending-*")):
                    victim(path, "abandoned capture", None)
            finally:
                _IO_LOCK.release()
        for trash in sorted(root.glob(".trash-*")) if apply else ():
            size = _dir_bytes(trash, deadline)
            _bounded_rmtree(trash, deadline, anchor=root)
            result["freed_bytes"] += size
            log.info("checkpoint gc: removed %s bytes=%d", trash.name, size)
    except WorkspaceError:
        pass                                            # budget spent: the next pass continues
    except OSError as e:
        log.warning("checkpoint gc %s: %s", pool.owner, e)
        result["errors"] += 1
    return result


def checkpoint_bytes(pool_root: Path, deadline: float | None = None) -> int:
    """Disk used by every owner's WAIT snapshots (du estimate; 0 when there are none)."""
    root = Path(pool_root) / "checkpoints"
    if not root.is_dir():
        return 0
    return _dir_bytes(root, deadline if deadline is not None else time.monotonic() + 120)


# Not "releasing"/"restoring": those are being read or written right now. "restored"/"active" are
# recovery copies kept after a restore; the next WAIT writes a new generation instead of reading them.
_COMPACTABLE_PHASES = ("verified", "released", "restored", "active")


# A snapshot whose compaction failed fails the same way on every pass; without a pause it is retried
# each time under the base lock and eats the pass budget. The memory lives in the store, so a restart
# does not reset it. A final entry (base absent from the base clone, or a rewrite that saved nothing)
# is never retried; an entry disappears with its snapshot.
_COMPACT_RETRY_AFTER = 6 * 3600
_COMPACT_FAILURES = ("CREATE TABLE IF NOT EXISTS checkpoint_compact_failures ("
                     "checkpoint TEXT PRIMARY KEY, reason TEXT NOT NULL, failed_at REAL NOT NULL, "
                     "final INTEGER NOT NULL DEFAULT 0)")
# Scratch directories and generated keys differ on every attempt; a repeat is the same reason without them.
_SCRATCH_NAME = re.compile(r"(\.(?:compact-src|verify|preflight|pending)-|compact-)[A-Za-z0-9_]+")


def _compact_reason(error: Exception) -> str:
    return _SCRATCH_NAME.sub(r"\1*", str(error))[:300]


def _remember_compact(db, path: Path, reason: str, *, final: bool) -> None:
    db.execute("INSERT INTO checkpoint_compact_failures VALUES (?,?,?,?) ON CONFLICT(checkpoint) DO UPDATE "
               "SET reason=excluded.reason, failed_at=excluded.failed_at, final=excluded.final",
               (str(path), reason, time.time(), int(final)))


def compact_checkpoints(pool: WorkspacePool, *, apply: bool = False, deadline: float | None = None) -> dict:
    """One-shot migration: rewrite waiting old-format snapshots (still holding regenerable caches)
    as new-format ones, one at a time, each replay-verified before the swap. Rows being released or
    restored right now are skipped. Dry run lists the candidates and their current size."""
    deadline = deadline if deadline is not None else time.monotonic() + 3600
    result = {"owner": pool.owner, "candidates": [], "compacted": 0, "freed_bytes": 0, "errors": 0}
    db = pool.store.db
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='workspace_checkpoints'").fetchone():
        return result
    checkpoints = pool._checkpoints()
    db.execute(_COMPACT_FAILURES)
    rows = [dict(r) for r in db.execute("SELECT * FROM workspace_checkpoints WHERE owner=?", (pool.owner,))]
    for row in rows:
        path = row["checkpoint"]
        if not Path(path).is_dir():
            db.execute("DELETE FROM checkpoint_compact_failures WHERE checkpoint=?", (path,))
    for row in rows:
        if time.monotonic() >= deadline:
            break
        path = Path(row["checkpoint"])
        if row["phase"] not in _COMPACTABLE_PHASES or row["repo"] not in pool.repos or not path.is_dir():
            continue
        memo = db.execute("SELECT reason, failed_at, final FROM checkpoint_compact_failures WHERE checkpoint=?",
                          (str(path),)).fetchone()
        if memo is not None and (memo["final"] or time.time() - memo["failed_at"] < _COMPACT_RETRY_AFTER):
            continue
        try:
            if not checkpoints.is_old_format(path):
                continue
        except WorkspaceError:
            result["errors"] += 1
            continue
        entry = {"checkpoint": str(path), "attempt": row["attempt"], "bytes": _dir_bytes(path, deadline)}
        result["candidates"].append(entry)
        if not apply:
            continue
        repo = pool.repos[row["repo"]]                 # this pool's own repo config: its own base clone
        try:
            with _bounded_base_lock(repo.base, deadline) as locked:
                if not locked:
                    result["errors"] += 1
                    continue
                now = db.execute("SELECT phase FROM workspace_checkpoints WHERE owner=? AND attempt=? AND repo=?",
                                 (pool.owner, row["attempt"], row["repo"])).fetchone()
                if now is None or now["phase"] not in _COMPACTABLE_PHASES:
                    continue                        # a restore moved the row while we waited for the lock
                done = checkpoints.compact(path, base_objects=base_objects_dir(Path(repo.base),
                                                                               getattr(pool, "env", None)))
        except (WorkspaceError, OSError) as e:
            reason = _compact_reason(e)
            final = isinstance(e, CheckpointBaseMissing)
            repeat = memo is not None and memo["reason"] == reason
            log.log(logging.DEBUG if repeat else logging.WARNING, "checkpoint compact %s: %s%s", path.name, e,
                    "; not retried" if final else f"; retry in {_COMPACT_RETRY_AFTER // 3600}h")
            entry["error"] = str(e)
            result["errors"] += 1
            _remember_compact(db, path, reason, final=final)
            continue
        db.execute("DELETE FROM checkpoint_compact_failures WHERE checkpoint=?", (str(path),))
        if done.get("compacted") and done["after"] >= done["before"]:
            # Rewritten without saving anything: a further pass would do the same work again.
            _remember_compact(db, path, "compaction saved nothing", final=True)
        if done.get("skipped"):
            entry["skipped"] = done["skipped"]
            result["skipped"] = result.get("skipped", 0) + 1
            log.info("checkpoint compact: %s skipped (%s)", path.name, done["skipped"])
        if done.get("compacted"):
            entry["after"] = done["after"]
            result["compacted"] += 1
            result["freed_bytes"] += max(0, done["before"] - done["after"])
            log.info("checkpoint compact: %s %d -> %d bytes", path.name, done["before"], done["after"])
    return result
