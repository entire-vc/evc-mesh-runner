"""Explicit operator rebind; only the owning controller changes its WAIT by CAS.

The CLI writes an immutable private request, never Runner SQLite or a lease.
An authority digest records the operator's reviewed repair, not a signature or
permission inferred from a Mesh comment. Historical ledger cursors stay intact.
"""
import argparse
import copy
import json
import time

from .native_result_ledger import Refused, binding, digest, fields, identity, uuid
from .native_result_wait import current_receiver

SPOOL = "native-rebind-requests"
FIELDS = ("version", "request_id", "agent", "task", "attempt", "session",
          "generation", "old_registration", "registration", "binding", "owner",
          "authority_sha256", "reason", "created", "expires")


def validate(request, agent):
    fields(request, FIELDS)
    if (type(request["version"]) is not int or request["version"] != 1
            or request["agent"] != agent or not isinstance(agent, str) or not agent):
        raise Refused("native rebind request scope changed")
    for key in ("request_id", "task", "attempt", "session", "owner"):
        uuid(request[key])
    for key in ("generation", "old_registration", "registration", "authority_sha256"):
        digest(request[key])
    binding(request["binding"])
    if request["registration"] == request["old_registration"]:
        raise Refused("native rebind requires a new registration")
    reason = request["reason"]
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 512:
        raise Refused("native rebind requires bounded repair grounds")
    if (any(type(request[k]) is not int for k in ("created", "expires"))
            or not 0 < request["expires"] - request["created"] <= 900):
        raise Refused("invalid native rebind deadline")
    return request


def plan(ledger, request, attempt, task, owner, *, channels=None):
    """Validate fresh authority and return replacement WAIT plus private audit.

    No mutation, ACK, cursor reset, event publish, READY or lease acquisition.
    The controller must commit this against the exact expected state and WAIT.
    """
    validate(request, attempt.get("agent"))
    if (request["owner"] != owner or request["task"] != attempt.get("task_id")
            or request["attempt"] != attempt.get("id")
            or request["session"] != attempt.get("session_id")):
        raise Refused("native rebind receiver scope changed")
    old = attempt.get("wait") or {}
    if (attempt.get("state") not in ("waiting", "unknown")
            or attempt.get("pid") or attempt.get("checkout")
            or old.get("kind") != "native"
            or old.get("target") != request["old_registration"]
            or identity(old) != request["generation"]
            or (attempt.get("state") == "unknown" and not old.get("native_delivery_failed"))):
        raise Refused("native rebind requires the exact inactive WAIT")
    # Use the freshly read Mesh epoch, never the previous delivery's captured
    # receiver. The same attempt/session must still own that released checkout.
    candidate = copy.deepcopy(attempt)
    candidate.update(state="waiting", checkout={"generation": task.get("checkout_generation")})
    current = current_receiver(candidate, task, owner)
    registered = ledger.readback(request["registration"])
    if registered["receiver"] != current or registered["binding"] != request["binding"]:
        raise Refused("native rebind registration changed")
    if channels is None:
        if not registered["binding"]["test"]:
            raise Refused("production native rebind requires the enrolled channel")
    else:
        from .native_result_channel import load
        desc, _ = load(channels / request["registration"])
        if (desc["source"]["binding"] != registered["binding"]
                or desc["receiver"] != current or desc["owner"] != owner):
            raise Refused("native rebind channel scope changed")
    wait = {"kind": "native", "target": request["registration"], "native_receiver": current,
            "native_rebind": request["request_id"]}
    audit = {"request": request, "previous_wait": copy.deepcopy(old),
             "receiver": current, "previous_state": attempt["state"],
             "pending_events_retained": True}
    return wait, audit


def submit(state, request):
    from . import workspace_requests as spool
    validate(request, request.get("agent"))
    root = spool.directory(state, request["agent"], SPOOL)
    path = root / (request["request_id"] + ".request.json")
    if path.exists():
        old = spool._read(path)
        validate(old, request["agent"])
        # Deadline does not renew on retry. All other fields are immutable.
        if any(old[k] != request[k] for k in FIELDS if k not in ("created", "expires")):
            raise Refused("native rebind request id already bound")
        request = old
    else:
        # Reuse the existing bounded private spool and durable publication.
        import os
        if root.exists():
            with os.scandir(root) as entries:
                names = []
                for entry in entries:
                    names.append(entry.name)
                    if len(names) > spool.MAX_REQUESTS * 2:
                        raise Refused("native rebind spool full")
            if sum(n.endswith(".request.json") for n in names) >= spool.MAX_REQUESTS:
                raise Refused("native rebind spool full")
        if not spool._publish(path, request):
            return submit(state, request)
    receipt = root / (request["request_id"] + ".receipt.json")
    return {"request": request, "receipt": spool._read(receipt) if receipt.exists() else None}


def command(argv):
    from . import config
    parser = argparse.ArgumentParser(prog="mesh-runner native-rebind")
    parser.add_argument("--config", required=True)
    parser.add_argument("--request-file", required=True,
                        help="private exact repair request; no automatic identity selection")
    parser.add_argument("--apply", action="store_true", help="submit to controller; default validates only")
    args = parser.parse_args(argv)
    from .workspace_requests import _read
    try:
        from pathlib import Path
        request = _read(Path(args.request_file))
        cfg = config.load(args.config)
        if request.get("agent") not in {a.name for a in cfg.agents}:
            raise Refused("unknown native rebind agent")
        validate(request, request["agent"])
        result = submit(cfg.state_dir, request) if args.apply else {"valid_schema": True, "submitted": False}
    except (ValueError, OSError, TypeError, KeyError):
        print(json.dumps({"error": "native_rebind_request_refused"}))
        return 2
    # Do not print the private request, source path, reason or authority context.
    print(json.dumps({"request_id": request["request_id"], "submitted": args.apply,
                      "receipt": result.get("receipt")}))
    return 0
