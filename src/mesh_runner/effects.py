"""What an attempt's state means for the rest of Mesh.

Other people and tools read a task, not the runner: the board shows a task's
runtime state, schedulers honour ``start_after``, dependency views follow
``blocks`` edges, and an agent asked for a decision only learns about it from
a task assigned to it. This module keeps those in step with the attempt:

- runtime state: ``working`` while a turn runs, ``waiting`` (with what it
  waits for) or ``stopped`` (human gate) while it waits, ``queued`` when woken;
  re-published every ``HEARTBEAT`` because Mesh treats it as stale after 15 min,
  cleared when the attempt closes;
- ``start_after``: only a time wait writes one (its time, with the evidence Mesh asks of an
  agent); cleared on wake. An event wait (pipeline, mr, merge, card, decision, blocking, queue)
  follows its event with no timer and writes nothing; a hold an older runner wrote for one is
  taken back the next time the wait is touched. Schedules are compared as instants (Mesh
  answers them in UTC, not as written); a clear refused with 409 is retried on a
  fresh read, and one that still fails is remembered in the store and retried on
  later ticks until it lands or the task carries another schedule;
- runtime state of tasks the agent holds but does not run (why they wait) is
  remembered in the runner's store, so a restart still clears it once the task
  is done, cancelled, in backlog or another agent's;
- ``card`` waits add a ``blocks`` edge to the awaited task, removed on wake;
- ``decision:@handle`` waits on another agent file one ``[decision]`` task
  assigned to it (a mention alone wakes nobody), retained until answered,
  superseded, or the source task ends; asked again within ``decision_reuse_hours``
  of filing, the same source and addressee reuse that task (the new question is
  a comment on it, a closed one is reopened) instead of filing another; the same
  question again (``normalized_question``) is attached to its request with no
  comment and no reopen (``identical_no_reopen``); a repeat of any question
  already answered on that request (closed as done) never reopens it, not even
  after a re-check, and the asker is sent back with the answer reference;
  a person is asked by Mesh from the agent's ``❓ Blocking`` line, never here.

Every call is best effort: a failed side effect is logged and retried by the
next tick, it never changes the attempt's own state.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import re
import time
import uuid

from . import answer_desk, card_wait_reconcile, outcome
from .mesh import MeshClient, MeshError
from .blocking_path import BlockingPath, Budget

DIAGNOSTIC_TICK_SECONDS = 15.0
from .selection import StatusCategories, parse_time
from .store import OPEN, Attempt, Store, iso, now

log = logging.getLogger("mesh_runner")

HEARTBEAT = dt.timedelta(minutes=5)
CREATE_WINDOW = dt.timedelta(minutes=10)   # a lost create may still commit this long
SKIP_WRITES_PER_TICK = 20
SKIP_EVERY = dt.timedelta(minutes=2)
SKIP_EVALUATE = 40                      # tasks whose hold reason is read per pass
DECISION_REQUEST_LABEL = "decision-request"
DECISION_LABELS = [DECISION_REQUEST_LABEL, "pin-assignee", "owner-locked"]
DECISION_OWNER = "_decision"          # source-task identity survives lane/attempt changes
DECISION_MARK = "decision_request"
DECISION_RECENT_MARK = "decision_recent"   # store mark: requests filed per source, by recipient, for reuse
CLOSE_BLOCKED_MARK = "close_blocked_human"  # store mark on a request: its close was refused by a human gate
CLOSE_BLOCKED_RETRY = dt.timedelta(hours=6)  # a gate-refused close is retried this often unless the card changes
SKIP_MARK = "skip_state"                # store mark: the skip state this agent published on a task
SKIP_SEED_DAYS = 30                     # first run with a store: tasks of this age may carry stale states
SKIP_CLEARS_PER_TICK = 10               # clears take at most this share of a pass's writes
SKIP_CLEAR_TRIES = 3                    # a clear that fails this often is given up (logged)
STATE_FIELDS = ("kind", "ref", "reason")
BACKLOG_BLOCKER = "blocked by a card in backlog: "
ACCESS_HELD = "capability held; waits for authenticated own-access recheck"   # access_recovery.REASON
EDGE_REMOVED_MARK = "card_edge_user_removed"   # store mark: a user deleted this lane's WAIT edge to <short target ref>
EDGE_DENIED_MARK = "edge_denied"        # store mark prefix: an edge this lane's key may not remove / add (HTTP 403)
UNSCHEDULE_MARK = "unschedule"          # store mark: a start_after this lane set and could not clear yet
UNSCHEDULE_TRIES = 2                    # clears per attempt when Mesh answers 409: one re-read, one retry
MIN_WHY = 10                            # Mesh: why must say what happens at the date
_URL = re.compile(r"https://[^\s<>\"'`]+")
_URL_TRAILING = ".,;:!?)]}>»*_"          # punctuation that ends a sentence, not the URL
EVIDENCE_MAX = 500                      # Mesh bounds event_ref, source, why and the clear reason to this
# Words Mesh refuses in an event_ref as "a retry or poll, not an external event" (domain.genericRetryWords)
GENERIC_RETRY_WORDS = frozenset({"retry", "retries", "poll", "polling", "backoff", "recheck", "timer",
                                 "reminder", "later", "followup"})
UNSCHEDULE_PER_PASS = 10                # pending clears retried per tick
UNSCHEDULE_RETRY = dt.timedelta(minutes=1)        # first retry of a pending clear; doubles
UNSCHEDULE_RETRY_MAX = dt.timedelta(minutes=30)
ORPHAN_CHECK = dt.timedelta(minutes=10)           # per task: how often a held task is checked for an orphan
_TRAILING_PUNCT = " \t.,;:!?…)]}>»\"'`*_~-—–"     # trailing characters a repeated question differs by


def normalized_question(note: str, source_id: str = "") -> str:
    """A question as compared for "the same question again": case-folded, whitespace collapsed,
    trailing punctuation and trailing references to the asking task itself (``#<8hex>``, its
    uuid) removed. Other task references are content: "status of #A?" and "status of #B?" differ."""
    text = " ".join((note or "").split()).casefold()
    source = (source_id or "").casefold()
    refs = sorted({source, source[:8]} - {""}, key=len, reverse=True)
    own = (re.compile(r"[\s(\[]*(?:#|(?<![\w-]))(?:" + "|".join(map(re.escape, refs)) + r")$")
           if refs else None)
    while True:
        before = text
        text = text.rstrip(_TRAILING_PUNCT)
        if own is not None:
            text = own.sub("", text)
        if text == before:
            return text


def same_instant(a: str | None, b: str | None) -> bool:
    """Two start_after values name the same moment. Mesh answers them in UTC with its own
    sub-second precision (``...132Z``) whatever the runner wrote (``...132+00:00``, or an
    agent's ``+03:00``): text is never compared, parsed instants are."""
    ta, tb = parse_time(a), parse_time(b)
    if ta is None or tb is None:
        return False
    # both sides cut to milliseconds, the precision Mesh keeps; a schedule set a moment
    # apart by someone else is a different instant and is never taken for ours
    return ta.replace(microsecond=ta.microsecond // 1000 * 1000) == tb.replace(microsecond=tb.microsecond // 1000 * 1000)


def own_schedule(start_after: str | None, wait: dict | None) -> bool:
    """``start_after`` is the schedule this wait set: the one Mesh confirmed, or the one whose
    answer was lost but may have committed (``scheduled_maybe``)."""
    w = wait or {}
    return any(same_instant(start_after, v) for v in (w.get("scheduled"), w.get("scheduled_maybe")) if v)


def _error_code(e: MeshError) -> str:
    """Mesh's machine-readable ``error_code`` of a refusal ('' when it sent none)."""
    return str(e.body.get("error_code") or e.body.get("code") or "") if isinstance(e.body, dict) else ""


def _event_ref(kind: str, ref: str) -> str:
    """A stable name for what a wait awaits, shaped for Mesh's check: at most 500 characters and no
    word that names a timer (``retry``, ``poll``, ``timer`` ...; Mesh splits on non-alphanumerics). A
    reference holding one of those words (a project called ``retry-service``) is replaced by a digest
    of itself: still stable, still unique per target."""
    text = re.sub(r"[^a-z0-9]+", "-", f"{kind}-{ref}".casefold()).strip("-")
    if set(text.split("-")) & GENERIC_RETRY_WORDS:
        text = f"{kind}-{hashlib.sha1(str(ref).encode()).hexdigest()[:12]}"
    return text[:EVIDENCE_MAX]


def _asked_at(posted, floor: str) -> str:
    """When a question reached its addressee: the later of ``floor`` (taken right before the
    write that carries it) and the Mesh time of the request card or comment written. Only an
    addressee comment after this instant answers it. The wait's own declaration time is earlier
    and would count a comment written before the question was put; the later of the two clocks
    errs towards asking again, never towards a send-back."""
    at = posted.get("created_at") if isinstance(posted, dict) else None
    server, local = parse_time(at), parse_time(floor)
    return at if server and (local is None or server > local) else floor


# ------------------------------------------------------- operational constraints
# Additive contract v1: the producer names the
# reason, who lifts it and the exact existing event, per observation. It adds no wait
# mechanism: wake.predicate / ref / target copy what the wait record already holds.
CONSTRAINT_ID_MAX = 128
_CI_FROM_STATUS = {"manual": "manual_required", "waiting_for_resource": "capacity"}


def _cid(*parts: str) -> str:
    text = ":".join(p for p in parts if p)
    return text if len(text) <= CONSTRAINT_ID_MAX else f"{parts[0]}:{hashlib.sha1(text.encode()).hexdigest()[:16]}"


def _constraint(cls: str, code: str, cid: str, source: str, *, actor: dict | None = None,
                predicate: str = "unknown", ref: str | None = None, target: dict | None = None,
                observed_at: str | None = None, params: dict | None = None, reason: str | None = None,
                ci: dict | None = None) -> dict:
    """One constraint. ``observed_at`` left out is stamped by ``Effects._put`` at write time (a
    state the runner re-observes every pass); a value given is the real observation moment and is
    never refreshed by a heartbeat. ``next_actor`` is unknown unless the wait names who acts."""
    out: dict = {"id": cid, "class": cls, "reason_code": code,
                 "next_actor": actor or {"kind": "unknown"},
                 "wake": {"predicate": predicate}, "source": {"kind": source}}
    if ref:
        out["wake"]["ref"] = ref[:300]
    if target:
        out["wake"]["target"] = target
    if observed_at:
        out["observed_at"] = observed_at
    if params:
        out["reason_params"] = params
    if reason:
        out["reason"] = reason[:500]
    if ci:
        out["ci"] = ci
    return out


def _wake_target(kind: str, target: str, w: dict, until: str | None) -> dict | None:
    """The typed-wait fields of a pipeline / MR wait, copied from its record."""
    out: dict = {}
    if kind in ("pipeline", "mr", "merge"):
        out["provider"] = "gitlab"
        sep = "#" if kind == "pipeline" else "!"
        project, _, number = str(target).rpartition(sep)
        if project and number.isdigit():
            out["project_path"] = project[:300]
            out["pipeline_id" if kind == "pipeline" else "mr_iid"] = int(number)
    contract = w.get("ci_change") if isinstance(w.get("ci_change"), dict) else {}
    if contract.get("predicate"):
        out["until"] = str(contract["predicate"])
    if contract.get("jobs"):
        out["required_jobs"] = [str(j)[:100] for j in contract["jobs"]][:16]
    if until:
        out["until_at"] = until
    return out or None


def wait_constraints(a: Attempt, until: str | None = None) -> list[dict]:
    """Constraints of a waiting attempt: its own declared wait (local_wait, never folded into a
    Mesh dependency) and, only when GitLab itself reported the awaited pipeline as manual or
    waiting for a runner, a ci_capacity one. Scope and position are provider evidence this runner
    does not hold, so they stay absent; a lane position is never a CI position."""
    w = a.wait or {}
    kind, target = w.get("kind"), str(w.get("target") or "")
    if kind not in ("card", "pipeline", "mr", "merge", "decision", "time"):
        return []
    since = w.get("since") if parse_time(w.get("since")) else None
    actor = {"kind": "agent", "name": str(w["handle"])[:100]} if kind == "decision" and w.get("handle") else None
    ref = f"card:#{target}" if kind == "card" else (target or None)
    out = [_constraint("local_wait", f"wait_{kind}", _cid("wait", kind, target or "-"), "runner_wait", actor=actor,
                       predicate=kind, ref=ref, target=_wake_target(kind, target, w, until), observed_at=since,
                       params={"target": target[:100]} if target else None)]
    if kind in ("pipeline", "mr", "merge"):
        seen = w.get("seen") if isinstance(w.get("seen"), dict) else {}
        status = str(seen.get("pipeline") or str(seen.get("mr_pipeline") or "").rpartition(":")[2])
        if status in _CI_FROM_STATUS:
            out.append(_constraint(
                "ci_capacity", "ci_" + _CI_FROM_STATUS[status], _cid("ci", kind, target), "gitlab",
                predicate=kind, ref=target, target=_wake_target(kind, target, w, until), observed_at=since,
                ci={"state": _CI_FROM_STATUS[status], "provider": "gitlab"}))
    return out


def runtime_state(a: Attempt, lane: str) -> dict | None:
    """The runtime_state a task shows for this attempt (Mesh's vocabulary)."""
    if a.state in ("starting", "running", "unknown"):
        return {"kind": "working", "lane": lane}
    if a.state == "ready":
        return {"kind": "queued", "lane": lane}
    if a.state != "waiting":
        return None
    w = a.wait
    kind = w.get("kind")
    if kind == "blocking":
        return {"kind": "stopped", "reason_code": "human_gate", "reason": "waits for a human answer"}
    if kind == "card":
        out = {"kind": "waiting", "ref": f"card:#{w.get('target')}"}
    elif kind in ("pipeline", "mr"):
        out = {"kind": "waiting", "ref": f"{kind}:{w.get('target')}"}
    elif kind == "merge":
        out = {"kind": "waiting", "ref": f"mr:{w.get('target')}"}
    elif kind == "decision":
        out = {"kind": "waiting", "ref": f"decision:@{w.get('handle')}" if w.get("handle") else "time"}
    elif kind == "time":
        out = {"kind": "waiting", "ref": "time"}
    else:
        return None
    until = parse_time(w.get("until"))
    if until and until > now() + dt.timedelta(seconds=30):   # Mesh refuses until < since (= now): never in the past
        out["until"] = w["until"]
    if constraints := wait_constraints(a, out.get("until")):
        out["constraints"] = constraints
    return out


def _stopped(code: str, reason: str) -> dict:
    return {"kind": "stopped", "reason_code": code, "reason": reason}


def skip_state(reason: str, lane: str) -> dict | None:
    """The runtime_state a task the agent does not start shows for ``reason``
    (AgentRunner.hold_reason's vocabulary). None = show nothing."""
    r = reason or ""
    if r.startswith(BACKLOG_BLOCKER):
        # nobody feeds backlog: without a word the task would wait forever, silently
        return {"kind": "waiting", "ref": f"card:#{r[len(BACKLOG_BLOCKER):].split(',')[0].strip()}",
                "reason_code": "blocker_in_backlog", "reason": "blocked by a card in backlog: nobody picks it up"}
    if r.startswith("blocked by "):
        return {"kind": "waiting", "ref": f"card:#{r[len('blocked by '):].split(',')[0].strip()}"}
    if r.startswith("start_after "):
        until = parse_time(r[len("start_after "):])
        out = {"kind": "waiting", "ref": "time"}
        if until and until > now() + dt.timedelta(seconds=30):   # never in the past for the server
            out["until"] = iso(until)
        return out
    if r == "waiting for a human decision":
        return _stopped("human_gate", "waits for a human decision")
    if r == "checked out by another writer":
        return _stopped("checked_out_elsewhere", "checked out by another writer")
    if r == "supervised task":
        return _stopped("supervised_todo", "supervised: waits for a human to start it")
    if r.startswith("label park") or r.startswith("label park:"):
        return _stopped("park", "explicitly parked")
    if r.startswith("umbrella") or r.startswith("parent waits on"):
        return _stopped("umbrella", "epic: waits for child cards")
    if r.startswith("review: just sent"):
        return _stopped("review_cooldown", "just sent to review")
    if r.startswith("review: already reviewed"):
        out = _stopped("review_refeed_hold", "already reviewed; waits for a new comment")
        if "(DO-NOT-SHIP)" in r:
            # rejected on the verdict: only a new commit or comment moves it, never a retry
            out["constraints"] = [_constraint(
                "review_rejection", "review_do_not_ship", "review:verdict", "review_verdict",
                actor={"kind": "agent", "name": "author"}, predicate="comment",
                reason="review verdict DO-NOT-SHIP; waits for a fix or a new comment")]
        return out
    if r == ACCESS_HELD:
        return {"kind": "stopped", "reason": "capability held; waits for authenticated own-access recheck",
                "constraints": [_constraint(
                    "expired_authority", "capability_held", "authority:capability", "access_recovery",
                    reason="capability held; waits for an authenticated own-access recheck")]}
    if r.startswith("review: the agent's own work"):
        return {"kind": "stopped", "reason": "waits for someone else to review it"}
    if r.startswith("waiting for new input after "):
        return {"kind": "waiting", "reason": r[:200]}
    if r.startswith(("failed recently", "backing off", "daily turn budget spent")):
        return {"kind": "stopped", "reason": r[:200]}
    if r.startswith("gate: "):
        return {"kind": "stopped", "reason": r[len("gate: "):][:200]}
    if r == "paused":
        return _stopped("pause", "operator pause")
    if r == "dependencies unreadable (403)":
        # held until a blocker it cannot see closes: say so, or it waits without a word
        return {"kind": "waiting", "reason_code": "dependencies_unreadable",
                "reason": "blocker card is not readable by this agent",
                "constraints": [_constraint("write_acl", "dependencies_unreadable", "acl:dependencies", "mesh_acl",
                                            reason="a blocker card is not readable by this agent")]}
    if r.startswith("label ") and r.endswith(" missing"):
        return _stopped("outside_lane_scope", "outside this agent's scope")
    if r.startswith("repository preflight: "):
        # held until an operator adds the repository to this lane, or the card's merge request links change
        return _stopped("outside_lane_scope", ("the card's repository is not in this agent's configuration ("
                                               + r[len("repository preflight: "):] + ")")[:200])
    return None                                # a transient read error etc.: say nothing


def _same(shown: dict, published: dict) -> bool:
    """The state on the task is the one published (a code the server refused is absent)."""
    if shown.get("reason_code") and shown["reason_code"] != published.get("reason_code"):
        return False
    return all((shown.get(k) or None) == (published.get(k) or None) for k in STATE_FIELDS)


class Effects:
    def __init__(self, mesh: MeshClient, cats: StatusCategories, lane: str, enabled: bool = True,
                 store: Store | None = None, decision_metadata_recipients: tuple[str, ...] = (),
                 answer_marker: bool = False, decision_reuse_hours: float = 0.0,
                 identical_no_reopen: bool = False):
        self.mesh, self.cats, self.lane, self.enabled = mesh, cats, lane, enabled
        # the same question again (normalized_question) is attached to its request: no new card,
        # no reopen, no repeat comment
        self.identical_no_reopen = identical_no_reopen
        self.answer_marker = answer_marker     # decision_wake = "answer": the request says how to answer
        # a request filed for a source and addressee this recently is reused for a new question; None = never
        self.reuse_window = dt.timedelta(hours=decision_reuse_hours) if decision_reuse_hours > 0 else None
        self.store = store
        self._blocking_path = BlockingPath(mesh, store, lane)
        self._decision_metadata_recipients = frozenset(decision_metadata_recipients)
        self.agent_id = ""                    # set by the runner once it knows who it is
        self.desk = None                      # answer_desk.AnswerDesk: questions its lanes answer without a card
        self._published: dict[str, tuple[dict, dt.datetime]] = {}
        self._failed_put: dict[str, dt.datetime] = {}
        self._agents: dict[str, dict] = {}
        self._agents_at: dt.datetime | None = None
        self._refused_codes: set[str] = set()   # reason codes this Mesh does not know (yet)
        self._skipped: dict[str, dict | None] = {}   # tasks whose state publish_skips owns -> what it wrote
        self._skipped_loaded = store is None
        self._clear_cursor = ""               # clears go round, not from the same first tasks every pass
        self._clear_fails: dict[str, int] = {}
        self._decision_checked: dict[str, dt.datetime] = {}
        self._decision_cursor = ""
        self._close_blocked: dict[str, dict] = {}   # request id -> gate-refused close, when there is no store
        self._unschedule_due: dict[str, tuple[dt.datetime, dt.timedelta]] = {}   # task -> (next try, step)
        self._orphan_checked: dict[str, dt.datetime] = {}
        self._edges_denied: set[tuple[str, str]] = set()     # memory of 403 edge operations when there is no store

    def _stamp(self, state: dict) -> dict:
        """A constraint with no observation time of its own is observed now (the runner
        re-reads it every pass); one with a time keeps it, so a heartbeat never freshens it."""
        cs = state.get("constraints")
        if not cs:
            return state
        at = iso(now())
        return {**state, "constraints": [c if c.get("observed_at") else {**c, "observed_at": at} for c in cs]}

    def _put(self, task_id: str, state: dict) -> None:
        """PUT a runtime state. A Mesh that does not know a reason code refuses the
        whole write (422 on ``reason_code``): the state goes out without the code,
        its reason text says the same. A refused ``constraints`` list (422 naming a
        ``constraints[...]`` field) is dropped the same way: the base state still goes out."""
        state = self._stamp(state)
        code = state.get("reason_code")
        body = {k: v for k, v in state.items() if k != "reason_code"} if code in self._refused_codes else state
        try:
            self.mesh.put_runtime_state(task_id, body)
        except MeshError as e:
            field = e.body.get("field") if isinstance(e.body, dict) else None
            if e.status == 422 and str(field or "").startswith("constraints") and "constraints" in body:
                log.warning("%s: Mesh refused constraints on #%s (%s); publishing without them", self.lane, task_id[:8], field)
                self.mesh.put_runtime_state(task_id, {k: v for k, v in body.items() if k != "constraints"})
                return
            if not (code and body is state and e.status == 422 and field in (None, "reason_code")):
                raise
            self._refused_codes.add(code)
            log.info("%s: Mesh does not know reason code %s; publishing without it", self.lane, code)
            self.mesh.put_runtime_state(task_id, {k: v for k, v in state.items() if k != "reason_code"})

    # ------------------------------------------------------------- skip memory
    def _load_skipped(self) -> None:
        """What earlier runs published (the store), and once per store the tasks an
        older runner may have left a state on without remembering it."""
        if self._skipped_loaded:
            return
        self._skipped_loaded = True
        for task_id, value in self.store.marked(self.lane, SKIP_MARK).items():
            try:
                self._skipped[task_id] = json.loads(value)
            except ValueError:
                self._skipped[task_id] = None
        if not self.store.mark(self.lane, "_effects", "skip_seeded"):
            for task_id in self.store.tasks_seen(self.lane, now() - dt.timedelta(days=SKIP_SEED_DAYS)):
                if task_id not in self._skipped:      # unknown: checked before clearing
                    self._skipped[task_id] = None
                    # kept like a published one, so a restart resumes the sweep where it stopped
                    self.store.mark(self.lane, task_id, SKIP_MARK, "null")
            self.store.mark(self.lane, "_effects", "skip_seeded", iso())

    def _remember(self, task_id: str, state: dict | None) -> None:
        if self._skipped.get(task_id, False) == state:
            return
        self._skipped[task_id] = state
        if self.store is not None and state is not None:
            self.store.mark(self.lane, task_id, SKIP_MARK, json.dumps(state, sort_keys=True))

    def _forget(self, task_id: str) -> None:
        self._clear_fails.pop(task_id, None)
        if self._skipped.pop(task_id, False) is not False and self.store is not None:
            self.store.mark(self.lane, task_id, SKIP_MARK, "")

    def _clear_skip(self, task_id: str, published: dict | None) -> bool:
        """Clear a skip state this agent wrote on a task it no longer holds; True when
        nothing of it is left to clear. A state someone else wrote since (another
        lane, the task's new owner) is left alone. ``published`` None: written by an
        older run that did not remember it; cleared only where no other agent owns
        the task now."""
        try:
            task = self.mesh.task(task_id)
            task = task.get("task", task) if isinstance(task, dict) else {}
        except MeshError as e:
            if e.status == 403:                # not readable to this agent any more: whose it is is unknown
                return self.clear_state(task_id) if published is not None else True
            if e.status not in (404, 410):
                return False                   # unknown now: try again next pass (a few times)
            return self.clear_state(task_id)   # gone: the plain clear
        if "runtime_state" in task:            # a Mesh that shows the state: look before clearing
            shown = task.get("runtime_state") or {}
            if not shown or shown.get("source") == "checkout":
                return True                    # nothing written to clear
            if shown.get("lane") not in (None, "", self.lane):
                return True                    # another lane's
            if published is not None and not _same(shown, published):
                return True                    # rewritten since by someone else
            if published is None and not self._unowned(task):
                return True
        elif published is None:
            return True                        # cannot tell whose it is: never guess
        return self.clear_state(task_id)

    def _unowned(self, task: dict) -> bool:
        """No other agent works on this task now: closed, in backlog, or this agent's."""
        try:
            if self.cats.category(task) in ("done", "cancelled", "backlog"):
                return True
        except MeshError:
            return False
        assignee = task.get("assignee_id") or (task.get("assignee") or {}).get("id")
        return bool(self.agent_id) and assignee == self.agent_id

    # ------------------------------------------------------------- runtime state
    def publish(self, attempts: list[Attempt]) -> None:
        if not self.enabled:
            return
        self.reconcile_decisions()
        self.retry_unschedules()
        current = now()
        tick_deadline = time.monotonic() + DIAGNOSTIC_TICK_SECONDS
        for a in attempts:
            desired = runtime_state(a, self.lane)
            if desired is not None and a.state == "waiting" and a.wait.get("kind") == "card":
                # A budget per card: one shared 3 s budget starved every card
                # after the first uncached ones. The tick cap bounds the total.
                if time.monotonic() < tick_deadline:
                    budget = Budget()
                    budget.deadline = min(budget.deadline, tick_deadline)
                    desired["reason"] = self._blocking_path.resolve(a, budget)
            prev = self._published.get(a.task_id)
            if desired is None:
                continue
            if prev and prev[0] == desired and current - prev[1] < HEARTBEAT:
                continue
            failed = self._failed_put.get(a.task_id)
            if failed and current - failed < HEARTBEAT:
                continue                       # back off after a refused write
            try:
                self._put(a.task_id, desired)
                self._published[a.task_id] = (desired, current)
                self._failed_put.pop(a.task_id, None)
            except MeshError as e:
                self._failed_put[a.task_id] = current
                log.warning("%s: runtime-state #%s: %s", self.lane, a.task8, e)

    def publish_skips(self, states: dict[str, dict], keep: set[str] = frozenset(),
                      budget: int = SKIP_WRITES_PER_TICK) -> None:
        """Runtime state for tasks the agent holds but does not run (why they
        wait). Clears the ones it published earlier that are neither skipped any
        more nor in ``keep`` (a task whose reason could not be read this time
        keeps its last state instead of flapping). Real changes and clears are
        written before heartbeat refreshes; at most ``budget`` writes. What it
        published is kept in the store, so a restart does not forget to clear it."""
        if not self.enabled:
            return
        self._load_skipped()
        current = now()
        pending = sorted(t for t in self._skipped if t not in states and t not in keep)
        at = next((i for i, t in enumerate(pending) if t > self._clear_cursor), 0)
        clears = min(budget, SKIP_CLEARS_PER_TICK)     # the rest of the pass is for publishing
        for task_id in pending[at:] + pending[:at]:
            if clears <= 0:
                break
            clears -= 1
            budget -= 1
            self._clear_cursor = task_id
            if self._clear_skip(task_id, self._skipped[task_id]):
                self._forget(task_id)
                continue
            fails = self._clear_fails.get(task_id, 0) + 1
            self._clear_fails[task_id] = fails
            if fails >= SKIP_CLEAR_TRIES:
                log.warning("%s: runtime-state clear #%s failed %d times; given up", self.lane, task_id[:8], fails)
                self._forget(task_id)
        if budget <= 0:
            return
        changed, stale = [], []
        for task_id, desired in states.items():
            prev = self._published.get(task_id)
            failed = self._failed_put.get(task_id)
            if failed and current - failed < HEARTBEAT:
                continue                       # back off after a refused write
            if not prev or prev[0] != desired:
                changed.append(task_id)
            elif current - prev[1] >= HEARTBEAT:
                stale.append(task_id)
            else:
                self._remember(task_id, desired)
        for task_id in changed + stale:
            if budget <= 0:
                return
            budget -= 1
            try:
                self._put(task_id, states[task_id])
                self._published[task_id] = (states[task_id], current)
                self._remember(task_id, states[task_id])
                self._failed_put.pop(task_id, None)
            except MeshError as e:
                self._failed_put[task_id] = current
                log.warning("%s: runtime-state #%s: %s", self.lane, task_id[:8], e)

    def taken(self, task_id: str) -> None:
        """An attempt owns this task's state from now on."""
        if self.enabled:
            self._load_skipped()
        self._forget(task_id)

    def clear_state(self, task_id: str) -> bool:
        """True when the state is gone (cleared now, or nothing to clear)."""
        if not self.enabled:
            return True
        try:
            self.mesh.delete_runtime_state(task_id)
        except MeshError as e:
            if e.status not in (404, 410):
                log.warning("%s: runtime-state clear #%s: %s", self.lane, task_id[:8], e)
                return False
        self._published.pop(task_id, None)
        return True

    # ------------------------------------------------------------- waits
    def on_wait(self, a: Attempt, task: dict, wait: dict, me: dict) -> None:
        """Called once when an attempt starts waiting; mutates ``wait`` bookkeeping."""
        if not self.enabled:
            return
        wait["fx"] = True                   # effects applied; a wait without it predates them
        kind = wait.get("kind")
        if kind == "time" and wait.get("until"):
            self._schedule(a, wait, wait["until"])
        else:
            self._drop_event_hold(a, wait)
        if kind == "card" and not wait.get("server_observed"):
            self._add_edge(a, wait)
        if kind == "decision" and wait.get("handle"):
            if wait.get("decision_request_id") and not self._typed_question(a, wait):
                return                      # the question could not be read: adopt() retries
            if answer_desk.defer(self.desk, self, a, task, wait, me):
                return                      # the addressee's answer desk takes it; adopt() files the card if not
            # ``ask_agent`` records the question on Mesh, but its comment wakes nobody and no queue
            # shows the addressee an open request: the request card is still what starts the addressee
            self._file_decision(a, task, wait, me)
            if wait.get("decision_request_id") and wait.get("fx"):
                wait["ask_filed"] = True

    def _typed_question(self, a: Attempt, wait: dict) -> bool:
        """Put the words of an ``ask_agent`` question on its wait. The typed wait carries only
        ``asked <agent>``; the question is in the comment Mesh wrote for the request (its metadata
        names the request). True when the wait holds the question; a failed or empty read leaves
        ``fx`` off so the wait is adopted again."""
        if wait.get("question"):
            return True
        rid = str(wait["decision_request_id"])
        try:
            comments = self.mesh.comments_all(a.task_id)
        except MeshError as e:
            wait["fx"] = False
            log.warning("%s: decision request #%s: question read HTTP %s", self.lane, a.task8, e.status)
            return False
        asked = next((c for c in reversed(comments)
                      if str((c.get("metadata") or {}).get("decision_request_id") or "") == rid
                      and (c.get("metadata") or {}).get("command") == "ask_agent"), None)
        if asked is None:
            wait["fx"] = False
            log.warning("%s: decision request #%s: no ask_agent comment names request %s", self.lane, a.task8,
                        rid[:8])
            return False
        head, _, text = (asked.get("body") or "").partition("\n\n")
        question = " ".join((text or head).split())
        handle = str(wait.get("handle") or "").lstrip("@")
        wait["question"] = question
        wait["asked_note"] = wait.get("note") or ""
        # the shape a marker question has: what the request card quotes and what a repeat is compared by
        wait["note"] = question
        wait["line"] = f"⏳ WAIT decision:@{handle}: {question}"
        return True

    def adopt(self, a: Attempt, task: dict, wait: dict, me: dict) -> bool:
        """Apply on_wait to a wait that began while effects were off (an older runner,
        or mesh_effects=false): its addressee was never asked and nothing is scheduled.
        True when it did so."""
        if not self.enabled or (wait.get("fx") and not self._typed_ask_unfiled(wait)):
            return False
        if answer_desk.waiting(self.desk, self, a, wait):
            return False
        self.on_wait(a, task, wait, me)
        return True

    @staticmethod
    def _typed_ask_unfiled(wait: dict) -> bool:
        """An ``ask_agent`` wait whose addressee was never given a request card: parked by a runner
        that left the asking to Mesh, which delivers nothing."""
        return bool(wait.get("kind") == "decision" and wait.get("handle") and wait.get("decision_request_id")
                    and not wait.get("request") and not wait.get("ask_filed"))

    def renew(self, a: Attempt, wait: dict) -> None:
        """Nothing is renewed: an event wait writes no hold. What an older runner wrote for one
        (a rolling horizon) is taken back here, the first time the wait is touched."""
        if self.enabled and wait.get("kind") != "time":
            self._drop_event_hold(a, wait)

    def _drop_event_hold(self, a: Attempt, wait: dict) -> None:
        """An event wait follows its event without a timer: a start_after this
        wait still carries from an older runner is cleared through compare-clear, only if Mesh shows
        exactly that instant."""
        if wait.get("scheduled") or wait.get("scheduled_maybe"):
            self._end_schedule(a, wait)

    def on_end(self, a: Attempt, wait: dict, why: str, *, close_request: bool = False) -> None:
        """Undo scheduling/edges on every wake; an observation is not an answer."""
        if not self.enabled or not wait:
            return
        self._retain_legacy_decision(a, wait)
        if wait.get("scheduled") or wait.get("scheduled_maybe"):
            self._end_schedule(a, wait)
        if wait.get("edge"):
            if self._edge_denied(a.task_id, "del:" + wait["edge"]):
                wait.pop("edge", None)         # a denial is permanent for this key: not asked again
            else:
                try:
                    if self._edge_still_ours(a, wait):
                        self.mesh.delete_dependency(a.task_id, wait["edge"])
                    wait.pop("edge", None)
                except MeshError as e:
                    if e.status == 403:
                        self._deny_edge(a.task_id, "del:" + wait["edge"])
                        wait.pop("edge", None)
                    log.warning("%s: edge #%s: %s%s", self.lane, a.task8, e,
                                "; not removable by this lane, not retried" if e.status == 403 else "")
        wait.pop("observe_edge", None)          # a read-only watch ends with the wait; the edge is untouched
        if close_request and wait.get("kind") in ("decision", "blocking"):
            active = self._decision(a.task_id)
            if active:
                self._end_decision(a.task_id, active, "awaited answer received")
                if active.get("state") == "closed":
                    wait.pop("request", None)
            elif wait.get("request"):
                self._close_decision(wait, why)

    # ------------------------------------------------------------- helpers
    def _schedule(self, a: Attempt, wait: dict, when: str) -> None:
        """Hold the task until ``when`` on the server. Mesh takes an agent's start_after only with
        ``start_after_evidence`` (event_ref, source, why); a refusal is logged once for this wait
        and is not repeated by ``renew``: the wait itself stays local to the runner."""
        evidence = self.start_after_evidence(a, wait, when)
        if evidence is None:
            if not wait.get("schedule_skipped"):
                wait["schedule_skipped"] = "no https source for the evidence"
                log.info("%s: schedule #%s: not written, Mesh needs an https source for start_after_evidence "
                         "and its URL is not https; the wait stays local", self.lane, a.task8)
            return
        try:
            self.mesh.patch_task(a.task_id, {"start_after": when, "start_after_evidence": evidence})
            wait["scheduled"] = when
            wait.pop("scheduled_maybe", None)
            wait.pop("schedule_skipped", None)
            self._forget_unschedule(a.task_id)     # the lane owns the task's schedule again
        except MeshError as e:
            if e.status == 0 or e.status >= 500:
                # the answer was lost, the write may have committed: remember it so the end
                # of the wait still takes it back (only if Mesh shows exactly this instant)
                wait["scheduled_maybe"] = when
            elif e.status == 422:
                # a refusal of this evidence repeats on every try: say it once, keep the wait local
                wait["schedule_skipped"] = _error_code(e) or "refused"
            log.warning("%s: schedule #%s: %s", self.lane, a.task8, e)

    def start_after_evidence(self, a: Attempt, wait: dict, when: str) -> dict | None:
        """What Mesh asks of an agent's start_after (docs/api/typed-outcomes.md, "start_after"), for
        a time wait: ``event_ref`` names the awaited moment, ``source`` is the first https URL in the
        agent's WAIT line (the external event's source) or else the card's own URL, ``why`` is the
        WAIT line. None when no https source can be built."""
        line = " ".join(str(wait.get("line") or "").split())
        note = " ".join(str(wait.get("note") or "").split())
        cited = _URL.search(line)
        source = cited.group(0).rstrip(_URL_TRAILING) if cited else ""
        if not source:
            base = str(getattr(self.mesh, "base", "") or "")
            if not base.startswith("https://"):
                return None
            source = f"{base}/tasks/{a.task_id}"
        why = line
        if len(why) < MIN_WHY and note:
            why = f"{why} {note}".strip()
        if len(why) < MIN_WHY:
            why = f"{why} (the runner holds the card until {when})".strip()
        ref = f"{a.task8}-{(parse_time(when) or now()).strftime('%Y%m%dT%H%MZ')}"
        return {"event_ref": _event_ref("time", ref), "source": source[:EVIDENCE_MAX], "why": why[:EVIDENCE_MAX]}

    # ------------------------------------------------------------- unschedule
    def _end_schedule(self, a: Attempt, wait: dict) -> None:
        """Take back the start_after this wait set. Never forgets one it could not clear:
        a failure is kept in the store and retried by ``retry_unschedules``."""
        mine = [v for v in (wait.get("scheduled"), wait.get("scheduled_maybe")) if v]
        outcome, held = self._unschedule(a.task_id, mine)
        if outcome in ("cleared", "gone"):
            self._forget_unschedule(a.task_id)
        elif self.store is not None:
            self._remember_unschedule(a.task_id, [held] if held else mine)
            log.warning("%s: unschedule #%s: %s; retried on later ticks", self.lane, a.task8, outcome)
        else:
            log.warning("%s: unschedule #%s: %s", self.lane, a.task8, outcome)
            return                             # no store: the wait keeps it, the next wake retries
        wait.pop("scheduled", None)
        wait.pop("scheduled_maybe", None)

    def _unschedule(self, task_id: str, mine: list[str]) -> tuple[str, str]:
        """Clear the task's start_after if it is one of ``mine``. Returns (outcome, instant):
        ``cleared``; ``gone`` (no schedule, someone else's, a person's, or the task is gone: left
        alone); anything else is the error that kept it. Mesh clears an agent's start_after only
        through compare-clear, which removes exactly the schedule that was read: on a 409 the task
        is read again and the clear tried once more."""
        last = ""
        for attempt in range(UNSCHEDULE_TRIES):
            try:
                task = self.mesh.task(task_id)
            except MeshError as e:
                if e.status in (404, 410):
                    return "gone", ""
                return str(e), ""
            task = task.get("task", task) if isinstance(task, dict) else {}
            current = task.get("start_after")
            held = next((v for v in mine if same_instant(current, v)), "")
            if not held:
                return "gone", ""              # nothing to clear, or a schedule someone else set
            try:
                self._clear_start_after(task_id, task)
                if attempt:
                    log.info("%s: unschedule #%s: cleared after %d conflict(s)", self.lane, task_id[:8], attempt)
                return "cleared", held
            except MeshError as e:
                last = str(e)
                if e.status == 403 and "person" in str(e.body or e).casefold():
                    log.info("%s: unschedule #%s: start_after was set by a person; left alone",
                             self.lane, task_id[:8])
                    return "gone", held
                if e.status != 409:
                    return last, held
        return last, held

    def _clear_start_after(self, task_id: str, task: dict) -> None:
        """One clear of the schedule ``task`` shows: compare-clear with the date, version, origin and
        receipt as read, so a schedule set again meanwhile (even to the same instant) is not removed.
        A Mesh that shows no ``start_after_origin`` predates provenance and has no such endpoint: the
        plain null write is all it knows."""
        origin, version = task.get("start_after_origin"), task.get("version")
        if origin is None or version is None:
            self.mesh.patch_task(task_id, {"start_after": None})
            return
        receipt = task.get("start_after_receipt_id")
        self.mesh.compare_clear_start_after(
            task_id, expected_start_after=task["start_after"], expected_version=version,
            expected_origin=origin, schedule_receipt_id=receipt,
            # the same read repeats the same key (a lost answer is replayed, not cleared twice);
            # a changed schedule or version is a different clear
            idempotency_key=f"mesh-runner:{self.lane}:{receipt or origin}:{version}"[:200],
            reason="the runner's wait ended; it takes back the start_after it set")

    def clear_start_after(self, task_id: str, current: str) -> str:
        """Take back ``current`` (a start_after read from the task) outside a wait's own end, for a
        task that goes back to the queue. Never raises: what could not be cleared is kept for the
        per-tick retry and the caller carries on. Returns the outcome of ``_unschedule``."""
        outcome, held = self._unschedule(task_id, [current])
        if outcome not in ("cleared", "gone"):
            if self.store is not None:
                self._remember_unschedule(task_id, [held or current])
            log.warning("%s: unschedule #%s: %s%s", self.lane, task_id[:8], outcome,
                        "; retried on later ticks" if self.store is not None else "")
        return outcome

    def _remember_unschedule(self, task_id: str, scheduled: list[str]) -> None:
        self.store.mark(self.lane, task_id, UNSCHEDULE_MARK,
                        json.dumps({"scheduled": scheduled, "since": iso()}))
        self._unschedule_due[task_id] = (now() + UNSCHEDULE_RETRY, UNSCHEDULE_RETRY)

    def _forget_unschedule(self, task_id: str) -> None:
        self._unschedule_due.pop(task_id, None)
        mark = self.store.mark(self.lane, task_id, UNSCHEDULE_MARK) if self.store is not None else None
        if mark and mark[0]:
            self.store.mark(self.lane, task_id, UNSCHEDULE_MARK, "")

    def retry_unschedules(self) -> None:
        """Pending clears of schedules this lane set: retried with a growing pause until one
        lands or the task no longer carries that instant. Logged once per outcome."""
        if not self.enabled or self.store is None:
            return
        budget = UNSCHEDULE_PER_PASS
        for task_id, value in sorted(self.store.marked(self.lane, UNSCHEDULE_MARK).items()):
            if budget <= 0:
                break
            due = self._unschedule_due.get(task_id)
            if due and now() < due[0]:
                continue
            try:
                scheduled = json.loads(value).get("scheduled") or []
            except (ValueError, AttributeError):
                scheduled = []
            scheduled = [scheduled] if isinstance(scheduled, str) else [str(v) for v in scheduled]
            # a schedule that has passed holds nothing any more: stop retrying it
            scheduled = [v for v in scheduled if (parse_time(v) or now()) > now()]
            if not scheduled:
                self._forget_unschedule(task_id)
                continue
            budget -= 1
            outcome, _ = self._unschedule(task_id, scheduled)
            if outcome == "cleared":
                log.info("%s: unschedule #%s: cleared on retry", self.lane, task_id[:8])
            elif outcome == "gone":
                log.info("%s: unschedule #%s: start_after changed meanwhile; left alone",
                         self.lane, task_id[:8])
            else:
                step = min((due[1] if due else UNSCHEDULE_RETRY) * 2, UNSCHEDULE_RETRY_MAX)
                self._unschedule_due[task_id] = (now() + step, step)
                continue                       # already logged when it was first kept
            self._forget_unschedule(task_id)

    def reconcile_orphan_schedule(self, task: dict) -> bool:
        """A task held by a start_after that a closed attempt (any lane of this runner) set
        and no open attempt owns: that schedule outlived its wait. Clear it; True when the
        task is no longer held by it. At most once per ``ORPHAN_CHECK`` per task."""
        if not self.enabled or self.store is None:
            return False
        current = task.get("start_after")
        until = parse_time(current)
        task_id = task.get("id") or ""
        if not task_id or until is None or until <= now():
            return False
        checked = self._orphan_checked.get(task_id)
        if checked and now() - checked < ORPHAN_CHECK:
            return False
        self._orphan_checked[task_id] = now()
        # every attempt on the task, however old: a time wait may schedule days ahead, so an
        # attempt closed long ago can still hold the task (the task_id index keeps this cheap)
        attempts = self.store.on_task(task_id)

        def schedules(a: Attempt) -> list[str]:
            w = a.wait or {}
            return [v for v in (w.get("scheduled"), w.get("scheduled_maybe")) if v]
        if any(a.state in OPEN for a in attempts):
            return False                       # an open attempt owns the task's waits
        mine = [v for a in attempts for v in schedules(a) if same_instant(current, v)]
        if not mine:
            return False
        outcome, _ = self._unschedule(task_id, mine)
        if outcome == "cleared":
            log.info("%s: #%s start_after %s left by an ended wait; cleared",
                     self.lane, task_id[:8], current)
            return True
        if outcome != "gone":
            log.warning("%s: orphan unschedule #%s: %s", self.lane, task_id[:8], outcome)
        return False

    def _edge_denied(self, task_id: str, what: str) -> bool:
        key = f"{EDGE_DENIED_MARK}:{what}"
        if (task_id, key) in self._edges_denied:
            return True
        if self.store is None:
            return False
        if self.store.mark(self.lane, task_id, key):
            self._edges_denied.add((task_id, key))
            return True
        return False

    def _deny_edge(self, task_id: str, what: str) -> None:
        """Remember that Mesh answered 403 for this edge operation: the same key is refused the
        same way on every later tick, so asking again only fills the log."""
        key = f"{EDGE_DENIED_MARK}:{what}"
        self._edges_denied.add((task_id, key))
        if self.store is not None:
            self.store.mark(self.lane, task_id, key, "403")

    def _edge_still_ours(self, a: Attempt, wait: dict) -> bool:
        """True while the edge this WAIT created still has the shape it was created with
        (blocks / task_done). A gone edge needs no delete; one an operator narrowed
        (task_done -> deployed...) or retyped is theirs now: cleanup neither deletes nor downgrades it.
        An unreadable edge set raises MeshError: the edge stays recorded and cleanup is retried."""
        outgoing = card_wait_reconcile.readable_outgoing(self.mesh.dependencies(a.task_id), a.task_id)
        if outgoing is None:
            raise MeshError(502, "edge set unreadable")
        for e in outgoing:
            if e.get("id") == wait["edge"]:
                if (e.get("dependency_type") == "blocks"
                        and e.get("condition") == (wait.get("edge_condition") or "task_done")):
                    return True
                log.info("%s: edge #%s %s is %s/%s now (changed by its owner); left in place",
                         self.lane, a.task8, wait["edge"], e.get("dependency_type"), e.get("condition"))
                return False
        return False

    def _add_edge(self, a: Attempt, wait: dict) -> None:
        if self._edge_denied(a.task_id, "add:" + str(wait.get("target"))):
            return
        try:
            target = self.mesh.task_by_short(wait["target"])
            target_id = target.get("id")
            if not target_id or target_id == a.task_id:
                return
            # Never recreate or take over what an operator shaped: an existing edge to this
            # target (any type/condition, e.g. narrowed task_done -> deployed) stays as it is and
            # is not ours to clean up; a link a user deleted for this target is not restored by
            # a re-declared WAIT. An unreadable edge set creates nothing (fail closed).
            outgoing = card_wait_reconcile.readable_outgoing(self.mesh.dependencies(a.task_id), a.task_id)
            if outgoing is None:
                return
            existing = [e for e in outgoing if e["depends_on_task_id"] == target_id]
            if existing:
                # Read-only binding: the wait may watch that one blocking edge's typed verdict
                # (observe_edge) but it owns nothing, so cleanup never deletes or downgrades it.
                blocking = [e for e in existing if e["dependency_type"] == "blocks"]
                if len(blocking) == 1 and len(existing) == 1:
                    wait["observe_edge"] = blocking[0]["id"]
                return
            if self.store is not None and self.store.mark(self.lane, a.task_id, f"{EDGE_REMOVED_MARK}:{wait.get('target')}"):
                log.info("%s: edge #%s → #%s was removed by a user; not restored", self.lane, a.task8, wait.get("target"))
                return
            # a card wait with a typed target_condition names what it waits for; the plain form stays task_done
            condition = str(wait.get("card_condition") or "")
            edge = (self.mesh.add_dependency(a.task_id, target_id, condition) if condition
                    else self.mesh.add_dependency(a.task_id, target_id))
            edge_id = edge.get("id") or (edge.get("dependency") or {}).get("id")
            if edge_id:
                wait["edge"] = edge_id
                if condition:
                    wait["edge_condition"] = condition
        except MeshError as e:
            if e.status == 403:            # a card of a workspace this key cannot see: asked once
                self._deny_edge(a.task_id, "add:" + str(wait.get("target")))
                log.warning("%s: edge #%s → #%s: %s; not retried", self.lane, a.task8, wait.get("target"), e)
            elif e.status != 409:          # an edge that already exists is fine
                log.warning("%s: edge #%s → #%s: %s", self.lane, a.task8, wait.get("target"), e)

    def _resolve_agent(self, handle: str, me: dict) -> dict | None:
        key = (handle or "").casefold()
        stale = self._agents_at is None or now() - self._agents_at > dt.timedelta(minutes=10)
        if key and key not in self._agents and stale:
            # a MeshError propagates: the caller retries instead of treating the
            # addressee as a human
            fresh = {str(ag.get("name", "")).casefold(): ag
                     for ag in self.mesh.workspace_agents(me.get("workspace_id", ""))}
            self._agents, self._agents_at = fresh, now()
        return self._agents.get(key)

    def agent_names(self, me: dict) -> list[str]:
        """Names of the workspace agents (cached for 10 minutes like ``_resolve_agent``).
        A lookup failure returns what is cached: the caller only uses it for a hint."""
        stale = self._agents_at is None or now() - self._agents_at > dt.timedelta(minutes=10)
        if stale:
            try:
                self._agents = {str(ag.get("name", "")).casefold(): ag
                                for ag in self.mesh.workspace_agents(me.get("workspace_id", ""))}
                self._agents_at = now()
            except Exception:
                pass
        return [str(ag.get("name") or "") for ag in self._agents.values() if ag.get("name")]

    def agent_name_by_id(self, agent_id: str, me: dict) -> str:
        """The name of a workspace agent given its id, '' when unknown (a lookup
        failure is not an error here: the caller falls back to the id)."""
        def find() -> str:
            return next((str(ag.get("name") or "") for ag in self._agents.values()
                         if ag.get("id") == agent_id), "")
        if find():
            return find()
        try:
            self._agents = {str(ag.get("name", "")).casefold(): ag
                            for ag in self.mesh.workspace_agents(me.get("workspace_id", ""))}
            self._agents_at = now()
        except Exception:
            return ""
        return find()

    def pending_cleanup(self, wait: dict) -> bool:
        return bool(wait.get("scheduled") or wait.get("scheduled_maybe") or wait.get("edge")
                    or wait.get("request"))

    @staticmethod
    def _decision_key(task_id: str, recipient: str, wait: dict) -> str:
        question = " ".join((wait.get("note") or "").split())
        identity = [task_id, recipient, question]
        return hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()

    def _retain_legacy_decision(self, a: Attempt, wait: dict) -> None:
        if not wait.get("request") or self._decision(a.task_id) or self.store is None:
            return
        try:
            recipient = wait.get("recipient_id")
            if not recipient:
                recipient = (self._resolve_agent(wait.get("handle") or "", self.mesh.me()) or {}).get("id")
            if not recipient:
                return                     # uncertainty: leave the existing wait receipt intact
            record = {"key": self._decision_key(a.task_id, recipient, wait), "recipient": recipient,
                      "lane": self.lane, "state": "open", "request": dict(wait["request"])}
            self._save_decision(a.task_id, record)
        except MeshError as e:
            log.warning("%s: retain decision #%s: HTTP %s", self.lane, a.task8, e.status)

    def _decision(self, task_id: str) -> dict:
        mark = self.store.mark(DECISION_OWNER, task_id, DECISION_MARK) if self.store else None
        return json.loads(mark[0]) if mark and mark[0] else {}

    def _save_decision(self, task_id: str, record: dict) -> None:
        if self.store is not None:
            value = json.dumps(record, sort_keys=True) if record and record.get("state") != "closed" else ""
            self.store.mark(DECISION_OWNER, task_id, DECISION_MARK, value)
            if not value:
                self._decision_checked.pop(task_id, None)
            self._remember_request(task_id, record)

    # ------------------------------------------------------------- request reuse
    @staticmethod
    def _question_text(wait: dict) -> str:
        """A question as compared for a verbatim repeat: whitespace collapsed, case-folded."""
        return " ".join((wait.get("note") or "").split()).casefold()

    def _same_question(self, task_id: str, asked: str | None, note: str) -> bool:
        """``note`` asks what ``asked`` (a remembered question) asked: verbatim up to whitespace and
        case, or, with ``identical_no_reopen``, up to ``normalized_question``."""
        if asked is None:
            return False
        if self.identical_no_reopen:
            return normalized_question(asked, task_id) == normalized_question(note, task_id)
        return asked == self._question_text({"note": note})

    def _asked_on(self, task_id: str, entry: dict | None, note: str) -> bool:
        """``note`` repeats a question already put on the request ``entry`` stands for: its current
        question or any question answered on it before (``answered``), not only the last one."""
        entry = entry or {}
        return any(self._same_question(task_id, q, note)
                   for q in (entry.get("question"), *(entry.get("answered") or ())) if q is not None)

    @staticmethod
    def _answered_history(reuse: dict, closed_done: bool) -> list[str]:
        """The questions answered on a request: those it carried before, plus its current one when
        the request was closed as done (an answer, not a cancellation). Newest last. Never truncated:
        an evicted question could reopen its request again; the reuse window bounds the history,
        since the whole entry expires with ``decision_reuse_hours``."""
        history = [q for q in (reuse.get("answered") or ()) if q]
        current = reuse.get("question")
        if closed_done and current and current not in history:
            history.append(current)
        return history

    def _identical(self, task_id: str, entry: dict | None, wait: dict) -> bool:
        """``wait`` repeats a question of the request ``entry`` stands for, from the same asker:
        attached to that request as it is (``identical_no_reopen``)."""
        entry = entry or {}
        return (self.identical_no_reopen and entry.get("lane") in (None, "", self.lane)
                and self._asked_on(task_id, entry, wait.get("note") or ""))

    def _recent(self, task_id: str) -> dict:
        """Requests filed for this source, by recipient id (kept after they close)."""
        mark = self.store.mark(DECISION_OWNER, task_id, DECISION_RECENT_MARK) if self.store else None
        try:
            recent = json.loads(mark[0]) if mark and mark[0] else {}
        except ValueError:
            return {}
        return recent if isinstance(recent, dict) else {}

    @staticmethod
    def _filed_at(record: dict, remembered: dict | None, default: str | None = None) -> str:
        """When a record's request was filed: its own stamp, its create's start, what was
        remembered for the same card, else ``default`` (now)."""
        return (record.get("filed_at") or record.get("started_at") or (remembered or {}).get("filed_at")
                or (iso() if default is None else default))

    def _fresh(self, entry: dict | None) -> bool:
        """The request was filed within the reuse window."""
        filed = parse_time((entry or {}).get("filed_at"))
        return bool(self.reuse_window and filed and now() - filed < self.reuse_window)

    def _remember_request(self, task_id: str, record: dict) -> None:
        """Keep the request a record is bound to, so a later question from the same source to
        the same recipient finds it after it closed (and after a restart)."""
        if self.reuse_window is None or self.store is None:
            return
        req, recipient = (record or {}).get("request"), (record or {}).get("recipient")
        if not isinstance(req, dict) or not req.get("id") or not recipient:
            return
        recent = self._recent(task_id)
        prev = recent.get(recipient) or {}
        same_card = (prev.get("request") or {}).get("id") == req["id"]
        entry = {"request": {"id": req["id"], "project_id": req.get("project_id")},
                 "filed_at": self._filed_at(record, prev if same_card else None),
                 "question": record.get("question"), "asked_at": record.get("asked_at"),
                 "lane": record.get("lane")}
        if record.get("answered"):
            entry["answered"] = list(record["answered"])
        # expired entries go, the current recipient's included: an expired request is never reused
        kept = {r: e for r, e in {**recent, recipient: entry}.items() if self._fresh(e)}
        if kept != recent:
            self.store.mark(DECISION_OWNER, task_id, DECISION_RECENT_MARK,
                            json.dumps(kept, sort_keys=True) if kept else "")

    def _forget_request(self, task_id: str, recipient: str) -> bool:
        """Drop a remembered request that cannot be reused (gone or reassigned). False, so the
        caller files a new one."""
        recent = self._recent(task_id)
        if recent.pop(recipient, None) is not None:
            self.store.mark(DECISION_OWNER, task_id, DECISION_RECENT_MARK,
                            json.dumps(recent, sort_keys=True) if recent else "")
        return False

    def _reusable(self, task_id: str, recipient: str, active: dict) -> dict | None:
        """The request this source last filed for ``recipient`` within the reuse window:
        the open record's when it is for this recipient, else the remembered one."""
        if self.reuse_window is None or self.store is None:
            return None
        if (active and active.get("state") == "open" and active.get("recipient") == recipient
                and (active.get("request") or {}).get("id")):
            remembered = self._recent(task_id).get(recipient) or {}
            same_card = (remembered.get("request") or {}).get("id") == active["request"]["id"]
            entry = dict(active, filed_at=self._filed_at(active, remembered if same_card else None, ""))
            return entry if self._fresh(entry) else None
        entry = self._recent(task_id).get(recipient)
        return dict(entry) if entry and (entry.get("request") or {}).get("id") and self._fresh(entry) else None

    def _end_decision(self, task_id: str, record: dict, why: str) -> bool:
        if record.get("state") == "closed":
            return True
        if not record.get("request"):
            # A create may have succeeded before its response/local receipt. Never
            # create another question until reconcile_decisions adopts or repeats it.
            return False
        record.update(state="closing", close_reason=why)
        self._save_decision(task_id, record)
        pending = {"request": dict(record["request"])}
        self._close_decision(pending, why, task_id=task_id, record=record)
        if pending.get("request"):
            return False
        record["state"] = "closed"
        self._save_decision(task_id, record)
        return True

    def reconcile_decisions(self) -> None:
        """Bounded retries survive a failed close and an attempt disappearing.

        Each owner checks at most ten retained questions per pass, no more often
        than the runtime-state heartbeat. Reassignment alone never closes one.
        """
        if not self.enabled or self.store is None:
            return
        records = {task_id: json.loads(value) for task_id, value in
                   self.store.marked(DECISION_OWNER, DECISION_MARK).items()}
        keys = sorted(task_id for task_id, record in records.items()
                      if record.get("lane") == self.lane and record.get("state") in ("open", "closing", "creating"))
        start = next((i for i, key in enumerate(keys) if key > self._decision_cursor), 0)
        budget = 10
        for task_id in keys[start:] + keys[:start]:
            if budget <= 0:
                break
            self._decision_cursor = task_id
            previous = self._decision_checked.get(task_id)
            if previous and now() - previous < HEARTBEAT:
                continue
            self._decision_checked[task_id] = now()
            budget -= 1
            record = records[task_id]
            if record.get("state") == "creating":
                self._reconcile_creating(task_id, record)
                continue
            if record.get("state") == "closing":
                self._end_decision(task_id, record, record.get("close_reason") or "answered")
                continue
            try:
                task = self.mesh.task(task_id)
                task = task.get("task", task)
                if self.cats.category(task) in ("done", "cancelled"):
                    self._end_decision(task_id, record, "source task is terminal")
            except MeshError as e:
                log.warning("%s: decision terminal check #%s: HTTP %s", self.lane, task_id[:8], e.status)

    def _reconcile_creating(self, task_id: str, record: dict) -> None:
        """Settle a create whose outcome the runner never saw (timeout, 5xx, crash).

        A card carrying this create's marker is adopted. Only after the create window
        and two searches that both found nothing is the create repeated, once, and only
        while the lane still waits on the very question the record was made for.
        """
        try:
            source = self.mesh.task(task_id)
            source = source.get("task", source)
            project_id = record.get("project_id") or source.get("project_id")
            terminal = self.cats.category(source) in ("done", "cancelled")
            found = self._find_decision_card(project_id, record) if project_id else None
        except MeshError as e:
            log.warning("%s: decision reconcile #%s: HTTP %s", self.lane, task_id[:8], e.status)
            return
        if found:
            why = "source task is terminal" if terminal else ""
            if not terminal:
                try:
                    if self._question_still_awaited(task_id, record) is None:
                        why = "lane no longer waits on this question"
                except MeshError as e:
                    log.warning("%s: decision reconcile #%s: HTTP %s", self.lane, task_id[:8], e.status)
                    return
            record.update(state="open", request={"id": found["id"], "project_id": project_id},
                          asked_at=_asked_at(found, record.get("asked_at") or record.get("started_at") or iso()))
            for stale in ("absent", "reported"):
                record.pop(stale, None)
            self._save_decision(task_id, record)
            if why:
                # A card nobody waits for is closed, never bound to the lane.
                log.info("%s: decision create #%s found card #%s but %s: closing it, key=%s",
                         self.lane, task_id[:8], found["id"][:8], why, record["key"])
                self._end_decision(task_id, record, why)
            else:
                log.info("%s: decision create #%s adopted card #%s, key=%s", self.lane, task_id[:8],
                         found["id"][:8], record["key"])
            return
        if terminal:
            self._save_decision(task_id, {})
            log.info("%s: decision create #%s dropped: source task is terminal, no card, key=%s",
                     self.lane, task_id[:8], record["key"])
            return
        record["absent"] = int(record.get("absent") or 0) + 1
        started = parse_time(record.get("started_at"))     # legacy records carry none: long past
        if record["absent"] < 2 or (started and now() - started < CREATE_WINDOW):
            self._save_decision(task_id, record)
            return
        if record.get("retried"):
            try:
                if self._question_still_awaited(task_id, record) is None:
                    self._save_decision(task_id, {})    # nobody waits for it any more
                    log.info("%s: decision create #%s dropped: retry spent and the lane moved on, key=%s",
                             self.lane, task_id[:8], record["key"])
                    return
            except MeshError as e:
                log.warning("%s: decision reconcile #%s: HTTP %s", self.lane, task_id[:8], e.status)
                return
            if not record.get("exhausted"):
                record["exhausted"] = True
                log.error("%s: decision create #%s: no card after the retry; operator reconciliation "
                          "required, key=%s", self.lane, task_id[:8], record["key"])
            self._save_decision(task_id, record)
            return
        self._retry_create(task_id, record, source, project_id)

    def _question_still_awaited(self, task_id: str, record: dict) -> tuple[Attempt, dict] | None:
        """The waiting attempt and recipient when this lane still waits on the very
        question (same key, same recipient) the record was made for, else None.
        A MeshError propagates: uncertainty is neither yes nor no."""
        waiting = next((x for x in self.store.waiting_on_task(task_id)
                        if x.task_id == task_id and x.agent == self.lane
                        and x.wait.get("kind") == "decision" and x.wait.get("handle")), None)
        if not waiting:
            return None
        addressee = self._resolve_agent(waiting.wait["handle"], self.mesh.me())
        if (not addressee or addressee.get("id") != record.get("recipient")
                or self._decision_key(task_id, addressee["id"], waiting.wait) != record["key"]):
            return None
        return waiting, addressee

    def _find_decision_card(self, project_id: str, record: dict) -> dict | None:
        """The card this create produced, oldest first. A record made before create
        markers existed matches by key, and only while the card is still open: the key
        repeats for every cycle of the same question, so a closed match is an old one."""
        create_id = record.get("create_id")
        token = f"decision-create:{create_id}" if create_id else f"decision-key:{record['key']}"
        marker = f"[runner {token}]"
        hits = []
        for item in self.mesh.find_tasks(project_id, token):
            card = self.mesh.task(item["id"])
            card = card.get("task", card)
            if marker not in (card.get("description") or ""):
                continue
            if not create_id and self.cats.category(card) in ("done", "cancelled"):
                continue
            hits.append(card)
        hits.sort(key=lambda c: c.get("created_at") or "")
        return hits[0] if hits else None

    def _retry_create(self, task_id: str, record: dict, source: dict, project_id: str) -> None:
        try:
            awaited = self._question_still_awaited(task_id, record)
        except MeshError as e:
            log.warning("%s: decision reconcile #%s: HTTP %s", self.lane, task_id[:8], e.status)
            return
        if awaited is None:
            self._save_decision(task_id, {})        # the lane no longer waits on this question
            log.info("%s: decision create #%s dropped: no card and the lane waits on another question, "
                     "key=%s", self.lane, task_id[:8], record["key"])
            return
        waiting, addressee = awaited
        create_id = record.get("create_id") or uuid.uuid4().hex
        try:
            # Every preflight read comes first: a transient failure here leaves the
            # record untouched, so the next pass still has its one retry.
            fields = self._decision_fields(waiting, dict(source, project_id=project_id), waiting.wait,
                                           addressee, record["key"], create_id)
        except MeshError as e:
            log.warning("%s: decision create retry #%s: preflight HTTP %s", self.lane, task_id[:8], e.status)
            return
        record["create_id"] = create_id
        record["retried"] = True                    # persisted right before the POST: at most one retry
        self._save_decision(task_id, record)
        asking = iso()
        try:
            created = self.mesh.create_task(project_id, fields)
            if not created.get("id"):
                raise MeshError(0, "missing task identity")
        except MeshError as e:
            # One POST, whatever the answer: `retried` stays set, nothing posts again.
            if e.status in (400, 401, 403, 404, 422):
                record["exhausted"] = True
                self._save_decision(task_id, record)
                log.error("%s: decision re-create rejected HTTP %s; operator reconciliation required, "
                          "key=%s", self.lane, e.status, record["key"])
            else:
                log.warning("%s: decision create retry #%s: HTTP %s", self.lane, task_id[:8], e.status)
            return
        record.update(state="open", request={"id": created["id"], "project_id": project_id},
                      asked_at=_asked_at(created, asking))
        for stale in ("absent", "reported"):
            record.pop(stale, None)
        self._save_decision(task_id, record)
        log.info("%s: decision create #%s repeated once after no card was found: #%s, key=%s",
                 self.lane, task_id[:8], created["id"][:8], record["key"])

    def close_terminal_decision(self, a: Attempt) -> None:
        """Source-task termination closes even a question retained by an older attempt.

        Infrastructure failures and reassignment do not answer a product question.
        An unavailable source card is uncertainty, never permission to close it.
        """
        if not self.enabled or not self.store:
            return
        active = self._decision(a.task_id)
        if not active or active.get("state") == "closed":
            return
        try:
            task = self.mesh.task(a.task_id)
            task = task.get("task", task)
            if self.cats.category(task) in ("done", "cancelled"):
                self._end_decision(a.task_id, active, "source task is terminal")
        except MeshError as e:
            log.warning("%s: decision terminal check #%s: HTTP %s", self.lane, a.task8, e.status)

    def _file_decision(self, a: Attempt, task: dict, wait: dict, me: dict) -> None:
        try:
            # A retained question belongs to its source, not an old attempt's
            # cached ownership. Re-check permission before reuse or supersession.
            current = self.mesh.task(a.task_id)
            current = current.get("task", current)
            assignee = current.get("assignee_id") or (current.get("assignee") or {}).get("id")
            if (assignee != me.get("id")
                    or self.cats.category(current) in ("done", "cancelled")):
                return
            task = current
            addressee = self._resolve_agent(wait["handle"], me)
        except MeshError as e:
            wait["fx"] = False
            log.warning("%s: decision request #%s: lookup HTTP %s", self.lane, a.task8, e.status)
            return
        if not addressee or addressee.get("id") in (me.get("id"), None):
            return                          # a person or ourselves: the comment is the ask
        wait["recipient_id"] = addressee["id"]
        note = (wait.get("note") or "").strip()
        key = self._decision_key(a.task_id, addressee["id"], wait)
        active = self._decision(a.task_id)
        if not active and not wait.get("request") and self._mirror(a, addressee, wait, me):
            return
        if not active and wait.get("request"):
            # Upgrade an existing parked wait without filing a second request.
            active = {"key": key, "recipient": addressee["id"], "lane": self.lane, "state": "open",
                      "request": wait["request"]}
            self._save_decision(a.task_id, active)
        if active and active.get("state") != "closed":
            if (active.get("key") == key and active.get("attached_closed") and wait.get("rechecks")
                    and active.get("request")):
                # attached to a closed request, handed back by the re-check and asked again: a request
                # closed as done answered it and stays closed (``_reuse_decision`` attaches again); one
                # closed without an answer (cancelled) is reopened and the addressee asked once more
                reuse = self._reusable(a.task_id, addressee["id"], active) or dict(active)
                if self._reuse_decision(a, task, wait, addressee, key, reuse, ask=True):
                    return
            elif active.get("key") == key:
                active["lane"] = self.lane
                self._save_decision(a.task_id, active)
                if active.get("request"):
                    wait["request"] = dict(active["request"])
                if active.get("attached_closed"):
                    wait["attached_closed"] = active["attached_closed"]
                return                      # pending create is fail-closed too
            if active.get("recipient") == addressee["id"]:
                # a changed question to the same recipient is asked on the request it already has
                reuse = self._reusable(a.task_id, addressee["id"], active)
                if reuse is None and active.get("request") and self._identical(a.task_id, active, wait):
                    reuse = dict(active)    # the same question is attached to its open request, however old
                if reuse and self._reuse_decision(a, task, wait, addressee, key, reuse):
                    return
            if not self._end_decision(a.task_id, active, "superseded by a changed question or recipient"):
                wait["fx"] = False
                return
        reuse = self._reusable(a.task_id, addressee["id"], {})
        if reuse and self._reuse_decision(a, task, wait, addressee, key, reuse):
            return
        if self.store is None:
            # Durable identity is required for creation; no restart-unsafe fallback.
            wait["fx"] = False
            return
        posting = False
        try:
            create_id = uuid.uuid4().hex
            fields = self._decision_fields(a, task, wait, addressee, key, create_id)
            asking = iso()
            active = {"key": key, "recipient": addressee["id"], "lane": self.lane, "state": "creating",
                      "create_id": create_id, "project_id": task["project_id"], "started_at": iso(),
                      "filed_at": asking, "question": self._question_text(wait), "asked_at": asking}
            self._save_decision(a.task_id, active)  # before POST: crash cannot trigger another create
            posting = True
            created = self.mesh.create_task(task["project_id"], fields)
            if not created.get("id"):
                raise MeshError(0, "missing task identity")
            active.update(state="open", request={"id": created["id"], "project_id": task["project_id"]},
                          asked_at=_asked_at(created, asking))
            self._save_decision(a.task_id, active)
            wait["request"] = dict(active["request"])
            log.info("%s: #%s decision request → %s: #%s", self.lane, a.task8, addressee.get("name"),
                     created["id"][:8])
        except MeshError as e:
            # Rejection before creation is retryable. A timeout/server failure
            # may have committed: retain 'creating'; reconcile_decisions adopts
            # the card carrying create_id, or retries once after the create window.
            if not posting or e.status in (400, 401, 403, 404, 422):
                if active.get("state") == "creating":
                    self._save_decision(a.task_id, {})
                wait["fx"] = False
            else:
                log.error("%s: decision create #%s ambiguous; reconciling by create_id=%s key=%s",
                          self.lane, a.task8, active.get("create_id"), key)
                active["reported"] = True
                self._save_decision(a.task_id, active)
            log.warning("%s: decision request #%s: HTTP %s", self.lane, a.task8, e.status)

    @staticmethod
    def _card_refs(note: str, own: str) -> set[str]:
        """Short ids of the cards a question names, the asking task's own excluded."""
        return set(re.findall(r"(?<![\w-])#([0-9a-f]{8})\b", (note or "").casefold())) - {own[:8].casefold()}

    def _counter_request(self, a: Attempt, addressee: dict, wait: dict, me: dict) -> tuple[str, dict] | None:
        """An open request ``addressee`` put to this lane that asks the same thing: the same question
        (``normalized_question``) or the same target card. (source task id, its record) or None."""
        if self.store is None:
            return None
        note = wait.get("note") or ""
        question = normalized_question(note, a.task_id)
        refs = self._card_refs(note, a.task_id)
        for source, value in sorted(self.store.marked(DECISION_OWNER, DECISION_MARK).items()):
            try:
                record = json.loads(value)
            except ValueError:
                continue
            if (source == a.task_id or record.get("state") != "open" or record.get("recipient") != me.get("id")
                    or str(record.get("lane") or "").casefold() != str(addressee.get("name") or "").casefold()
                    or not (record.get("request") or {}).get("id")):
                continue
            theirs = record.get("question") or ""
            if (question and normalized_question(theirs, source) == question) or (
                    refs & self._card_refs(theirs, source)):
                return source, record
        return None

    def _mirror(self, a: Attempt, addressee: dict, wait: dict, me: dict) -> bool:
        """The addressee already asked this lane the same thing: no request is filed. Both waits
        move to a ``card:`` target (the card both questions name, else the addressee's request,
        which this lane answers on its own session; the addressee's wait moves there too). True when this wait was converted."""
        found = self._counter_request(a, addressee, wait, me)
        if not found:
            return False
        source, record = found
        shared = sorted(self._card_refs(wait.get("note") or "", a.task_id)
                        & self._card_refs(record.get("question") or "", source))
        target = shared[0] if shared else record["request"]["id"][:8]
        wait.update(kind="card", target=target, mirror=source[:8])
        wait.pop("handle", None)
        self._add_edge(a, wait)
        for other in self.store.waiting_on_task(source):
            w = dict(other.wait or {})
            if other.task_id == source and w.get("kind") == "decision" and not w.get("mirror"):
                w.update(kind="card", target=target, mirror=a.task8)
                w.pop("handle", None)
                self._add_edge(other, w)
                self.store.update(other, wait=w)
        if self.store is not None:
            self.store.event(a.id, "decision_mirror", f"#{source[:8]} -> card:#{target}")
        log.info("%s: #%s decision to %s mirrors #%s: both wait on card:#%s, no request filed", self.lane,
                 a.task8, addressee.get("name"), source[:8], target)
        return True

    def _retire_decision_wait(self, request_id: str, source_id: str, recipient_id: str) -> None:
        """Before reopening a request, retire only its previous typed wait on this source.

        The addressee may run on another host: Mesh's durable receipt is the
        authority, never the local store or just a matching pair of cards.
        """
        edges = card_wait_reconcile.readable_outgoing(self.mesh.dependencies(request_id), request_id)
        if edges is None:
            log.warning("decision request #%s: preserve dependencies: unreadable edge set", request_id[:8])
            return
        for edge in edges:
            if edge["depends_on_task_id"] != source_id or edge["dependency_type"] != "blocks":
                continue
            owner = edge.get("owner_wait_id")
            if not owner or edge.get("owner_agent_id") != recipient_id:
                log.info("decision request #%s: preserve edge %s: no matching wait owner", request_id[:8],
                         edge["id"])
                continue
            try:
                receipt = self.mesh.wait_receipt(request_id, owner)
            except MeshError as e:
                if e.status not in (404, 410):
                    raise
                log.warning("decision request #%s: preserve edge %s: wait receipt unavailable",
                            request_id[:8], edge["id"])
                continue
            owned = card_wait_reconcile.owned_decision_wait(edge, receipt, request_id, source_id, recipient_id)
            if owned is None:
                log.info("decision request #%s: preserve edge %s: receipt does not prove ownership",
                         request_id[:8], edge["id"])
                continue
            try:
                self.mesh.delete_wait_dependency(request_id, *owned)
            except MeshError as e:
                if e.status not in (404, 409, 410):
                    raise
                log.info("decision request #%s: preserve changed/absent edge %s: HTTP %s",
                         request_id[:8], edge["id"], e.status)
                continue
            log.info("decision request #%s: retired previous wait edge %s", request_id[:8], edge["id"])

    def _reuse_decision(self, a: Attempt, task: dict, wait: dict, addressee: dict, key: str,
                        reuse: dict, ask: bool = False) -> bool:
        """Ask a question on the request this source already filed for ``addressee``: reopen it
        when closed (the assignee is kept), append the question as a comment, bind it to the
        wait. True when handled (done, or failed and left for a retry: ``fx`` cleared); False
        when that request cannot be reused (gone, reassigned, no todo status): file a new one.
        The same question is attached to it as it is (``_attach_identical``) unless ``ask``."""
        req = dict(reuse["request"])
        identical = self._identical(a.task_id, reuse, wait)
        try:
            card = self.mesh.task(req["id"])
            card = card.get("task", card) if isinstance(card, dict) else {}
            project_id = req.get("project_id") or card.get("project_id")
            assignee = card.get("assignee_id") or (card.get("assignee") or {}).get("id")
            if not project_id or assignee != addressee["id"]:
                return self._forget_request(a.task_id, addressee["id"])   # someone else holds it now
            req["project_id"] = project_id
            category = self.cats.category(card)
            reopened = category in ("done", "cancelled")
            answered = self._answered_history(reuse, category == "done")
            if identical and not (ask and category == "cancelled"):
                # an answered question is never asked again: no move, no comment on its request
                return self._attach_identical(a, wait, addressee, key, dict(reuse, answered=answered), req,
                                              closed=reopened, answered=category == "done")
            if reopened:
                todo = next((s["id"] for s in self.mesh.statuses(project_id) if s.get("category") == "todo"),
                            None)
                if not todo:
                    return False
                self._retire_decision_wait(req["id"], a.task_id, addressee["id"])
                self.mesh.move(req["id"], todo)     # idempotent: a retry after a failed comment repeats it
            asking = iso()
            posted = self.mesh.add_comment(req["id"], self._decision_ask(a, task, wait, addressee, again=True),
                                           key=f"decision-reask|{a.id}|{a.turns}")
        except MeshError as e:
            if e.status in (404, 410):
                return self._forget_request(a.task_id, addressee["id"])
            wait["fx"] = False              # adopt() retries on a later tick
            log.warning("%s: decision request #%s: reuse #%s: HTTP %s", self.lane, a.task8, req["id"][:8],
                        e.status)
            return True
        record = {"key": key, "recipient": addressee["id"], "lane": self.lane, "state": "open",
                  "request": req, "filed_at": reuse.get("filed_at"), "question": self._question_text(wait),
                  "asked_at": _asked_at(posted, asking), "reused": int(reuse.get("reused") or 0) + 1}
        if reuse.get("create_id"):
            record["create_id"] = reuse["create_id"]
        if answered:
            record["answered"] = answered
        self._save_decision(a.task_id, record)
        wait["request"] = dict(req)
        log.info("%s: #%s decision request → %s: #%s reused%s", self.lane, a.task8, addressee.get("name"),
                 req["id"][:8], " (reopened)" if reopened else "")
        return True

    def _attach_identical(self, a: Attempt, wait: dict, addressee: dict, key: str, reuse: dict, req: dict,
                          closed: bool, answered: bool = False) -> bool:
        """Bind a repeated question to the request that already carries it: no move, no comment.
        Reopening a request or commenting on it starts a new session for the addressee (measured:
        the same question minutes apart, each costing the addressee a fresh start to re-read the
        source and answer as before). The asker waits on the answer like any decision wait."""
        record = {"key": key, "recipient": addressee["id"], "lane": self.lane, "state": "open",
                  "request": req, "filed_at": reuse.get("filed_at"), "question": reuse.get("question"),
                  "asked_at": reuse.get("asked_at") or iso(), "reused": int(reuse.get("reused") or 0),
                  "attached": int(reuse.get("attached") or 0) + 1}
        if reuse.get("create_id"):
            record["create_id"] = reuse["create_id"]
        if reuse.get("answered"):
            record["answered"] = list(reuse["answered"])
        if closed:
            # nobody is asked again: a bounded re-check hands the question back to the asker
            # instead of a wait on a closed request that only a stray comment would end; the same
            # question declared after that re-check asks the addressee again only when the request
            # was closed without an answer (cancelled): an answered one is never reopened for it
            record["attached_closed"] = req["id"][:8]
            if answered:
                record["attached_answered"] = True
        self._save_decision(a.task_id, record)
        wait["request"] = dict(req)
        if closed:
            wait["attached_closed"] = req["id"][:8]
            if answered:
                wait["attached_answered"] = True
        if self.store is not None:
            self.store.event(a.id, "decision_attached", f"#{req['id'][:8]} {'closed' if closed else 'open'}")
        log.info("%s: #%s decision request → %s: #%s attached: the same question, not %s", self.lane, a.task8,
                 addressee.get("name"), req["id"][:8], "reopened" if closed else "asked again")
        return True

    def _decision_ask(self, a: Attempt, task: dict, wait: dict, addressee: dict, again: bool = False) -> str:
        """The question as the addressee reads it: in a new request's body, or (``again``) in
        a comment on the request it already has."""
        note = (wait.get("note") or "").strip()
        head = (f"@{addressee.get('name')}, {self.lane} asks again on task #{a.task8} "
                f"«{task.get('title', '')}» ({a.task_id}).\n" if again else
                f"@{addressee.get('name')}, {self.lane} is waiting for your decision on task "
                f"#{a.task8} «{task.get('title', '')}» ({a.task_id}).\n")
        return (head
                + (f"It was put to @{wait['asked_handle']} and comes to you instead: {wait['redirected']}.\n"
                   if wait.get("asked_handle") else "") + "\n"
                f"Question (verbatim):\n> {wait.get('line') or note}\n\n"
                f"Answer with a comment ON TASK #{a.task8}: that comment resumes {self.lane}. "
                + (f"Start it with `Answer:` (or @-mention {self.lane}): a progress note without either "
                   f"does not resume {self.lane}. " if self.answer_marker else "")
                + f"Do not take #{a.task8} yourself. This request is closed automatically "
                f"once the answer arrives.")

    def _decision_fields(self, a: Attempt, task: dict, wait: dict, addressee: dict, key: str,
                         create_id: str) -> dict:
        """The decision card for a wait. ``create_id`` is a per-create marker in the
        description so a create whose response was lost can be found again; the key
        alone repeats for every question put to the same recipient."""
        note = (wait.get("note") or "").strip()
        todo = next((s["id"] for s in self.mesh.statuses(task["project_id"])
                     if s.get("category") == "todo"), None)
        short = note if len(note) <= 80 else note[:79].rstrip() + "…"
        body = (f"[runner decision-request:{a.id}]\n[runner decision-key:{key}]\n"
                f"[runner decision-create:{create_id}]\n" + self._decision_ask(a, task, wait, addressee))
        fields = {"title": f"[decision] {short or 'decision needed'} (#{a.task8})", "description": body,
                  "assignee_id": addressee["id"], "assignee_type": "agent", "priority": "high",
                  "labels": list(DECISION_LABELS)}
        # Scope uses the resolved identity, never a handle or the source lane.
        if addressee["id"] in self._decision_metadata_recipients:
            fields["labels"].append("runner:metadata-only")
        if todo:
            fields["status_id"] = todo
        return fields

    def _close_decision(self, wait: dict, why: str, *, task_id: str = "", record: dict | None = None) -> None:
        req = wait["request"]
        blocked = self._close_blocked_get(req["id"])
        card: dict = {}
        try:
            card = self.mesh.task(req["id"])
            card = card.get("task", card)
            if self.cats.category(card) not in ("done", "cancelled"):
                if blocked and not self._close_retry_due(blocked, card):
                    return              # still behind a human gate: no write until the card changes
                if card.get("checked_out_by"):
                    # The addressee's turn on the request is still running. Closing the card under its
                    # live checkout makes its next heartbeat fail and the turn is killed. Its
                    # own outcome moves the card; this close is tried again on a later pass.
                    return
                done = next((s["id"] for s in self.mesh.statuses(req["project_id"]) if s.get("category") == "done"),
                            None)
                if not done:
                    return
                # Without a durable legacy identity an optional comment cannot
                # be retried safely. Status closure still has an observable result.
                if record is not None and not record.get("close_comment_attempted"):
                    # Persist intent before POST: a crash or lost response must
                    # never repeat the comment. The move remains retryable.
                    # This is at-most-once attempt, not guaranteed delivery;
                    # the terminal card status is the authoritative receipt.
                    record["close_comment_attempted"] = True
                    self._save_decision(task_id, record)
                    self.mesh.add_comment(req["id"], f"[runner] closed: {why}", key=f"request-closed|{req['id']}")
                self.mesh.move(req["id"], done)
            wait.pop("request", None)
            if blocked:
                self._close_blocked_set(req["id"], None)
                log.info("%s: close decision request #%s: closed, human gate no longer blocks it (blocked since %s)",
                         self.lane, req["id"][:8], blocked.get("since") or "?")
        except MeshError as e:
            if e.status in (404, 410):
                wait.pop("request", None)
                if blocked:
                    self._close_blocked_set(req["id"], None)
            elif _human_gate_refusal(e):
                self._close_blocked_hold(req["id"], card, blocked, e)
            else:
                log.warning("%s: close decision request #%s: %s", self.lane, req["id"][:8], e)

    # ------------------------------------------------------------- gate-refused close
    def _close_blocked_get(self, request_id: str) -> dict:
        if self.store is None:
            return self._close_blocked.get(request_id) or {}
        mark = self.store.mark(DECISION_OWNER, request_id, CLOSE_BLOCKED_MARK)
        try:
            return json.loads(mark[0]) if mark and mark[0] else {}
        except ValueError:
            return {}

    def _close_blocked_set(self, request_id: str, value: dict | None) -> None:
        if self.store is None:
            if value:
                self._close_blocked[request_id] = value
            else:
                self._close_blocked.pop(request_id, None)
            return
        self.store.mark(DECISION_OWNER, request_id, CLOSE_BLOCKED_MARK,
                        json.dumps(value, sort_keys=True) if value else "")

    @staticmethod
    def _close_retry_due(blocked: dict, card: dict) -> bool:
        """A gate-refused close is tried again once the card changed (the gate may be
        answered or cleared), or after CLOSE_BLOCKED_RETRY in any case."""
        if str(card.get("updated_at") or "") != str(blocked.get("updated_at") or ""):
            return True
        tried = parse_time(blocked.get("at"))
        return tried is None or now() - tried >= CLOSE_BLOCKED_RETRY

    def _close_blocked_hold(self, request_id: str, card: dict, blocked: dict, err: MeshError) -> None:
        """Remember that Mesh refused this close behind a human gate, keyed to the card's
        current ``updated_at``, so the next passes do not repeat the write."""
        try:
            fresh = self.mesh.task(request_id)    # the refused write or our comment may have moved it
            card = fresh.get("task", fresh)
        except MeshError:
            pass
        self._close_blocked_set(request_id, {"at": iso(), "since": blocked.get("since") or iso(),
                                             "updated_at": str(card.get("updated_at") or "")})
        if not blocked:
            log.warning("%s: close decision request #%s: awaiting human sign-off, retrying when the card "
                        "changes or every %dh: %s", self.lane, request_id[:8],
                        CLOSE_BLOCKED_RETRY.total_seconds() // 3600, err)


def _human_gate_refusal(err: MeshError) -> bool:
    """Mesh refused a write because the task awaits a person's sign-off (human_gate)."""
    if err.status != 422:
        return False
    text = f"{err} {json.dumps(err.body, default=str) if err.body is not None else ''}".casefold()
    return "human_gate" in text or "human sign-off" in text or "human gate" in text
