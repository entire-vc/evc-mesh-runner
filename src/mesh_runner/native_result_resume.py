"""Controller-owned resume fence and source-proven receiver consumption ACK.

The grant lives in Runner Store, never in an agent-supplied request. Runner
creates it at its real spawn boundary after acquiring a fresh Mesh checkout,
then reads the resumed session's own Codex rollout before releasing that lease.
No caller-supplied generation is accepted as a substitute for fresh Mesh state.
This module changes only the private transport ledger. No SQLite/lease/process
effects. An ACK confirms transport consumption, never operational approval.
"""
from copy import deepcopy
import hashlib
from pathlib import Path

from .native_result_ledger import Refused, canonical, event, identity, receiver, uuid
from .native_result_source import ACK_MARKER, enroll, scan


def leased_receiver(attempt, task, owner, state):
    uuid(owner)
    if not isinstance(attempt, dict) or not isinstance(task, dict):
        raise Refused("resume context missing")
    checkout = attempt.get("checkout")
    if not isinstance(checkout, dict) or type(task.get("checkout_generation")) is not int:
        raise Refused("resume checkout missing")
    current = receiver({"task": attempt.get("task_id"), "attempt": attempt.get("id"),
                        "session": attempt.get("session_id"),
                        "generation": checkout.get("generation")})
    if (attempt.get("state") != state or attempt.get("mode") != "work"
            or task.get("id") != current["task"] or task.get("assignee_id") != owner
            or task.get("assignee_type") != "agent" or task.get("checked_out_by") != owner
            or task.get("checkout_session_id") != current["attempt"]
            or task.get("checkout_generation") != current["generation"]
            or task.get("human_gate") or task.get("start_after")):
        raise Refused("resume receiver access revoked or epoch changed")
    return current


def ack_frame(packet, owner):
    """Expected whole assistant ACK after the receiver has read the packet."""
    uuid(owner)
    return {"binding": {"producer": packet["receiver"]["session"],
                        "task": packet["receiver"]["task"], "stage": "transport-ack",
                        "package_sha256": identity(packet),
                        "test": packet["value"]["binding"]["test"]},
            "sequence": 1, "status": "result", "receipt_sha256": packet["event"],
            "owner": owner, "reason": "consumed"}


def _packet(ledger, packet, *, allow_acked=False):
    """Match exact pending bytes, including binding/test and registered epoch."""
    value = event(packet["value"])
    if identity(value) != packet["event"]:
        raise Refused("resume event digest mismatch")
    target = receiver(packet["receiver"])
    row = ledger.readback(packet["registration"])
    if row["receiver"] != target or row["binding"] != value["binding"]:
        raise Refused("resume registration mismatch")
    if allow_acked and row.get("last_ack") == packet["event"]:
        return
    if row["pending"] != packet["event"] or row["cursor"] >= value["sequence"]:
        raise Refused("resume event is not pending")
    if ledger.delivery(packet["registration"], target) != packet:
        raise Refused("resume packet changed")


def authorize(ledger, packet, attempt, task, owner, root, source):
    """Call only at real Codex resume, after route/session fallback decisions."""
    _packet(ledger, packet)
    current = leased_receiver(attempt, task, owner, "ready")
    old = packet["receiver"]
    if (any(current[k] != old[k] for k in ("task", "attempt", "session"))
            or current["generation"] < old["generation"]):
        raise Refused("native delivery cannot cross attempt/session or older epoch")
    expected = ack_frame(packet, owner)
    source = enroll(root, source, expected["binding"], marker=ACK_MARKER, at_end=True)
    return {"packet": deepcopy(packet), "receiver": current, "owner": owner,
            "source": source, "ack": expected}


def _codex_source(attempt, env):
    home = Path(env.get("CODEX_HOME") or Path(env.get("HOME") or Path.home()) / ".codex")
    root = home / "sessions"
    session = uuid(attempt.get("session_id"))
    candidates = list(root.glob(f"**/rollout-*{session}*.jsonl"))
    if len(candidates) != 1:
        raise Refused("one original receiver rollout required")
    return root, candidates[0]


def authorize_codex(ledger, packet, attempt, task, owner, env):
    """Use the actual launch environment's sessions root, not an uploaded file."""
    root, source = _codex_source(attempt, env)
    return authorize(ledger, packet, attempt, task, owner, root, source)


def prepare_prompt(ledger, attempt, task, owner, env, prompt, *, complete=False):
    """Bind the grant BEFORE Runner persists its ordinary prompt intent.

    A controller-proven unsent retry preserves the exact prompt and original
    source fence. Reacquiring a checkout can advance its epoch, but cannot change
    packet/session/source identity or turn a pending cursor into an ACK.
    """
    wait = attempt.get("wait") or {}
    packet = wait.get("native_packet")
    if not packet:
        raise Refused("native packet missing")
    if not complete:
        if wait.get("native_grant"):
            raise Refused("pending native grant requires exact unsent prompt recovery")
        grant = authorize_codex(ledger, packet, attempt, task, owner, env)
        return prompt + instruction(grant), grant
    intent = wait.get("interactive_prompt_intent") or {}
    grant = deepcopy(wait.get("native_grant") or {})
    if (intent.get("retryable") is not True or intent.get("prompt") != prompt
            or intent.get("sha256") != hashlib.sha256(prompt.encode()).hexdigest()
            or intent.get("harness") != "codex" or intent.get("resume") is not True
            or intent.get("session_id") != attempt.get("session_id")):
        raise Refused("native retry lacks exact controller-proven unsent intent")
    _packet(ledger, packet)
    current = leased_receiver(attempt, task, owner, "ready")
    prior = receiver(grant.get("receiver"))
    expected = ack_frame(packet, owner)
    root, source = _codex_source(attempt, env)
    enrolled = grant.get("source") or {}
    if (grant.get("packet") != packet or grant.get("owner") != owner
            or grant.get("ack") != expected or enrolled.get("binding") != expected["binding"]
            or enrolled.get("marker") != ACK_MARKER
            or enrolled.get("root") != str(root) or enrolled.get("source") != str(source)
            or any(prior[k] != current[k] for k in ("task", "attempt", "session"))
            or prior["generation"] > current["generation"]
            or not prompt.endswith(instruction(grant))):
        raise Refused("native retry grant scope changed")
    scan(enrolled)  # revalidate inode/metadata/fence; never advance it on a retry
    grant["receiver"] = current
    return prompt, grant


def consume(ledger, grant, attempt, task, owner):
    """At settle, before lease release. ACK then persist returned source cursor.

    A crash after ACK but before Store commit replays the same source frame and
    exact ACK idempotently. Restart alone or an empty turn cannot consume it.
    """
    current = leased_receiver(attempt, task, owner, "running")
    if grant["owner"] != owner or grant["receiver"] != current:
        raise Refused("resume authorization no longer current")
    packet = grant["packet"]
    _packet(ledger, packet, allow_acked=True)
    expected = ack_frame(packet, owner)
    if grant["ack"] != expected or grant["source"]["binding"] != expected["binding"]:
        raise Refused("resume authorization binding changed")
    if grant["source"].get("marker") != ACK_MARKER:
        raise Refused("resume ACK source marker changed")
    batch = scan(grant["source"])
    if any(value != expected for value in batch["frames"]):
        raise Refused("receiver ACK contents mismatch")
    result = None
    if batch["frames"]:
        # The old registered epoch is closed ONLY by the controller-owned
        # handoff above, never represented as the fresh current Mesh epoch.
        result = ledger.ack(packet["registration"], packet["event"], packet["receiver"])
    updated = deepcopy(grant)
    updated["source"]["cursor"] = batch["cursor"]
    return {"grant": updated, "ack": result}


def instruction(grant):
    return ("\nNative transport consumption contract: read and validate the exact packet "
            "below. Only after consuming it, emit the following ACK as a separate whole "
            "assistant message (no code fence or prose). It confirms transport consumption "
            "only; it grants no operational approval. Do not ACK unread/invalid content.\n"
            "Packet: " + canonical(grant["packet"]).decode() + "\n"
            + ACK_MARKER + canonical(grant["ack"]).decode() + "\n")
