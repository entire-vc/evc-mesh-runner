"""What an attempt's state means for the rest of Mesh.

Other people and tools read a task, not the runner: the board shows a task's
runtime state, schedulers honour ``start_after``, dependency views follow
``blocks`` edges, and an agent asked for a decision only learns about it from
a task assigned to it. This module keeps those in step with the attempt:

- runtime state: ``working`` while a turn runs, ``waiting`` (with what it
  waits for) or ``stopped`` (human gate) while it waits, ``queued`` when woken;
  re-published every ``HEARTBEAT`` because Mesh treats it as stale after 15 min,
  cleared when the attempt closes;
- ``start_after``: a time wait uses its time; an event wait gets a rolling 24 h
  horizon renewed before it runs out; cleared on wake, so nothing else picks the
  task up while its session is parked. Schedules are compared as instants (Mesh
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
  comment and no reopen (``identical_no_reopen``);
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
import uuid

from .mesh import MeshClient, MeshError
from .blocking_path import BlockingPath, Budget
from .selection import StatusCategories, parse_time
from .store import OPEN, Attempt, Store, iso, now

log = logging.getLogger("mesh_runner")

HEARTBEAT = dt.timedelta(minutes=5)
CREATE_WINDOW = dt.timedelta(minutes=10)   # a lost create may still commit this long
EVENT_HORIZON = dt.timedelta(hours=24)
RENEW_BEFORE = dt.timedelta(hours=6)
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
ANSWER_QUOTE = 300                      # chars of the addressee's answer quoted in a repeat send-back
SKIP_MARK = "skip_state"                # store mark: the skip state this agent published on a task
SKIP_SEED_DAYS = 30                     # first run with a store: tasks of this age may carry stale states
SKIP_CLEARS_PER_TICK = 10               # clears take at most this share of a pass's writes
SKIP_CLEAR_TRIES = 3                    # a clear that fails this often is given up (logged)
STATE_FIELDS = ("kind", "ref", "reason")
BACKLOG_BLOCKER = "blocked by a card in backlog: "
EDGE_DENIED_MARK = "edge_denied"        # store mark prefix: an edge this lane's key may not remove / add (HTTP 403)
UNSCHEDULE_MARK = "unschedule"          # store mark: a start_after this lane set and could not clear yet
UNSCHEDULE_TRIES = 3                    # PATCHes per clear when Mesh answers 409 (re-read in between)
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


def _asked_at(posted, floor: str) -> str:
    """When a question reached its addressee: the later of ``floor`` (taken right before the
    write that carries it) and the Mesh time of the request card or comment written. Only an
    addressee comment after this instant answers it. The wait's own declaration time is earlier
    and would count a comment written before the question was put; the later of the two clocks
    errs towards asking again, never towards a send-back."""
    at = posted.get("created_at") if isinstance(posted, dict) else None
    server, local = parse_time(at), parse_time(floor)
    return at if server and (local is None or server > local) else floor


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
        return _stopped("review_refeed_hold", "already reviewed; waits for a new comment")
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
                "reason": "blocker card is not readable by this agent"}
    if r.startswith("project not served") or (r.startswith("label ") and r.endswith(" missing")):
        return _stopped("outside_lane_scope", "outside this agent's scope")
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

    def _put(self, task_id: str, state: dict) -> None:
        """PUT a runtime state. A Mesh that does not know a reason code refuses the
        whole write (422 on ``reason_code``): the state goes out without the code,
        its reason text says the same."""
        code = state.get("reason_code")
        body = {k: v for k, v in state.items() if k != "reason_code"} if code in self._refused_codes else state
        try:
            self.mesh.put_runtime_state(task_id, body)
        except MeshError as e:
            if not (code and body is state and e.status == 422
                    and (not isinstance(e.body, dict) or e.body.get("field") in (None, "reason_code"))):
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
        diagnostic_budget = Budget()
        for a in attempts:
            desired = runtime_state(a, self.lane)
            if desired is not None and a.state == "waiting" and a.wait.get("kind") == "card":
                desired["reason"] = self._blocking_path.resolve(a, diagnostic_budget)
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
        elif kind in ("pipeline", "mr", "merge", "card", "decision", "blocking"):
            self._schedule(a, wait, iso(now() + EVENT_HORIZON))
        if kind == "card":
            self._add_edge(a, wait)
        if kind == "decision" and wait.get("handle"):
            self._file_decision(a, task, wait, me)

    def adopt(self, a: Attempt, task: dict, wait: dict, me: dict) -> bool:
        """Apply on_wait to a wait that began while effects were off (an older runner,
        or mesh_effects=false): its addressee was never asked and nothing is scheduled.
        True when it did so."""
        if not self.enabled or wait.get("fx"):
            return False
        self.on_wait(a, task, wait, me)
        return True

    def renew(self, a: Attempt, wait: dict) -> None:
        """Keep an event wait's rolling horizon ahead of now."""
        if not self.enabled or wait.get("kind") == "time":
            return
        until = parse_time(wait.get("scheduled"))
        if until and until - now() < RENEW_BEFORE:
            self._schedule(a, wait, iso(now() + EVENT_HORIZON))

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
                    self.mesh.delete_dependency(a.task_id, wait["edge"])
                    wait.pop("edge", None)
                except MeshError as e:
                    if e.status == 403:
                        self._deny_edge(a.task_id, "del:" + wait["edge"])
                        wait.pop("edge", None)
                    log.warning("%s: edge #%s: %s%s", self.lane, a.task8, e,
                                "; not removable by this lane, not retried" if e.status == 403 else "")
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
        try:
            self.mesh.patch_task(a.task_id, {"start_after": when})
            wait["scheduled"] = when
            wait.pop("scheduled_maybe", None)
            self._forget_unschedule(a.task_id)     # the lane owns the task's schedule again
        except MeshError as e:
            if e.status == 0 or e.status >= 500:
                # the answer was lost, the write may have committed: remember it so the end
                # of the wait still takes it back (only if Mesh shows exactly this instant)
                wait["scheduled_maybe"] = when
            log.warning("%s: schedule #%s: %s", self.lane, a.task8, e)

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
        ``cleared``; ``gone`` (no schedule, someone else's, or the task is gone: left alone);
        anything else is the error that kept it. A 409 is retried on a fresh read."""
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
                self.mesh.patch_task(task_id, {"start_after": None})
                if attempt:
                    log.info("%s: unschedule #%s: cleared after %d conflict(s)", self.lane, task_id[:8], attempt)
                return "cleared", held
            except MeshError as e:
                last = str(e)
                if e.status != 409:
                    return last, held
        return last, held

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

    def _add_edge(self, a: Attempt, wait: dict) -> None:
        if self._edge_denied(a.task_id, "add:" + str(wait.get("target"))):
            return
        try:
            target = self.mesh.task_by_short(wait["target"])
            target_id = target.get("id")
            if not target_id or target_id == a.task_id:
                return
            edge = self.mesh.add_dependency(a.task_id, target_id)
            edge_id = edge.get("id") or (edge.get("dependency") or {}).get("id")
            if edge_id:
                wait["edge"] = edge_id
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

    def _identical(self, task_id: str, entry: dict | None, wait: dict) -> bool:
        """``wait`` repeats the question of the request ``entry`` stands for, from the same asker:
        attached to that request as it is (``identical_no_reopen``)."""
        entry = entry or {}
        return (self.identical_no_reopen and entry.get("lane") in (None, "", self.lane)
                and self._same_question(task_id, entry.get("question"), wait.get("note") or ""))

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

    def answered_repeat(self, a: Attempt, handle: str, note: str, comments: list[dict], me: dict) -> str:
        """Send-back text when ``WAIT decision:@handle: note`` repeats, verbatim, the last
        question this source put to that agent and the agent has commented on the source since
        it was asked: asking again gets the same answer. '' = file or reuse as usual."""
        if not self.enabled or self.reuse_window is None or self.store is None:
            return ""
        question = self._question_text({"note": note})
        if not question or not handle:
            return ""
        try:
            addressee = self._resolve_agent(handle, me)
        except MeshError:
            return ""
        if not addressee or addressee.get("id") in (me.get("id"), None):
            return ""                       # a person or ourselves: never handled here
        entry = self._reusable(a.task_id, addressee["id"], self._decision(a.task_id))
        asked = parse_time((entry or {}).get("asked_at"))
        if not entry or asked is None or not self._same_question(a.task_id, entry.get("question"), note):
            return ""
        name = str(addressee.get("name") or handle)

        def by_addressee(c: dict) -> bool:
            author = c.get("author_id") or (c.get("author") or {}).get("id")
            who = str(c.get("author_name") or (c.get("author") or {}).get("name") or "").casefold()
            return author == addressee["id"] or (bool(who) and who == name.casefold())
        try:
            # the answer is a comment on the source task; with decision_wake = "answer" it may
            # also be one on the request itself
            on_request = self.mesh.comments(entry["request"]["id"])
        except MeshError:
            on_request = []
        answers = [c for c in list(comments) + list(on_request) if by_addressee(c)
                   and (c.get("body") or "").strip() and (parse_time(c.get("created_at")) or asked) > asked]
        if not answers:
            return ""
        last = max(answers, key=lambda c: parse_time(c.get("created_at")))
        quote = " ".join(last["body"].split())
        if len(quote) > ANSWER_QUOTE:
            quote = quote[:ANSWER_QUOTE - 1].rstrip() + "…"
        return (f"@{name} already answered this exact question on this task after you asked it: «{quote}». "
                "The same question will get the same answer, so this decision wait was sent back and "
                f"nothing was asked again. Wait for the event that changes the answer "
                "(`⏳ WAIT card:#<8hex>`, `⏳ WAIT mr:<group>/<project>!<iid>`, "
                "`⏳ WAIT pipeline:<group>/<project>#<id>`, or a time with its reason), "
                f"or ask @{name} a different question.")

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
                # attached to a closed request, handed back by the re-check and asked again: the asker
                # still needs it, so the addressee is asked once more (the request is reopened)
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

    def _reuse_decision(self, a: Attempt, task: dict, wait: dict, addressee: dict, key: str,
                        reuse: dict, ask: bool = False) -> bool:
        """Ask a question on the request this source already filed for ``addressee``: reopen it
        when closed (the assignee is kept), append the question as a comment, bind it to the
        wait. True when handled (done, or failed and left for a retry: ``fx`` cleared); False
        when that request cannot be reused (gone, reassigned, no todo status): file a new one.
        The same question is attached to it as it is (``_attach_identical``) unless ``ask``."""
        req = dict(reuse["request"])
        identical = not ask and self._identical(a.task_id, reuse, wait)
        try:
            card = self.mesh.task(req["id"])
            card = card.get("task", card) if isinstance(card, dict) else {}
            project_id = req.get("project_id") or card.get("project_id")
            assignee = card.get("assignee_id") or (card.get("assignee") or {}).get("id")
            if not project_id or assignee != addressee["id"]:
                return self._forget_request(a.task_id, addressee["id"])   # someone else holds it now
            req["project_id"] = project_id
            reopened = self.cats.category(card) in ("done", "cancelled")
            if identical:
                return self._attach_identical(a, wait, addressee, key, reuse, req, closed=reopened)
            if reopened:
                todo = next((s["id"] for s in self.mesh.statuses(project_id) if s.get("category") == "todo"),
                            None)
                if not todo:
                    return False
                self.mesh.move(req["id"], todo)     # idempotent: a retry after a failed comment repeats it
            asking = iso()
            posted = self.mesh.add_comment(req["id"], self._decision_ask(a, task, wait, addressee, again=True))
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
        self._save_decision(a.task_id, record)
        wait["request"] = dict(req)
        log.info("%s: #%s decision request → %s: #%s reused%s", self.lane, a.task8, addressee.get("name"),
                 req["id"][:8], " (reopened)" if reopened else "")
        return True

    def _attach_identical(self, a: Attempt, wait: dict, addressee: dict, key: str, reuse: dict, req: dict,
                          closed: bool) -> bool:
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
        if closed:
            # nobody is asked again: a bounded re-check hands the question back to the asker
            # instead of a wait on a closed request that only a stray comment would end; the same
            # question declared after that re-check asks the addressee again
            record["attached_closed"] = req["id"][:8]
        self._save_decision(a.task_id, record)
        wait["request"] = dict(req)
        if closed:
            wait["attached_closed"] = req["id"][:8]
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
                    self.mesh.add_comment(req["id"], f"[runner] closed: {why}")
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
