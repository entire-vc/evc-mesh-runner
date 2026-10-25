"""Wires the chat service into a runner: the per-agent launch plan and the loop thread.

The plan reuses the agent's own routing (``_fresh_env``: account, profile, identity) and its
interactive adapter, so a chat lands in the same profile (CLAUDE.md, skills, .mcp.json) as the
agent's cards. A chat never runs headless: without an interactive adapter the turn fails with
a stable error instead of being billed per token.

chat_workspace: ``none`` and ``read`` run in the agent's profile directory with no repository
checkout; ``write`` (a slot from the card pool) is not implemented here and is refused.
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
from .interactive import InteractiveAdapter, read_meta

log = logging.getLogger("mesh_runner")
NS = uuid.UUID("5d8e1d6e-7c1a-4c53-9a0e-6b1d2a1f0c11")
STEP_TIMEOUT = 30
REAP_EVERY = 30.0


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


def routed(ar, clock=time.monotonic, ttl: float = ENV_TTL) -> tuple[dict, str]:
    """``ar._fresh_env()`` kept for ``ttl`` seconds per agent: it runs the lane's env command (the account
    router, ~3 s and sometimes far more under load), which a chat answering a person cannot pay per message.
    A lane moved to another account is picked up within ``ttl`` seconds."""
    with _env_lock:                       # a miss also holds the lock: env, harness and profile are read as one
        hit = _env_cache.get(id(ar))
        if hit and clock() - hit[0] < ttl:
            ar._routed_harness = hit[2]
            target = ar.adapter_for(hit[2])          # the profile routing lives on the adapter, not in the env
            if hasattr(target, "profile"):
                target.profile = hit[3]
            return dict(hit[1]), hit[2]
        env = ar._fresh_env()
        harness = ar._routed_harness
        _env_cache[id(ar)] = (clock(), dict(env), harness, getattr(ar.adapter_for(harness), "profile", None))
        return dict(env), harness


def routed_env(ar, clock=time.monotonic, ttl: float = ENV_TTL) -> dict:
    return routed(ar, clock, ttl)[0]


def make_plan(ar, client: ChatClient, chat_workspace: str = "none"):
    """``plan(turn)`` for ``DriverDelivery``, bound to one AgentRunner ``ar``."""
    def plan(turn: dict):
        if chat_workspace == "write":
            raise ChatRefused("chat_workspace_unsupported", "chat_workspace=write is not available on this runner")
        env, harness = routed(ar)                          # account/profile routing, may raise
        adapter = ar.adapter_for(harness)
        if not isinstance(adapter, InteractiveAdapter):
            raise ChatRefused("headless_forbidden", "chats run in interactive sessions only")
        thread = turn["thread_id"]
        key = window_key(thread)
        meta = read_meta(adapter.dir, key)
        warm_id = str(meta.get("session_id") or "")
        sid = warm_id or (session_id_for(thread) if adapter.session_id_known_upfront else "")
        resume = bool(warm_id)
        argv = (adapter.resume_argv if resume else adapter.start_argv)(sid, ar.agent.profile_dir, [])
        i = argv.index("--")
        # a chat answers a person: short polling and settle (the card defaults are 1 s / 1.5 s)
        argv = argv[:i] + ["--key", key, "--poll", CHAT_POLL, "--settle", CHAT_SETTLE] + argv[i:]
        argv = adapter.with_read_only(argv)                 # no repository in none/read: enforced by the harness
        env = dict(env)
        env["MESH_CHAT_TOKEN_FILE"] = str(chat_dir(ar.cfg.state_dir, thread) / "session-token")
        history = None
        if not resume:                                      # a new session for an old chat: feed it the past
            try:
                history = client.history(thread, limit=20).get("messages") or None
            except ChatError:
                history = None
        files = fetch_attachments(client, turn, ar.cfg.state_dir)
        return argv, ar.agent.profile_dir, env, build_prompt(turn, files, history), harness
    return plan


class ChatHost:
    """One chat service for the agents this runner serves (those listed in the config)."""

    def __init__(self, runner, client: ChatClient, agent_ids: dict[str, str], max_per_agent: int,
                 host_cap: int, idle_minutes: float, chat_workspace: str = "none"):
        self.runner, self.client = runner, client
        self.by_id = agent_ids                              # Mesh agent id -> runner agent name
        self.tmux = interactive.Tmux(runner.cfg.tmux_binary)
        self.runners = {ar.agent.name: ar for ar in runner.agents}
        pool = ChatPool(max_per_agent=max_per_agent, max_host=host_cap, idle_minutes=idle_minutes,
                        host_used=self._card_sessions)
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

    def _stop(self, agent_id: str, thread: str) -> None:
        stop_window(self.tmux, self.runner.cfg.state_dir, self._name(agent_id), thread)

    def _delete(self, agent_id: str, thread: str) -> bool:
        name = self._name(agent_id)
        # transcripts live under the agent's routed account; no env, no delete (the failure is acked and retried)
        env = routed_env(self.runners[name])
        return delete_chat(self.tmux, self.runner.cfg.state_dir, name, thread, env)

    def _token_path(self, turn: dict) -> Path:
        return chat_dir(self.runner.cfg.state_dir, turn["thread_id"]) / "session-token"

    def _stop_orphans(self) -> None:
        """Chat windows that outlived a runner restart are not in the pool: stop them (the
        session stays resumable from its metadata), so the limits and idle rules hold."""
        try:
            for session, window in self._own_windows():
                if window.startswith(interactive.CHAT_PREFIX):
                    self.tmux.kill_window(session, window)
        except Exception as e:
            log.warning("chat orphan sweep: %s", type(e).__name__)

    def loop(self, stop: threading.Event) -> None:
        backoff, reaped = 1.0, 0.0
        try:
            self.service.recover()
        except Exception as e:
            log.warning("chat recover: %s", getattr(e, "error_code", type(e).__name__))
        self._stop_orphans()
        while not stop.is_set():
            try:
                self.service.step(STEP_TIMEOUT)
                backoff = 1.0
            except ChatError as e:
                log.warning("chat claim: %s", e.error_code)
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
            if self.service.free_slots() <= 0 or time.monotonic() < self.service.hold_until:
                stop.wait(1.0)
