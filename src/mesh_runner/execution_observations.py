"""Attempt-scoped execution observations (Mesh <-> Runner contract v1, §3-§5).

Publishes what the launched adapter actually runs, separately from the configured route:

  POST /workspaces/:ws/runtime/controllers/:ref/execution-instances   boot registration -> generation
  POST /workspaces/:ws/runtime/controllers/:ref/execution-reports     attempt rows, sequence-ordered

Rules kept here, each one covered by tests/test_execution_observations.py:

* ``observed.model`` / ``observed.effort`` come only from the current turn's adapter evidence. Usage,
  session reports, the profile default and the local environment are never a fallback; unknown is null.
* A new turn (resume included) starts with model and effort null until a new observation arrives.
* A boot registers a fresh instance; after a 409 (or a refused boot) the old publisher stays silent for good.
* A 403 on a report is a scope answer, not a fence: the shared Runner serves many projects and only
  the ones with the Runner turned on accept rows. The batch is split per (project, agent) scope; a
  scope refused on its own is dropped and not reported again for DENY_SECONDS, the others go on.
* Every outgoing field is whitelisted: no env, argv, output, path, credential_ref or free-form text.
* Telemetry never raises into execution: a failed send is bounded, sanitised and dropped.

Stdlib only; the clock and the transport are injected so restart/replay/future/late cases run on a
fixed clock.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import quote

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
HEARTBEAT_SECONDS = 30.0
MAX_AGE_SECONDS = 120
MAX_ATTEMPTS_PER_BATCH = 200
MAX_BODY_BYTES = 256 * 1024
SEND_BACKOFF = (0.5, 2.0)          # sleeps between the (at most 3) sends of one batch
DENY_SECONDS = 600.0               # a scope Mesh refused (403) is not reported again before this
TERMINAL = ("stopped", "completed", "failed")
STATES = ("starting", "running", "waiting") + TERMINAL
HARNESSES = ("claude", "codex")

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/\[\]-]{0,99}")      # a model or effort name
_DIGEST = re.compile(r"[0-9a-f]{64}")
# evidence kinds a model/effort may come from; "agent_profile", usage and environment are absent on purpose
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:+-]{0,99}")           # deployed API token for attempt_id / turn_id
SOURCES = ("adapter_stream", "adapter_launch", "adapter_turn_context")


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _epoch(value) -> float | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() if isinstance(value, str) else None
    except ValueError:
        return None


def _safe(pattern, value) -> str | None:
    return value if isinstance(value, str) and pattern.fullmatch(value) else None


@dataclass
class Identity:
    """Who an attempt belongs to; every id is opaque and checked against its pattern."""
    workspace_id: str          # receiver workspace
    project_id: str
    agent_id: str
    binding_ref: str
    task_id: str | None
    attempt_id: str

    def valid(self) -> bool:
        return (all(_safe(_UUID, v) for v in (self.workspace_id, self.project_id, self.agent_id))
                and bool(_safe(_ID, self.binding_ref)) and bool(_safe(_TOKEN, self.attempt_id))
                and (self.task_id is None or bool(_safe(_UUID, self.task_id))))


@dataclass
class ResolvedRoute:
    """What the local resolver selected for an attempt (override/pin/env included), with its source."""
    source: str
    revision: int | None = None
    digest: str | None = None
    profile_ref: str | None = None
    harness: str | None = None
    model: str | None = None
    effort: str | None = None

    def wire(self) -> dict:
        out = {"source": _safe(_ID, self.source) or "unknown"}
        if type(self.revision) is int and self.revision >= 1:
            out["revision"] = self.revision
        for key, pattern in (("digest", _DIGEST), ("profile_ref", _ID), ("harness", _ID),
                             ("model", _VALUE), ("effort", _VALUE)):
            value = _safe(pattern, getattr(self, key))
            if value:
                out[key] = value
        return out


@dataclass
class _Attempt:
    identity: Identity
    turn_id: str | None = None
    state: str = "starting"
    harness: str | None = None
    resolved: ResolvedRoute | None = None
    model: str | None = None
    effort: str | None = None
    source: str | None = None
    model_at: float | None = None
    effort_at: float | None = None
    launch_at: float = 0.0     # when this turn was launched: the real provenance before any model/effort is seen
    heartbeat_at: float = 0.0
    dirty: bool = True
    turn_no: int = 0
    rev: int = 0               # bumped on every change; a send clears only the revision it carried


class Publisher:
    """One per runner process boot. Not a lease: it never claims a controller or a launch."""

    def __init__(self, transport, controller_ref: str, clock, *, instance_id: str | None = None,
                 sleep=None):
        self._t = transport
        self.controller_ref = controller_ref
        self._clock = clock
        self._sleep = sleep or threading.Event().wait
        self.instance_id = instance_id or str(uuid.uuid4())
        self.generation: int | None = None
        self.sequence = 0
        self.fenced = False
        self._lock = threading.Lock()
        self._attempts: dict[str, _Attempt] = {}
        self._pending: dict | None = None      # the unacknowledged batch: a retry never changes its body
        self._sent: dict[str, tuple] = {}      # attempt_id -> (revision, state) the pending batch carried
        self.last_error = ""
        self.last_sent_at: float | None = None
        self._boot_try: float | None = None
        self._retry_at = 0.0                   # no send before this time after a failed batch
        self._pending_at = 0.0
        self._denied: dict[tuple, float] = {}  # (project_id, agent_id) -> until: Mesh refused that scope

    # -- boot ---------------------------------------------------------------------------
    def boot(self) -> bool:
        """Register this boot; telemetry-only. A fenced publisher never registers again."""
        if self.fenced:
            return False
        if self.generation is not None:
            return True
        code, body = self._t.post_instance({"schema_version": SCHEMA_VERSION, "instance_id": self.instance_id})
        if code in (403, 404, 409):
            self._fence(code)
            return False
        gen = body.get("generation") if code == 200 and isinstance(body, dict) else None
        if type(gen) is not int or gen < 1:
            self._note(code)
            return False
        self.generation = gen
        return True

    def ensure_boot(self) -> bool:
        """Register this boot even with no attempt yet, so a new boot fences the previous one at once.
        A failed try is repeated at most every heartbeat interval; a fenced publisher never tries."""
        if self.fenced or self.generation is not None:
            return not self.fenced
        now = self._clock()
        if self._boot_try is not None and now - self._boot_try < HEARTBEAT_SECONDS:
            return False
        self._boot_try = now
        try:
            return self.boot()
        except Exception as e:      # telemetry must not touch execution
            self.last_error = type(e).__name__
            return False

    # -- attempt lifecycle (called by the runner; never raises) ---------------------------
    def begin_turn(self, identity: Identity, *, harness: str, resolved: ResolvedRoute | None = None,
                   turn_id: str | None = None) -> str | None:
        """A turn starts (first start or resume): model and effort reset to unknown."""
        if not identity.valid() or harness not in HARNESSES:
            return None
        with self._lock:
            if self._is_denied((identity.project_id, identity.agent_id)):
                return None
            row = self._attempts.get(identity.attempt_id)
            if row is None:
                row = self._attempts[identity.attempt_id] = _Attempt(identity)
            row.turn_no += 1
            row.turn_id = _safe(_TOKEN, turn_id) or f"{identity.attempt_id}:t{row.turn_no}"[:100]
            row.harness = harness                  # the launched adapter, not the configured selector
            row.resolved = resolved
            row.model = row.effort = row.source = None
            row.model_at = row.effort_at = None
            row.state, row.heartbeat_at = "starting", self._clock()
            row.launch_at = row.heartbeat_at
            row.rev, row.dirty = row.rev + 1, True
            return row.turn_id

    def observe(self, attempt_id: str, turn_id: str | None, *, model: str | None = None,
                effort: str | None = None, source: str = "adapter_stream", at: float | None = None) -> bool:
        """Adapter evidence for the CURRENT turn only; a late report for an older turn is dropped."""
        if source not in SOURCES:
            return False
        with self._lock:
            row = self._attempts.get(attempt_id)
            if row is None or row.turn_id != turn_id or row.state in TERMINAL:
                return False
            now = self._clock()
            when = now if at is None else min(at, now)
            changed = False
            for name, value, pattern in (("model", model, _VALUE), ("effort", effort, _VALUE)):
                value = _safe(pattern, value)
                if value and getattr(row, name) != value:
                    setattr(row, name, value)
                    setattr(row, name + "_at", when)
                    row.source = source
                    changed = True
            if changed:
                row.rev, row.dirty = row.rev + 1, True
            return changed

    def heartbeat(self, attempt_id: str, state: str = "running") -> None:
        with self._lock:
            row = self._attempts.get(attempt_id)
            if row is not None and row.state not in TERMINAL and state in STATES:
                if row.state != state:
                    row.rev, row.dirty = row.rev + 1, True
                row.state, row.heartbeat_at = state, self._clock()

    def finish(self, attempt_id: str, state: str) -> None:
        if state not in TERMINAL:
            return
        with self._lock:
            row = self._attempts.get(attempt_id)
            if row is not None and row.state not in TERMINAL:
                row.state, row.heartbeat_at = state, self._clock()
                row.rev, row.dirty = row.rev + 1, True

    # -- publication ----------------------------------------------------------------------
    def due(self) -> bool:
        with self._lock:
            if self._clock() < self._retry_at:
                return False
            if self._pending is not None or any(r.dirty for r in self._attempts.values()):
                return True
            live = any(r.state not in TERMINAL for r in self._attempts.values())
            return live and self._clock() - (self.last_sent_at or 0.0) >= HEARTBEAT_SECONDS

    def _row(self, row: _Attempt) -> dict:
        i = row.identity
        out = {"workspace_id": i.workspace_id, "project_id": i.project_id, "agent_id": i.agent_id,
               "binding_ref": i.binding_ref, "task_id": i.task_id, "controller_ref": self.controller_ref,
               "instance_id": self.instance_id, "generation": self.generation, "attempt_id": i.attempt_id,
               "turn_id": row.turn_id, "state": row.state,
               "observed": {"harness": row.harness, "model": row.model, "effort": row.effort,
                            # the API requires a source and a time even while model/effort are unknown: the launch
                            "source": row.source or "adapter_launch",
                            "observed_at": iso(max(row.model_at or 0, row.effort_at or 0) if row.source
                                               else row.launch_at)}}
        out["resolved_route"] = row.resolved.wire() if row.resolved is not None else {"source": "unresolved"}
        # per-field times differ when model and effort were seen at different moments; the API takes
        # provenance beside `observed`, not inside it
        if row.source and row.model_at != row.effort_at and row.model_at and row.effort_at:
            out["provenance"] = {"model": {"source": row.source, "observed_at": iso(row.model_at)},
                                "effort": {"source": row.source, "observed_at": iso(row.effort_at)}}
        return out

    def flush(self) -> str:
        """Send what is due. Returns a short outcome label; execution never depends on it."""
        if self.fenced:
            return "fenced"
        try:
            if self.generation is None and not self.boot():
                return "fenced" if self.fenced else "no_boot"
            with self._lock:
                if self._clock() < self._retry_at:
                    return "backoff"
                if self._pending is not None and self._clock() - self._pending_at > MAX_AGE_SECONDS:
                    self._pending, self._sent = None, {}     # too old to be fresh evidence: rebuild from live rows
                if self._pending is None:
                    rows = [r for r in self._attempts.values() if (r.dirty or r.state not in TERMINAL)
                            and not self._is_denied((r.identity.project_id, r.identity.agent_id))]
                    if not rows:
                        return "idle"
                    rows = rows[:MAX_ATTEMPTS_PER_BATCH]
                    self.sequence += 1
                    self._pending_at = self._clock()
                    self._pending = {"schema_version": SCHEMA_VERSION, "instance_id": self.instance_id,
                                     "generation": self.generation, "sequence": self.sequence,
                                     "reported_at": iso(self._clock()),
                                     "attempts": [self._row(r) for r in rows]}
                    self._sent = {r.identity.attempt_id: (r.rev, r.state) for r in rows}
                body = self._pending
                sent = dict(self._sent)
            if len(json.dumps(body, separators=(",", ":"))) > MAX_BODY_BYTES:
                self._pending = None
                return "too_large"
            outcome = self._send(body, sent)
            return self._isolate(body, sent) if outcome == "forbidden" else outcome
        except Exception as e:      # telemetry must not touch the attempt
            self.last_error = type(e).__name__
            return "error"

    def _send(self, body: dict, sent: dict) -> str:
        outcome = "transport"
        for delay in (0.0,) + SEND_BACKOFF:
            if delay:
                self._sleep(delay)
            code, _ = self._t.post_report(body)
            if code == 200:
                with self._lock:
                    self._pending = None
                    self._sent = {}
                    self._acked(sent)
                return "sent"
            if code == 403:
                with self._lock:
                    self._pending, self._sent = None, {}
                return "forbidden"         # a scope answer: flush() isolates the refused scope
            if code in (404, 409):
                # 409: a lower sequence, a changed replay or a newer generation. Never re-register,
                # never replay the backlog: this publisher is historical now.
                self._fence(code)
                return "fenced"
            outcome = "transport" if code is None else "server_error" if code >= 500 else "rejected"
            if outcome == "rejected":
                with self._lock:
                    self._pending = None      # a 4xx replay would be refused again
                break
        with self._lock:
            self._retry_at = self._clock() + HEARTBEAT_SECONDS     # one batch of 3 sends per interval, not per tick
        self._note(None if outcome == "transport" else 0)
        return outcome

    def _acked(self, sent: dict) -> None:
        """Rows a 200 carried (lock held): clean unless they changed in flight; terminal ones are forgotten."""
        self._retry_at = 0.0
        self.last_sent_at = self._clock()
        for aid, (rev, state) in sent.items():
            row = self._attempts.get(aid)
            if row is None or row.rev != rev:
                continue              # changed while the POST was in flight: next batch carries it
            row.dirty = False
            if state in TERMINAL:     # a terminal row is delivered once, then forgotten
                del self._attempts[aid]

    def _is_denied(self, scope: tuple) -> bool:
        until = self._denied.get(scope)
        if until is None:
            return False
        if self._clock() >= until:
            del self._denied[scope]   # the project may have been turned on since: try again
            return False
        return True

    def _deny(self, scope: tuple) -> None:
        """Mesh refused this (project, agent) on its own (lock held): drop its rows, skip it for a while."""
        self._denied[scope] = self._clock() + DENY_SECONDS
        for aid in [a for a, r in self._attempts.items() if (r.identity.project_id, r.identity.agent_id) == scope]:
            del self._attempts[aid]
        log.info("execution observations: project %s refused agent %s (Runner not enabled there?); "
                 "skipped for %ds", scope[0][:8], scope[1][:8], int(DENY_SECONDS))

    def _isolate(self, body: dict, sent: dict) -> str:
        """A batch was refused with 403. One scope: deny it. Several: send each scope on its own (each
        with its own sequence) so only the refused ones are dropped."""
        groups: dict[tuple, list] = {}
        for row in body["attempts"]:
            groups.setdefault((row["project_id"], row["agent_id"]), []).append(row)
        if len(groups) == 1:
            with self._lock:
                self._deny(next(iter(groups)))
            return "denied"
        for scope, attempts in groups.items():
            with self._lock:
                self.sequence += 1
                part = {**body, "sequence": self.sequence, "reported_at": iso(self._clock()), "attempts": attempts}
                part_sent = {a["attempt_id"]: sent[a["attempt_id"]] for a in attempts if a["attempt_id"] in sent}
            code, _ = self._t.post_report(part)
            if code == 200:
                with self._lock:
                    self._acked(part_sent)
            elif code == 403:
                with self._lock:
                    self._deny(scope)
            elif code in (404, 409):
                self._fence(code)
                return "fenced"
            else:                     # transport or server trouble: the rows stay dirty for the next batch
                with self._lock:
                    self._retry_at = self._clock() + HEARTBEAT_SECONDS
                self._note(code)
                return "transport" if code is None else "server_error" if code >= 500 else "rejected"
        return "isolated"

    def _fence(self, code) -> None:
        with self._lock:
            self.fenced = True
            self._pending = None
            self._attempts.clear()
        self.last_error = f"fenced http {code}"
        log.warning("execution observations: publisher fenced (http %s)", code)

    def _note(self, code) -> None:
        self.last_error = "transport" if code is None else f"http {code}"


class Transport:
    """The two telemetry endpoints over the reporter's own client (same key, same no-redirect opener)."""

    def __init__(self, client, controller_ref: str):
        from mesh_runner.catalog import safe_ref
        if not safe_ref(controller_ref):
            raise ValueError("invalid controller ref")
        self._c = client
        self._base = client.base + quote(controller_ref, safe="")

    def post_instance(self, body: dict):
        code, _tag, value = self._c._exchange("POST", self._base + "/execution-instances", body)
        return code, value

    def post_report(self, body: dict):
        code, _tag, value = self._c._exchange("POST", self._base + "/execution-reports", body)
        return code, value


class RunnerHook:
    """What an AgentRunner calls around a turn. Every method swallows its own failures: a broken
    telemetry path must never change a task, a checkout or the outcome of an attempt."""

    def __init__(self, publisher: Publisher, workspace_id: str, binding_for, project_for=lambda _t: None):
        self.pub = publisher
        self.workspace_id = workspace_id
        self._binding_for = binding_for        # agent_id -> binding_ref | None (applied catalog slice)
        self._project_for = project_for        # task_id -> project uuid | None
        self._turns: dict[str, str] = {}
        self._started: dict[str, float] = {}

    def bound(self, agent_id: str) -> bool:
        try:
            return bool(self._binding_for(agent_id))
        except Exception:
            return False

    def turn_started(self, attempt_id: str, task_id: str, agent_id: str, harness: str, resolved=None,
                     project_id: str | None = None) -> None:
        try:
            binding, project = self._binding_for(agent_id), project_id or self._project_for(task_id)
            if not binding or not project:
                return            # scope unknown: stay unknown rather than guess
            ident = Identity(self.workspace_id, project, agent_id, binding, task_id, attempt_id)
            turn = self.pub.begin_turn(ident, harness=harness, resolved=resolved)
            if turn:
                self._turns[attempt_id] = turn
                self._started[attempt_id] = self.pub._clock()
        except Exception as e:
            log.debug("execution observations: turn start skipped: %s", type(e).__name__)

    def turn_settled(self, attempt_id: str, context: dict | None = None, state: str = "waiting",
                     source: str = "adapter_stream") -> None:
        """``context`` is what the harness itself recorded for this turn: ``{"model", "effort", "at"}``
        (Claude's stream system event, Codex's turn_context record). A configured/profile default must
        not be passed. A record older than this turn's start is a previous turn's and is dropped."""
        try:
            turn = self._turns.get(attempt_id)
            ctx = context or {}
            if turn and (ctx.get("model") or ctx.get("effort")):
                at = _epoch(ctx.get("at"))
                if at is None or at >= self._started.get(attempt_id, 0.0):
                    self.pub.observe(attempt_id, turn, model=ctx.get("model"), effort=ctx.get("effort"),
                                     source=source, at=at)
            if state in TERMINAL:
                self.pub.finish(attempt_id, state)
                self._turns.pop(attempt_id, None)
            else:
                self.pub.heartbeat(attempt_id, state)
        except Exception as e:
            log.debug("execution observations: settle skipped: %s", type(e).__name__)

    def attempt_ended(self, attempt_id: str, state: str) -> None:
        """The attempt left the runner for good: completed and failed are explicit, every other end
        (cancelled, abandoned, handed back) is reported as stopped. Delivered once, then forgotten."""
        try:
            self.pub.finish(attempt_id, state if state in ("completed", "failed") else "stopped")
            self._turns.pop(attempt_id, None)
            self._started.pop(attempt_id, None)
        except Exception as e:
            log.debug("execution observations: end skipped: %s", type(e).__name__)

    def tick(self) -> None:
        try:
            self.pub.ensure_boot()
            if self.pub.due():
                self.pub.flush()
        except Exception as e:
            log.debug("execution observations: tick skipped: %s", type(e).__name__)


def loop(hook: RunnerHook, stop: threading.Event, every: float = 5.0) -> None:
    while not stop.is_set():
        hook.tick()
        stop.wait(every)


def binding_lookup(catalog_dir, workspace_id: str):
    """agent_id -> binding_ref from the controller's ``desired.json`` (re-read when it changes). Only an
    enabled binding of this agent in this workspace counts: Mesh refuses rows for any other one."""
    path = os.path.join(str(catalog_dir), "desired.json")
    cache = {"mtime": None, "map": {}}

    def lookup(agent_id: str) -> str | None:
        try:
            mtime = os.path.getmtime(path)
            if mtime != cache["mtime"]:
                with open(path, encoding="utf-8") as fh:
                    bindings = ((json.load(fh).get("catalog") or {}).get("bindings")) or {}
                found = {}
                for ref, b in bindings.items():
                    ident = b.get("binding") or {} if isinstance(b, dict) else {}
                    if (b.get("enabled") is True and ident.get("workspace_id") == workspace_id
                            and _safe(_UUID, ident.get("agent_id")) and _safe(_ID, ref)):
                        found.setdefault(ident["agent_id"], ref)
                cache.update(mtime=mtime, map=found)
        except (OSError, ValueError, AttributeError):
            return None
        return cache["map"].get(agent_id)
    return lookup


def launch_route(harness: str, argv: list[str], profile_model: str = "") -> ResolvedRoute:
    """The model and effort the runner put on the native command line of this turn (the lane's
    configured args after every override): what it asked for, not what the harness confirmed."""
    model = effort = None
    i = 0
    while i < len(argv):
        arg = argv[i]
        nxt = argv[i + 1] if i + 1 < len(argv) else None
        if arg in ("--model", "-m") and nxt:
            model, i = nxt, i + 1
        elif arg.startswith("--model="):
            model = arg.split("=", 1)[1]
        elif arg == "--effort" and nxt:
            effort, i = nxt, i + 1
        elif arg == "-c" and nxt:
            key, _, value = nxt.partition("=")
            value = value.strip().strip('"').strip("'")
            if key.strip() == "model":
                model = value
            elif key.strip() == "model_reasoning_effort":
                effort = value
            i += 1
        i += 1
    return ResolvedRoute(source="runner_launch", harness=harness if harness in HARNESSES else None,
                         model=model or profile_model or None, effort=effort)


class Router:
    """The shared Runner serves several workspaces: one RunnerHook (publisher + controller) per
    workspace. An attempt goes to the workspace whose catalog binds its agent; an agent bound
    nowhere is not reported."""

    def __init__(self, hooks: list[RunnerHook]):
        self.hooks = list(hooks)
        self._by_attempt: dict[str, RunnerHook] = {}

    def bound(self, agent_id: str) -> bool:
        return any(h.bound(agent_id) for h in self.hooks)

    def turn_started(self, attempt_id, task_id, agent_id, harness, resolved=None, project_id=None) -> None:
        try:
            hook = next((h for h in self.hooks if h.bound(agent_id)), None)
            if hook is None:
                return
            self._by_attempt[attempt_id] = hook
            hook.turn_started(attempt_id, task_id, agent_id, harness, resolved, project_id=project_id)
        except Exception as e:
            log.debug("execution observations: route skipped: %s", type(e).__name__)

    def turn_settled(self, attempt_id, context=None, state="waiting", source="adapter_stream") -> None:
        hook = self._by_attempt.get(attempt_id)
        if hook is not None:
            hook.turn_settled(attempt_id, context, state, source)

    def attempt_ended(self, attempt_id, state) -> None:
        hook = self._by_attempt.pop(attempt_id, None)
        if hook is not None:
            hook.attempt_ended(attempt_id, state)

    def tick(self) -> None:
        for hook in self.hooks:
            hook.tick()
