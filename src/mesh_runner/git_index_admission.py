"""Narrow reviewed admission: clear skip-worktree on two literal paths of a live Git index.

One closed operation (``git-index-clear-skip-worktree``), no wildcard and no free command.
The CLI only publishes a bounded one-shot request; the owning Runner tick is the single issuer
of the durable, scoped receipt. Neither the submitter nor the task text is an authority.

Issuer: an immutable policy file whose SHA-256 is pinned in the Runner config
(``[git_index_admission] policy_sha256``). The config reaches the host through the signed,
independently reviewed release/activation chain, so the pinned digest is the reviewed
revision. A policy that does not match its pin, a revocation list that cannot be read, or a
request that differs from the policy in any bound field is refused; there is no fallback and no
self-issued grant. The receipt grants authority only: fresh pins/CAS, quiescence, the cron
mutex and rollback remain the executor's preconditions, and nothing here touches a Git index.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import time
import uuid
from pathlib import Path

from . import __version__
from . import workspace_requests as spool_io

SPOOL = 'git-index-admission'
OPERATION = 'git-index-clear-skip-worktree'
ALLOWED_PATHS = ('scripts/macmini-disk-cleanup.sh', 'scripts/slot-archive-remove.py')
GRANT_KEY = 'git_index_grant'
MAX_GRANT_SECONDS = 3600
MAX_POLICY_BYTES = 8192
FIELDS = {'version', 'request_id', 'agent', 'task', 'attempt', 'generation', 'created', 'expires',
          'operation', 'actor', 'source_task', 'package_sha256', 'paths', 'policy'}
POLICY_FIELDS = {'version', 'revision', 'issuer', 'review_ref', 'operation', 'actor', 'source_task',
                 'package_artifact', 'package_sha256', 'paths', 'target_root', 'not_before', 'expires',
                 'grant_seconds'}


class Unverifiable(Exception):
    """Policy or revocation state cannot be proven: fail closed."""


def _uuid(v) -> bool:
    try:
        return isinstance(v, str) and str(uuid.UUID(v)) == v
    except ValueError:
        return False


def _hex64(v) -> bool:
    try:
        return isinstance(v, str) and len(v) == 64 and int(v, 16) >= 0 and v == v.lower()
    except ValueError:
        return False


def _read_private(path: Path, limit: int) -> bytes:
    """Regular, owned, not group/other writable, no symlink; bounded read."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as e:
        raise Unverifiable('unreadable') from e
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o022 or info.st_size > limit):
            raise Unverifiable('unsafe file')
        return os.read(fd, limit + 1)
    finally:
        os.close(fd)


def load_policy(cfg) -> dict:
    """The pinned policy, or Unverifiable. ``cfg`` is the config's GitIndexAdmissionConfig."""
    if cfg is None or not cfg.policy or not _hex64(cfg.policy_sha256):
        raise Unverifiable('not configured')
    raw = _read_private(cfg.policy, MAX_POLICY_BYTES)
    if hashlib.sha256(raw).hexdigest() != cfg.policy_sha256:
        raise Unverifiable('policy digest does not match its pin')
    try:
        p = json.loads(raw)
    except ValueError as e:
        raise Unverifiable('policy is not JSON') from e
    if (not isinstance(p, dict) or set(p) != POLICY_FIELDS or p['version'] != 1 or type(p['version']) is not int
            or p['operation'] != OPERATION
            or not all(isinstance(p[k], str) and p[k] and len(p[k]) <= 200 for k in ('revision', 'issuer', 'review_ref'))
            or not all(_uuid(p[k]) for k in ('actor', 'source_task', 'package_artifact'))
            or not _hex64(p['package_sha256'])
            or p['paths'] != sorted(ALLOWED_PATHS)
            or not isinstance(p['target_root'], str) or not os.path.isabs(p['target_root'])
            or '\0' in p['target_root']
            or any(type(p[k]) is not int for k in ('not_before', 'expires', 'grant_seconds'))
            or not 0 < p['grant_seconds'] <= MAX_GRANT_SECONDS or p['expires'] <= p['not_before']):
        raise Unverifiable('policy schema')
    return p


def load_revocations(cfg) -> set[str]:
    """Revoked policy revisions and request ids. An unreadable list is UNKNOWN, not empty."""
    if cfg is None or not cfg.revocations:
        raise Unverifiable('revocation list not configured')
    raw = _read_private(cfg.revocations, MAX_POLICY_BYTES)
    try:
        v = json.loads(raw)
    except ValueError as e:
        raise Unverifiable('revocation list is not JSON') from e
    if (not isinstance(v, dict) or set(v) != {'version', 'revoked'} or v['version'] != 1
            or not isinstance(v['revoked'], list) or len(v['revoked']) > 256
            or not all(isinstance(x, str) and len(x) <= 200 for x in v['revoked'])):
        raise Unverifiable('revocation list schema')
    return set(v['revoked'])


def validate(r: dict, agent: str) -> None:
    if set(r) != FIELDS or type(r['version']) is not int or r['version'] != 1 or r['agent'] != agent:
        raise ValueError('request schema mismatch')
    for key in ('request_id', 'task', 'attempt', 'actor', 'source_task'):
        if not _uuid(r[key]):
            raise ValueError('invalid request identity')
    for key in ('generation', 'package_sha256', 'policy'):
        if not _hex64(r[key]):
            raise ValueError('invalid digest')
    if not isinstance(r['operation'], str) or not isinstance(r['paths'], list) \
            or not all(isinstance(x, str) and len(x) <= 256 for x in r['paths']) or len(r['paths']) > 8:
        raise ValueError('invalid operation or paths')
    if any(type(r[k]) is not int for k in ('created', 'expires')):
        raise ValueError('invalid deadline')
    if not 0 < r['expires'] - r['created'] <= spool_io.MAX_TTL:
        raise ValueError('invalid deadline')


def submit(state: Path, cfg, agent: str, task: str, attempt: str, token: str, request_id: str, ttl: int) -> dict:
    """Publish a request pinned to the reviewed policy. Reading the policy here only fills the
    fields; the consumer re-verifies everything and is the sole issuer."""
    policy = load_policy(cfg)
    moment = int(time.time())
    r = dict(version=1, request_id=request_id, agent=agent, task=task, attempt=attempt, generation=token,
             created=moment, expires=moment + ttl, operation=policy['operation'], actor=policy['actor'],
             source_task=policy['source_task'], package_sha256=policy['package_sha256'],
             paths=list(policy['paths']), policy=cfg.policy_sha256)
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
            return submit(state, cfg, agent, task, attempt, token, request_id, ttl)
    receipt = root / (request_id + '.receipt.json')
    return {'request': r, 'receipt': spool_io._read(receipt) if receipt.exists() else None}


def consume(state: Path, agent: str, handler) -> None:
    spool_io.consume(state, agent, handler, SPOOL, validate)


def mismatch(request: dict, policy: dict) -> str:
    """'' when every bound field of the request equals the reviewed policy."""
    if request['operation'] != OPERATION or policy['operation'] != OPERATION:
        return 'operation'
    if request['paths'] != policy['paths']:
        return 'paths'
    if request['package_sha256'] != policy['package_sha256']:
        return 'package'
    if request['task'] != policy['source_task']:
        return 'task'                    # the grant lives only on the policy's own source task
    if request['source_task'] != policy['source_task']:
        return 'source_task'
    if request['actor'] != policy['actor']:
        return 'actor'
    return ''


def window(policy: dict, now: int) -> str:
    if now < policy['not_before']:
        return 'not_yet_valid'
    if now >= policy['expires']:
        return 'policy_expired'
    return ''


def new_grant(request: dict, policy: dict, now: int) -> dict:
    """The durable record. ``expires`` is fixed here; a repeated request never recomputes it."""
    return {'version': 1, 'request_id': request['request_id'], 'agent': request['agent'],
            'actor': request['actor'], 'task': request['task'], 'attempt': request['attempt'],
            'generation': request['generation'], 'operation': OPERATION, 'paths': list(policy['paths']),
            'package_sha256': policy['package_sha256'], 'package_artifact': policy['package_artifact'],
            'source_task': policy['source_task'], 'issuer': policy['issuer'], 'revision': policy['revision'],
            'review_ref': policy['review_ref'], 'policy': request['policy'], 'target_root': policy['target_root'],
            'issued': now, 'expires': min(now + policy['grant_seconds'], policy['expires']),
            'state': 'granted', 'notified': False}


def receipt(request: dict, status: str, grant: dict | None = None, reason: str = '') -> dict:
    """Bounded, secret-free receipt: safe identity, scope, issuer/policy revision, validity."""
    out = {'status': status, 'at': int(time.time()), 'version': __version__, 'operation': OPERATION,
           'request_id': request['request_id'], 'agent': request['agent'], 'task': request['task'],
           'attempt': request['attempt'], 'generation': request['generation'], 'reason': reason[:200]}
    if grant:
        out.update({k: grant[k] for k in ('actor', 'paths', 'package_sha256', 'package_artifact', 'source_task',
                                          'issuer', 'revision', 'review_ref', 'policy', 'issued', 'expires')})
        out['revoked'] = grant['state'] == 'revoked'
    return out


def load_grant(saved) -> dict | None:
    try:
        g = json.loads(saved[0]) if saved and saved[0] else None
    except ValueError:
        return None
    return g if isinstance(g, dict) and g.get('version') == 1 else None


SCHEMA = """
CREATE TABLE IF NOT EXISTS git_index_requests (
    request_id TEXT PRIMARY KEY,           -- a request id issues at most once, never purged
    task TEXT NOT NULL, attempt TEXT NOT NULL, issued INTEGER NOT NULL
);
"""


def ensure(store) -> None:
    store.db.executescript(SCHEMA)


def current_problem(grant: dict, cfg, attempt_id: str, now: int) -> str:
    """'' only while this exact grant is still valid. Every delivery/use calls this; any doubt is a
    refusal. Task ownership is checked by the caller against Mesh."""
    if grant.get('state') != 'granted':
        return 'ended'
    if grant.get('attempt') != attempt_id:
        return 'other_attempt'
    if now >= int(grant.get('expires', 0)):
        return 'expired'
    try:
        policy = load_policy(cfg)
        revoked = load_revocations(cfg)
    except Unverifiable as e:
        return 'unverifiable: ' + str(e)
    if cfg.policy_sha256 != grant.get('policy') or policy['revision'] != grant.get('revision'):
        return 'policy_changed'
    if grant['revision'] in revoked or grant['request_id'] in revoked:
        return 'revoked'
    if window(policy, now) == 'policy_expired':
        return 'expired'
    return ''


def instructions(grant: dict) -> str:
    when = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(int(grant['expires'])))
    return ("\n[runner] Scoped admission " + grant['request_id'] + " (policy revision " + grant['revision']
            + ", issuer " + grant['issuer'] + ") is in force for this attempt until " + when + ". "
            "It authorizes exactly one metadata-only effect: " + OPERATION + " on " + ", ".join(grant['paths'])
            + " in " + grant['target_root'] + " for package " + grant['package_sha256'] + ". "
            "It does NOT permit editing source in the shared fleet checkout or in any checkout, and it does not "
            "replace your own preconditions (fresh pins/CAS, quiescence, the cron mutex, bounded rollback). "
            "The existing ban on editing shared checkouts and the isolation of your leased checkout are unchanged. "
            "Do not act on this notice after the stated time or once a later turn says it ended.\n")


def ended_instructions(grant: dict, why: str) -> str:
    return ("\n[runner] Scoped admission " + grant['request_id'] + " has ENDED (" + why[:120] + "). "
            "The narrow Git-index exception no longer applies: do not change any Git index. "
            "A new explicit admission request is required.\n")


MARK = "\n[runner] Scoped admission "


def strip(prompt: str) -> str:
    """Remove every earlier scoped-admission paragraph (each ends with a newline) from a prompt."""
    while (i := prompt.find(MARK)) >= 0:
        j = prompt.find("\n", i + 1)
        prompt = prompt[:i] + (prompt[j:] if j >= 0 else "")
    return prompt
