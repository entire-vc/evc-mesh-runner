"""Durable enrolled-source cursor, separate from receiver consumption.

This adapter performs one bounded scan per explicit invocation. Its caller must
supply a real event observer; this module does not install one, run a polling
loop, wake a Runner attempt or certify source enrollment on another machine.
"""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import stat
import tempfile

from .native_result_ledger import Refused, binding, canonical, digest, fields, uuid
from .native_result_source import enroll, scan
from .native_result_lineage import prepare, validate, verify_local


class SourceJournal:
    def __init__(self, directory):
        self.root = Path(directory)

    @classmethod
    def initialize(cls, directory, native_root, native_source, binding):
        registration = enroll(native_root, native_source, binding)
        root = Path(directory)
        root.mkdir(mode=0o700, parents=True, exist_ok=False)
        journal = cls(root)
        with journal._locked():
            journal._save({"version": 1, "source": registration})
        return journal

    @classmethod
    def initialize_successor(cls, directory, previous, native_root, native_source,
                             successor, operator, authority_sha256):
        # One source-journal lock covers readback, both real file enrollments
        # and the new journal commit. The old cursor/state is never changed.
        with previous._locked():
            old = previous._load()
            if old["version"] != 1:
                raise Refused("further succession requires an explicit lineage-chain contract")
            source, lineage = prepare(old["source"], native_root, native_source,
                                      successor, operator, authority_sha256)
            root = Path(directory)
            root.mkdir(mode=0o700, parents=True, exist_ok=False)
            journal = cls(root)
            with journal._locked():
                journal._save({"version": 2, "source": source, "lineage": lineage})
        return journal

    @classmethod
    def initialize_stage(cls, directory, previous, contract, operator, authority_sha256):
        """Enroll a later stage at drained EOF without changing old state/ACKs.

        This lock fences cooperating journal users, not the native writer.
        The same authenticated operator must arrange the actual stage cutover.
        No skipped old-stage frames are repaired or silently consumed here.
        """
        contract = binding(contract)
        operator, authority_sha256 = uuid(operator), digest(authority_sha256)
        with previous._locked():
            old = previous._load()
            source = old["source"]
            if old["version"] == 2:
                verify_local(old["lineage"], source)
            end = enroll(source["root"], source["source"], source["binding"], at_end=True)
            if end != source:
                raise Refused("previous source must be drained without changing its identity")
            following = enroll(source["root"], source["source"], contract, at_end=True)
            lineage = {"version": 2, "previous": source, "successor": following,
                       "previous_lineage": old.get("lineage"), "operator": operator,
                       "authority_sha256": authority_sha256}
            validate(lineage, following)
            # Includes a second actual boundary/identity read before committing.
            verify_local(lineage, following)
            if enroll(source["root"], source["source"], source["binding"], at_end=True) != source:
                raise Refused("previous source changed during stage enrollment")
            root = Path(directory)
            root.mkdir(mode=0o700, parents=True, exist_ok=False)
            journal = cls(root)
            with journal._locked():
                journal._save({"version": 2, "source": following, "lineage": lineage})
        return journal

    @contextmanager
    def _locked(self):
        info = self.root.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise Refused("private owned source journal required")
        fd = os.open(self.root / "lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise Refused("private owned source lock required")
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _load(self):
        fd = os.open(self.root / "source.json", os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise Refused("private owned source state required")
            try:
                state = json.load(stream)
            except ValueError:
                raise Refused("source state corrupt; recovery required") from None
        if not isinstance(state, dict) or type(state.get("version")) is not int:
            raise Refused("source state corrupt; recovery required")
        fields(state, ("version", "source", "lineage") if state["version"] == 2 else ("version", "source"))
        if state["version"] not in (1, 2) or not isinstance(state["source"], dict):
            raise Refused("source state corrupt; recovery required")
        fields(state["source"], ("root", "source", "device", "inode", "metadata_sha256", "binding", "cursor"))
        if state["version"] == 2:
            validate(state["lineage"], state["source"])
        return state

    def _save(self, state):
        fd, name = tempfile.mkstemp(prefix="commit-", dir=self.root)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(canonical(state))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.root / "source.json")
            parent = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
        finally:
            Path(name).unlink(missing_ok=True)

    def readback(self):
        with self._locked():
            return self._load()["source"]

    def enrollment(self):
        """Source plus pinned provenance in one locked readback."""
        with self._locked():
            return self._load()

    def ingest(self, ledger):
        """Publish before cursor commit. Crash/retry repeats exact event IDs.

        A refusal never advances the cursor; partial publishing can safely be
        replayed. This returns ingestion evidence, NOT receiver ACK or readiness.
        """
        with self._locked():
            state = self._load()
            if state["version"] == 2:
                verify_local(state["lineage"], state["source"])
            batch = scan(state["source"])
            receipts = [ledger.publish(frame, batch["producer"], batch["task"])
                        for frame in batch["frames"]]
            if batch["cursor"] != state["source"]["cursor"]:
                state["source"]["cursor"] = batch["cursor"]
                self._save(state)
            return {"cursor": batch["cursor"], "events": receipts}
