"""Read-only card-WAIT explanation; never an eligibility or wake predicate."""
from __future__ import annotations

from dataclasses import dataclass, field
import datetime as dt
import json
import re
import time

from .mesh import MeshError
from .store import iso, now

MARK = 'blocking_path_diagnostic_v1'
CACHE_SECONDS = 300
MAX_GETS, MAX_DEPTH, MAX_NODES, SECONDS = 12, 4, 8, 8.0
# Mesh's typed nonblocking edges; digest compatibility also recognizes the
# legacy depends_on spelling as blocking. Unknown types never prove no blocker.
BLOCKING_TYPES = ('blocks', 'depends_on')
NONBLOCKING_TYPES = ('relates_to', 'is_child_of')
UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
SHORT = re.compile(r'[0-9a-f]{8}')


def _addressee(row):
    # The assignee of an open, ungated end-of-chain card is who must move it
    # (CI, MR, work). A gated end asks a person; neither owner nor assignee
    # proves who, so that stays unknown.
    name = row.get('assignee_name')
    if not isinstance(name, str):
        assignee = row.get('assignee')
        name = assignee.get('name') if isinstance(assignee, dict) else None
    return name if isinstance(name, str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', name) else 'unknown'


def _format_reason(task_id, paths, gates, checked_at, addressees=()):
    fields = [' -> '.join('#' + tid[:8] for tid in [task_id] + path) for path in paths]
    fields += ['#' + tid[:8] + ' human_gate=true gate_owner=' + owner for tid, owner in gates]
    names = sorted(set(addressees))
    tail = '; addressee=' + (','.join(names) if names else 'unknown') + '; observed_at=' + checked_at
    reason = 'Blocking path diagnostic: ' + '; '.join(fields) + tail
    if len(reason) <= 800:
        return reason
    # Preserve whole facts and the mandatory epistemic/time suffix. A clipped
    # owner or chain must never look like a complete successful diagnostic.
    prefix = 'Blocking path diagnostic UNKNOWN (output overflow): '
    kept = []
    for field in fields:
        candidate = (prefix + '; '.join(kept + [field]) + '; omitted_fields='
                     + str(len(fields) - len(kept) - 1) + tail)
        if len(candidate) > 800:
            break
        kept.append(field)
    return (prefix + '; '.join(kept) + '; omitted_fields='
            + str(len(fields) - len(kept)) + tail)


class Incomplete(Exception):
    pass


@dataclass
class Budget:
    max_gets: int = MAX_GETS
    gets: int = 0
    nodes: int = 0
    deadline: float = field(default_factory=lambda: time.monotonic() + SECONDS)

    def get(self, mesh, path):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise Incomplete('deadline')
        if self.gets >= self.max_gets:
            raise Incomplete('GET budget')
        reader = getattr(mesh, 'bounded_get', None)
        if not callable(reader):
            raise Incomplete('bounded GET unavailable')
        self.gets += 1
        result = reader(path, timeout=remaining)
        if time.monotonic() >= self.deadline:
            raise Incomplete('deadline')
        return result


class BlockingPath:
    def __init__(self, mesh, store, lane):
        self.mesh, self.store, self.lane = mesh, store, lane
        self._cache = {}

    def resolve(self, attempt, budget):
        target = attempt.wait.get('target')
        saved = self.store.mark(self.lane, attempt.task_id, MARK) if self.store else None
        try:
            record = json.loads(saved[0]) if saved and len(saved[0]) <= 4096 else self._cache.get(attempt.task_id, {})
            checked = dt.datetime.fromisoformat(record.get('checked_at', ''))
            age = (now() - checked).total_seconds()
            if (record.get('target') == target and 0 <= age < CACHE_SECONDS
                    and isinstance(record.get('reason'), str) and len(record['reason']) <= 800):
                return record['reason']
        except (ValueError, TypeError, AttributeError, RecursionError):
            pass
        paths, gates, addressees = [], [], []
        visited = set()

        def walk(value, ancestry):
            if len(ancestry) >= MAX_DEPTH:
                raise Incomplete('depth cut')
            if not isinstance(value, str) or not (UUID.fullmatch(value) or SHORT.fullmatch(value)):
                raise Incomplete('invalid card identity')
            endpoint = '/tasks/' + (value if UUID.fullmatch(value) else 'by-short-id/' + value)
            row = budget.get(self.mesh, endpoint)
            if isinstance(row, dict):
                row = row.get('task', row)
            if not isinstance(row, dict) or not isinstance(row.get('id'), str) or not UUID.fullmatch(row['id']):
                raise Incomplete('task shape')
            tid = row['id']
            if not tid.startswith(value):
                raise Incomplete('task identity mismatch')
            path = ancestry + [tid]
            if tid in ancestry or tid == attempt.task_id:
                raise Incomplete('cycle')
            if tid in visited:
                return
            if budget.nodes >= MAX_NODES:
                raise Incomplete('node cut')
            budget.nodes += 1
            visited.add(tid)
            category = row.get('status_category')
            if category not in ('todo', 'in_progress', 'review', 'done', 'cancelled', 'backlog'):
                raise Incomplete('status unknown')
            if category in ('done', 'cancelled'):
                paths.append(path)
                return
            gate = row.get('human_gate')
            if gate is not True and gate is not False:
                raise Incomplete('human gate unknown')
            deps = budget.get(self.mesh, '/tasks/' + tid + '/dependencies')
            if not isinstance(deps, dict) or not isinstance(deps.get('outgoing'), list):
                raise Incomplete('dependency shape')
            rows = deps['outgoing']
            if len(rows) > 256:
                raise Incomplete('edge cut')
            # Current endpoint returns complete outgoing/incoming lists without
            # pagination fields. If metadata appears, it must strictly attest
            # completeness; bool is not a count despite being an int subclass.
            if (('has_more' in deps and deps['has_more'] is not False)
                    or ('next_page' in deps and deps['next_page'] is not None)
                    or ('total' in deps and (type(deps['total']) is not int
                                            or deps['total'] != len(rows)))):
                raise Incomplete('pagination cut')
            children = []
            for dep in rows:
                if not isinstance(dep, dict) or not isinstance(dep.get('dependency_type'), str):
                    raise Incomplete('dependency shape')
                if dep['dependency_type'] in NONBLOCKING_TYPES:
                    continue
                if dep['dependency_type'] not in BLOCKING_TYPES:
                    raise Incomplete('dependency type unknown')
                child = dep.get('depends_on_task_id')
                if not isinstance(child, str) or not UUID.fullmatch(child):
                    raise Incomplete('dependency identity')
                children.append(child)
            if gate:
                info = row.get('human_gate_info')
                owner = info.get('owner_agent_id') if isinstance(info, dict) else None
                # Owner is the asker. Neither it nor assignee proves the addressee.
                owner = owner if isinstance(owner, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,64}', owner) else 'unknown'
                gates.append((tid, owner))
            if not children:
                paths.append(path)
                addressees.append('unknown' if gate else _addressee(row))
            for child in sorted(set(children)):
                walk(child, path)

        failure = ''
        try:
            walk(target, [])
        except Incomplete as error:
            failure = str(error)
        except MeshError as error:
            failure = 'HTTP ' + str(error.status)
        except (ValueError, TypeError, KeyError):
            failure = 'response unknown'
        checked_at = iso(now())
        if failure:
            reason = ('Blocking path diagnostic UNKNOWN (' + failure
                      + '); addressee=unknown; observed_at=' + checked_at)
        else:
            reason = _format_reason(attempt.task_id, paths, gates, checked_at, addressees)
        record = {'target': target, 'checked_at': checked_at, 'reason': reason}
        if self.store:
            self.store.mark(self.lane, attempt.task_id, MARK, json.dumps(record))
        else:
            self._cache[attempt.task_id] = record
        return reason
