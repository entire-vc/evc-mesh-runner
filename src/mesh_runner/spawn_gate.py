"""One-shot headless handoff, owned by Adapter.spawn in its original process group.

No native program starts until the registered parent supplies the complete prompt.
The supervisor reports the actual native Popen result separately from its own
startup, persists that boundary, then waits for the ordinary native child.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mesh_runner.adapters.base import proc_stamp
from mesh_runner.interactive import read_delivery, write_delivery


def main(argv: list[str]) -> int:
    path, token, digest, status_fd = Path(argv[1]), argv[2], argv[3], int(argv[4])
    native = argv[6:]
    directory, key = path.parent, path.name.removesuffix('.delivery.json')
    journal = read_delivery(directory, key)
    if journal.get('token') != token or journal.get('sha256') != digest:
        return 1

    def phase(name, **facts):
        journal.update(phase=name, **facts)
        write_delivery(directory, key, journal)

    def status(**facts):
        os.write(status_fd, (json.dumps(facts) + '\n').encode())
        os.close(status_fd)

    prompt = sys.stdin.buffer.read()
    if not prompt or hashlib.sha256(prompt).hexdigest() != digest:
        return 0                    # EOF/partial input never authorizes native exec
    phase('received')
    env = dict(os.environ)
    env.pop('_MESH_RUNNER_PROMPT_INTENT', None)
    env.pop('_MESH_RUNNER_PROMPT_SHA256', None)
    with tempfile.TemporaryFile() as stdin:
        stdin.write(prompt)
        stdin.seek(0)
        phase('native-exec-intent')
        try:
            proc = subprocess.Popen(native, stdin=stdin, env=env)
        except OSError as exc:
            phase('native-exec-error', errno=exc.errno)
            status(errno=exc.errno)
            return 1
        phase('native-exec-confirmed', native_pid=proc.pid, native_stamp=proc_stamp(proc.pid))
        status(exec_confirmed=True)
        code = proc.wait()
    # Native stdout is inherited from the original Adapter log descriptor.
    # Persist the result before publishing completion evidence.
    os.fsync(1)
    os.fsync(2)
    phase('native-exited', returncode=code)
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))
