"""Side services the runner process hosts next to the task lifecycle.

Agent Chat, the session-metadata receiver, the Mesh extension endpoint (with the editor-job bridge and the
native-result adoption), the controller announcement and the extension token rotation are products of their
own. They run as threads of the runner process because they share its agents, its host capacity and its
state directory, but none of them is a step in the life of a task: nothing in `AgentRunner` starts them and
the task loop does not wait for them. `Runner.run_forever` starts each through the function here.

Each function takes the `Runner` and the stop event; every one is off unless its own config enables it.

Chat, the editor-job routes and the Gmail observer can instead run as processes of their own
(top-level `service_processes`, see `service_process.py`): then the runner does not start them here,
its supervisor starts `mesh-runner service <name>`, and that process calls the same `serve_chat` /
`attach_editor` below.
"""
from __future__ import annotations

import dataclasses
import logging
import socket
import threading
from pathlib import Path

from . import native_result_config, native_result_wait
from .mesh import MeshError

log = logging.getLogger("mesh_runner")


def start_announce(runner, stop: threading.Event) -> None:
    """Lead every configured agent in Mesh (`[runner] announce_controllers`): the runner is the
    controller of its lanes, which the My Runner screen and the chat picker read."""
    if not runner.cfg.announce_controllers:
        return
    from .chat_announce import Announcer, announce_loop
    from .chat_protocol import load_runner_id
    clients, runner_id = {ar.agent.name: ar.mesh for ar in runner.agents}, load_runner_id(runner.cfg.state_dir)
    runner._announcer = Announcer(clients, runner_id)       # start_chat adds the chat snapshot to it
    threading.Thread(target=announce_loop, name="controller-announce", daemon=True, args=(clients, runner_id, stop),
                     kwargs={"announcer": runner._announcer}).start()


def start_chat(runner, stop: threading.Event) -> None:
    """Agent Chat for the configured agents, in threads of the runner (off unless `[chat] enabled`; with chat
    in `service_processes` the chat process serves it instead)."""
    cfg = runner.cfg
    if not cfg.chat_enabled or "chat" in getattr(cfg, "service_processes", ()):
        return
    serve_chat(runner, stop)


def serve_chat(runner, stop: threading.Event) -> bool:
    """Start the chat claim loops over ``runner.agents`` (the runner's live lanes, or the lanes a chat process
    built with ``chat_lanes``). False when no configured agent resolved, so nothing was started."""
    cfg = runner.cfg
    from .chat_host import ChatHost, claim_key
    from .chat_protocol import load_runner_id, registered_client
    ids = {}
    for ar in runner.agents:
        if ar.agent.name in cfg.chat_agents:
            try:
                me = ar.mesh.me()
                ids[str(me["id"])] = ar.agent.name
            except (MeshError, KeyError, TypeError) as e:
                log.warning("chat: %s has no Mesh identity yet: %s", ar.agent.name, type(e).__name__)
                continue
            if not getattr(ar, "me", None):
                ar.me = me                      # a lane nothing ticks learns who it is here (gate hooks name it)
    if not ids:
        log.warning("chat enabled but no configured agent resolved")
        return False
    runner_id = load_runner_id(cfg.state_dir)
    from .chat_announce import Announcer, announce_loop
    announcer = getattr(runner, "_announcer", None) if cfg.announce_controllers else None
    start_own = announcer is None           # otherwise _start_announce already leads every agent
    if start_own:
        announcer = Announcer({ar.agent.name: ar.mesh for ar in runner.agents if ar.agent.name in ids.values()},
                              runner_id)
    # Mesh authorises a claim per agent key ("the key identifies the agent"): one claim loop per agent,
    # each with its own key, one pool so the host cap is shared. `[chat] credential_file` stays the
    # key of an agent whose own key is not in the runner config.
    pool, hosts = None, []
    for agent_id, name in ids.items():
        ar = next(a for a in runner.agents if a.agent.name == name)
        client = registered_client(cfg.mesh_url, claim_key(ar, cfg.chat_credential), cfg.state_dir, runner_id,
                                   "mesh-runner@" + socket.gethostname(), agent=name)
        host = ChatHost(runner, client, {agent_id: name}, cfg.chat_max_per_agent,
                        cfg.chat_host_cap, cfg.chat_idle_minutes, cfg.chat_workspace, pool=pool)
        pool = host.pool
        hosts.append((name, host))
    if hosts:
        hosts[0][1]._stop_orphans()         # one sweep for the whole runner, before any loop can start a chat
        from .chat_host import runtime_snapshot
        tmux = hosts[0][1].tmux
        announcer.snapshot = lambda: runtime_snapshot(pool, tmux, ids)
        for _, host in hosts:
            host.service.on_release = announcer.kick
    if start_own:
        threading.Thread(target=announce_loop, name="chat-announce", daemon=True, args=(
            announcer.clients, runner_id, stop), kwargs={"announcer": announcer}).start()
    for name, host in hosts:
        threading.Thread(target=host.loop, args=(stop,), name=f"chat-{name}", daemon=True).start()
    log.info("chat serving %s", ", ".join(sorted(ids.values())))
    return True


def start_meta_receiver(runner, stop: threading.Event) -> None:
    """Strict existing-session metadata receiver (off unless `[session_metadata] enabled`): a thread in this
    runner, pinned to ONE existing native session; it never starts, resumes or adopts a session."""
    cfg = runner.cfg
    st = cfg.session_metadata
    if st is None:
        return
    ar = next((a for a in runner.agents if a.agent.name == st.agent), None)
    if ar is None:
        log.warning("session_metadata: agent %s is not configured; receiver off", st.agent)
        return
    from . import meta_delivery as md
    from .chat_host import pinned, routed
    from .interactive import InteractiveAdapter, Tmux, session_name

    def known(session_id: str) -> bool:
        env, harness, profile = routed(ar)
        adapter = pinned(ar.adapter_for(harness), profile)
        return isinstance(adapter, InteractiveAdapter) and adapter.session_known(
            session_id, ar.agent.profile_dir, env)
    env, harness, profile = routed(ar)
    adapter = pinned(ar.adapter_for(harness), profile)
    if not isinstance(adapter, InteractiveAdapter):
        log.warning("session_metadata: %s runs headless; receiver off", st.agent)
        return
    tmux = Tmux(cfg.tmux_binary)
    session = md.TmuxSession(tmux, session_name(st.agent), adapter.dir, st.identity.fields["session_id"], known)
    from .chat_protocol import ChatClient, load_runner_credential
    runner_id = st.identity.fields["runner_id"]
    plain = ChatClient(cfg.mesh_url, st.credential, runner_id=runner_id)
    try:
        rid, token = load_runner_credential(cfg.state_dir, lambda: plain.register_runner("mesh-runner@" + socket.gethostname()), st.agent)
    except Exception as e:                      # no registered runner token = no receiver
        log.warning("session_metadata: runner registration failed (%s); receiver off", type(e).__name__)
        return
    if rid != runner_id:
        log.warning("session_metadata: configured runner_id is not this runner's registered id; receiver off")
        return
    client = md.MetaClient(cfg.mesh_url, st.credential, token)
    state = cfg.state_dir / "session_metadata"
    # one consumer per identity over one ledger: the standalone `session-receiver` takes the same lock
    from . import session_receiver as sr
    lock = sr.ConsumerLock(state)
    if not lock.acquire("controller"):
        log.warning("session_metadata: another consumer owns the ledger; receiver off")
        return
    try:
        sr.bind_ledger(state, st.identity, adopt_existing=(state / "journal").is_dir())
    except sr.Refusal as e:
        lock.release()
        log.warning("session_metadata: %s; receiver off", e.code)
        return
    runner._meta_lock = lock                      # held for the life of the process
    receiver = md.MetaReceiver(st.identity, session, md.Journal(state / "journal"), client, state / "acks")
    threading.Thread(target=md.receiver_loop, args=(receiver, stop, st.interval), name="session-metadata",
                     daemon=True).start()
    log.info("session metadata receiver on for session %s", st.identity.fields["session_id"][:8])


def native_config_receiver(runner, policy):
    """Fresh real Store + Mesh context. Scope is not operator authority."""
    target = policy["receiver"]
    ar = next((a for a in runner.agents if a.agent.name == policy["agent"]), None)
    if ar is None:
        raise native_result_config.Refused("unknown_lane")
    a = ar.store.get(target["attempt"])
    if (a is None or a.agent != ar.agent.name or a.task_id != target["task"]
            or a.mode != "work" or a.state not in ("running", "waiting", "ready")
            or a.session_id != target["session"] or ar._maintenance_held(a)):
        raise native_result_config.Refused("receiver_not_current")
    task = ar._task(a.task_id)
    actual = native_result_wait.current_receiver(dict(dataclasses.asdict(a), state="waiting"), task, ar.agent_id)
    if actual != target:
        raise native_result_config.Refused("receiver_not_current")


def attach_editor(service, cfg) -> None:
    """The tgpeek editor-job routes (`[tgpeek_editor]`) and their signed operator lifecycle
    (`[tgpeek_editor_control]`) on an extension listener: the runner's, or the editor process's own."""
    from .host_admission import HostAdmission
    from .interactive import Tmux, state_root

    def admission():
        # one host cap with the card and chat windows: the same lock and count the tui driver uses (a flock and
        # tmux, so it holds across processes)
        return HostAdmission(state_root(cfg.state_dir), cfg.interactive_max_sessions, Tmux(cfg.tmux_binary))
    bridge = None
    if cfg.tgpeek_editor is not None:
        from .editor_job import build_bridge
        bridge = service.editor = build_bridge(cfg.tgpeek_editor, admission=admission())
        log.info("tgpeek editor-job routes on (executor %s)", "configured" if cfg.tgpeek_editor.argv else "unprovisioned")
    if cfg.tgpeek_editor_control_secret_file and cfg.config_path:
        import tomllib
        from .editor_control import EditorController
        path = cfg.config_path

        def section() -> dict:
            return tomllib.loads(Path(path).read_text()).get("tgpeek_editor") or {}
        service.controller = EditorController(service, cfg.state_dir, cfg.tgpeek_editor_control_secret_file, section,
                                              admission=admission, source=path)
        service.controller.adopt(bridge, cfg.tgpeek_editor)
        log.info("operator editor lifecycle on (loopback, signed)")


def start_extension(runner, stop: threading.Event):
    """The Mesh extension endpoint and the extension token's runner-rotation (`[extension]`)."""
    cfg = runner.cfg
    service = None
    if cfg.extension_enabled:
        from .extension_service import ExtensionService
        service = ExtensionService(cfg.state_dir, runner.wake_event, cfg.extension_workspace_id,
                                   expected_secret=cfg.extension_secret,
                                   trust_first_install=cfg.extension_trust_first_install, allow_rekey=cfg.extension_allow_rekey,
                                   tolerance=cfg.extension_tolerance_seconds)
        if "editor" not in getattr(cfg, "service_processes", ()):
            attach_editor(service, cfg)
        if cfg.native_result_adoption:
            service.native_config = native_result_config.Adoption(runner)
        service.serve(cfg.extension_host, cfg.extension_port)
        log.info("extension endpoint on %s:%s", cfg.extension_host, cfg.extension_port)
    token = cfg.extension_token
    if token is not None and token.file and cfg.extension_rotate_days > 0 and runner.agents:
        from .extension_service import rotate_token
        client = runner.agents[0].mesh

        def rotate_loop() -> None:
            while not stop.wait(cfg.extension_rotate_days * 86400):
                rotate_token(token, client)
        threading.Thread(target=rotate_loop, name="extension-token", daemon=True).start()
    return service
