"""Supported release launch/rollback boundary; direct old-code invocation bypasses it.

The guard owns the existing singleton through exec. Same exact activated target
may restart to reconcile pending delivery. A changed target or config may enter with
pending intent or UNKNOWN rows when the entering target reads the delivery journal as the
previous one wrote it (`journal_compatible`: the target's own guard, same state, same
CAPABILITY); the rows stay pending and are reconciled by the target's ordinary tick.
Bump CAPABILITY when the journal contract changes: such a target then needs the rows
settled first. No registry/schema/lease mutation is performed here.

`--previous-config` is accepted and ignored: the additive-config proof it fed is covered
by the rule above.
"""
from __future__ import annotations

import argparse
import ast
import fcntl
import hashlib
import json
import os
import sqlite3
import stat
import sys
import tempfile
import tomllib
from pathlib import Path

CAPABILITY = 1
HANDOFF = '_MESH_RUNNER_GUARDED_LOCK'
MARKER = 'activated-release.json'


class GuardRefused(RuntimeError):
    pass


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def target_identity(release: Path) -> str:
    source = release / 'src' / 'mesh_runner'
    module = source / 'launch_guard.py'
    try:
        tree = ast.parse(module.read_text())
        capabilities = [ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
                        and any(isinstance(t, ast.Name) and t.id == 'CAPABILITY' for t in n.targets)]
    except (OSError, ValueError, SyntaxError):
        raise GuardRefused('target-capability-unavailable') from None
    if capabilities != [CAPABILITY]:
        raise GuardRefused('target-capability-unsupported')
    paths = sorted(source.rglob('*.py'))
    if not paths or len(paths) > 2048 or not (source / 'runner.py').is_file():
        raise GuardRefused('target-tree-invalid')
    entries, size = [], 0
    for path in paths:
        if path.is_symlink() or not path.is_file():
            raise GuardRefused('target-tree-invalid')
        raw = path.read_bytes()
        size += len(raw)
        if size > 16 * 1024 * 1024:
            raise GuardRefused('target-tree-too-large')
        entries.append([str(path.relative_to(release)), digest(raw)])
    return digest(json.dumps(entries, separators=(',', ':')).encode())


def configured_state(config: Path) -> tuple[Path, str]:
    raw = config.read_bytes()
    setting = tomllib.loads(raw.decode())['runner'].get('state_dir')
    # Supported activation must name its exact state; no HOME/default discovery.
    if not isinstance(setting, str) or not setting:
        raise GuardRefused('explicit-state-dir-required')
    state = Path(setting).expanduser()
    if not state.is_absolute() or state.is_symlink():
        raise GuardRefused('state-dir-invalid')
    return state.resolve(), digest(raw)


TERMINAL_STATES = ('completed', 'aborted', 'failed', 'cancelled')


def _settled(state: Path, key: str, agent: str, status: str, pid: int, intent, others: frozenset) -> bool:
    """An intent that can no longer block a drain, proven by the target's own journal reader: the row is
    (READY and runner-marked retryable) or terminal, has no process, and the journal proves the native send
    never began. A live, ambiguous or unproven intent is never settled; a terminal state alone proves nothing."""
    if not isinstance(intent, dict) or intent.get('kind') != 'interactive' or pid:
        return False
    if not (status == 'ready' and intent.get('retryable') is True) and status not in TERMINAL_STATES:
        return False
    try:
        src = str(Path(__file__).resolve().parents[1])
        if src not in sys.path:
            sys.path.insert(0, src)
        from mesh_runner import delivery_recovery, interactive
        return delivery_recovery.proven_unsent(interactive.agent_dir(state, agent), key, intent, others)
    except (ImportError, OSError, ValueError, TypeError, KeyError):
        return False


def pending_agents(state: Path) -> frozenset:
    """Lanes owning at least one pending (unknown / send-intent, not proven unsent) row."""
    db = state / 'runner.sqlite'
    if not db.exists():
        return frozenset()
    if db.is_symlink():
        raise GuardRefused('registry-invalid')
    try:
        connection = sqlite3.connect(db.as_uri() + '?mode=ro', uri=True)
        try:
            rows = connection.execute('SELECT id, agent, state, pid, wait, session_id FROM attempts').fetchall()
        finally:
            connection.close()
        bound: dict[str, set] = {}
        for key, *_, sid in rows:
            if sid:
                bound.setdefault(sid, set()).add(key)
        found = set()
        for key, agent, status, pid, raw, _sid in rows:
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError()
            if status == 'unknown' or 'interactive_prompt_intent' in value:
                if _settled(state, key, agent, status, pid, value.get('interactive_prompt_intent'),
                            frozenset(s for s, owners in bound.items() if key not in owners)):
                    continue
                found.add(agent)
    except (sqlite3.Error, ValueError, TypeError):
        raise GuardRefused('registry-unreadable') from None
    return frozenset(found)


def pending(state: Path) -> bool:
    return bool(pending_agents(state))


def publish_marker(state: Path, value: dict) -> None:
    fd, name = tempfile.mkstemp(prefix='.activated-', dir=state)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, state / MARKER)
        dfd = os.open(state, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def release_capability(release: Path) -> int | None:
    """The delivery-journal contract version a release's guard declares; None when it cannot be read."""
    try:
        tree = ast.parse((release / 'src' / 'mesh_runner' / 'launch_guard.py').read_text())
        found = [ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
                 and any(isinstance(t, ast.Name) and t.id == 'CAPABILITY' for t in n.targets)]
    except (OSError, ValueError, SyntaxError):
        return None
    return found[0] if len(found) == 1 and type(found[0]) is int else None


def journal_compatible(state: Path, binding: dict, previous, release: Path | None) -> bool:
    """Pending rows may cross a target or config change when the entering target reads the journal as written.

    A pending row (UNKNOWN, or an unproven prompt-delivery intent) is reconciled by the ordinary tick from the
    delivery journal. That reconciliation depends on the journal contract, which CAPABILITY versions, and not on
    the release tree or the config bytes. So the guard that IS the target takes the rows over when the previously
    activated release, on this same state, declared the same CAPABILITY. The rows are not inspected, altered or
    declared safe here: they stay pending and stay fenced by the target's own tick, exactly as on a same-target
    restart. A state never activated before, a previous release that is gone or unreadable, another state, or a
    different CAPABILITY keeps the refusal.
    """
    if release is None or not isinstance(previous, dict) or previous.get('version') != 1:
        return False
    if Path(__file__).resolve().parents[2] != release.resolve():
        return False                                       # only the target's own guard vouches for its reader
    if previous.get('state') != binding['state']:
        return False
    try:
        was = release_capability(Path(str(previous.get('release') or '')))
    except (TypeError, ValueError):
        return False
    return was is not None and was == CAPABILITY


def validate_pending_binding(state: Path, binding: dict, release: Path | None = None) -> None:
    marker = state / MARKER
    previous = None
    if marker.exists():
        if marker.is_symlink():
            raise GuardRefused('activation-binding-invalid')
        try:
            previous = json.loads(marker.read_text())
        except (OSError, ValueError):
            raise GuardRefused('activation-binding-unreadable') from None
    if pending(state) and previous != binding and not journal_compatible(state, binding, previous, release):
        raise GuardRefused('pending-delivery-target-change')


def assert_held(fd: int, path: Path):
    info, actual = os.fstat(fd), path.stat()
    if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != (actual.st_dev, actual.st_ino):
        raise GuardRefused('singleton-file-changed')
    probe = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            fcntl.flock(probe, fcntl.LOCK_UN)
            raise GuardRefused('singleton-not-held')
    finally:
        os.close(probe)
    # If a different open description owns the lock, this fd cannot adopt it.
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return info


def check_locked(release: Path, config: Path, fd: int, previous_config: Path | None = None) -> dict:  # noqa: ARG001
    """Activation preflight under the operator's existing singleton; no binding write."""
    release, config = release.resolve(strict=True), config.resolve(strict=True)
    state, config_sha = configured_state(config)
    assert_held(fd, state / 'runner.lock')
    identity = target_identity(release)
    binding = {'version': 1, 'release': str(release), 'tree': identity,
               'config': str(config), 'config_sha256': config_sha, 'state': str(state)}
    validate_pending_binding(state, binding, release)
    if configured_state(config) != (state, config_sha) or target_identity(release) != identity:
        raise GuardRefused('activation-input-changed')
    return binding


def prepare(release: Path, config: Path, *, bind: bool = True, previous_config: Path | None = None):
    release, config = release.resolve(strict=True), config.resolve(strict=True)
    identity = target_identity(release)
    state, config_sha = configured_state(config)
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = state / 'runner.lock'
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    lock = os.fdopen(fd, 'r+')
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != (path.stat().st_dev, path.stat().st_ino):
            raise GuardRefused('singleton-file-changed')
        binding = {'version': 1, 'release': str(release), 'tree': identity,
                   'config': str(config), 'config_sha256': config_sha, 'state': str(state)}
        validate_pending_binding(state, binding, release)
        # Both config and complete target tree are checked again under singleton.
        if configured_state(config) != (state, config_sha) or target_identity(release) != identity:
            raise GuardRefused('activation-input-changed')
        if bind:
            publish_marker(state, binding)
        os.set_inheritable(fd, True)
        handoff = {**binding, 'fd': fd, 'device': info.st_dev, 'inode': info.st_ino}
        return lock, handoff
    except BaseException as exc:
        lock.close()
        if isinstance(exc, BlockingIOError):
            raise GuardRefused('runner-already-owns-state') from None
        raise


def inherited_lock(cfg):
    """Adopt only a held fd for this inode/config/target; never unlock/relock it."""
    raw = os.environ.pop(HANDOFF, None)
    if raw is None:
        return None
    try:
        value = json.loads(raw)
        fd = value['fd']
        if type(fd) is not int or fd < 3:
            raise ValueError()
        state = cfg.state_dir.resolve()
        path = state / 'runner.lock'
        info, actual = os.fstat(fd), path.stat()
        if ((info.st_dev, info.st_ino) != (actual.st_dev, actual.st_ino)
                or (info.st_dev, info.st_ino) != (value['device'], value['inode'])
                or value['state'] != str(state) or not cfg.config_path
                or configured_state(Path(cfg.config_path)) != (state, value['config_sha256'])
                or str(Path(cfg.config_path).resolve()) != value['config']
                or Path(__file__).resolve().parents[2] != Path(value['release'])
                or target_identity(Path(value['release'])) != value['tree']
                or json.loads((state / MARKER).read_text()) != {k: v for k, v in value.items() if k not in ('fd', 'device', 'inode')}):
            raise ValueError()
        # The same open description must actually own the existing singleton.
        assert_held(fd, path)
        os.set_inheritable(fd, False)
        return os.fdopen(fd, 'r+')
    except (OSError, ValueError, KeyError, TypeError, GuardRefused):
        raise GuardRefused('inherited-singleton-invalid') from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target-release', required=True, type=Path)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--previous-config', type=Path, default=None,
                        help='accepted for compatibility; unused')
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--log-level', default='INFO')
    args = parser.parse_args(argv)
    try:
        lock, value = prepare(args.target_release, args.config, bind=not args.check,
                            previous_config=args.previous_config)
        if args.check:
            lock.close()
            print(json.dumps({'eligible': True, 'activated': False}))
            return 0
        env = dict(os.environ)
        env['PYTHONPATH'] = str(Path(value['release']) / 'src')
        env[HANDOFF] = json.dumps(value, sort_keys=True)
        # Existing interpreter and existing CLI; no service, worker or second owner.
        os.execve(sys.executable, [sys.executable, '-m', 'mesh_runner.cli',
                  'once' if args.once else 'run', '--config', value['config'],
                  '--log-level', args.log_level], env)
    except (GuardRefused, OSError, ValueError, KeyError) as exc:
        print(json.dumps({'eligible': False, 'reason': str(exc) if isinstance(exc, GuardRefused) else type(exc).__name__}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
