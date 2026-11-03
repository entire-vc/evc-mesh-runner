"""Evidence recheck requests; only the existing owner tick may reconcile delivery.

The spool never imports ACKs or claimed outcomes. Evidence hashes only fence the
operator's observed snapshot; the owner still runs the native proof validator.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import stat
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

from . import interactive, headless_acceptance, workspace_requests as spool_io

SPOOL = 'delivery-rechecks'
FIELDS = spool_io.FIELDS | {'token', 'turn', 'evidence_sha256'}


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def evidence(state: Path, a) -> str:
    intent = a.wait['interactive_prompt_intent']
    directory = interactive.agent_dir(state, a.agent)
    journal_path = interactive.delivery_path(directory, a.id)
    journal = interactive.read_delivery(directory, a.id)
    paths = {journal_path, directory / (a.id + '.events.jsonl'), Path(intent['log_path'])}
    if intent.get('kind') == 'headless' and intent.get('harness') == 'claude':
        paths.add(interactive.delivery_path(directory, headless_acceptance.receipt_key(a.id, intent['token'])))
    if journal.get('rollout'):
        paths.add(Path(journal['rollout']))
    transcript = (journal.get('meta') or {}).get('transcript')
    if transcript:
        paths.add(Path(transcript))
    entries = []
    for path in sorted(paths):
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            entries.append([str(path), None])
            continue
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_size > 128 * 1024 * 1024:
                raise ValueError('native-evidence-invalid')
            h = hashlib.sha256()
            with os.fdopen(fd, 'rb', closefd=False) as stream:
                remaining = before.st_size
                while remaining:
                    block = stream.read(min(65536, remaining))
                    if not block:
                        raise ValueError('native-evidence-changing')
                    h.update(block)
                    remaining -= len(block)
                if stream.read(1):
                    raise ValueError('native-evidence-changing')
            after = os.fstat(fd)
            if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
                raise ValueError('native-evidence-changing')
            entries.append([str(path), h.hexdigest()])
        finally:
            os.close(fd)
    return sha(json.dumps(entries, sort_keys=True, separators=(',', ':')).encode())


def identity(a) -> dict:
    intent = a.wait.get('interactive_prompt_intent')
    if not isinstance(intent, dict):
        raise ValueError('no-pending-delivery')
    return dict(agent=a.agent, task=a.task_id, attempt=a.id, token=intent['token'],
                turn=intent['turn'], generation=intent['sha256'])


def snapshot(state: Path, agent: str, attempt: str) -> dict:
    db = sqlite3.connect((state / 'runner.sqlite').resolve(strict=True).as_uri() + '?mode=ro', uri=True)
    try:
        row = db.execute('SELECT id,agent,task_id,wait,state FROM attempts WHERE id=? AND agent=?', (attempt, agent)).fetchone()
    finally:
        db.close()
    if row is None:
        raise ValueError('attempt-not-owned-by-agent')
    a = SimpleNamespace(id=row[0], agent=row[1], task_id=row[2], wait=json.loads(row[3]))
    return {**identity(a), 'evidence_sha256': evidence(state, a), 'state': row[4],
            'reason': a.wait['interactive_prompt_intent'].get('resolution', '')}


def validate(r: dict, agent: str) -> None:
    if set(r) != FIELDS:
        raise ValueError('request-schema-mismatch')
    spool_io.validate({k: r[k] for k in spool_io.FIELDS}, agent)
    if type(r['turn']) is not int or r['turn'] < 1:
        raise ValueError('invalid-turn')
    for key, size in (('token', 32), ('evidence_sha256', 64)):
        if not isinstance(r[key], str) or len(r[key]) != size or any(c not in '0123456789abcdef' for c in r[key]):
            raise ValueError('invalid-identity')


def submit(state: Path, observed: dict, request_id: str, ttl: int) -> dict:
    moment = int(time.time())
    r = {k: observed[k] for k in ('agent', 'task', 'attempt', 'generation', 'token', 'turn', 'evidence_sha256')}
    r.update(version=1, request_id=request_id, created=moment, expires=moment + ttl)
    validate(r, r['agent'])
    root = spool_io.directory(state, r['agent'], SPOOL)
    path = root / (request_id + '.request.json')
    if path.exists():
        old = spool_io._read(path)
        validate(old, r['agent'])
        if any(old[k] != r[k] for k in FIELDS - {'created', 'expires'}):
            raise ValueError('request-id-already-bound')
        r = old
    else:
        if root.exists():
            names = []
            with os.scandir(root) as entries:
                for entry in entries:
                    names.append(entry.name)
                    if len(names) > spool_io.MAX_REQUESTS * 2:
                        raise ValueError('request-directory-full')
            if sum(n.endswith('.request.json') for n in names) >= spool_io.MAX_REQUESTS:
                raise ValueError('request-directory-full')
        if not spool_io._publish(path, r):
            return submit(state, observed, request_id, ttl)
    receipt = root / (request_id + '.receipt.json')
    return {'request': r, 'receipt': spool_io._read(receipt) if receipt.exists() else None}


def consume(state: Path, agent: str, handler):
    spool_io.consume(state, agent, handler, SPOOL, validate)


def command(argv=None):
    from .config import load
    p = argparse.ArgumentParser(prog='mesh-runner recheck-delivery')
    p.add_argument('--config', type=Path, required=True)
    p.add_argument('--agent', required=True)
    p.add_argument('--attempt', required=True)
    p.add_argument('--task')
    p.add_argument('--token')
    p.add_argument('--turn', type=int)
    p.add_argument('--evidence-sha256')
    p.add_argument('--request-id')
    p.add_argument('--ttl-seconds', type=int, default=300)
    p.add_argument('--submit', action='store_true')
    args = p.parse_args(argv)
    try:
        cfg = load(args.config)
        if args.agent not in {a.name for a in cfg.agents}:
            raise ValueError('agent-not-configured')
        observed = snapshot(cfg.state_dir, args.agent, args.attempt)
        if args.submit:
            if (args.task, args.token, args.turn, args.evidence_sha256) != tuple(observed[k] for k in ('task', 'token', 'turn', 'evidence_sha256')):
                raise ValueError('observed-identity-or-evidence-changed')
            if not args.request_id:
                raise ValueError('request-id-required')
            result = submit(cfg.state_dir, observed, args.request_id, args.ttl_seconds)
        else:
            result = observed
        print(json.dumps(result, sort_keys=True))
        return 0
    except (ValueError, KeyError, OSError, sqlite3.Error) as exc:
        print(json.dumps({'eligible': False, 'reason': str(exc) if isinstance(exc, ValueError) else type(exc).__name__}))
        return 2
