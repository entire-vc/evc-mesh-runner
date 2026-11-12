"""Opt-in machine queue WAIT protocol. No provider access or timer fallback.

Runner owns the manifest and SQLite intent marks; board-services owns receipts.
A ready transition and its terminal intent commit in one owner-store transaction.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
from uuid import UUID, uuid4

TTL = 600
SOURCE_INTERVAL = 300
MAX_BYTES = 4 * 1024 * 1024
MAX_CURRENT = 128
PREFIX = 'queue_wait:'
MARKER = re.compile(r'\[fleet queue-request:(\{[^\n]*\})\]')
MACHINE = re.compile(r'\[fiddler starvation-wake:(\d+)\]')


def stamp(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def contract(value):
    """v1: every target must recover. v2 names its mode (`any` | `all`) and demands a unit
    that became ready after `observed_at`; the observer is the other half of this wire."""
    version = value.get('version') if isinstance(value, dict) else None
    if (not isinstance(value, dict) or type(version) is not int
            or set(value) != ({'version', 'targets', 'observed_at'} if version == 1
                              else {'version', 'mode', 'targets', 'observed_at'} if version == 2 else None)
            or (version == 2 and value['mode'] not in ('any', 'all'))
            or type(value['observed_at']) is not int
            or not stamp(value['observed_at']) or not isinstance(value['targets'], list)
            or not 1 <= len(value['targets']) <= 16):
        raise ValueError('unknown queue contract')
    targets = []
    for target in value['targets']:
        if (not isinstance(target, dict) or set(target) != {'agent_id', 'project_id', 'lane'}
                or not isinstance(target['lane'], str)
                or not re.fullmatch(r'[\w .-]{1,80}', target['lane'])):
            raise ValueError('unknown queue target')
        targets.append(dict(agent_id=str(UUID(target['agent_id'])),
                            project_id=str(UUID(target['project_id'])), lane=target['lane']))
    ordered = sorted(targets, key=lambda t: (t['agent_id'], t['project_id']))
    clean = (dict(version=1, observed_at=value['observed_at'], targets=ordered) if version == 1
             else dict(version=2, mode=value['mode'], observed_at=value['observed_at'], targets=ordered))
    if clean != value or len({(t['agent_id'], t['project_id']) for t in targets}) != len(targets):
        raise ValueError('noncanonical queue contract')
    return clean


def as_any(value):
    """The explicit `WAIT queue:` event: retry the permitted pick on ANY fresh ready unit."""
    value = contract(value)
    return contract(dict(version=2, mode='any', observed_at=value['observed_at'], targets=value['targets']))


def served(store, a, owner, value):
    """Source already delivered/withdrawn under either wire version: it cannot wake again."""
    value = contract(value)
    v1 = dict(version=1, observed_at=value['observed_at'], targets=value['targets'])
    return consumed(store, a, owner, v1) or consumed(store, a, owner, as_any(v1))


def residual_key(owner, value):
    payload = json.dumps(dict(owner=owner, contract=contract(value)), sort_keys=True, separators=(',', ':'))
    return 'queue_residual:' + hashlib.sha256(payload.encode()).hexdigest()


def recovered_targets(receipt, req):
    """Targets the observer proved recovered for this exact generation, or None (unknown).

    Older observers publish no list: nothing is inferred, so no residual is created."""
    try:
        proof = receipt['requests'][req['generation_id']]['proof']
        listed = proof['recovered']
        wanted = {(t['agent_id'], t['project_id']) for t in req['contract']['targets']}
        got = set()
        for item in listed:
            if not isinstance(item, dict) or set(item) != {'agent_id', 'project_id'}:
                return None
            got.add((str(UUID(item['agent_id'])), str(UUID(item['project_id']))))
        return got if got and got <= wanted and len(got) == len(listed) else None
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def residual_mark(owner, req, receipt):
    """Marks recording which targets an ANY wake did not serve ({} when nothing remains or the served
    set is unproven). Written in the same transaction as the fulfilment. The residual starts as
    already delivered: the lead's first-move turn was spent on this source."""
    wire = contract(req['contract'])
    got = recovered_targets(receipt, req)
    if wire['version'] != 2 or wire['mode'] != 'any' or not got:
        return {}
    rest = [t for t in wire['targets'] if (t['agent_id'], t['project_id']) not in got]
    if not rest:
        return {}
    value = dict(version=1, source_observed_at=wire['observed_at'], generation_id=req['generation_id'],
                 served=[dict(agent_id=a, project_id=p) for a, p in sorted(got)],
                 remaining=rest)
    left = contract(dict(version=2, mode='any', observed_at=wire['observed_at'], targets=rest))
    return {residual_key(owner, wire): json.dumps(value, sort_keys=True),
            delivered_key(owner, left): 'delivered'}


def residual(store, a, owner, value):
    """The unserved-target contract left by a served ANY wake on `value`: same source stamp,
    only the targets no wake has covered. None when absent or malformed (fail closed)."""
    wire = contract(value)
    found = store.mark(a.agent, a.task_id, residual_key(owner, wire))
    try:
        saved = json.loads(found[0]) if found else None
        if (not isinstance(saved, dict) or saved.get('version') != 1
                or saved.get('source_observed_at') != wire['observed_at']):
            return None
        out = contract(dict(version=2, mode='any', observed_at=wire['observed_at'],
                            targets=saved['remaining']))
        have = {(t['agent_id'], t['project_id']) for t in wire['targets']}
        return out if {(t['agent_id'], t['project_id']) for t in out['targets']} < have else None
    except (ValueError, TypeError, KeyError, AttributeError):
        return None


def effective(store, a, owner, source):
    """What an explicit `WAIT queue:` observes for `source`: the ANY contract, or after each served
    wake the residual of it. None = fully served (or unproven): nothing to observe."""
    current = as_any(source)
    for _ in range(17):
        if not served(store, a, owner, current):
            return current
        current = residual(store, a, owner, current)
        if current is None:
            return None
    return None


def request(value):
    if (not isinstance(value, dict) or set(value) != {'version', 'generation_id', 'task_id', 'contract'}
            or type(value['version']) is not int or value['version'] != 1):
        raise ValueError('unknown queue request')
    clean = dict(version=1, generation_id=str(UUID(value['generation_id'])),
                 task_id=str(UUID(value['task_id'])), contract=contract(value['contract']))
    if clean != value:
        raise ValueError('noncanonical queue request')
    return clean


def new_request(task_id, value):
    return request(dict(version=1, generation_id=str(uuid4()), task_id=task_id, contract=value))


BUILTIN_HOLDS = ('no-runner', 'rescue-exhausted')


def held(labels, hold_labels=()):
    """A label that keeps the card from an automatic continuation."""
    return any(x.startswith('park:') or x in BUILTIN_HOLDS or x in hold_labels for x in labels)


def eligible(task, owner, hold_labels=()):
    labels = task.get('labels')
    return bool(task.get('assignee_id') == owner and task.get('human_gate') is False
                and isinstance(labels, list) and all(isinstance(x, str) for x in labels)
                and 'fleet-starvation-wake' in labels
                and not held(labels, hold_labels)
                and task.get('delegation_level') != 'supervised' and not task.get('completion_signal')
                and task.get('checked_out_by') in (None, '', owner))


def source(task, comments, owner, current_time, hold_labels=()):
    """Strictly validate the newest machine demand, allowing audited supersession.

    Older malformed contracts do not poison a later generation. A typed source
    without an orderable machine stamp remains unknown; it cannot be proved old.
    """
    if not eligible(task, owner, hold_labels):
        return None
    candidates = []
    try:
        for text in [task.get('description') or ''] + [c.get('body') or '' for c in comments]:
            if '[fleet queue-request:' not in text:
                continue
            machines = MACHINE.findall(text)
            if (not machines or len(machines) != text.count('[fiddler starvation-wake:')
                    or len({int(value) for value in machines}) != 1):
                return None
            candidates.append((int(machines[0]), text))
        if not candidates:
            return None
        newest = max(stamp for stamp, _ in candidates)
        rows = []
        for stamp, text in candidates:
            if stamp != newest:
                continue
            markers = MARKER.findall(text)
            if len(markers) != text.count('[fleet queue-request:'):
                return None
            for marker in markers:
                value = contract(json.loads(marker))
                if value['observed_at'] != stamp or value['observed_at'] > current_time:
                    return None
                rows.append(value)
        return rows[0] if all(c == rows[0] for c in rows) else None
    except (ValueError, TypeError, KeyError, AttributeError):
        return None


def read_json(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o077 or info.st_size > MAX_BYTES):
            raise ValueError('unsafe queue file')
        data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError('queue file bound')
    return json.loads(data)


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        info = path.lstat()
    except FileNotFoundError:
        info = None
    if info is not None and (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                             or info.st_mode & 0o077):
        raise ValueError('unsafe queue destination')
    payload = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()
    if len(payload) > MAX_BYTES:
        raise ValueError('queue manifest bound')
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def published_manifest(store):
    """Read the durable outbox, including inside the ready transaction."""
    found = store.mark('__runner__', '__queue__', 'manifest')
    try:
        return json.loads(found[0]) if found else None
    except (ValueError, TypeError):
        return None


def _terminal_ack(value):
    """External acknowledgement data is permission only after typed validation."""
    if (not isinstance(value, list) or len(value) > MAX_CURRENT
            or any(not isinstance(generation, str) for generation in value)):
        raise ValueError('unknown terminal acknowledgement')
    clean = {str(UUID(generation)) for generation in value}
    if clean != set(value):
        raise ValueError('unknown terminal acknowledgement')
    return clean


def available(state, exact_request, current_time, manifest=None):
    try:
        req = request(exact_request)
        if 'terminal_ack' in state:
            _terminal_ack(state['terminal_ack'])
        # An otherwise fresh proof from a superseded owner manifest is UNKNOWN.
        # Validate the same bounded wire shape accepted by the observer.
        if (not isinstance(manifest, dict)
                or set(manifest) != {'version', 'revision', 'requests', 'terminal'}
                or type(manifest['version']) is not int or manifest['version'] != 1
                or type(manifest['revision']) is not int or manifest['revision'] <= 0
                or not isinstance(manifest['requests'], list) or len(manifest['requests']) > MAX_CURRENT
                or not isinstance(manifest['terminal'], list) or len(manifest['terminal']) > MAX_CURRENT):
            return False
        rows = [request(value) for value in manifest['requests']]
        terminal = [str(UUID(value)) for value in manifest['terminal']]
        if (req not in rows or len({value['generation_id'] for value in rows}) != len(rows)
                or len({value['task_id'] for value in rows}) != len(rows)
                or terminal != manifest['terminal'] or len(set(terminal)) != len(terminal)
                or set(terminal) & {value['generation_id'] for value in rows}):
            return False
        row = state['requests'][req['generation_id']]
        proof = row['proof']
        verified, expires = proof['verified_at'], proof['expires_at']
        return bool(type(state['version']) is int and state['version'] == 1
                    and type(state['manifest_revision']) is int
                    and state['manifest_revision'] == manifest['revision']
                    and state['manifest_digest'] == digest(manifest)
                    and state['current'].get(req['task_id']) == req['generation_id']
                    and row['request'] == req and not row.get('identity_conflict') and not row.get('superseded')
                    and len(req['contract']['targets']) <= 4
                    and type(proof['version']) is int and proof['version'] == 1 and proof['request'] == req
                    and all(stamp(x) for x in (current_time, verified, expires,
                                              state['clock_watermark'], row['clock_watermark'], row['checked_at']))
                    and req['contract']['observed_at'] <= row['checked_at'] <= verified
                    <= row['clock_watermark'] <= state['clock_watermark'] <= current_time < expires
                    and expires == verified + TTL
                    and current_time >= max(state['clock_watermark'], row['clock_watermark']))
    except (KeyError, ValueError, TypeError, AttributeError):
        return False


def key(req):
    return PREFIX + request(req)['generation_id']


def consumed_key(owner, value):
    # Task and lane are also scoped by Store.mark's agent/task primary key.
    payload = json.dumps(dict(owner=owner, contract=contract(value)), sort_keys=True, separators=(',', ':'))
    return 'queue_consumed:' + hashlib.sha256(payload.encode()).hexdigest()


def delivered_key(owner, value):
    # The first-move turn for one source identity: fulfilled/withdrawn is a different fact.
    payload = json.dumps(dict(owner=owner, contract=contract(value)), sort_keys=True, separators=(',', ':'))
    return 'queue_delivered:' + hashlib.sha256(payload.encode()).hexdigest()


FLOOR_KEY = 'queue_floor'


def floor(store, agent, task_id):
    """Newest request stamp the owner has already been served: an older read is partial."""
    found = store.mark(agent, task_id, FLOOR_KEY)
    try:
        return int(found[0]) if found and found[0] else 0
    except (ValueError, TypeError):
        return 0


def raised_floor(store, agent, task_id, value):
    return str(max(floor(store, agent, task_id), contract(value)['observed_at']))


def delivered(store, a, owner, value):
    return bool(store.mark(a.agent, a.task_id, delivered_key(owner, value)))


def consumed(store, a, owner, value):
    if store.mark(a.agent, a.task_id, consumed_key(owner, value)):
        return True
    # Compatibility with pre-fix deliveries. Filter the full canonical identity
    # in SQLite, so unrelated history cannot suppress a genuinely new source.
    clean = contract(value)
    predicates = ["json_extract(value,'$.agent_id')=?",
                  "json_extract(value,'$.request.task_id')=?",
                  "json_extract(value,'$.request.contract.version')=1",
                  "json_extract(value,'$.request.contract.observed_at')=?",
                  "json_array_length(value,'$.request.contract.targets')=?"]
    args = [a.agent, a.task_id, owner, a.task_id, clean['observed_at'], len(clean['targets'])]
    for index, target in enumerate(clean['targets']):
        for field in ('agent_id', 'project_id', 'lane'):
            predicates.append(f"json_extract(value,'$.request.contract.targets[{index}].{field}')=?")
            args.append(target[field])
    rows = store.db.execute("SELECT value FROM marks WHERE agent=? AND task_id=? "
        "AND key LIKE 'queue_wait:%' AND json_valid(value) "
        "AND json_extract(value,'$.state')='complete' AND " + " AND ".join(predicates) +
        " LIMIT 129", args).fetchall()
    if len(rows) > 128:
        return True  # Only this exact source identity is ambiguous, never a new source.
    for row in rows:
        try:
            saved = json.loads(row[0])
            req = request(saved['request'])
            if saved.get('agent_id') == owner and req['task_id'] == a.task_id and req['contract'] == contract(value):
                return True
        except (ValueError, TypeError, KeyError):
            continue
    return False


def intent(a, req, owner, current_time):
    return dict(version=1, request=request(req), attempt_id=a.id, agent_id=owner,
                state='pending', clock_watermark=current_time, source_checked_at=0)


def pending(store, a, req, owner):
    """Malformed or terminal marks are never repaired into pending permission."""
    try:
        found = store.mark(a.agent, a.task_id, key(req))
        value = json.loads(found[0]) if found else None
        if (not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] != 1
                or value.get('request') != request(req) or value.get('attempt_id') != a.id
                or value.get('agent_id') != owner or value.get('state') != 'pending'
                or not stamp(value.get('clock_watermark'))
                or not (value.get('source_checked_at') == 0 and type(value['source_checked_at']) is int
                        or stamp(value.get('source_checked_at')))
                or value['source_checked_at'] > value['clock_watermark']):
            return None
        return value
    except (ValueError, TypeError, KeyError):
        return None


def cancel(store, a):
    req = a.wait.get('queue_request')
    if not req:
        return
    try:
        found = store.mark(a.agent, a.task_id, key(req))
        value = json.loads(found[0]) if found else None
        if value and value.get('state') == 'pending' and value.get('attempt_id') == a.id:
            value['state'] = 'cancelled'
            store.mark(a.agent, a.task_id, key(req), json.dumps(value))
    except (ValueError, TypeError, KeyError):
        return


def publish(store, requests_path, receipts_path):
    """Single Runner writer. Persist revision before atomic publication.

    Terminal intents remain in SQLite for deduplication. Only unacknowledged
    terminal IDs are sent, in bounded batches; old revisions cannot replay GC.
    """
    old_mark = store.mark('__runner__', '__queue__', 'manifest')
    previous = json.loads(old_mark[0]) if old_mark else dict(version=1, revision=0, requests=[], terminal=[])
    if (type(previous.get('revision')) is not int or previous['revision'] < 0):
        raise ValueError('unknown manifest revision')
    try:
        receipt = read_json(receipts_path)
    except (OSError, ValueError, TypeError):
        receipt = {}
    if not isinstance(receipt, dict):
        receipt = {}
    revision = receipt.get('manifest_revision')
    acknowledged = (type(receipt.get('version')) is int and receipt['version'] == 1
                    and type(revision) is int and 0 < revision <= previous['revision']
                    and isinstance(receipt.get('terminal_ack'), list)
                    and len(receipt['terminal_ack']) <= MAX_CURRENT)
    terminal_ack = set()
    if acknowledged:
        terminal_ack = _terminal_ack(receipt['terminal_ack'])
    attempts = {a.id: a for agent in store.db.execute('SELECT DISTINCT agent FROM attempts')
                for a in store.by_state(agent[0], ('waiting',))}
    rows = store.db.execute('SELECT agent,task_id,key,value FROM marks WHERE key LIKE ? ORDER BY key',
                            (PREFIX + '%',)).fetchall()
    current, terminal = [], []
    for agent, task_id, mark_key, raw in rows:
        value = json.loads(raw)  # corrupt durable intent fails export closed
        req = request(value['request'])
        if key(req) != mark_key or req['task_id'] != task_id:
            raise ValueError('unknown intent identity')
        a = attempts.get(value.get('attempt_id'))
        owned = bool(a and a.agent == agent and a.task_id == task_id
                     and a.wait.get('kind') == 'queue' and a.wait.get('queue_request') == req)
        changed = False
        if value.get('state') == 'pending' and not owned:
            value['state'], changed = 'cancelled', True
        if value.get('state') == 'pending':
            current.append(req)
        elif value.get('state') in ('complete', 'cancelled'):
            if req['generation_id'] in terminal_ack:
                value['acknowledged'], changed = True, True
            if not value.get('acknowledged'):
                terminal.append(req['generation_id'])
        else:
            raise ValueError('unknown queue intent state')
        if changed:
            store.mark(agent, task_id, mark_key, json.dumps(value))
    if len(current) > MAX_CURRENT or len({r['task_id'] for r in current}) != len(current):
        raise ValueError('current queue manifest bound')
    body = dict(version=1, requests=sorted(current, key=lambda r: r['generation_id']),
                terminal=sorted(terminal)[:MAX_CURRENT])
    if previous['revision'] > 0 and all(previous.get(k) == v for k, v in body.items()):
        value = previous
    else:
        value = dict(body, revision=previous['revision'] + 1)
        store.mark('__runner__', '__queue__', 'manifest', json.dumps(value))
    write_json(requests_path, value)
    return value
