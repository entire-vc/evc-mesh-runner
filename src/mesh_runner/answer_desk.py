"""Answer desk: a question one lane asks another lane of this runner is answered without a request card.

``ask_agent`` opens a decision request on Mesh and delivers it to nobody, so the asking lane files a
``[decision]`` card for the addressee (``effects._file_decision``). For an addressee listed in
``[runner] cardless_question_lanes`` the desk replaces that card:

- asker side (``defer``, called from ``Effects.on_wait``): the question is put on the addressee's desk instead
  of a card when the addressee is a lane of this process, listed, not paused, its desk is live (the endpoint
  answered) and the asking card is in the addressee's workspace (the endpoint lists only the workspace the
  addressee's key opened). The wait carries ``cardless = {since, lane}``; the request is queued in the store.
- addressee side (``AnswerDesk.step``, one thread of the runner): for each queued request the desk runs one
  agent turn in the lane's profile, keyed by the ASKING card (a second question about it resumes the same
  session), asking it to answer with the Mesh command ``answer_decision``. The answer lands on the asking card
  and wakes the asker exactly as an answer on a request card does. Still open after the turn: one reminder
  turn; still open after that: failed.
- fallback (``waiting``, called from ``Effects.adopt`` on every pass over the parked wait): the request card is
  filed through the usual path when 20 minutes passed, the desk failed the request, or the addressee cannot
  take it any more (paused, removed, not listed, desk disabled). A question is never lost.

A turn runs the way a chat turn runs (``chat_host`` / ``chat_delivery``): the lane's routed env and harness
(``chat_host.routed``: account routing), its transport gate (``ChatLane.transport_hold``), an interactive
session in a tmux window driven by ``DriverDelivery``, no worktree, no card, no checkout. The window key has
the chat prefix (``chat-ask<task>``), so the card reaper and ``make_room`` leave it alone and it counts against
the host cap; the desk stops it after ``IDLE_MINUTES`` (its metadata stays, so the next question resumes).

A desk session has no worktree. It runs with the lane's ordinary tools, as a request card does, and is told to
change no files; for the addressees of ``decision_request_metadata_only_recipients`` the harness denies commands
and writes, as it does on their request cards. A question that needs changes or a worktree is not answered
there: the prompt tells the agent to say so and stop without ``answer_decision``; the request stays open, the
reminder turn ends the same way, the desk fails it and the asker files the card.

Known interaction: the chat service's start sweep (``ChatHost._stop_orphans``) kills every ``chat-*`` window of
its lanes, desk windows included, so a restart of the chat process can cut a running desk turn; the reminder
turn and the card fallback cover it.

Off (``cardless_question_lanes = []``): nothing here is built, no thread, no request, no mark.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import threading
import time
import uuid
from pathlib import Path

from .mesh import MeshError

log = logging.getLogger("mesh_runner")

MARK = "answer_desk"                 # store mark: agent = addressee lane, task_id = "_desk:<request id>"
POLL_SECONDS = 30.0                  # at most one list read per lane in this time (the post-turn check aside)
DISABLED_SECONDS = 600.0             # a Mesh without the route (404) is asked again after this
ERROR_LOG_SECONDS = 60.0             # any other read failure is logged at most once a minute per lane
LEASE_SECONDS = 15 * 60.0            # one turn's limit; a ``running`` request older than this was cut by a crash
FALLBACK_SECONDS = 20 * 60.0         # the asker files the card when the desk has not answered by then
IDLE_MINUTES = 10.0                  # a desk session idle this long is stopped (resumable from its metadata)
STEP_SECONDS = 2.0
KEEP_DAYS = 7                        # marks older than this are emptied
THREAD_PREFIX = "ask"                # window key chat-ask<task>: not hex, so never a chat thread's key
DONE = ("answered", "failed", "carded")


def _key(rid: str) -> str:
    return "_desk:" + rid


def thread_of(task_id: str) -> str:
    """The session key of the questions about one asking card."""
    return THREAD_PREFIX + task_id


def _since(value: str) -> float:
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def prompt(lane: str, item: dict, read_only: bool = True) -> str:
    """The question as the addressee reads it (the meaning of ``Effects._decision_ask``, with the command in
    place of a comment). ``read_only``: the session cannot run commands (see ``AnswerDesk.read_only``)."""
    rid = str(item.get("id") or "")
    asker = str((item.get("from_agent") or {}).get("name") or "another agent")
    task = item.get("task") or {}
    task_id = str(item.get("task_id") or task.get("id") or "")
    short = task_id[:8]
    question = "\n> ".join(str(item.get("question") or "").splitlines() or [""])
    return (f"@{lane}, {asker} is waiting for your decision on task #{short} «{task.get('title', '')}» ({task_id}).\n\n"
            f"Question (verbatim):\n> {question}\n\n"
            f"Answer with the Mesh command `answer_decision` (request_id `{rid}`, answer: your decision and why): "
            f"it writes the answer on task #{short} and resumes {asker}. Do not take, move or edit #{short}.\n"
            + (f"This session has no worktree and cannot run commands or change files (reading files and Mesh works). "
               f"If a sound answer needs work you cannot do here, "
               if read_only else
               f"This session has no worktree of its own: read and run what the question names where it is, and "
               f"change no files. If a sound answer needs changes or a worktree of your own, ")
            + f"do NOT call `answer_decision`: say so in one line and stop — the question then reaches you as an "
              f"ordinary request card.")


def reminder(item: dict) -> str:
    rid = str(item.get("id") or "")
    asker = str((item.get("from_agent") or {}).get("name") or "the asking agent")
    return (f"Request `{rid}` from {asker} is still open: no answer was recorded. Call `answer_decision` now "
            f"with request_id `{rid}` and your answer. If a sound answer needs work this session cannot do, reply "
            f"with one line saying so and stop: the question will reach you as a request card.")


def desk_plan(ar, task_id: str, text: str, read_only: bool = True):
    """``plan(turn)`` for ``DriverDelivery``: the chat plan without the chat parts (no chat token, no history,
    no attachments). The lane's routed env as a card turn gets it (its agent key reaches the profile's Mesh
    MCP, which ``answer_decision`` needs), the profile directory as cwd. ``read_only``: the harness denies
    commands and writes; otherwise the lane's ordinary tools, as on a request card."""
    from .chat_delivery import window_key
    from .chat_host import ChatRefused, pinned, routed, session_for
    from .chat_lane import lane_of
    from .interactive import InteractiveAdapter
    lane = lane_of(ar)

    def plan(turn: dict):
        env, harness, profile = routed(lane)
        adapter = pinned(lane.adapter_for(harness), profile)
        if not isinstance(adapter, InteractiveAdapter):
            raise ChatRefused("headless_forbidden", "the answer desk runs interactive sessions only")
        thread = thread_of(task_id)
        sid, resume = session_for(adapter, lane, thread, env)
        cwd = lane.agent.profile_dir
        argv = (adapter.resume_argv if resume else adapter.start_argv)(sid, cwd, [])
        i = argv.index("--")
        argv = argv[:i] + ["--key", window_key(thread)] + argv[i:]
        if read_only:
            argv = adapter.with_read_only(argv)
        return argv, cwd, dict(env), text, harness
    return plan


class AnswerDesk:
    """The desk of every listed lane of one runner process. ``lanes``: every lane of the process (the asker
    side checks an addressee against them); ``store``: a connection of its own, used under ``lock``."""

    def __init__(self, cfg, lanes, store, *, clock=time.time, spawn=None, run_turn=None, tmux=None):
        self.cfg, self.store, self.clock = cfg, store, clock
        self.lanes = {ar.agent.name: ar for ar in lanes}
        wanted = {str(n).strip().casefold() for n in cfg.cardless_question_lanes}
        self.listed = {name for name in self.lanes if "*" in wanted or name.casefold() in wanted}
        self.spawn = spawn or (lambda fn: threading.Thread(target=fn, daemon=True, name="answer-desk-turn").start())
        self.run_turn = run_turn or self._drive
        self.tmux = tmux
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.live: dict[str, bool] = {}          # lane -> the endpoint answered (a desk not probed yet is not live)
        self.disabled_until: dict[str, float] = {}
        self.next_poll: dict[str, float] = {}
        self.polled_at: dict[str, float] = {}
        self.open: dict[str, list[dict]] = {}    # lane -> the last list read
        self.busy: dict[str, str] = {}           # lane -> request id of its running turn
        self.error_logged: dict[str, float] = {}
        self.idle: dict[tuple[str, str], float] = {}   # (lane, window) -> end of its last turn
        self._pruned = 0.0

    def _iso(self) -> str:
        return dt.datetime.fromtimestamp(self.clock(), dt.timezone.utc).isoformat(timespec="milliseconds")

    # ------------------------------------------------------------------ marks
    def _get(self, lane: str, rid: str) -> dict:
        with self.lock:
            row = self.store.mark(lane, _key(rid), MARK)
        try:
            data = json.loads(row[0]) if row and row[0] else {}
        except ValueError:
            data = {}
        return data if isinstance(data, dict) else {}

    def _set(self, lane: str, rid: str, record: dict) -> None:
        with self.lock:
            self.store.mark(lane, _key(rid), MARK, json.dumps(record))

    def _marks(self, lane: str) -> dict[str, dict]:
        with self.lock:
            rows = self.store.marked(lane, MARK)
        out = {}
        for key, value in rows.items():
            try:
                out[key[len("_desk:"):]] = json.loads(value)
            except ValueError:
                continue
        return out

    def state(self, lane: str, rid: str) -> str:
        return str(self._get(lane, rid).get("state") or "")

    # ------------------------------------------------------------------ who can take a question
    def _lane(self, name: str):
        name = str(name or "").casefold()
        return next((ar for n, ar in self.lanes.items() if n.casefold() == name and n in self.listed), None)

    @staticmethod
    def _paused(ar) -> bool:
        try:
            return bool(ar.paused())
        except Exception:
            return True

    def hold(self, name: str) -> str:
        """Why the desk of lane ``name`` cannot take a question now ('' = it can)."""
        ar = self._lane(name)
        if ar is None:
            return "not listed"
        if getattr(ar, "launch_mode", "interactive") == "headless":
            return "headless lane"
        if self.clock() < self.disabled_until.get(ar.agent.name, 0.0):
            return "desk disabled: Mesh has no decision-request list"
        if not self.live.get(ar.agent.name):
            return "desk not live yet"
        if self._paused(ar):
            return "paused"
        return ""

    def defer(self, asker: str, a, task: dict, wait: dict, addressee: dict | None, me: dict) -> bool:
        """Asker side: True when the question goes to the addressee's desk instead of a request card (the wait
        then carries ``cardless``). Anything uncertain answers False: the card is filed as before."""
        rid = str(wait.get("decision_request_id") or "")
        if not rid or wait.get("desk_fallback") or not addressee or not addressee.get("id"):
            return False
        ar = self._lane(addressee.get("name"))
        if ar is None or ar.agent.name.casefold() == str(asker).casefold() or self.hold(ar.agent.name):
            return False
        them = getattr(ar, "me", None) or {}
        if them.get("id") != addressee["id"]:
            return False
        # the endpoint lists the requests of the workspace the addressee's key opened; a card read with the
        # asker's own key is in the asker's workspace unless Mesh names another
        card_ws = str((task or {}).get("workspace_id") or (me or {}).get("workspace_id") or "")
        if not card_ws or card_ws != str(them.get("workspace_id") or ""):
            return False
        lane = ar.agent.name
        known = self._get(lane, rid)
        if known.get("state") in ("failed", "carded"):
            return False
        at = self._iso()
        if not known:
            self._set(lane, rid, {"state": "queued", "at": at, "since": at, "task": a.task_id, "from": asker})
        wait["cardless"] = {"since": at, "lane": lane}
        log.info("%s: #%s question to %s goes to its answer desk (request %s), no request card", asker, a.task8,
                 lane, rid[:8])
        return True

    def still_waiting(self, wait: dict) -> str:
        """Asker side: '' while the desk may still answer the cardless question, otherwise why the card is due."""
        cardless = wait.get("cardless") or {}
        lane, rid = str(cardless.get("lane") or ""), str(wait.get("decision_request_id") or "")
        if self.clock() - _since(cardless.get("since") or "") >= FALLBACK_SECONDS:
            return "not answered within %d minutes" % (FALLBACK_SECONDS // 60)
        state = self.state(lane, rid) if lane and rid else ""
        if state == "failed":
            return "the answer desk could not get an answer"
        if state == "answered":
            return ""                                     # the wake is on its way
        return self.hold(lane)

    def carded(self, lane: str, rid: str, why: str) -> None:
        """The asker filed the card: the desk does not answer this request any more."""
        record = self._get(lane, rid)
        if record.get("state") in ("answered", "failed", "carded") or not record:
            return
        record.update(state="carded", at=self._iso(), why=why[:120])
        self._set(lane, rid, record)

    # ------------------------------------------------------------------ addressee side
    def attach(self) -> "AnswerDesk":
        for ar in self.lanes.values():
            effects = getattr(ar, "effects", None)
            if effects is not None:
                effects.desk = self
        return self

    def start(self, stop: threading.Event) -> None:
        self.stop = stop
        self._stop_orphans()
        threading.Thread(target=self.loop, args=(stop,), name="answer-desk", daemon=True).start()
        log.info("answer desk: lanes %s", ", ".join(sorted(self.listed)) or "-")

    def loop(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.step()
            except Exception as e:                        # one bad pass must not end the desk
                log.warning("answer desk: %s: %s", type(e).__name__, str(e)[:200])
            stop.wait(STEP_SECONDS)

    def step(self) -> None:
        now = self.clock()
        for name in sorted(self.listed):
            self._lane_step(self.lanes[name], now)
        self._reap(now)
        self._prune(now)

    def _held(self, ar) -> str:
        if self._paused(ar):
            return "paused"
        for gate in ("memgate", "loadgate"):
            if getattr(getattr(ar, gate, None), "holding", False):
                return gate
        return ""

    def _poll(self, ar, now: float) -> list[dict] | None:
        name = ar.agent.name
        self.next_poll[name] = now + POLL_SECONDS
        try:
            items = ar.mesh.decision_requests("open")
        except MeshError as e:
            if e.status == 404:
                self.disabled_until[name] = now + DISABLED_SECONDS
                self.live[name] = False
                log.warning("%s: answer desk off for %d min: Mesh has no decision-request list (404)", name,
                            DISABLED_SECONDS // 60)
            elif now - self.error_logged.get(name, -1e9) >= ERROR_LOG_SECONDS:
                self.error_logged[name] = now
                log.warning("%s: answer desk: decision requests: %s", name, str(e)[:200])
            return None
        self.live[name] = True
        self.disabled_until.pop(name, None)
        self.polled_at[name] = now
        self.open[name] = [i for i in items if isinstance(i, dict) and i.get("state", "open") == "open"]
        return self.open[name]

    def _lane_step(self, ar, now: float) -> None:
        name = ar.agent.name
        if name in self.busy or self._held(ar):
            return
        if now < self.disabled_until.get(name, 0.0):
            return
        marks = self._marks(name)
        todo = {rid: m for rid, m in marks.items() if self._due(m, now)}
        for rid, mark in list(todo.items()):
            if mark.get("state") == "running" and mark.get("recovered"):
                # its retry was cut as well: failed, so the asker files the card
                mark.update(state="failed", at=self._iso(), why="cut twice without a result")
                self._set(name, rid, mark)
                del todo[rid]
                log.warning("%s: answer desk request %s: cut twice without a result; failed", name, rid[:8])
        if not self.live.get(name):                       # the probe: is the desk live on this Mesh at all
            if now >= self.next_poll.get(name, 0.0):
                self._poll(ar, now)
            if not self.live.get(name):
                return
        if not todo:
            return
        if now >= self.next_poll.get(name, 0.0) and self._poll(ar, now) is None:
            return
        listed = {str(i.get("id")): i for i in self.open.get(name, [])}
        for rid, mark in sorted(todo.items(), key=lambda kv: kv[1].get("since") or ""):
            if rid in listed:
                continue
            # gone from a list read after it was queued (a second of slack for the stamps' precision)
            if self.polled_at.get(name, 0.0) > _since(mark.get("at") or "") + 1.0:
                mark.update(state="answered", at=self._iso(), why="no longer open")
                self._set(name, rid, mark)                # answered meanwhile, withdrawn or deleted
        item = next((listed[rid] for rid, _ in sorted(todo.items(), key=lambda kv: kv[1].get("since") or "")
                     if rid in listed), None)
        if item is None:
            return
        from .chat_lane import lane_of
        try:
            reason = lane_of(ar).transport_hold()
        except Exception as e:
            reason = f"gate failed: {type(e).__name__}"
        if reason:
            return
        rid = str(item["id"])
        mark = todo[rid]
        if mark.get("state") == "running":                # cut by a crash: retried once
            mark["recovered"] = True
        mark.update(state="running", at=self._iso())
        self._set(name, rid, mark)
        self.busy[name] = rid
        self._emit(ar, "answer_started", item)
        self.spawn(lambda: self._answer(ar, item))

    def _due(self, mark: dict, now: float) -> bool:
        state = mark.get("state")
        if state == "queued":
            return True
        return state == "running" and now - _since(mark.get("at") or "") >= LEASE_SECONDS

    def _answer(self, ar, item: dict) -> None:
        name, rid = ar.agent.name, str(item["id"])
        task_id = str(item.get("task_id") or (item.get("task") or {}).get("id") or "")
        mark = self._get(name, rid)
        state, why, turns = "failed", "", 0
        try:
            if mark.get("state") == "running" and mark.get("recovered") and mark.get("turns"):
                texts = [reminder(item)]                  # the crashed run already asked once
            else:
                texts = [prompt(name, item, self.read_only(ar)), reminder(item)]
            for text in texts:
                turns += 1
                ended = self.run_turn(ar, rid, task_id, text)
                self.idle[(name, task_id)] = self.clock()
                mark.update(turns=int(mark.get("turns") or 0) + 1)
                self._set(name, rid, mark)
                still = self._still_open(ar, rid)
                if still is None:
                    why = "the request list could not be read after the turn"
                    break
                if not still:
                    state = "answered"
                    break
                why = f"still open after {turns} turn(s) ({ended or 'no result'})"
        except Exception as e:
            why = f"turn failed: {type(e).__name__}: {str(e)[:120]}"
            log.warning("%s: answer desk request %s: %s", name, rid[:8], why)
        finally:
            mark.update(state=state, at=self._iso(), why=why)
            self._set(name, rid, mark)
            self.busy.pop(name, None)
        self._emit(ar, "answer_done" if state == "answered" else "answer_failed", item, turns=turns, why=why)
        log.log(logging.INFO if state == "answered" else logging.WARNING, "%s: answer desk request %s from %s: %s%s",
                name, rid[:8], (item.get("from_agent") or {}).get("name") or "?", state, f" ({why})" if why else "")

    def read_only(self, ar) -> bool:
        """Whether the lane answers without commands. A request card gives its addressee the lane's ordinary
        tools, except the addressees of ``decision_request_metadata_only_recipients``, whose requests are
        confined by the harness; the desk keeps that split. Most questions between lanes ask for a verdict on
        a commit or a diff, which nobody can give without ``git`` (the first live question, 2026-11-11, was
        declined for that reason and went to a card)."""
        confined = set(getattr(self.cfg, "decision_request_metadata_only_recipients", None) or [])
        return str((getattr(ar, "me", None) or {}).get("id") or "") in confined

    def _still_open(self, ar, rid: str) -> bool | None:
        name, now = ar.agent.name, self.clock()
        try:
            items = ar.mesh.decision_requests("open")
        except MeshError as e:
            log.warning("%s: answer desk: decision requests after a turn: %s", name, str(e)[:200])
            return None
        self.polled_at[name] = now
        self.open[name] = [i for i in items if isinstance(i, dict) and i.get("state", "open") == "open"]
        return any(str(i.get("id")) == rid for i in self.open[name])

    @staticmethod
    def _emit(ar, kind: str, item: dict, **extra) -> None:
        emit = getattr(ar, "emit", None)
        if emit is None:
            return
        try:
            emit(kind, request=str(item.get("id") or ""),
                 task=str(item.get("task_id") or (item.get("task") or {}).get("id") or ""),
                 **{"from": str((item.get("from_agent") or {}).get("name") or ""), "to": ar.agent.name}, **extra)
        except Exception as e:
            log.warning("answer desk event %s: %s", kind, type(e).__name__)

    # ------------------------------------------------------------------ the session
    def _drive(self, ar, rid: str, task_id: str, text: str) -> str:
        """One turn in the lane's session for ``task_id``; the type of its last event (final / error)."""
        from .chat_delivery import DriverDelivery
        from .chat_lane import lane_of
        lane = lane_of(ar)
        delivery = DriverDelivery(lane.routed_harness, desk_plan(lane, task_id, text, self.read_only(ar)),
                                  Path(self.cfg.state_dir) / "answer_desk" / "logs")
        deadline = time.monotonic() + LEASE_SECONDS
        last = ""
        turn = {"turn_id": uuid.uuid4().hex, "thread_id": thread_of(task_id)}
        for ev in delivery.run(turn, lambda: self.stop.is_set() or time.monotonic() > deadline):
            if ev.get("type") in ("final", "error"):
                last = str(ev.get("stop_reason") or ev.get("code") or ev["type"])
        return last

    def _tmux(self):
        if self.tmux is None:
            from .interactive import Tmux
            self.tmux = Tmux(self.cfg.tmux_binary)
        return self.tmux

    def _reap(self, now: float) -> None:
        from .chat_machine import stop_window
        for (name, task_id), end in list(self.idle.items()):
            if self.busy.get(name) or now - end < IDLE_MINUTES * 60:
                continue
            try:
                stop_window(self._tmux(), self.cfg.state_dir, name, thread_of(task_id))
            except Exception as e:
                log.warning("%s: answer desk session stop: %s", name, type(e).__name__)
                continue
            self.idle.pop((name, task_id), None)

    def _stop_orphans(self) -> None:
        """Desk windows that outlived a restart: nothing tracks their idle time, so they are stopped (their
        metadata stays: the next question resumes the session)."""
        from .chat_delivery import window_key
        from .interactive import session_name
        prefix = window_key(THREAD_PREFIX)
        try:
            own = {session_name(n) for n in self.listed}
            for session, window in self._tmux().all_windows():
                if session in own and window.startswith(prefix):
                    self._tmux().kill_window(session, window)
        except Exception as e:
            log.warning("answer desk orphan sweep: %s", type(e).__name__)

    def _prune(self, now: float) -> None:
        if now - self._pruned < 3600:
            return
        self._pruned = now
        for name in self.listed:
            for rid, mark in self._marks(name).items():
                if mark.get("state") in DONE and now - _since(mark.get("at") or "") > KEEP_DAYS * 86400:
                    with self.lock:
                        self.store.mark(name, _key(rid), MARK, "")

    # ------------------------------------------------------------------ status
    def status(self, name: str) -> dict:
        today = dt.datetime.fromtimestamp(self.clock(), dt.timezone.utc).date().isoformat()
        marks = self._marks(name)

        def counted(state: str) -> int:
            return sum(1 for m in marks.values() if m.get("state") == state and str(m.get("at") or "")[:10] == today)
        out = {"open": len(self.open.get(name, [])), "running": self.busy.get(name, ""),
               "answered_today": counted("answered"), "failed_today": counted("failed")}
        hold = self.hold(name)
        if hold:
            out["hold"] = hold
        return out

    def annotate(self, rows: list[dict]) -> None:
        """Put the ``answer_desk`` block on the status rows of the listed lanes."""
        for row in rows:
            name = row.get("agent")
            if name in self.listed:
                try:
                    row["answer_desk"] = self.status(name)
                except Exception as e:
                    row["answer_desk"] = {"error": type(e).__name__}


# ---------------------------------------------------------------------- hooks
def build(cfg, lanes, store=None) -> AnswerDesk | None:
    """The desk of a runner, attached to every lane's effects; None when ``cardless_question_lanes`` is empty."""
    if not getattr(cfg, "cardless_question_lanes", None):
        return None
    if store is None:
        from .store import Store
        store = Store(Path(cfg.state_dir) / "runner.sqlite")
    desk = AnswerDesk(cfg, lanes, store)
    if not desk.listed:
        log.warning("cardless_question_lanes names no lane of this runner: the answer desk stays off")
        return None
    return desk.attach()


def defer(desk, effects, a, task: dict, wait: dict, me: dict) -> bool:
    """``Effects.on_wait``: True when a typed question goes to the addressee's desk (no card now)."""
    if desk is None or not wait.get("decision_request_id") or wait.get("desk_fallback"):
        return False
    try:
        addressee = effects._resolve_agent(wait.get("handle") or "", me)
    except MeshError:
        return False                                      # the card path retries the lookup
    if not desk.defer(effects.lane, a, task, wait, addressee, me):
        return False
    if getattr(effects, "store", None) is not None:
        effects.store.event(a.id, "decision_cardless", f"{wait['cardless']['lane']} {wait['decision_request_id']}")
    return True


def waiting(desk, effects, a, wait: dict) -> bool:
    """``Effects.adopt``: True while a cardless question may still be answered by the desk. Otherwise the wait
    is turned back into an ordinary one that files its card on this pass (``desk_fallback`` keeps it from
    being deferred again)."""
    if not wait.get("cardless"):
        return False
    why = desk.still_waiting(wait) if desk is not None else "the answer desk is off"
    if not why:
        return True
    cardless = wait.pop("cardless")
    wait["desk_fallback"] = why
    if desk is not None:
        desk.carded(str(cardless.get("lane") or ""), str(wait.get("decision_request_id") or ""), why)
    if getattr(effects, "store", None) is not None:
        effects.store.event(a.id, "decision_card_fallback", why[:200])
    log.info("%s: #%s question to %s: request card filed: %s", effects.lane, a.task8, cardless.get("lane"), why)
    return False
