"""Which repositories this lane may work in: Mesh ``repository_access``, read hot every tick.

Two separate things (docs/repository-access.md):

* the host inventory (``[[repositories]]``): which base clones exist on this host, each with its
  canonical project ``(provider, host, project_path)`` stated explicitly. A clone is never a
  permission, and a missing clone is never derived from a git remote;
* the permission: ``GET /agents/me`` -> ``repository_access``. ``null``/absent = not migrated yet,
  the lane's legacy ``agents.repos`` decide; ``{revision, repositories: [...]}`` = exactly those
  canonical projects (``[]`` = none at all).

The last valid Mesh state is kept in the runner's Store, so neither an outage, a malformed answer,
nor a restart during one widens it, and a later ``null``/missing field after an explicit state does
not silently reopen the legacy list. Pure data and the keeper; the runner applies the result.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import urllib.parse
from dataclasses import dataclass
from typing import NamedTuple

log = logging.getLogger("mesh_runner.repo_access")

FIELD = "repository_access"
STORE_TASK = "_repository_access"     # a marks row, not a task (tasks_seen skips the "_" prefix)
STORE_KEY = "last_known_good"
DEFAULT_PORTS = {"443", "80"}
_HOST = re.compile(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?(?::[0-9]{1,5})?")
_PATH = re.compile(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+")
_PROVIDER = re.compile(r"[a-z][a-z0-9_-]{0,31}")


class Identity(NamedTuple):
    provider: str
    host: str
    project_path: str

    def __str__(self) -> str:
        return f"{self.provider}:{self.host}/{self.project_path}"


def normalize_host(raw: object) -> str:
    """Lowercase host, no scheme, no userinfo, no default port, no trailing slash."""
    if not isinstance(raw, str):
        raise ValueError("host must be a string")
    s = raw.strip().lower()
    if "://" in s:
        s = s.split("://", 1)[1]
    s = s.rstrip("/").rsplit("@", 1)[-1]
    if ":" in s:
        name, port = s.rsplit(":", 1)
        s = name if port in DEFAULT_PORTS else s
    if not _HOST.fullmatch(s) or ".." in s:
        raise ValueError("invalid host")
    return s


def normalize_path(raw: object) -> str:
    """``group/project`` without leading/trailing ``/`` and without ``.git``; case is kept."""
    if not isinstance(raw, str):
        raise ValueError("project_path must be a string")
    s = raw.strip().strip("/")
    if s.endswith(".git"):
        s = s[:-4].rstrip("/")
    if not _PATH.fullmatch(s) or any(part in (".", "..") for part in s.split("/")):
        raise ValueError("invalid project_path")
    return s


def identity(provider: object, host: object, project_path: object) -> Identity:
    if not isinstance(provider, str) or not _PROVIDER.fullmatch(provider.strip().lower()):
        raise ValueError("invalid provider")
    return Identity(provider.strip().lower(), normalize_host(host), normalize_path(project_path))


def card_identity(base_url: str, project: str, provider: str = "gitlab") -> Identity | None:
    """Canonical identity of a project a card's typed MR link names on the configured forge."""
    try:
        return identity(provider, urllib.parse.urlsplit(base_url).netloc, project)
    except ValueError:
        return None


@dataclass(frozen=True)
class Access:
    """One explicit Mesh state: the exact projects this lane may work in."""
    revision: int
    repositories: frozenset

    def allows(self, ident: Identity | None) -> bool:
        return ident is not None and ident in self.repositories

    def digest(self) -> str:
        rows = sorted(list(i) for i in self.repositories)
        return hashlib.sha256(json.dumps([self.revision, rows]).encode()).hexdigest()[:12]

    def to_json(self) -> str:
        return json.dumps({"revision": self.revision,
                           "repositories": [i._asdict() for i in sorted(self.repositories)]},
                          sort_keys=True)


def parse(value: object) -> Access:
    """An explicit ``repository_access`` object; anything not exactly valid raises ValueError
    (whole answer refused: one bad row never yields a partial list)."""
    if not isinstance(value, dict):
        raise ValueError("repository_access is not an object")
    rev, rows = value.get("revision"), value.get("repositories")
    if type(rev) is not int or rev < 0:
        raise ValueError("revision must be a non-negative integer")
    if not isinstance(rows, list):
        raise ValueError("repositories must be a list")
    out = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("repository entry is not an object")
        out.add(identity(row.get("provider"), row.get("host"), row.get("project_path")))
    return Access(rev, frozenset(out))


class AccessKeeper:
    """Current permission of one lane: ``None`` = legacy (never saw an explicit Mesh state)."""

    def __init__(self, store, agent: str):
        self.store, self.agent = store, agent
        self.current: Access | None = None
        self._said: set[str] = set()
        saved = store.mark(agent, STORE_TASK, STORE_KEY)
        if saved and saved[0]:
            try:
                self.current = parse(json.loads(saved[0]))
            except (ValueError, TypeError):
                # a damaged record must not reopen legacy rights: deny all until Mesh answers again
                log.error("%s: stored repository access unreadable; denying every repository until Mesh "
                          "answers with a valid state", agent)
                self.current = Access(-1, frozenset())

    @property
    def mesh(self) -> bool:
        return self.current is not None

    def stamp(self) -> str:
        return "legacy" if self.current is None else f"mesh:{self.current.revision}:{self.current.digest()}"

    def _once(self, key: str, level: int, msg: str, *args) -> None:
        if key not in self._said:
            self._said.add(key)
            log.log(level, msg, *args)

    def observe(self, me: dict) -> bool:
        """Apply one fresh ``/agents/me``; True when the permission changed."""
        if not isinstance(me, dict):
            return False
        value = me.get(FIELD)
        if value is None:
            if self.current is not None:
                self._once(f"gone:{self.stamp()}", logging.WARNING,
                           "%s: Mesh returned no repository_access after explicit revision %s; keeping it "
                           "(legacy agents.repos is not reopened)", self.agent, self.current.revision)
            return False
        try:
            fresh = parse(value)
        except ValueError as e:
            raw = hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()[:12]
            self._once(f"bad:{raw}", logging.WARNING,
                       "%s: malformed repository_access from Mesh ignored (%s); keeping %s",
                       self.agent, e, self.stamp())
            return False
        if fresh == self.current:
            return False
        if self.current is not None and fresh.revision <= self.current.revision:
            # Mesh raises the revision on every change: an older or equal one is a stale or reordered
            # answer and must never undo a newer revoke
            self._once(f"stale:{self.stamp()}:{fresh.revision}:{fresh.digest()}", logging.WARNING,
                       "%s: repository_access revision %s is not newer than %s; ignored, keeping %s",
                       self.agent, fresh.revision, self.current.revision, self.stamp())
            return False
        self.store.mark(self.agent, STORE_TASK, STORE_KEY, fresh.to_json())
        before, self.current = self.stamp(), fresh
        log.info("%s: repository access %s -> %s (%d repositories)", self.agent, before, self.stamp(),
                 len(fresh.repositories))
        return True

    def lane_repos(self, legacy: list, inventory: list) -> list:
        """RepoSpecs this lane may lease now: legacy list, or inventory entries Mesh allows."""
        if self.current is None:
            return legacy
        return [spec for spec in inventory if self.current.allows(getattr(spec, "project", None) or None)]

    def note_legacy_drift(self, legacy: list, effective: list) -> None:
        """Log once per revision/composition where Mesh and the legacy list differ."""
        if self.current is None:
            return
        a, b = sorted(s.name for s in legacy), sorted(s.name for s in effective)
        if a != b:
            self._once(f"drift:{self.stamp()}", logging.INFO,
                       "%s: Mesh repository access %s differs from legacy agents.repos: mesh=%s legacy=%s",
                       self.agent, self.stamp(), b, a)

    def describe(self) -> dict:
        if self.current is None:
            return {"source": "legacy"}
        return {"source": "mesh", "revision": self.current.revision,
                "repositories": [str(i) for i in sorted(self.current.repositories)]}
