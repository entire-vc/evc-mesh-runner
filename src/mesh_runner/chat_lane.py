"""What Agent Chat needs from an agent lane, and nothing more.

A chat turn runs in the lane's account, profile and harness, may be held by the lane's start gate and, in
``chat_workspace = "write"``, leases a slot of the lane's worktree pool. Those four things are all chat takes
from ``AgentRunner``; this module names them, so the chat code depends on this small surface instead of the
lane's whole object, and the same chat code runs in two places:

- in the runner process, over the live ``AgentRunner`` objects whose ticks run next to it;
- in its own process (``mesh-runner service chat``), over ``AgentRunner`` objects built for the chat agents only
  (``chat_lanes``): nothing ticks them, they are read for routing, the adapter and the transport gate.

Both modes call the same ``AgentRunner`` methods, so the routing and gate logic exists once.
"""
from __future__ import annotations


class ChatLane:
    """One agent lane as chat sees it. Stateless: it reads through to the lane object it wraps (``owner``),
    so building one per call costs nothing and two of them over one lane share every cache keyed on it."""

    def __init__(self, owner):
        self.owner = owner

    @property
    def name(self) -> str:
        return self.owner.agent.name

    @property
    def agent(self):
        """The lane's configuration: profile_dir, repos, key, harness."""
        return self.owner.agent

    @property
    def cfg(self):
        return self.owner.cfg

    @property
    def pool(self):
        """The lane's worktree pool (``chat_workspace = "write"`` only); None when the lane has none."""
        return getattr(self.owner, "pool", None)

    @property
    def routed_harness(self) -> str:
        """The harness the lane was last routed to; only a fallback, a turn names the one its env routed it to."""
        return getattr(self.owner, "_routed_harness", "") or self.owner.agent.harness

    def read_env(self) -> dict:
        """The lane's session environment as its env file, env command and inline values give it now (slow:
        the env command may route the lane to another account)."""
        return self.owner._read_env()

    def route_env(self, raw: dict) -> tuple[dict, str, object]:
        """``(session env, harness, profile)`` a read env routes the lane to; pure."""
        return self.owner._route_env(raw)

    def adapter_for(self, harness: str):
        return self.owner.adapter_for(harness)

    def transport_hold(self) -> str:
        """The operator's start gate in its ``transport`` phase: '' = a new chat turn may be claimed, otherwise
        the reason it is held. A gate that cannot run raises; the caller treats that as a hold."""
        reason, _ = self.owner._gate("transport", "", "chat")
        return reason


def lane_of(obj) -> ChatLane:
    """``obj`` as a ChatLane: a lane is returned as it is, anything else (an ``AgentRunner``) is wrapped."""
    return obj if isinstance(obj, ChatLane) else ChatLane(obj)


def chat_lanes(cfg) -> list:
    """The lanes of the configured chat agents for a chat process: ``AgentRunner`` objects that nothing
    ticks, each with its own connection to the shared store. Building one starts no session, runs no git
    and calls nothing remote; ``serve_chat`` asks Mesh who each agent is."""
    from .runner import AgentRunner
    from .store import Store
    wanted = set(cfg.chat_agents)
    return [AgentRunner(cfg, a, Store(cfg.state_dir / "runner.sqlite")) for a in cfg.agents if a.name in wanted]
