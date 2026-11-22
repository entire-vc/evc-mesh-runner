"""Scoped additive admission of one configured repository to an existing attempt.

The CLI only reads the fresh config and publishes a bounded one-shot request;
the owning Runner tick is the single writer. The request pins the exact
RepoSpec the operator reviewed, so a config edited after submission is refused.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path

from . import workspace_requests as spool_io
from . import repo_access
from .workspace import RepoSpec

SPOOL = 'repo-admission'
FIELDS = {'version', 'request_id', 'agent', 'task', 'attempt', 'generation', 'created', 'expires',
          'repo', 'spec'}


def configured_spec(cfg, agent, name: str) -> RepoSpec | None:
    """Pin the same source as the lane, without changing the registry or calling Mesh.

    Only a never-migrated lane uses legacy repos. An explicit cached permission, including
    deny-all, selects the allowed host inventory. The tick revalidates the live permission.
    An unreadable registry must not silently reopen legacy access.
    """
    path = cfg.state_dir / 'runner.sqlite'
    access = None
    if path.exists():
        try:
            with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
                row = db.execute('SELECT value FROM marks WHERE agent=? AND task_id=? AND key=?',
                                 (agent.name, repo_access.STORE_TASK, repo_access.STORE_KEY)).fetchone()
            if row and row[0]:
                access = repo_access.parse(json.loads(row[0]))
        except (sqlite3.Error, ValueError, TypeError) as exc:
            raise ValueError('repository access registry unreadable') from exc
    specs = (agent.repos if access is None else
             [s for s in cfg.host_repositories if access.allows(s.project or None)])
    return next((s for s in specs if s.name == name), None)


def spec_digest(repo: RepoSpec) -> str:
    raw = json.dumps([repo.name, str(repo.base), repo.remote, repo.branch, repo.max_slots,
                      [list(x) for x in repo.fetch_url_overrides]], separators=(',', ':')).encode()
    return hashlib.sha256(raw).hexdigest()


def validate(r: dict, agent: str) -> None:
    if set(r) != FIELDS or type(r['version']) is not int or r['version'] != 1 or r['agent'] != agent:
        raise ValueError('request schema mismatch')
    for key in ('request_id', 'task', 'attempt'):
        if not isinstance(r[key], str) or str(uuid.UUID(r[key])) != r[key]:
            raise ValueError('invalid request identity')
    for key in ('generation', 'spec'):
        if not isinstance(r[key], str) or len(r[key]) != 64:
            raise ValueError('invalid digest')
        int(r[key], 16)
    if not isinstance(r['repo'], str) or not r['repo'] or len(r['repo']) > 128:
        raise ValueError('invalid repo')
    if any(type(r[k]) is not int for k in ('created', 'expires')):
        raise ValueError('invalid deadline')
    if not 0 < r['expires'] - r['created'] <= spool_io.MAX_TTL:
        raise ValueError('invalid deadline')


def submit(state: Path, agent: str, task: str, attempt: str, token: str, request_id: str,
           repo: RepoSpec, ttl: int) -> dict:
    moment = int(time.time())
    r = dict(version=1, request_id=request_id, agent=agent, task=task, attempt=attempt,
             generation=token, created=moment, expires=moment + ttl, repo=repo.name,
             spec=spec_digest(repo))
    validate(r, agent)
    root = spool_io.directory(state, agent, SPOOL)
    path = root / (request_id + '.request.json')
    if path.exists():
        old = spool_io._read(path)
        validate(old, agent)
        if any(old[k] != r[k] for k in ('agent', 'task', 'attempt', 'generation', 'repo', 'spec')):
            raise ValueError('request id already bound to another identity')
        r = old
    else:
        if root.exists():
            names = [e.name for e in root.iterdir()]
            if sum(n.endswith('.request.json') for n in names) >= spool_io.MAX_REQUESTS:
                raise ValueError('request directory full; archive receipts before retry')
        if not spool_io._publish(path, r):
            return submit(state, agent, task, attempt, token, request_id, repo, ttl)
    receipt = root / (request_id + '.receipt.json')
    return {'request': r, 'receipt': spool_io._read(receipt) if receipt.exists() else None}


def consume(state: Path, agent: str, handler) -> None:
    spool_io.consume(state, agent, handler, SPOOL, validate)
