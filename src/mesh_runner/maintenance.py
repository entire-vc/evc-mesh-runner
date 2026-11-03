"""Owner-side maintenance window for one leased, waiting workspace.

An external mutation (a rename plus symlink of a slot by its owner) needs the Runner to stop
touching exactly that attempt's workspace, without releasing its WAIT, lease or checkout. The CLI
only publishes bounded one-shot requests (``acquire`` / ``finish``); the owning Runner tick is the
single writer. The barrier is a durable row in the Runner's SQLite store, written before the ready
receipt and removed only by an explicit ``finish`` whose identity is re-proven: a restart, a lost
receipt or a foreign finish never lifts it, and no timer does either. The request TTL covers only
a request nobody has accepted yet.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path

from . import __version__
from . import workspace_requests as spool_io

SPOOL = 'workspace-maintenance'
OPS = ('acquire', 'finish')
FIELDS = {'version', 'request_id', 'agent', 'task', 'attempt', 'generation', 'created', 'expires',
          'op', 'window', 'pool', 'slot', 'lease'}

SCHEMA = """
CREATE TABLE IF NOT EXISTS maintenance_windows (
    window TEXT PRIMARY KEY,               -- request id of the acquire
    agent TEXT NOT NULL, task TEXT NOT NULL, attempt TEXT NOT NULL, generation TEXT NOT NULL,
    pool TEXT NOT NULL, slot TEXT NOT NULL, lease TEXT NOT NULL,
    state TEXT NOT NULL,                   -- held | finished
    opened_at INTEGER NOT NULL, closed_at INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS maintenance_one_held_per_attempt
    ON maintenance_windows(attempt) WHERE state='held';
CREATE UNIQUE INDEX IF NOT EXISTS maintenance_one_held_per_slot
    ON maintenance_windows(slot) WHERE state='held';
"""


def lease_token(slot: dict) -> str:
    """Identity of one lease: the slot row as written by the lease transition. The row changes
    only on a state transition, so a re-lease of the same path yields another token."""
    raw = json.dumps([slot['path'], slot['pool'], slot['attempt'], slot['updated_at']],
                     separators=(',', ':')).encode()
    return hashlib.sha256(raw).hexdigest()


def ensure(store) -> None:
    store.db.executescript(SCHEMA)


def held(store, *, attempt: str = '', path: str = '') -> dict | None:
    """The open window that closes this attempt (every slot of it) or this slot path, if any."""
    ensure(store)
    row = store.db.execute(
        "SELECT * FROM maintenance_windows WHERE state='held' AND (attempt=? OR slot=?)",
        (attempt or '-', path or '-')).fetchone()
    return dict(row) if row else None


def window(store, window_id: str) -> dict | None:
    ensure(store)
    row = store.db.execute("SELECT * FROM maintenance_windows WHERE window=?", (window_id,)).fetchone()
    return dict(row) if row else None


def validate(r: dict, agent: str) -> None:
    if set(r) != FIELDS or type(r['version']) is not int or r['version'] != 1 or r['agent'] != agent:
        raise ValueError('request schema mismatch')
    if r['op'] not in OPS:
        raise ValueError('invalid operation')
    for key in ('request_id', 'task', 'attempt', 'window'):
        if not isinstance(r[key], str) or str(uuid.UUID(r[key])) != r[key]:
            raise ValueError('invalid request identity')
    if r['op'] == 'acquire' and r['window'] != r['request_id']:
        raise ValueError('acquire opens the window named by its own request id')
    for key in ('generation', 'lease'):
        if not isinstance(r[key], str) or len(r[key]) != 64:
            raise ValueError('invalid digest')
        int(r[key], 16)
    for key in ('pool', 'slot'):
        if not isinstance(r[key], str) or not r[key] or len(r[key]) > 512 or '\0' in r[key]:
            raise ValueError('invalid target')
    if any(type(r[k]) is not int for k in ('created', 'expires')):
        raise ValueError('invalid deadline')
    if not 0 < r['expires'] - r['created'] <= spool_io.MAX_TTL:
        raise ValueError('invalid deadline')


def submit(state: Path, agent: str, op: str, task: str, attempt: str, token: str, request_id: str,
           pool: str, slot: str, lease: str, ttl: int, window_id: str = '') -> dict:
    moment = int(time.time())
    r = dict(version=1, request_id=request_id, agent=agent, task=task, attempt=attempt,
             generation=token, created=moment, expires=moment + ttl, op=op,
             window=window_id or request_id, pool=pool, slot=slot, lease=lease)
    validate(r, agent)
    root = spool_io.directory(state, agent, SPOOL)
    path = root / (request_id + '.request.json')
    if path.exists():
        old = spool_io._read(path)
        validate(old, agent)
        if any(old[k] != r[k] for k in FIELDS - {'created', 'expires'}):
            raise ValueError('request id already bound to another identity')
        r = old
    else:
        if root.exists():
            names = [e.name for e in root.iterdir()]
            if sum(n.endswith('.request.json') for n in names) >= spool_io.MAX_REQUESTS:
                raise ValueError('request directory full; archive receipts before retry')
        if not spool_io._publish(path, r):
            return submit(state, agent, op, task, attempt, token, request_id, pool, slot, lease, ttl, window_id)
    receipt = root / (request_id + '.receipt.json')
    return {'request': r, 'receipt': spool_io._read(receipt) if receipt.exists() else None}


def receipt(request: dict, status: str, barrier: str, reason: str = '') -> dict:
    """Bounded, secret-free receipt: request + pinned identity + barrier state + version + cause."""
    return {'status': status, 'at': int(time.time()), 'version': __version__, 'op': request['op'],
            'window': request['window'], 'request_id': request['request_id'], 'agent': request['agent'],
            'task': request['task'], 'attempt': request['attempt'], 'generation': request['generation'],
            'pool': request['pool'], 'slot': request['slot'], 'lease': request['lease'],
            'barrier': barrier, 'reason': reason[:200]}


def consume(state: Path, agent: str, handler) -> None:
    spool_io.consume(state, agent, handler, SPOOL, validate)


def inspect(state: Path, store, agent: str, request_id: str = '') -> dict:
    """Read-only view: open windows of this agent, plus one request's receipt when named."""
    ensure(store)
    rows = [dict(r) for r in store.db.execute(
        "SELECT * FROM maintenance_windows WHERE agent=? ORDER BY opened_at DESC LIMIT 20", (agent,))]
    out: dict = {'windows': rows}
    if request_id:
        if str(uuid.UUID(request_id)) != request_id:       # a name, never a path
            raise ValueError('invalid request identity')
        root = spool_io.directory(state, agent, SPOOL)
        path = root / (request_id + '.receipt.json')
        out['receipt'] = spool_io._read(path) if path.exists() else None
    return out
