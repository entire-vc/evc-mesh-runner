"""Private, bounded one-shot requests consumed by the owning Runner tick.

Requests are immutable; receipts are separate durable files. Retrying a request
never renews its deadline. No registry or workspace writes happen in the CLI.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import time
import uuid
from pathlib import Path

MAX_BYTES = 4096
MAX_REQUESTS = 32
MAX_TTL = 900
FIELDS = {'version', 'request_id', 'agent', 'task', 'attempt', 'generation', 'created', 'expires'}


def generation(a) -> str:
    # Bind both the wait identity and its declaration; updated_at alone changes
    # during ordinary wake checks and is not a stable wait-generation token.
    raw = json.dumps({'started': a.wait.get('started') or a.updated_at, 'turns': a.turns, 'session': a.session_id},
                     sort_keys=True, separators=(',', ':')).encode()
    return hashlib.sha256(raw).hexdigest()


def directory(state: Path, agent: str, spool: str = 'workspace-requests') -> Path:
    # Agent names are not paths. Hashing also supports existing Unicode names.
    parent = state / spool
    root = parent / hashlib.sha256(agent.encode()).hexdigest()
    for path in (parent, root):
        if path.is_symlink():
            raise ValueError('invalid request directory')
        if path.exists():
            info = path.stat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError('request directory must be private and owned')
    return root


def _read(path: Path) -> dict:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES
                or info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise ValueError('invalid request file')
        raw = os.read(fd, MAX_BYTES + 1)
    finally:
        os.close(fd)
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('invalid request object')
    return value


def _publish(path: Path, value: dict) -> bool:
    """Atomic exclusive publication, including directory durability."""
    path.parent.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.mkdir(mode=0o700, exist_ok=True)
    if path.parent.is_symlink() or not path.parent.is_dir():
        raise ValueError('invalid request directory')
    data = json.dumps(value, sort_keys=True).encode()
    if len(data) > MAX_BYTES:
        raise ValueError('request too large')
    fd, name = tempfile.mkstemp(prefix='.request-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        try:
            os.link(name, path)
        except FileExistsError:
            return False
        dfd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
        return True
    finally:
        os.unlink(name)


def validate(r: dict, agent: str) -> None:
    if set(r) != FIELDS or type(r['version']) is not int or r['version'] != 1 or r['agent'] != agent:
        raise ValueError('request schema mismatch')
    for key in ('request_id', 'task', 'attempt'):
        if not isinstance(r[key], str) or str(uuid.UUID(r[key])) != r[key]:
            raise ValueError('invalid request identity')
    if not isinstance(r['generation'], str) or len(r['generation']) != 64:
        raise ValueError('invalid wait generation')
    int(r['generation'], 16)
    if any(type(r[k]) is not int for k in ('created', 'expires')):
        raise ValueError('invalid deadline')
    if not 0 < r['expires'] - r['created'] <= MAX_TTL:
        raise ValueError('invalid deadline')


def submit(state: Path, agent: str, task: str, attempt: str, token: str,
           request_id: str, ttl: int) -> dict:
    moment = int(time.time())
    r = dict(version=1, request_id=request_id, agent=agent, task=task,
             attempt=attempt, generation=token, created=moment, expires=moment + ttl)
    validate(r, agent)
    root = directory(state, agent)
    path = root / (request_id + '.request.json')
    if path.exists():
        old = _read(path)
        validate(old, agent)
        if any(old[k] != r[k] for k in ('agent', 'task', 'attempt', 'generation')):
            raise ValueError('request id already bound to another identity')
        r = old
    else:
        # Terminal files also count. No unbounded history or silent eviction.
        if root.exists():
            names = []
            with os.scandir(root) as entries:
                for entry in entries:
                    names.append(entry.name)
                    if len(names) > MAX_REQUESTS * 2:
                        raise ValueError('request directory full')
            if sum(n.endswith('.request.json') for n in names) >= MAX_REQUESTS:
                raise ValueError('request directory full; archive receipts before retry')
        if not _publish(path, r):
            return submit(state, agent, task, attempt, token, request_id, ttl)
    receipt = root / (request_id + '.receipt.json')
    return {'request': r, 'receipt': _read(receipt) if receipt.exists() else None}


def consume(state: Path, agent: str, handler, spool: str = 'workspace-requests', check=None) -> None:
    try:
        _consume(state, agent, handler, spool, check or validate)
    except (OSError, ValueError):
        # A full/unavailable spool must not stop normal admission. If release
        # preceded receipt failure, the next tick reconciles its durable journal.
        return


def _consume(state: Path, agent: str, handler, spool: str, check) -> None:
    try:
        root = directory(state, agent, spool)
    except (ValueError, OSError):
        return
    if not root.exists() or root.is_symlink():
        return
    # Bounded directory enumeration, fail closed if the spool exceeded its cap.
    names = []
    with os.scandir(root) as entries:
        for entry in entries:
            names.append(entry.name)
            if len(names) > MAX_REQUESTS * 2:
                return
    for name in sorted(names):
        if not name.endswith('.request.json'):
            continue
        path = root / name
        receipt = root / name.replace('.request.json', '.receipt.json')
        if receipt.exists():
            continue
        try:
            r = _read(path)
            check(r, agent)
            if name != r['request_id'] + '.request.json':
                raise ValueError('filename identity mismatch')
            moment = int(time.time())
            if r['created'] > moment + 5:
                result = 'rejected_clock'
            else:
                result = handler(r)
        except (ValueError, OSError, TypeError, KeyError):
            result = 'rejected_schema'
        _publish(receipt, {'status': result, 'at': int(time.time())})
        return                         # at most one request per agent tick
