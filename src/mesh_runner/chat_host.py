"""Wires the chat service into a runner: the per-agent launch plan and the loop thread.

The plan reuses the agent's own routing (``_fresh_env``: account, profile, identity) and its
interactive adapter, so a chat lands in the same profile (CLAUDE.md, skills, .mcp.json) as the
agent's cards. A chat never runs headless: without an interactive adapter the turn fails with
a stable error instead of being billed per token.

chat_workspace: ``none`` and ``read`` run in the agent's profile directory with no repository
checkout. ``write`` leases a slot of the agent's card worktree pool (its first configured repo)
for the life of the chat session and returns it when the session stops (idle, Stop, eviction,
delete); the session runs with the slot as its working directory and the harness confines its writes to it
(Codex: its workspace-write sandbox). A harness with no such sandbox (Claude) is refused with
``chat_workspace_unconfinable`` rather than run unconfined.
The agent's profile directory is not the cwd, so its CLAUDE.md is not loaded in a write chat. A pool with no free
slot refuses the turn with ``chat_workspace_busy``.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from pathlib import Path

from . import interactive
from .chat_delivery import DriverDelivery, window_key
from .chat_machine import build_prompt, chat_dir, delete_chat, fetch_attachments, stop_window
from .chat_pool import ChatPool
from .chat_protocol import ChatClient, ChatError
from .chat_service import ChatService
from .interactive import CHAT_PREFIX, InteractiveAdapter, read_meta
from .workspace import WorkspaceError

log = logging.getLogger("mesh_runner")
NS = uuid.UUID("5d8e1d6e-7c1a-4c53-9a0e-6b1d2a1f0c11")
# A claim that gets no answer is a lost connection (nothing at Mesh sees it, the runner waits for its own
# read timeout), so the poll is short and the read timeout tight: a turn queued meanwhile waits for one
# poll plus the slack, not for the 45 s the old 30 s poll + 15 s slack took to give up.
STEP_TIMEOUT = 3
REAP_EVERY = 30.0


def claim_key(ar, fallback: str) -> str:
    """The key a chat claim for agent ``ar`` is made with: Mesh authorises a claim per agent key, so the
    agent's own key; ``fallback`` (``[chat] credential_file``) only for an agent with no key of its own."""
    return getattr(ar.agent, "key", "") or fallback


class ChatRefused(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def session_id_for(thread_id: str) -> str:
    """Stable per chat, so a Claude session resumes after a restart without any stored state."""
    return str(uuid.uuid5(NS, "mesh-chat:" + thread_id))


CHAT_POLL, CHAT_SETTLE = "0.2", "0.3"


ENV_TTL = 30.0
_env_cache: dict[int, tuple[float, dict, str, object]] = {}
_env_lock = threading.Lock()


def routed(ar, clock=time.monotonic, ttl: float = ENV_TTL) -> tuple[dict, str, object]:
    """``(env, harness, profile)`` of the lane's routing, one entry kept per agent: reading it runs the
    lane's env command (the account router, ~3 s and sometimes far more under load), which a chat
    answering a person cannot pay per message. Only the first call of an agent waits for it; an entry older
    than ``ttl`` seconds is served as it is while one background refresh replaces it, so a lane moved to
    another account is picked up within ``ttl`` plus one refresh. The router runs outside every lock. The
    three values come from one entry and a chat builds its command line from them on a private adapter
    (``pinned``), never from the shared adapter that card turns re-route."""
    lock = _agent_lock(ar)
    with lock:
        with _env_lock:
            hit = _env_cache.get(id(ar))
        if hit is None:                          # the agent's first call: nothing to serve, wait for the router
            return _store(ar, clock, ar._read_env())
        with _env_lock:
            refresh = clock() - hit[0] >= ttl and id(ar) not in _refreshing
            if refresh:
                _refreshing.add(id(ar))
    if refresh:
        threading.Thread(target=_refresh, args=(ar, clock, lock), name="chat-env-refresh", daemon=True).start()
    return dict(hit[1]), hit[2], hit[3]


def _agent_lock(ar) -> threading.RLock:
    with _env_lock:
        return _agent_locks.setdefault(id(ar), threading.RLock())


def pinned(adapter, profile):
    """A private copy of ``adapter`` routed to ``profile``: the command line of a chat turn is built from
    the entry its env came from, whatever a card turn sets on the shared adapter meanwhile."""
    import copy
    own = copy.copy(adapter)
    base = getattr(adapter, "base", None)
    if base is not None:
        object.__setattr__(own, "base", copy.copy(base))
        target = own.base
    else:
        target = own
    if profile is not None and hasattr(target, "profile"):
        target.profile = profile
    return own


_refreshing: set[int] = set()
_agent_locks: dict[int, threading.RLock] = {}


def _store(ar, clock, raw: dict) -> tuple[dict, str, object]:
    from .runner import session_env
    env, harness, profile = ar._route_env(raw)           # pure: derived from the read env alone
    env = session_env(env)                                # what a session gets: the allowlisted runner env + the lane's
    with _env_lock:
        _env_cache[id(ar)] = (clock(), dict(env), harness, profile)
    return dict(env), harness, profile


def _refresh(ar, clock, lock) -> None:
    try:
        raw = ar._read_env()                     # the slow part, no lock held
        with lock:
            _store(ar, clock, raw)
    except Exception as e:                       # the stale entry stays; the next call tries again
        log.warning("chat env refresh: %s", type(e).__name__)
    finally:
        with _env_lock:
            _refreshing.discard(id(ar))


def routed_env(ar, clock=time.monotonic, ttl: float = ENV_TTL) -> dict:
    return routed(ar, clock, ttl)[0]


MCP_NAME = "evc-mesh"
# the model otherwise reads "remember this" as its harness's own memory (seen live: a file write, no Mesh call)
MEMORY_NOTE = (f"Shared memory in this chat is the `remember` tool of the `{MCP_NAME}` server. "
               f"When the person asks you to remember something (\"remember this\"), call that tool.")


def with_chat_mcp(adapter, argv: list[str], cfg, token_file: str) -> list[str]:
    """The session's Mesh MCP in chat-token mode, injected on the command line (the agent's profile is
    not touched): ``evc-mesh`` is ``[chat] mcp_command`` with the thread's token file and no agent key,
    so a ``remember`` lands in the chat's audit window. Unconfigured: argv unchanged."""
    command = list(getattr(cfg, "chat_mcp_command", ()) or ())
    if not command:
        return argv
    url = getattr(cfg, "chat_mcp_url", "") or getattr(cfg, "mesh_url", "")
    env = {"MESH_API_URL": url, "MESH_CHAT_TOKEN_FILE": token_file}
    return adapter.with_instructions(adapter.with_mcp(argv, MCP_NAME, command, env), MEMORY_NOTE)


def lease_slot(ar, thread: str) -> Path:
    """The chat's worktree slot: the one it already holds, else a new lease from the agent's pool."""
    key = window_key(thread)
    held = ar.pool.leased_path(key)
    if held is not None:
        return held
    repos = list(getattr(ar.agent, "repos", None) or [])
    if not repos:
        raise ChatRefused("chat_workspace_no_repo", "chat_workspace=write: the agent has no repository configured")
    try:
        return ar.pool.lease(key, repos[0].name)
    except WorkspaceError as e:
        raise ChatRefused("chat_workspace_busy", f"no worktree slot for a chat: {e}"[:200]) from e


def return_slot(ar, thread: str) -> None:
    """Give the chat's slot back; an unsafe one (uncommitted work) stays held for the operator, never wiped."""
    pool = getattr(ar, "pool", None)
    if pool is None:
        return
    for path, reason in pool.release(window_key(thread)):
        log.warning("chat %s: worktree %s kept: %s", window_key(thread), path, reason)


def make_plan(ar, client: ChatClient, chat_workspace: str = "none"):
    """``plan(turn)`` for ``DriverDelivery``, bound to one AgentRunner ``ar``."""
    def plan(turn: dict):
        env, harness, profile = routed(ar)                 # account/profile routing, may raise
        adapter = pinned(ar.adapter_for(harness), profile)
        if not isinstance(adapter, InteractiveAdapter):
            raise ChatRefused("headless_forbidden", "chats run in interactive sessions only")
        thread = turn["thread_id"]
        key = window_key(thread)
        meta = read_meta(adapter.dir, key)
        warm_id = str(meta.get("session_id") or "")
        sid = warm_id or (session_id_for(thread) if adapter.session_id_known_upfront else "")
        resume = bool(warm_id)
        had_slot = chat_workspace == "write" and ar.pool.leased_path(key) is not None
        slot = lease_slot(ar, thread) if chat_workspace == "write" else None
        # a write chat runs IN its slot, so its write tools reach the slot and nothing else of the agent's
        # (the profile directory stays out of reach; the card sessions' profile-cwd + add-dir model is not used)
        cwd = slot or ar.agent.profile_dir
        argv = (adapter.resume_argv if resume else adapter.start_argv)(sid, cwd, [])
        i = argv.index("--")
        # a chat answers a person: short polling and settle (the card defaults are 1 s / 1.5 s)
        argv = argv[:i] + ["--key", key, "--poll", CHAT_POLL, "--settle", CHAT_SETTLE] + argv[i:]
        # none/read: no repository, no writes; write: writes only in the slot. Both enforced by the harness
        try:
            argv = adapter.with_read_only(argv) if slot is None else adapter.with_slot_confined(argv)
        except NotImplementedError as e:
            if slot is not None:
                if not had_slot:                            # leased just now; a held one may have a live window
                    return_slot(ar, thread)
                raise ChatRefused("chat_workspace_unconfinable", f"chat_workspace=write: {harness} {e}") from e
            raise
        env = adapter.without_local_memory(dict(env))      # a chat's memory is Mesh, not the harness's files
        env.pop("MESH_AGENT_KEY", None)                     # a chat writes as the person's chat, never as the agent
        token_file = str(chat_dir(ar.cfg.state_dir, thread) / "session-token")
        env["MESH_CHAT_TOKEN_FILE"] = token_file
        argv = with_chat_mcp(adapter, argv, ar.cfg, token_file)
        history = None
        if not resume:                                      # a new session for an old chat: feed it the past
            try:
                history = client.history(thread, limit=20).get("messages") or None
            except ChatError:
                history = None
        files = fetch_attachments(client, turn, ar.cfg.state_dir)
        return argv, cwd, env, build_prompt(turn, files, history), harness
    return plan


class ChatHost:
    """One chat service for the agents this runner serves (those listed in the config)."""

    def __init__(self, runner, client: ChatClient, agent_ids: dict[str, str], max_per_agent: int,
                 host_cap: int, idle_minutes: float, chat_workspace: str = "none", pool: ChatPool | None = None):
        self.runner, self.client = runner, client
        self.by_id = agent_ids                              # Mesh agent id -> runner agent name
        self.tmux = interactive.Tmux(runner.cfg.tmux_binary)
        self.runners = {ar.agent.name: ar for ar in runner.agents}
        # one pool per runner: hosts of several agents share the host cap (each host has its own claim key)
        self.pool = pool or ChatPool(max_per_agent=max_per_agent, max_host=host_cap, idle_minutes=idle_minutes,
                                     host_used=self._card_sessions)
        pool = self.pool
        self.workspace = chat_workspace
        self.service = ChatService(client, list(agent_ids), pool, self._delivery, self._stop, self._delete,
                                   token_path=self._token_path)

    def _name(self, agent_id: str) -> str:
        return self.by_id[agent_id]

    def _own_windows(self) -> list[tuple[str, str]]:
        """Windows of this runner's agents only: the tmux server is shared with other runners on
        the host, whose card and chat windows are neither counted nor stopped here."""
        own = {interactive.session_name(name) for name in self.runners}
        return [(s, w) for s, w in self.tmux.all_windows() if s in own]

    def _card_sessions(self) -> int:
        try:
            return sum(1 for _, w in self._own_windows() if not w.startswith(interactive.CHAT_PREFIX))
        except Exception:
            return 0

    def _delivery(self, agent_id: str):
        ar = self.runners[self._name(agent_id)]
        # the harness here is only a fallback: the plan returns the one its environment was routed with
        return DriverDelivery(ar._routed_harness or ar.agent.harness, make_plan(ar, self.client, self.workspace),
                              self.runner.cfg.state_dir / "chat" / "logs")

    def _return_if_gone(self, name: str, thread: str) -> None:
        """Give the chat's slot back once its window is confirmed gone by a listing that tmux answered;
        a window that survived the kill, or an unreadable list, keeps the slot (the restart sweep returns it)."""
        listed = self.tmux.windows_strict()
        key, session = window_key(thread), interactive.session_name(name)
        if listed is None or (session, key) in listed:
            log.warning("chat %s: window not confirmed stopped, worktree slot kept", key)
            return
        return_slot(self.runners[name], thread)

    def _stop(self, agent_id: str, thread: str) -> None:
        stop_window(self.tmux, self.runner.cfg.state_dir, self._name(agent_id), thread)
        if self.workspace == "write":
            self._return_if_gone(self._name(agent_id), thread)

    def _delete(self, agent_id: str, thread: str) -> bool:
        name = self._name(agent_id)
        # transcripts live under the agent's routed account; no env, no delete (the failure is acked and retried)
        env = routed_env(self.runners[name])
        found = delete_chat(self.tmux, self.runner.cfg.state_dir, name, thread, env)
        if self.workspace == "write":                       # after the window is gone; a failed delete is retried
            self._return_if_gone(name, thread)
        return found

    def _token_path(self, turn: dict) -> Path:
        return chat_dir(self.runner.cfg.state_dir, turn["thread_id"]) / "session-token"

    def _stop_orphans(self) -> None:
        """Chat windows that outlived a runner restart are not in the pool: stop them (the
        session stays resumable from its metadata), so the limits and idle rules hold. Covers every
        agent of the runner, so it runs once before the claim loops start, never inside a loop."""
        swept = False
        try:
            own = {interactive.session_name(name) for name in self.runners}

            def chat_windows():
                listed = self.tmux.windows_strict()
                if listed is None:
                    raise OSError("tmux window list unavailable")      # an unreadable list looks like an empty one
                return [(s, w) for s, w in listed if s in own and w.startswith(interactive.CHAT_PREFIX)]
            for session, window in chat_windows():
                self.tmux.kill_window(session, window)
            swept = not chat_windows()                                 # confirmed gone, not assumed
        except Exception as e:
            log.warning("chat orphan sweep: %s", type(e).__name__)
        # every chat window is gone, so every chat lease is orphaned; after a failed sweep a window may still
        # be using its slot, and the leases stay until the next start
        if swept and getattr(self, "workspace", "none") == "write":
            for ar in self.runners.values():
                try:
                    for slot in ar.pool.rows():
                        if slot["state"] == "leased" and str(slot["attempt"]).startswith(CHAT_PREFIX):
                            for path, reason in ar.pool.release(slot["attempt"]):
                                log.warning("chat orphan %s: worktree %s kept: %s", slot["attempt"], path, reason)
                except Exception as e:
                    log.warning("chat orphan slots (%s): %s", ar.agent.name, type(e).__name__)

    def _claim_agents(self) -> list[str]:
        """Hold new turns before claim; active workers, commands and idle reap continue."""
        eligible = []
        for agent_id, name in self.by_id.items():
            try:
                reason, _ = self.runners[name]._gate("transport", "", "chat")
            except Exception:                        # a broken admission hook must not claim a turn
                continue
            if not reason:
                eligible.append(agent_id)
        return eligible

    def loop(self, stop: threading.Event) -> None:
        backoff, reaped = 1.0, 0.0
        try:
            self.service.recover()
        except Exception as e:
            log.warning("chat recover: %s", getattr(e, "error_code", type(e).__name__))
        while not stop.is_set():
            t0 = time.monotonic()
            eligible, gate_s = [], 0.0
            try:
                eligible = self._claim_agents()
                gate_s = time.monotonic() - t0
                self.service.step(STEP_TIMEOUT, claim_agents=eligible)
                backoff = 1.0
            except ChatError as e:
                # the time before the failure tells a stalled connection from a read timeout; gate and
                # claim apart say which of them it was
                claim_s = getattr(self.service, "claim_s", None)
                log.warning("chat claim (%s): %s after %.1fs (gate %.1fs, claim %s)%s",
                            ", ".join(self.by_id.values()), e.error_code, time.monotonic() - t0, gate_s,
                            "-" if claim_s is None else f"{claim_s:.1f}s", f" ({e})" if e.status == 0 else "")
                stop.wait(backoff)
                backoff = min(30.0, backoff * 2)
            except Exception as e:
                log.warning("chat loop: %s", type(e).__name__)
                stop.wait(backoff)
                backoff = min(30.0, backoff * 2)
            if time.monotonic() - reaped > REAP_EVERY:
                reaped = time.monotonic()
                try:
                    self.service.reap_idle()
                except Exception as e:
                    log.warning("chat reap: %s", type(e).__name__)
            if not eligible or self.service.free_slots() <= 0 or time.monotonic() < self.service.hold_until:
                stop.wait(1.0)
