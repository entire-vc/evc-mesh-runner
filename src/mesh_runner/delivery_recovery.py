"""Recover an ended native turn without starting a driver or sending a prompt."""
from __future__ import annotations

import glob
import hashlib
import io
import json
import os
import re
import time
from pathlib import Path

from .interactive import Tmux, delivery_path, read_delivery
from .tui_driver import Driver, DriverError, _exact


START_LINE = re.compile(r"exited during start-up, starting again \(\d+/\d+\) in \d+s: Error: .*failed during TUI bootstrap")


def legacy_bootstrap_unsent(journal: dict, intent: dict, other_sessions: frozenset = frozenset()) -> bool:
    """Journals written before the driver recorded `bootstrap-failed`: the codex window died while the
    TUI was bootstrapping, so no input was ever possible. Proven only when ALL hold: no session and no
    rollout bound, the driver's own stderr names typed bootstrap failures and never a finished turn, and no
    codex rollout for this cwd appeared after the send-intent (any candidate leaves it pending). A rollout whose
    id is the bound session of a DIFFERENT attempt (`other_sessions`) belongs to that attempt, not to this send."""
    if journal.get('harness') != 'codex' or journal.get('session_id') or journal.get('rollout'):
        return False
    try:
        stderr = Path(intent['log_path']).with_suffix('.stderr').read_text(errors='replace')
        started = delivery_path(Path(intent['directory']), intent['key']).stat().st_mtime \
            if intent.get('directory') and intent.get('key') else None
    except (OSError, KeyError):
        return False
    lines = [x for x in stderr.splitlines() if x.strip()]
    if not any(START_LINE.search(x) for x in lines) or any(x.startswith('timing:') for x in lines):
        return False
    return started is not None and not fresh_rollouts(journal, started, other_sessions)


def unbound_bootstrap_proof(directory: Path, key: str, intent: dict, other_sessions: frozenset = frozenset()) -> bool:
    """Pre-activation screen for a `session-unbound` row: true only when the journal and the driver's own
    stderr prove the codex window died in TUI bootstrap before it could take input. Never starts a driver."""
    try:
        journal = read_delivery(directory, key)
        if journal.get('token') != intent.get('token') or journal.get('sha256') != intent.get('sha256'):
            return False
        if journal.get('phase') == 'bootstrap-failed':
            return bool(journal.get('bootstrap_error')) and journal.get('harness') == 'codex' \
                and not journal.get('session_id') and not journal.get('rollout')
        return journal.get('phase') not in ('reserved', 'received') and legacy_bootstrap_unsent(
            journal, {**intent, 'directory': str(directory), 'key': key}, other_sessions)
    except (OSError, ValueError, KeyError, TypeError):
        return False


def fresh_rollouts(journal: dict, since: float, other_sessions: frozenset = frozenset()) -> list[str]:
    homes = {os.environ.get('CODEX_HOME') or os.path.join(os.path.expanduser('~'), '.codex')}
    homes.add(str(journal.get('codex_home') or '')) if journal.get('codex_home') else None
    cwd = os.path.realpath(str(journal.get('cwd') or ''))
    found = []
    for home in homes:
        for path in glob.glob(os.path.join(home, 'sessions', '*', '*', '*', 'rollout-*.jsonl')):
            try:
                if os.path.getmtime(path) < since - 5:
                    continue
                with open(path, 'rb') as fh:
                    meta = json.loads(fh.readline(1 << 20))
            except (OSError, ValueError):
                found.append(path)               # unreadable candidate: not proof of absence
                continue
            payload = meta.get('payload') or {}
            if os.path.realpath(str(payload.get('cwd') or '')) == cwd and payload.get('id') not in other_sessions:
                found.append(path)
    return found


def recover(directory: Path, key: str, intent: dict, agent: str, tmux: Tmux,
            other_sessions: frozenset = frozenset()) -> str:
    journal = read_delivery(directory, key)
    if journal.get('token') != intent.get('token') or journal.get('sha256') != intent.get('sha256'):
        return 'identity-mismatch'
    if journal.get('phase') in ('reserved', 'received'):
        return 'unsent'                    # every paste is preceded by a durable send-intent
    if journal.get('phase') == 'bootstrap-failed' and journal.get('bootstrap_error'):
        return 'unsent'                    # the driver proved the window died before it could take input
    harness = journal.get('harness')
    if harness not in ('claude', 'codex'):
        return 'native-contract-unavailable'
    # Recovery uses only explicit journal paths, never an ambient account/profile.
    d = Driver(harness, agent, directory, tmux, 0, Path(journal['cwd']),
               journal.get('session_id') or '', 'resume', key, [], out=io.StringIO(),
               env={'HOME': str(directory), 'CODEX_HOME': str(directory / '.recovery-no-profile')})
    native = d.read_events()
    base = journal['native_base']
    if not isinstance(base, int) or base < 0 or len(native) < base:
        return 'cursor-mismatch'
    prefix = hashlib.sha256(json.dumps(native[:base], sort_keys=True).encode()).hexdigest()
    if prefix != journal.get('native_prefix'):
        return 'cursor-mismatch'
    sid = journal.get('session_id')
    if not sid:
        if len(native) == base and legacy_bootstrap_unsent(
                journal, {**intent, 'directory': str(directory), 'key': key}, other_sessions):
            return 'unsent'
        return 'session-unbound'
    if harness == 'codex':
        return recover_codex(d, journal, native, base, sid, intent)
    submits = [(i, e) for i, e in enumerate(native[base:], base)
               if e.get('hook_event_name') == 'UserPromptSubmit']
    ours = [(i, e) for i, e in submits if e.get('session_id') == sid and e.get('prompt_id')
            and _exact(str(e.get('prompt') or '')) == _exact(intent['prompt'])]
    if len(ours) != 1:
        return 'acceptance-unproven'
    index, submitted = ours[0]
    if any(i > index for i, _ in submits):
        return 'later-native-prompt'
    ends = [e for e in native[index + 1:] if e.get('hook_event_name') in ('Stop', 'StopFailure')
            and e.get('session_id') == sid and e.get('prompt_id') == submitted['prompt_id']]
    if not ends:
        return 'native-running-or-incomplete'
    end = ends[-1]
    d.meta = dict(journal.get('meta') or {})
    if d.meta.get('transcript') and end.get('transcript_path') != d.meta['transcript']:
        return 'transcript-mismatch'
    d.prompt, d.prompt_id, d.turn_offset = intent['prompt'], submitted['prompt_id'], journal['offset']
    if d.window_state() == 'alive':
        if d.busy():
            return 'native-running-or-incomplete'
        size = d.transcript_size()
        time.sleep(d.t['settle'])
        if d.busy() or d.transcript_size() != size:
            return 'native-running-or-incomplete'
    if d.read_events() != native:
        return 'native-evidence-changing'
    try:
        d.emit_claude({'event': end, 'offset': journal['offset'], 'totals': {}})
    except DriverError:
        return 'native-result-incomplete'
    d.emit({'type': 'mesh_runner.prompt_ack', 'token': intent['token'], 'sha256': intent['sha256'],
            'recovered_from': 'claude.session-prompt-id-stop'})
    persist_result(d, intent)
    return 'completed'


def persist_result(d: Driver, intent: dict) -> None:
    # Rebuild the complete result instead of appending duplicate streamed records.
    # A crash after replace but before registry CAS is reconciled by the same exact ACK.
    path = Path(intent['log_path'])
    tmp = path.with_suffix('.recovery-tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as fh:
        fh.write(d.out.getvalue())
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def recover_codex(d: Driver, journal: dict, native: list[dict], base: int, sid: str, intent: dict) -> str:
    # notify is terminal evidence only. A busy-screen transition never proves
    # delivery, and a rollout task end alone does not identify its user prompt.
    notes = [e for e in native[base:] if e.get('type') == 'agent-turn-complete'
             and e.get('thread-id') == sid]
    ours = [e for e in notes if e.get('turn-id') and isinstance(e.get('input-messages'), list)
            and e['input-messages'] and isinstance(e['input-messages'][-1], str)
            and _exact(e['input-messages'][-1]) == _exact(intent['prompt'])]
    if len(ours) != 1:
        return 'codex-acceptance-unproven'
    end = ours[0]
    if notes != [end]:
        return 'later-native-prompt'
    if journal.get('turn_id') and journal['turn_id'] != end['turn-id']:
        return 'codex-turn-mismatch'
    cursor, rollout = journal.get('rollout_cursor'), journal.get('rollout')
    if type(cursor) is not int or cursor < 0 or not rollout:
        return 'codex-rollout-unbound'
    raw = Path(rollout).read_bytes()
    if len(raw) < cursor or hashlib.sha256(raw[:cursor]).hexdigest() != journal.get('rollout_prefix'):
        return 'codex-rollout-cursor-mismatch'
    lines = raw[cursor:].splitlines()
    starts = [d.codex_task_start(line) for line in lines if d.codex_task_start(line) is not None]
    ends = [d.codex_task_end(line) for line in lines if d.codex_task_end(line) is not None]
    if starts != [end['turn-id']] or len(ends) != 1 or ends[0]['error']:
        return 'codex-native-turn-incomplete'
    if ends[0]['message'] != end.get('last-assistant-message'):
        return 'codex-native-result-mismatch'
    d.meta = dict(journal.get('meta') or {})
    d.prompt, d.turn_id = intent['prompt'], end['turn-id']
    d.rollout, d.scan_from = rollout, cursor
    if d.rollout_thread() != sid:
        return 'codex-native-session-mismatch'
    if d.window_state() == 'alive':
        if d.busy():
            return 'native-running-or-incomplete'
        time.sleep(d.t['settle'])
        if d.busy() or Path(rollout).read_bytes() != raw:
            return 'native-evidence-changing'
    if d.read_events() != native:
        return 'native-evidence-changing'
    d.emit_codex({'event': end, 'totals': journal.get('totals') or {}})
    d.emit({'type': 'mesh_runner.prompt_ack', 'token': intent['token'], 'sha256': intent['sha256'],
            'recovered_from': 'codex.session-turn-notify-rollout'})
    persist_result(d, intent)
    return 'completed'
