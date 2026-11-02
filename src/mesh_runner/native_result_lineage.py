"""Explicit source-machine succession and same-file stage enrollment.

The operator pins the authority/evidence digest and privately transfers the
resulting channel bootstrap. A Mesh comment or this manifest alone is not
authentication. Old journals, registrations, sequence space and ACKs stay intact.
"""
import os
from pathlib import Path

from .native_result_ledger import Refused, binding, digest, fields, uuid
from .native_result_source import _metadata, _open, enroll, scan

MAX_LINEAGE = 16
SOURCE_FIELDS = ("root", "source", "device", "inode", "metadata_sha256")


def source_identity(value):
    fields(value, ("root", "source", "device", "inode", "metadata_sha256", "binding", "cursor"))
    binding(value["binding"])
    digest(value["metadata_sha256"])
    for name in ("device", "inode", "cursor"):
        if type(value[name]) is not int or value[name] < (1 if name == "inode" else 0):
            raise Refused("invalid enrolled source identity/cursor")
    for name in ("root", "source"):
        if (not isinstance(value[name], str) or not Path(value[name]).is_absolute()
                or ".." in Path(value[name]).parts):
            raise Refused("absolute enrolled source path required")
    if not Path(value["source"]).is_relative_to(value["root"]):
        raise Refused("source outside enrolled root")
    return value


def validate(value, current):
    """Validate a bounded chain without consulting the receiver's filesystem.

    Version 1 changes the actual producer/file. Version 2 changes the stage or
    package on the same file, preserving any earlier source-machine succession.
    A predecessor's cursor may advance after its enrollment fence, but its
    binding and physical identity must match the next edge's previous source.
    """
    source_identity(current)
    node, owner = value, None
    for _ in range(MAX_LINEAGE):
        if not isinstance(node, dict) or type(node.get("version")) is not int:
            raise Refused("unsupported succession version")
        version = node["version"]
        if version not in (1, 2):
            raise Refused("unsupported succession version")
        names = ("version", "previous", "successor", "operator", "authority_sha256")
        fields(node, (*names, "previous_lineage") if version == 2 else names)
        operator = uuid(node["operator"])
        digest(node["authority_sha256"])
        if owner is not None and operator != owner:
            raise Refused("lineage operator differs")
        owner = operator
        old, new = source_identity(node["previous"]), source_identity(node["successor"])
        if (any(current[k] != new[k] for k in new if k != "cursor")
                or current["cursor"] < new["cursor"]):
            raise Refused("successor source or EOF fence changed")
        if version == 1:
            expected = dict(old["binding"], producer=new["binding"]["producer"])
            if (old["binding"]["producer"] == new["binding"]["producer"]
                    or expected != new["binding"]
                    or (old["device"], old["inode"]) == (new["device"], new["inode"])):
                raise Refused("succession must preserve task/stage/package/test and change actual source")
            return value
        unchanged = ("producer", "task", "test")
        if (any(old[k] != new[k] for k in SOURCE_FIELDS)
                or old["cursor"] != new["cursor"]
                or any(old["binding"][k] != new["binding"][k] for k in unchanged)
                or old["binding"] == new["binding"]):
            raise Refused("stage enrollment must preserve actual source/producer/task/test and change stage or package")
        if node["previous_lineage"] is None:
            return value
        current, node = old, node["previous_lineage"]
    raise Refused("lineage exceeds bounded depth")


def prepare(previous, native_root, native_source, successor, operator, authority_sha256):
    """Both files are enrolled on the actual source machine, under the old lock.

    Refuse undrained old input. Fence all existing successor frames at EOF.
    This does not stop another process: sole-operator/watcher cutover is a
    required operational condition, checked before the private bootstrap moves.
    """
    source_identity(previous)
    end = enroll(previous["root"], previous["source"], previous["binding"], at_end=True)
    if end != previous:
        raise Refused("previous source must be drained without changing its identity")
    scan(previous)
    contract = dict(previous["binding"], producer=uuid(successor))
    following = enroll(native_root, native_source, contract, at_end=True)
    lineage = {"version": 1, "previous": previous, "successor": following,
               "operator": uuid(operator), "authority_sha256": digest(authority_sha256)}
    validate(lineage, following)
    return following, lineage


def verify_local(value, current):
    """Recheck pinned files and boundaries, scanning only the active binding.

    A historical stage's suffix contains later-stage frames, so it must never
    be scanned using that historical binding. Version 1 retains its exact old
    file EOF fence. These pins do not hash historical record contents or stop
    the producer; an authenticated sole-operator cutover remains necessary.
    """
    validate(value, current)
    node = value
    while node is not None:
        old = node["previous"]
        if node["version"] == 1:
            if enroll(old["root"], old["source"], old["binding"], at_end=True) != old:
                raise Refused("historical source changed since succession")
        else:
            _verify_boundary(old)
        _verify_boundary(node["successor"])
        node = node.get("previous_lineage")
    _verify_boundary(current)
    scan(current)
    return value


def _verify_boundary(source):
    stream, info = _open(source["root"], source["source"])
    with stream:
        metadata, start = _metadata(stream, source["binding"]["producer"])
        if ((info.st_dev, info.st_ino) != (source["device"], source["inode"])
                or metadata != source["metadata_sha256"]):
            raise Refused("enrolled source changed since succession or stage enrollment")
        cursor = source["cursor"]
        if not start <= cursor <= info.st_size:
            raise Refused("historical source cursor lost or source truncated")
        stream.seek(cursor - 1, os.SEEK_SET)
        if stream.read(1) != b"\n":
            raise Refused("historical source cursor is not a complete record boundary")
