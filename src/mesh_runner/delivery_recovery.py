"""Recover an ended native turn without starting a driver or sending a prompt."""
from __future__ import annotations

import glob
import hashlib
import io
import json
import os
import re
import subprocess
import stat
import sqlite3
import sys
import time
from pathlib import Path

from .interactive import (Tmux, delivery_path, read_delivery, read_meta, read_progress,
                          pid_alive, session_name, meta_path, progress_path)
from .adapters.base import group_alive, precise_stamp
from .iterm_transport import process_argv
from .qa_assignment import native_session_in_argv
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


def _quota_unsent(journal: dict, intent: dict) -> bool:
    prompt = intent.get('prompt')
    if not isinstance(prompt, str):
        return False
    try:
        digest = hashlib.sha256(prompt.encode('utf-8')).hexdigest()
    except UnicodeError:
        return False
    return (journal.get('phase') == 'pre-send-failed' and journal.get('failure_kind') == 'quota'
            and journal.get('harness') == 'claude' and intent.get('harness') == 'claude'
            and digest == intent.get('sha256'))


def proven_quota_unsent(directory: Path, key: str, intent: dict) -> bool:
    """Exact typed pre-send quota proof, without reading or driving a native session."""
    try:
        journal = read_delivery(directory, key)
        return (journal.get('token') == intent.get('token') and journal.get('sha256') == intent.get('sha256')
                and _quota_unsent(journal, intent))
    except (OSError, ValueError, KeyError, TypeError):
        return False


def legacy_journal_unsent(journal: dict, intent: dict) -> bool:
    """Exact legacy non-delivery proof, including the pending UTF-8 bytes."""
    try:
        prompt, token, digest = intent.get('prompt'), intent.get('token'), intent.get('sha256')
        return (set(journal) == {'version', 'token', 'sha256', 'phase'}
                and type(journal['version']) is int and journal['version'] == 1
                and journal['phase'] in ('reserved', 'received')
                and type(intent.get('version')) is int and intent['version'] == 1
                and intent.get('kind') == 'interactive' and intent.get('harness') == 'claude'
                and intent.get('retryable') is True and intent.get('resume') is True
                and isinstance(intent.get('session_id'), str) and bool(intent['session_id'])
                and isinstance(token, str) and bool(token) and journal['token'] == token
                and isinstance(digest, str) and re.fullmatch(r'[0-9a-f]{64}', digest) is not None
                and journal['sha256'] == digest and isinstance(prompt, str)
                and hashlib.sha256(prompt.encode('utf-8')).hexdigest() == digest)
    except (ValueError, KeyError, TypeError, UnicodeError):
        return False


def proven_legacy_unsent(directory: Path, key: str, intent: dict) -> bool:
    """Non-delivery only; callers must separately prove writer absence and fresh admission."""
    try:
        return legacy_journal_unsent(read_delivery(directory, key), intent)
    except (OSError, ValueError, TypeError):
        return False


def _process_argv(pid: int) -> list[str]:
    """Structured OS argv boundary on the supported local platforms; no environment."""
    if sys.platform == 'darwin':
        return process_argv(pid)
    if sys.platform.startswith('linux'):
        try:
            with open(f'/proc/{pid}/cmdline', 'rb') as stream:
                raw = stream.read((1 << 22) + 1)
            return [x.decode('utf-8', 'replace') for x in raw.rstrip(b'\0').split(b'\0')] \
                if raw.endswith(b'\0') and len(raw) <= 1 << 22 else []
        except OSError:
            pass
    return []


def _zombie_now(pid: int) -> bool:
    """Only two fresh kernel zombie observations of the same own process prove no writer."""
    def state():
        result = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "pid=,uid=,ppid=,stat="],
                                capture_output=True, text=True, timeout=10)
        rows = [line.split() for line in result.stdout.splitlines() if line.strip()]
        if (result.returncode or len(rows) != 1 or len(rows[0]) != 4
                or rows[0][0] != str(pid) or rows[0][1] != str(os.geteuid())
                or not rows[0][2].isdecimal() or int(rows[0][2]) <= 0
                or re.fullmatch(r'Z[<NLsl+]*', rows[0][3]) is None):
            return None
        return rows[0]
    try:
        birth = precise_stamp(pid)
        first = state()
        return bool(birth and first and state() == first and precise_stamp(pid) == birth)
    except (OSError, subprocess.SubprocessError):
        return False


def _session_process_absent(session_id: str, key: str) -> bool:
    """Inspect only local process identity/argv, never environment or transcript.

    Old journals did not persist a native PID. Enumerating our UID and checking
    the structured session flag also catches a native child detached from tmux.
    An unreadable live process is unknown, never evidence of absence.
    """
    try:
        result = subprocess.run(["/bin/ps", "-ax", "-o", "pid=,uid="],
                                capture_output=True, text=True, timeout=10)
        rows = [line.split() for line in result.stdout.splitlines() if line.strip()]
        if (result.returncode or not rows or len(rows) > 65536
                or any(len(row) != 2 or not row[0].isdecimal()
                       or re.fullmatch(r'-?[0-9]+', row[1]) is None
                       or int(row[0]) <= 0 for row in rows)):
            return False
        # Darwin selectors also include a login process by its real UID. The
        # uid column is the effective owner; never read another user's argv.
        pids = [int(pid) for pid, uid in rows if int(uid) == os.geteuid()]
        if not pids:
            return False
        for pid in pids:
            argv = _process_argv(pid)
            if not argv:
                if pid_alive(pid) and not _zombie_now(pid):
                    return False
            elif (native_session_in_argv(argv, session_id)
                  or any(value == key and i > 0 and argv[i - 1] == '--key'
                         or value == '--key=' + key for i, value in enumerate(argv))):
                return False
        return True
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def legacy_writer_stopped(directory: Path, key: str, intent: dict, agent: str, tmux: Tmux) -> bool:
    """Positive stopped-writer check for the old exact unsent journal. No signals."""
    return (proven_legacy_unsent(directory, key, intent)
            and legacy_processes_stopped(directory, key, intent, agent, tmux))


def legacy_processes_stopped(directory: Path, key: str, intent: dict, agent: str, tmux: Tmux) -> bool:
    """Process proof only; caller must also bind an exact non-delivery journal."""
    present = []
    for path in (progress_path(directory, key), meta_path(directory, key)):
        try:
            if not stat.S_ISREG(path.lstat().st_mode):
                return False
            present.append(True)
        except FileNotFoundError:
            present.append(False)
        except OSError:
            return False
    progress, meta = read_progress(directory, key), read_meta(directory, key)
    driver = progress.get('pid')
    if present == [False, True]:
        # Ordinary cleanup removes DONE progress before retiring the idle
        # session metadata. Extra matching idle metadata is not a live writer;
        # window and complete session/attempt process absence remain mandatory.
        if (meta.get('session_id') != intent['session_id'] or meta.get('harness') != 'claude'
                or type(meta.get('busy_pid')) is not int or meta['busy_pid'] != 0):
            return False
    elif any(present):
        if (type(driver) is not int or driver <= 0 or progress.get('phase') != 'done'
                or meta.get('session_id') != intent['session_id'] or meta.get('harness') != 'claude'
                or type(meta.get('busy_pid')) is not int or meta['busy_pid'] not in (0, driver)
                or pid_alive(driver) or group_alive(driver)):
            return False
    # Normal cleanup may have removed both legacy metadata files. Their absence
    # alone grants nothing: the current process scan covers the exact native
    # session AND attempt driver, including children detached from tmux.
    windows = tmux.windows_strict()
    return (windows is not None and (session_name(agent), key) not in windows
            and _session_process_absent(intent['session_id'], key))


def recovery_journal_safe(directory: Path, key: str, archived: dict) -> bool:
    """The original journal or a reserved orphan before the next SQLite outbox write.

    An orphan supplies no writer-absence proof; the caller still scans processes
    and windows. Any later native phase or malformed record remains held.
    """
    try:
        current = read_delivery(directory, key)
        return (current == archived or (
            set(current) == {'version', 'token', 'sha256', 'phase'}
            and type(current['version']) is int and current['version'] == 1
            and current['phase'] == 'reserved'
            and isinstance(current['token'], str) and bool(current['token'])
            and current['token'] != archived['token']
            and isinstance(current['sha256'], str)
            and re.fullmatch(r'[0-9a-f]{64}', current['sha256']) is not None))
    except (OSError, ValueError, KeyError, TypeError):
        return False


def restored_scope_inventory(db, key: str, agent: str, intent: dict, paths: list) -> bool:
    """The existing completed checkpoint journal binds old paths to this attempt's leases."""
    try:
        old = intent.get('workspaces')
        if (not isinstance(old, list) or not old or not isinstance(paths, list) or not paths
                or old == paths or any(not isinstance(x, str) or not x for x in old + paths)
                or len(set(old)) != len(old) or len(set(paths)) != len(paths)):
            return False
        rows = db.execute('SELECT repo, source, target, phase FROM workspace_checkpoints '
                          'WHERE owner=? AND attempt=? ORDER BY repo', (agent, key)).fetchall()
        if (len(rows) != len(old) or len(rows) != len(paths)
                or {r[1] for r in rows} != set(old) or [r[2] for r in rows] != paths
                or any(r[3] != 'active' for r in rows)):
            return False
        leases = db.execute("SELECT pool, path FROM slots WHERE attempt=? AND state='leased'", (key,)).fetchall()
        return {(r[0], r[1]) for r in leases} == {(agent + '/' + r[0], r[2]) for r in rows}
    except (sqlite3.Error, ValueError, TypeError, KeyError):
        return False


def unknown_restored_unsent(db, directory: Path, key: str, agent: str, tmux: Tmux) -> bool:
    """Carry, never settle, a legacy unsent request fenced after a completed path restore."""
    try:
        row = db.execute('SELECT state,pid,harness,session_id,workspaces,wait FROM attempts '
                         'WHERE id=? AND agent=?', (key, agent)).fetchone()
        if not row or tuple(row[:4]) != ('unknown', 0, 'claude', ''):
            return False
        paths, wait = json.loads(row[4]), json.loads(row[5])
        intent = wait.get('interactive_prompt_intent')
        return (isinstance(intent, dict) and not intent.get('resolution')
                and 'unsent_scope_recovery' not in wait
                and restored_scope_inventory(db, key, agent, intent, paths)
                and legacy_writer_stopped(directory, key, intent, agent, tmux))
    except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, AttributeError):
        return False


_NEEDS_NATIVE = ("the exact native acceptance record for this token and prompt hash "
                 "(a single UserPromptSubmit/turn-complete bound to this session, then its terminal event)")
EXPLAIN = {
    'acceptance-unproven': _NEEDS_NATIVE + " is missing or ambiguous in the native event stream",
    'codex-acceptance-unproven': _NEEDS_NATIVE + " is missing or ambiguous in the codex notify stream",
    'session-unbound': "no native session is bound to this send and no typed bootstrap-failure proof exists",
    'cursor-mismatch': "the native event stream no longer matches the cursor recorded before the send",
    'native-evidence-unreadable': "the journal or native evidence could not be read",
    'native-running-or-incomplete': "the native turn has no terminal event yet or is still settling",
    'native-evidence-changing': "native evidence changed while it was being read",
    'later-native-prompt': "a later native prompt follows this one, so its result cannot be attributed",
    'headless-exec-unconfirmed': "the headless exec left neither a bound terminal nor a native acceptance receipt",
    'ack-identity-mismatch': "no driver acknowledgement for this exact token and prompt hash",
}


def explain(resolution: str) -> dict:
    """Operator-facing meaning of an unresolved delivery. Descriptive only: grants no authority and is never
    read back as evidence. Missing ACK, a vanished driver and an empty transcript are never proof of non-delivery."""
    return {
        'missing_evidence': EXPLAIN.get(resolution) or ("native proof incomplete (" + (resolution or 'no resolution recorded') + ")"),
        'replay': 'forbidden',
        'scope': 'this attempt and its own lane workspace/lease only; other tasks of the lane and other lanes keep running',
        'next_action': ("mesh-runner recheck-delivery --agent <lane> --attempt <attempt> (read-only), then --submit with the "
                        "printed task/token/turn/evidence_sha256 once the evidence hash differs from the last unresolved "
                        "recheck; the owner tick validates and consumes it. Without new native evidence there is no "
                        "supported resolution, the attempt stays unresolved:" + (resolution or 'native-proof-incomplete')),
    }


def proven_unsent(directory: Path, key: str, intent: dict, other_sessions: frozenset = frozenset()) -> bool:
    """True only when the durable journal itself proves the native send never began for THIS intent: the
    token/sha match and the phase precedes every paste (`reserved`/`received` or the typed pre-send quota failure), or the typed bootstrap proofs
    of `unbound_bootstrap_proof` hold. A terminal row alone, an unreadable journal or a mismatch is not proof."""
    try:
        journal = read_delivery(directory, key)
        if journal.get('token') != intent.get('token') or journal.get('sha256') != intent.get('sha256'):
            return False
        if journal.get('phase') in ('reserved', 'received') or _quota_unsent(journal, intent):
            return True
    except (OSError, ValueError, KeyError, TypeError):
        return False
    return unbound_bootstrap_proof(directory, key, intent, other_sessions)


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
    if journal.get('phase') == 'pre-send-failed':
        if _quota_unsent(journal, intent):
            return 'quota-unsent'         # typed driver failure before any native paste; never an ACK
        return 'pre-send-failure-unrecognized'
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
    if not any(e.get('type') == 'agent-turn-complete' for e in native[base:]):
        failed = recover_codex_failed_turn(d, journal, native, sid, intent)
        if failed is not None:
            return failed
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


def _codex_user_text(line: bytes) -> str | None:
    """Extract a text-only user message. Invalid user content rejects the whole proof."""
    if b'"response_item"' not in line or b'"user"' not in line:
        return None
    rec = json.loads(line)
    item = rec.get('payload') if isinstance(rec, dict) and rec.get('type') == 'response_item' else None
    if not isinstance(item, dict) or item.get('type') != 'message' or item.get('role') != 'user':
        return None
    content = item.get('content')
    if not isinstance(content, list) or not content or not all(
            isinstance(part, dict) and set(part) == {'type', 'text'}
            and part['type'] in ('input_text', 'text') and isinstance(part['text'], str)
            for part in content):
        raise ValueError('user message is not exclusively typed text')
    return ''.join(part['text'] for part in content)


def _typed_codex_error(error) -> bool:
    """Codex's typed task error: exactly ``message`` and ``codex_error_info``, both non-empty strings.
    Any other shape (a bare string, a dict missing a field or carrying others) is not a typed refusal."""
    return (isinstance(error, dict) and set(error) == {'message', 'codex_error_info'}
            and all(isinstance(error[k], str) and error[k].strip() for k in error))


def recover_codex_failed_turn(d: Driver, journal: dict, native: list[dict], sid: str, intent: dict) -> str | None:
    """Codex calls its notify only for a task that finished with a message. A turn the provider refused
    (2026-11-10: HTTP 400 for every turn of an outdated CLI) ends within a second with a typed
    ``task_complete.error`` and no notify, so the live driver may never see it busy and the send stays
    unproven. The bound rollout is then the native acceptance record: after the journal's verified cursor
    exactly one task, holding exactly one user message equal to this prompt, ended with a typed error and
    no model output. Anything else (no such record, a second task or prompt, model output, an abort, an
    unreadable line) returns None and the caller keeps the notify rule."""
    cursor, rollout = journal.get('rollout_cursor'), journal.get('rollout')
    if type(cursor) is not int or cursor < 0 or not rollout:
        return None
    try:
        raw = Path(rollout).read_bytes()
    except OSError:
        return None                      # a gone or unreadable rollout proves nothing
    if len(raw) < cursor or hashlib.sha256(raw[:cursor]).hexdigest() != journal.get('rollout_prefix'):
        return None
    starts, users, ends = [], [], []
    try:
        for index, line in enumerate(raw[cursor:].splitlines()):
            if not line.strip():
                continue
            if d.codex_turn_aborted(line) is not None or d.codex_reply(line):
                return None
            started = d.codex_task_start(line)
            if started is not None:
                starts.append((index, started))
                continue
            text = _codex_user_text(line)
            if text is not None:
                users.append((index, text))
                continue
            if b'"task_complete"' in line:
                payload = json.loads(line).get('payload') or {}
                ends.append((index, payload, d.codex_task_end(line)))
    except (ValueError, AttributeError):
        return None
    if len(starts) != 1 or len(users) != 1 or len(ends) != 1:
        return None
    (s_at, turn), (u_at, text), (e_at, payload, end) = starts[0], users[0], ends[0]
    if (not turn or not s_at < u_at < e_at or _exact(text) != _exact(intent['prompt'])
            or end is None or not end['error'] or end['message'] or not _typed_codex_error(payload.get('error'))
            or payload.get('turn_id') != turn
            or (journal.get('turn_id') and journal['turn_id'] != turn)):
        return None
    d.meta = dict(journal.get('meta') or {})
    d.prompt, d.turn_id = intent['prompt'], turn
    d.rollout, d.scan_from = rollout, cursor
    if d.rollout_thread() != sid:
        return None
    if d.window_state() == 'alive':
        if d.busy():
            return 'native-running-or-incomplete'
        time.sleep(d.t['settle'])
        try:
            changed = d.busy() or Path(rollout).read_bytes() != raw
        except OSError:
            return 'native-evidence-changing'
        if changed:
            return 'native-evidence-changing'
    if d.read_events() != native:
        return 'native-evidence-changing'
    why = 'no model output; ' + end['error']
    d.emit_codex({'event': {'type': 'agent-turn-complete', 'thread-id': sid, 'turn-id': turn,
                            'last-assistant-message': '', 'no-reply': why},
                  'totals': journal.get('totals') or {}})
    d.emit({'type': 'mesh_runner.prompt_ack', 'token': intent['token'], 'sha256': intent['sha256'],
            'recovered_from': 'codex.session-rollout-failed-turn'})
    persist_result(d, intent)
    return 'completed'
