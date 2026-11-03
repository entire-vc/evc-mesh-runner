"""Darwin filesystem-event source observer; no task timers or agent-turn polling.

Register vnode watches before draining the enrolled source. New OS processes
always drain the persisted cursor before sleeping, so a crash, notification
coalescing or disconnected receiver cannot lose a committed native frame.
Transport backoff is internal to this process and never writes Mesh/Runner state.

A v3 channel additionally observes the operator-pinned native process lifetime.
Its supervisor and worker watch each other; an unclosed lifetime on restart
latches UNKNOWN before ingestion. UNKNOWN is observer-authenticated and does not
impersonate native failed/dead frames. Installation remains an operator action.
"""
import argparse
import json
import os
from pathlib import Path
import select
import signal
import stat
import sys
import time
from . import native_result_process as process_fence

from .native_result_channel import RemoteSink, Unavailable, read_private, save_private
from .native_result_ingest import SourceJournal
from .native_result_ledger import Refused, canonical, identity


class VnodeEvents:
    def __init__(self, registration, processes=()):
        if not hasattr(select, "kqueue"):
            raise Refused("kqueue source observer requires Darwin/BSD")
        self.queue, self.fds = select.kqueue(), []
        self.processes = dict(processes)
        try:
            source = Path(registration["source"])
            if not source.is_absolute() or ".." in source.parts:
                raise Refused("absolute enrolled source required")
            directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
            self.fds.append(directory)
            for part in source.parts[1:-1]:
                directory = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                    dir_fd=directory)
                self.fds.append(directory)
            fd = os.open(source.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
            self.fds.append(fd)
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or (info.st_dev, info.st_ino) != (registration["device"], registration["inode"])):
                raise Refused("enrolled source inode changed")
            terminal = select.KQ_NOTE_RENAME | select.KQ_NOTE_DELETE | select.KQ_NOTE_REVOKE
            changes = [select.kevent(item, filter=select.KQ_FILTER_VNODE,
                       flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                       fflags=terminal | select.KQ_NOTE_ATTRIB |
                       ((select.KQ_NOTE_WRITE | select.KQ_NOTE_EXTEND) if item == fd else 0))
                       for item in self.fds]
            for label, pin in processes:
                process_fence.verify(pin)
                changes.append(select.kevent(pin["pid"], filter=select.KQ_FILTER_PROC,
                    flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                    fflags=select.KQ_NOTE_EXIT | select.KQ_NOTE_EXEC))
            self.queue.control(changes, 0, 0)
            # Compare the exact birth/executable after kernel registration: a
            # reused PID or a process lost during arming is never readiness.
            for label, pin in processes:
                process_fence.verify(pin)
        except BaseException:
            self.close()
            raise

    def wait(self):
        events = self.queue.control(None, len(self.fds) + len(self.processes), None)
        for value in events:
            if value.filter == select.KQ_FILTER_PROC:
                label = next((name for name, pin in self.processes.items()
                              if pin["pid"] == value.ident), "enrolled")
                raise Refused(label + " process lifetime ended or executable changed")
            if value.flags & (select.KQ_EV_ERROR | select.KQ_EV_EOF):
                raise Refused("source vnode observation failed")
            if value.fflags & (select.KQ_NOTE_RENAME | select.KQ_NOTE_DELETE | select.KQ_NOTE_REVOKE):
                raise Refused("enrolled source path changed or revoked")

    def close(self):
        self.queue.close()
        for fd in self.fds:
            os.close(fd)
        self.fds.clear()


def emit(kind, **values):
    # Counts and receipt IDs only: no source text, commands or credentials.
    print(canonical({"event": kind, **values}).decode(), flush=True)


def observe(journal, sink, supervisor=None):
    watcher = None
    try:
        # A restart of a failed observer delivers its original evidence first.
        if (sink.root / "failure.json").exists():
            return _terminal(sink, "restart", "LatchedFailure", "prior observer failure")
        state = journal.enrollment()
        current = state["source"]
        if state.get("lineage") != sink.desc.get("lineage"):
            raise Refused("source journal lineage differs from authenticated channel")
        enrolled = sink.desc["source"]
        if (any(current[k] != enrolled[k] for k in enrolled if k != "cursor")
                or type(current["cursor"]) is not int or current["cursor"] < enrolled["cursor"]):
            raise Refused("source journal differs from channel enrollment")
        processes = []
        if sink.desc["version"] == 3:
            if supervisor is None:
                raise Refused("pinned native source requires a supervised observer")
            processes = [("native", sink.desc["process"]["identity"]), ("supervisor", supervisor)]
        watcher = VnodeEvents(current, processes)
        ready = False
        backoff = 1
        while True:
            try:
                # Drain bounded batches until EOF before sleeping on the kernel.
                for label, pin in processes:
                    process_fence.verify(pin)
                before = journal.readback()["cursor"]
                result = journal.ingest(sink)
                backoff = 1
                if result["events"]:
                    emit("ingested", frames=len(result["events"]), cursor=result["cursor"])
                if result["cursor"] != before:
                    continue
                if not ready:
                    emit("observing")
                    ready = True
                watcher.wait()
            except Unavailable:
                # Lost commit reply leaves cursor behind; next read replays the
                # same event. This is not a task schedule or a polling turn.
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)
    except (KeyboardInterrupt, InterruptedError):
        return _terminal(sink, "observer-stop", "Interrupted", "source observation interrupted")
    except (OSError, Refused, ValueError, KeyError, TypeError) as exc:
        return _terminal(sink, "observe-source", type(exc).__name__, str(exc)[:512] or "source refused")
    finally:
        if watcher is not None:
            watcher.close()


def _terminal(sink, operation, error, reason):
    backoff = 1
    while True:
        try:
            receipt = sink.fail(operation, error, reason)
            emit("unknown-delivered", receipt=receipt["event"], duplicate=receipt["duplicate"])
            return 2
        except Unavailable:
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)



def supervise(journal, sink):
    """One event-blocked worker; no task turns or native runtime mutations.

    The worker observes its parent's exact lifetime through kqueue. The parent
    waits on its own child. A durable start record before fork makes a joint
    crash visible on restart. Neither PID nor a fresh restart repairs a channel.
    """
    lock = os.open(sink.root / "supervisor.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    child = None
    if sink.desc["version"] != 3:
        os.close(lock)
        raise Refused("supervision requires explicit native process enrollment")
    try:
        import fcntl
        info = os.fstat(lock)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise Refused("private supervisor lock required")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refused("source observer supervisor already active") from None
        if (sink.root / "supervisor.json").exists():
            return _terminal(sink, "supervisor-restart", "UnclosedLifetime",
                             "prior observer lifetime exists; explicit repair required")
        pin = process_fence.snapshot(os.getpid())
        save_private(sink.root / "supervisor.json", canonical(
            {"version": 1, "channel": identity(sink.desc), "supervisor": pin}))
        incoming, outgoing = os.pipe()
        child = os.fork()
        if child == 0:
            os.close(lock)
            os.close(outgoing)
            try:
                if os.read(incoming, 1) != b"g":
                    code = _terminal(sink, "supervisor-start", "IncompleteStart", "supervisor start was not committed")
                else:
                    code = observe(journal, sink, pin)
            except BaseException:
                code = 3
            finally:
                os.close(incoming)
            os._exit(code)
        os.close(incoming)
        try:
            worker = process_fence.snapshot(child)
            save_private(sink.root / "supervisor.json", canonical(
                {"version": 1, "channel": identity(sink.desc), "supervisor": pin, "worker": worker}))
            os.write(outgoing, b"g")
        except (OSError, Refused):
            # Closing the start pipe makes our child report UNKNOWN, not ready.
            pass
        finally:
            os.close(outgoing)
        try:
            os.waitpid(child, 0)
        except (InterruptedError, KeyboardInterrupt):
            # This is our own observer child, never the native producer.
            try:
                os.kill(child, signal.SIGTERM)
            except ProcessLookupError:
                pass
            os.waitpid(child, 0)
        return _terminal(sink, "observer-exit", "WorkerStopped",
                         "source observer worker stopped; explicit repair required")
    finally:
        os.close(lock)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", required=True)
    parser.add_argument("--channel", required=True)
    parser.add_argument("--receiver-command", required=True,
                        help="private JSON argv file; typically an operator-pinned SSH command")
    parser.add_argument("--supervise", action="store_true")
    args = parser.parse_args()
    # SIGTERM records observer UNKNOWN rather than claiming producer death.
    def stop(signum, frame):
        raise InterruptedError("observer termination requested")
    signal.signal(signal.SIGTERM, stop)
    try:
        command = json.loads(read_private(Path(args.receiver_command)))
        sink = RemoteSink(args.channel, command)
        if args.supervise:
            return supervise(SourceJournal(args.journal), sink)
        return observe(SourceJournal(args.journal), sink)
    except (OSError, ValueError, TypeError, KeyError):
        print("native-result observer configuration refused", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
