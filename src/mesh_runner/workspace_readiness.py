"""Private owner-native observations. Never a lease, restore or launch grant.

Only the existing owner tick reads its registry. Clients submit signed immutable
requests and read durable receipts; they never open Store or checkpoint files.
"""
from __future__ import annotations

from contextlib import ExitStack
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import tempfile
import time
import uuid

from . import config as config_mod, workspace_requests as spool
from .workspace import base_lock
from . import __version__

SPOOL = 'workspace-readiness'
MAX_BYTES = 32768
MAX_MANIFEST = 4 * 1024 * 1024
MAX_PATHS = 8
MAX_TTL = 60
FIELDS = {'version', 'request_id', 'agent', 'task', 'attempt', 'session', 'generation',
          'created', 'expires', 'paths', 'previous', 'signature'}


class Refused(ValueError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def _hex(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def validate(r, agent):
    if (not isinstance(r, dict) or set(r) != FIELDS or type(r['version']) is not int
            or r['version'] != 1 or r['agent'] != agent or not _hex(r['generation'])
            or not _hex(r['signature'])):
        raise Refused('schema')
    for k in ('request_id', 'task', 'attempt', 'session'):
        if not isinstance(r[k], str) or str(uuid.UUID(r[k])) != r[k]:
            raise Refused('identity')
    if not isinstance(r['previous'], str):
        raise Refused('previous')
    if r['previous']:
        if not isinstance(r['previous'], str) or str(uuid.UUID(r['previous'])) != r['previous']:
            raise Refused('previous')
    if (any(type(r[k]) is not int for k in ('created', 'expires'))
            or not 0 < r['expires'] - r['created'] <= MAX_TTL):
        raise Refused('deadline')
    paths = r['paths']
    if not isinstance(paths, list) or not 0 < len(paths) <= MAX_PATHS:
        raise Refused('paths')
    for p in paths:
        if (not isinstance(p, dict) or set(p) != {'repo', 'source', 'checkpoint', 'manifest_sha256'}
                or not isinstance(p['repo'], str) or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', p['repo'])
                or not isinstance(p['source'], str) or len(p['source']) > 1024
                or not Path(p['source']).is_absolute() or str(Path(p['source'])) != p['source']
                or '..' in Path(p['source']).parts
                or not isinstance(p['checkpoint'], str) or not re.fullmatch('[0-9a-f]{32}', p['checkpoint'])
                or not _hex(p['manifest_sha256'])):
            raise Refused('paths')
    for key in ('repo', 'source', 'checkpoint'):
        if len({p[key] for p in paths}) != len(paths):
            raise Refused('duplicate_path')
    if len(canonical(r)) > MAX_BYTES:
        raise Refused('size')


def sign(r, key):
    if not key:
        raise Refused('signing_key_unavailable')
    return hmac.new(key.encode(), b'workspace-readiness-v1\0' + canonical(
        {k: v for k, v in r.items() if k != 'signature'}), hashlib.sha256).hexdigest()


def _read(path, limit=MAX_BYTES):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        s = os.fstat(fd)
        if not stat.S_ISREG(s.st_mode) or s.st_size > limit or s.st_uid != os.getuid() or s.st_mode & 0o077:
            raise Refused('private_file')
        raw = os.read(fd, limit + 1)
        if len(raw) > limit:
            raise Refused('size')
        return raw
    finally:
        os.close(fd)


def _publish(path, value):
    # Parent is private, owned, nonsymlink, validated by directory() at each call.
    path.parent.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.mkdir(mode=0o700, exist_ok=True)
    spool.directory(path.parent.parent.parent, value['agent'], SPOOL)
    raw = canonical(value)
    if len(raw) > MAX_BYTES:
        raise Refused('size')
    fd, tmp = tempfile.mkstemp(prefix='.readiness-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(raw)
            out.flush()
            os.fsync(out.fileno())
        try:
            os.link(tmp, path)
        except FileExistsError:
            return False
        dfd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
        return True
    finally:
        os.unlink(tmp)


def _names(root):
    if not root.exists():
        return []
    names = []
    with os.scandir(root) as entries:
        for e in entries:
            names.append(e.name)
            if len(names) > spool.MAX_REQUESTS * 2:
                raise Refused('spool_full')
    return names


def submit(state, request, key):
    r = dict(request, signature=sign(request, key))
    validate(r, r['agent'])
    root = spool.directory(state, r['agent'], SPOOL)
    path = root / (r['request_id'] + '.request.json')
    if path.exists():
        if json.loads(_read(path)) != r:
            raise Refused('request_id_conflict')
    else:
        if len(_names(root)) >= spool.MAX_REQUESTS * 2 - 1:
            raise Refused('spool_full')
        if not _publish(path, r) and json.loads(_read(path)) != r:
            raise Refused('request_id_conflict')
    return {'request_id': r['request_id'], 'receipt': receipt(state, r['agent'], r['request_id'])}


def receipt(state, agent, request_id):
    if str(uuid.UUID(request_id)) != request_id:
        raise Refused('identity')
    root = spool.directory(state, agent, SPOOL)
    path = root / (request_id + '.receipt.json')
    return json.loads(_read(path)) if path.exists() else None


def _source_digest():
    root = Path(__file__).parent
    return digest({str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
                   for p in sorted(root.rglob('*.py'))})


def controller(cfg):
    return {'instance': str(uuid.uuid4()), 'pid': os.getpid(), 'started': time.time_ns(),
            'executable': sys.executable, 'version': __version__, 'source_sha256': _source_digest(),
            'config_sha256': hashlib.sha256(cfg.source.read_bytes()).hexdigest() if cfg.source else ''}


def _authority(runner, request):
    # Reload actual source: disabled/removed lane and rotated/revoked key cannot
    # inherit an old process's authority. No credential bytes leave this method.
    if runner.cfg.source is None:
        raise Refused('config_unavailable')
    cfg = config_mod.load(runner.cfg.source)
    if not cfg.workspace_readiness_v1:
        raise Refused('disabled')
    agent = next((a for a in cfg.agents if a.name == request['agent']), None)
    if (agent is None or not agent.key or not hmac.compare_digest(request['signature'], sign(request, agent.key))):
        raise Refused('authorization')
    identity = runner._workspace_readiness_controller
    if (hashlib.sha256(cfg.source.read_bytes()).hexdigest() != identity['config_sha256']
            or _source_digest() != identity['source_sha256']):
        raise Refused('controller_drift')
    me = runner.mesh.me()
    me = me.get('agent', me)
    task = runner._task(request['task'])
    if (me.get('id') != runner.agent_id or not task or not runner._still_ours(task)
            or task.get('checked_out_by') not in (None, '', runner.agent_id)
            or task.get('checkout_session_id') != request['attempt']):
        raise Refused('ownership')
    return {'owner': runner.agent_id, 'task_version': task.get('version'),
            'checkout_generation': task.get('checkout_generation'),
            'checked_out': bool(task.get('checked_out_by')), 'checkout_attempt': request['attempt'],
            'human_gate': bool(task.get('human_gate')), 'start_after': task.get('start_after')}


def _manifest(root, checkpoint):
    # Never Checkpoints.read()/verify(): those may recover/rename/replay.
    fds = []
    try:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        fds.append(fd)
        fd = os.open(checkpoint, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
        fds.append(fd)
        mf = os.open('manifest.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        fds.append(mf)
        s = os.fstat(mf)
        if not stat.S_ISREG(s.st_mode) or s.st_size > MAX_MANIFEST or s.st_uid != os.getuid():
            raise Refused('manifest_unknown')
        with os.fdopen(os.dup(mf), 'rb') as src:
            raw = src.read(MAX_MANIFEST + 1)
        if len(raw) > MAX_MANIFEST:
            raise Refused('manifest_unknown')
        return hashlib.sha256(raw).hexdigest(), json.loads(raw)
    finally:
        for fd in reversed(fds):
            os.close(fd)


def _snapshot(runner, r):
    db, pool = runner.store.db, runner.pool
    a = runner.store.get(r['attempt'])
    if (a is None or a.agent != r['agent'] or a.task_id != r['task'] or a.session_id != r['session']
            or a.mode != 'work' or a.state not in ('waiting', 'ready')
            or spool.generation(a) != r['generation']):
        raise Refused('attempt_drift')
    if db.execute("SELECT 1 FROM attempts WHERE agent=? AND task_id=? AND id!=? "
                  "AND state IN ('starting','running','unknown','waiting','ready')",
                  (a.agent, a.task_id, a.id)).fetchone():
        raise Refused('attempt_conflict')
    if sorted(a.workspaces) != sorted(p['source'] for p in r['paths']):
        raise Refused('inventory_drift')
    # Do not initialize schema or invoke maintenance.held() inside this transaction.
    rows = [dict(x) for x in db.execute('SELECT * FROM workspace_checkpoints WHERE owner=? AND attempt=? LIMIT ?',
                                      (pool.owner, a.id, MAX_PATHS + 1))]
    if len(rows) != len(r['paths']) or {x['repo'] for x in rows} != {p['repo'] for p in r['paths']}:
        raise Refused('journal_unknown')
    by_repo = {x['repo']: x for x in rows}
    out = []
    for pin in r['paths']:
        repo, path = pin['repo'], pin['source']
        spec = pool.repos.get(repo)
        if (spec is None or Path(path).parent != pool.root / repo
                or not re.fullmatch('slot-[1-9][0-9]*', Path(path).name)):
            raise Refused('path_scope')
        row = by_repo[repo]
        cp_root = pool.root.parent / 'checkpoints' / pool.owner
        if row['source'] != path or row['checkpoint'] != str(cp_root / pin['checkpoint']):
            raise Refused('journal_drift')
        slot = db.execute("SELECT pool,state,CASE WHEN attempt=? THEN 'own' WHEN attempt='' THEN 'none' "
                          "ELSE 'other' END AS holder,updated_at FROM slots WHERE path=?", (a.id, path)).fetchone()
        occupancy = 'unknown'
        if slot and slot['pool'] == pool._pool(repo):
            if slot['state'] == 'free' and slot['holder'] == 'none':
                occupancy = 'free'
            elif slot['state'] == 'leased' and slot['holder'] == 'own':
                occupancy = 'exact-own'
            elif slot['holder'] == 'other' or slot['state'] == 'held':
                occupancy = 'conflict'
        sha, manifest = _manifest(cp_root, pin['checkpoint'])
        meta = manifest.get('metadata', {})
        continuity = (sha == pin['manifest_sha256'] and manifest.get('version') in (1, 2)
            and manifest.get('source') == path and meta.get('repo') == repo
            and meta.get('attempt') == a.id and meta.get('task') == a.task_id
            and meta.get('checkpoint_generation') == r['generation']
            and meta.get('session_id') == a.session_id and meta.get('harness') == a.harness
            and meta.get('cwd') == str(runner.agent.profile_dir)
            and meta.get('workspaces') == a.workspaces)
        # READY means a complete released journal or an exact-own restored path.
        phase_ok = ((row['phase'] == 'released' and not row['target'] and occupancy == 'free')
                    or (row['phase'] in ('restored', 'active') and row['target'] == path
                        and occupancy == 'exact-own'))
        out.append({'repo': repo, 'source': path, 'occupancy': occupancy,
                    'slot_generation': slot['updated_at'] if slot else None,
                    'checkpoint': pin['checkpoint'], 'manifest_sha256': sha,
                    'phase': row['phase'], 'target': row['target'],
                    'continuity': bool(continuity), 'phase_ready': bool(phase_ok)})
    return {'task': a.task_id, 'attempt': a.id, 'session': a.session_id, 'harness': a.harness,
            'cwd': str(runner.agent.profile_dir), 'generation': r['generation'], 'state': a.state,
            'inventory': list(a.workspaces), 'paths': out}


def _capture(runner, r, finish):
    from .checkpoint import _IO_LOCK
    with ExitStack() as locks:
        for repo in sorted({p['repo'] for p in r['paths']}):
            if repo not in runner.pool.repos:
                raise Refused('path_scope')
            lock = base_lock(runner.pool.repos[repo].base, timeout=0.1)
            if not lock.acquire(timeout=0.1):
                raise Refused('busy')
            locks.callback(lock.release)
        if not _IO_LOCK.acquire(timeout=0.1):
            raise Refused('busy')
        locks.callback(_IO_LOCK.release)
        db = runner.store.db
        busy = db.execute('PRAGMA busy_timeout').fetchone()[0]
        db.execute('PRAGMA busy_timeout=100')
        try:
            db.execute('BEGIN IMMEDIATE')
            try:
                return finish(_snapshot(runner, r))
            finally:
                db.execute('ROLLBACK')
        finally:
            db.execute(f'PRAGMA busy_timeout={int(busy)}')


def observe(runner, r):
    validate(r, runner.agent.name)
    now = int(time.time())
    if r['created'] > now + 5 or r['expires'] <= now:
        raise Refused('expired')
    auth = _authority(runner, r)
    snapshot = _capture(runner, r, lambda value: value)
    if _authority(runner, r) != auth:
        raise Refused('authority_drift')

    def deliver(current):
        if current != snapshot:
            raise Refused('observation_drift')
        moment = int(time.time())
        if moment >= r['expires']:
            raise Refused('expired')
        ready = all(p['continuity'] and p['phase_ready'] for p in snapshot['paths'])
        status = 'READY' if ready else 'NOT_READY'
        token = digest({'snapshot': snapshot, 'authority': auth,
                        'controller': runner._workspace_readiness_controller})
        if r['previous']:
            old = receipt(runner.cfg.state_dir, r['agent'], r['previous'])
            if (not old or old.get('status') != 'READY' or old.get('expires', 0) <= moment
                    or old.get('observation_generation') != token):
                status = 'STALE'
        result = {'version': 1, 'request_id': r['request_id'], 'agent': r['agent'],
                  'observation_id': str(uuid.uuid4()), 'observed': moment,
                  'expires': min(r['expires'], moment + MAX_TTL), 'status': status,
                  'request_sha256': digest(r), 'observation_generation': token,
                  'controller': runner._workspace_readiness_controller, 'authority': auth,
                  'snapshot': snapshot, 'previous': r['previous'], 'operational_authority': False}
        root = spool.directory(runner.cfg.state_dir, r['agent'], SPOOL)
        _publish(root / (r['request_id'] + '.receipt.json'), result)
        return result

    # Registry writes and cooperative checkpoint writers cannot race durable publication.
    # External Mesh checks run between captures, with neither DB nor IO locks held.
    return _capture(runner, r, deliver)


def consume(runner):
    try:
        _consume(runner)
    except (OSError, ValueError, sqlite3.Error):
        # Unavailable/full observation transport never holds ordinary work.
        return


def _consume(runner):
    """At most one bounded observation per ordinary owner tick, never a wake."""
    root = spool.directory(runner.cfg.state_dir, runner.agent.name, SPOOL)
    for name in sorted(_names(root)):
        if not re.fullmatch('[0-9a-f-]{36}\\.request\\.json', name):
            continue
        dest = root / name.replace('.request.json', '.receipt.json')
        if dest.exists():
            continue
        try:
            r = json.loads(_read(root / name))
            validate(r, runner.agent.name)
            if name != r['request_id'] + '.request.json':
                raise Refused('filename')
            observe(runner, r)
        except Exception as e:
            # No external response, raw registry, foreign identity or secret.
            reason = str(e) if isinstance(e, Refused) else 'unknown'
            result = {'version': 1, 'agent': runner.agent.name,
                      'request_id': name[:-len('.request.json')], 'status': 'UNKNOWN',
                      'reason': reason, 'observed': int(time.time()), 'operational_authority': False}
            _publish(dest, result)
        return
