"""Native WAIT observer for a Runner-owned, freshly read receiver.

The Runner calls observe before its generic comment/age observers and commits
READY + pending wake text + the event mark in ONE Store CAS. This module never
opens Runner SQLite, changes a lease, or treats a prepared outbox as an ACK.
Receiver generation is the Mesh checkout generation captured when registering.
"""
from .native_result_ledger import Ledger, Refused, receiver, uuid


def current_receiver(attempt, task, owner):
    """Require the same actual attempt/session and current Mesh checkout epoch."""
    uuid(owner)
    if not isinstance(attempt, dict) or not isinstance(task, dict):
        raise Refused("receiver context missing")
    checkout = attempt.get("checkout")
    # Normal WAIT releases the lease and clears Attempt.checkout. The controller
    # captures this registration-bound identity before release, in the same WAIT
    # envelope. It is an observation fence, never a usable checkout/lease token.
    saved = (attempt.get("wait") or {}).get("native_receiver")
    if isinstance(checkout, dict) and not checkout and saved:
        saved = receiver(saved)
        if any(saved[k] != attempt.get(v) for k, v in
               (("task", "task_id"), ("attempt", "id"), ("session", "session_id"))):
            raise Refused("saved native receiver scope changed")
        checkout = {"generation": saved["generation"]}
    if not isinstance(checkout, dict) or type(task.get("checkout_generation")) is not int:
        raise Refused("receiver checkout context missing")
    current = receiver({"task": attempt.get("task_id"), "attempt": attempt.get("id"),
                        "session": attempt.get("session_id"),
                        "generation": checkout.get("generation")})
    if (attempt.get("state") != "waiting" or attempt.get("mode") != "work"
            or task.get("id") != current["task"]
            or task.get("assignee_id") != owner
            or task.get("assignee_type") != "agent"
            or task.get("checked_out_by") not in (None, "", owner)
            or task.get("checkout_session_id") != current["attempt"]
            or task.get("checkout_generation") != current["generation"]):
        raise Refused("native receiver no longer current")
    if task.get("human_gate") or task.get("start_after"):
        raise Refused("receiver access or schedule gate active")
    return current


def arm(ledger, registration, attempt, task, owner):
    """Controller-only capture while settling the real leased receiver turn."""
    if attempt.get("state") != "running":
        raise Refused("native WAIT requires the current running receiver lease")
    current = current_receiver(dict(attempt, state="waiting"), task, owner)
    if ledger.readback(registration)["receiver"] != current:
        raise Refused("native WAIT registration belongs to another receiver")
    wait = attempt.get("wait") or {}
    captured = (wait.get("kind") == "native" and wait.get("target") == registration
                and wait.get("native_receiver") == current)
    if not captured and (task.get("checked_out_by") != owner or not attempt.get("checkout")):
        raise Refused("native WAIT requires the current running receiver lease")
    return current


def observe(ledger, registration, attempt, task, owner, *, channels=None):
    current = current_receiver(attempt, task, owner)
    if channels is not None:
        from .native_result_channel import failure_delivery
        packet = failure_delivery(channels, ledger, registration, current)
        if packet is not None:
            return packet
    packet = ledger.delivery(registration, current)
    if packet is None:
        return None
    return packet


def wake_text(packet):
    """Safe bounded prompt from the validated event; no source transcript text."""
    value = packet["value"]
    contract = value["binding"]
    return ("Native result transport event (not operational approval). "
            f"test={str(contract['test']).lower()} status={value['status']} "
            f"producer={contract['producer']} task={contract['task']} stage={contract['stage']} "
            f"package_sha256={contract['package_sha256']} receipt_sha256={value['receipt_sha256']} "
            f"registration={packet['registration']} event={packet['event']} "
            f"sequence={value['sequence']}. "
            f"Owner: {value['owner']}; reason: {value['reason']!r}. "
            "Read the exact registered outbox and acknowledge consumption through the native "
            "transport adapter. A wake is not an ACK, native receipt or permission for effects.")
