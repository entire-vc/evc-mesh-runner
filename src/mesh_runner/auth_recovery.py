"""Explicit auth-change trigger on the installed signed ingress, not an auth grant.

Only direct API-key authentication has a free remote proof here. Native OAuth,
custom gateways and local CLI login status remain UNKNOWN. Model entitlement,
verification-required and permission holds are deliberately separate.
"""
from __future__ import annotations

import argparse
import json
import time
import threading
import uuid
import urllib.error
import urllib.request

from . import auth
from .store import Store

OUTBOX = "auth_wake_v1"
_DELIVERY_LOCK = threading.Lock()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # Never forward the own credential to another origin.


def environment(ar):
    # Pure routing: do not replace a concurrently running agent's cached env.
    env, harness, _ = ar._route_env(ar._read_env())
    from .runner import session_env
    return session_env(env) if harness == "claude" else None


def probe(env):
    """Return auth-ready/deny/unknown; never starts a model or reads a credential store."""
    if (not env or not env.get("ANTHROPIC_API_KEY")
            or env.get("ANTHROPIC_BASE_URL", "") not in ("", "https://api.anthropic.com")
            or any(env.get(k) for k in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN",
                                       "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
                                       "CLAUDE_CODE_USE_FOUNDRY"))):
        return "unknown"
    # Official Models API: authentication proof only, no Messages entitlement.
    request = urllib.request.Request("https://api.anthropic.com/v1/models?limit=1",
        headers={"x-api-key": env["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01"})
    proxies = {k: env[k.upper() + "_PROXY"] for k in ("http", "https") if env.get(k.upper() + "_PROXY")}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(proxies), NoRedirect())
    try:
        with opener.open(request, timeout=12) as response:
            value = json.loads(response.read(65537))
            return "auth-ready" if response.status == 200 and isinstance(value, dict) and isinstance(value.get("data"), list) else "unknown"
    except urllib.error.HTTPError as exc:
        status = "deny" if exc.code in (401, 403) else "unknown"
        exc.close()
        return status
    except (OSError, ValueError):
        return "unknown"


def consume(runner, payload):
    """Fresh own probe, replay fence, then CAS exactly one durable generation."""
    if (not isinstance(payload, dict) or type(payload.get("version")) is not int or payload["version"] != 1
            or not runner.cfg.extension_enabled or payload.get("workspace_id") != runner.cfg.extension_workspace_id):
        return []
    observed = payload.get("observed_at")
    if type(observed) not in (float, int) or not time.time() - 300 <= observed <= time.time() + 5:
        return []
    event = payload.get("event_id")
    try:
        if str(uuid.UUID(event)) != event:
            return []
    except (ValueError, TypeError, AttributeError):
        return []
    profile, generation = payload.get("profile"), payload.get("generation")
    if not isinstance(profile, str) or not auth.PROFILE.fullmatch(profile) or not isinstance(generation, str) or not generation:
        return []
    ar = next((ar for ar in runner.agents if ar.agent.name == payload.get("lane")), None)
    if not ar or not ar.agent_id or ar.agent_id != payload.get("mesh_actor"):
        return []
    store = Store(runner.cfg.state_dir / "runner.sqlite")
    task = auth.PREFIX + profile
    try:
        row = store.mark(auth.OWNER, task, auth.KEY)
        scope = store.mark(auth.OWNER, task, "auth_hold_scope")
        # Old holds have no scope provenance. Never reinterpret verification as
        # authentication or clear legacy unknowns from a Models API observation.
        if (not row or not row[0] or not scope or scope[0] != generation
                or not generation.startswith(row[0] + ":") or not generation.endswith(":authentication")):
            return []
        from .store import parse_time
        since = parse_time(row[0])
        if since is None or observed < since.timestamp():
            return []
        env = environment(ar)
        if env is None or auth.profile_id(env, ar.agent.profile_dir) != profile:
            return []
        if not store.claim(auth.OWNER, task, "auth_event_v1:" + event, "observed"):
            return []
        if probe(env) != "auth-ready":
            return []
        # A changed routing/key during the probe is not proof for the new route.
        current = environment(ar)
        if current != env:
            return []
        # Hold release and its wake obligation are one durable transaction.
        # The provider request stays outside the writer lock.
        store.db.execute("SAVEPOINT auth_restore")
        try:
            changed = store.db.execute(
                "UPDATE marks SET value='' WHERE agent=? AND task_id=? AND key=? AND value=? "
                "AND EXISTS(SELECT 1 FROM marks AS s WHERE s.agent=? AND s.task_id=? AND s.key=? AND s.value=?)",
                (auth.OWNER, task, auth.KEY, row[0], auth.OWNER, task, "auth_hold_scope", generation)).rowcount
            if changed != 1:
                store.db.execute("RELEASE auth_restore")
                return []
            store.mark(auth.OWNER, task, OUTBOX, json.dumps(dict(
                generation=generation, lane=ar.agent.name, mesh_actor=ar.agent_id,
                workspace_id=runner.cfg.extension_workspace_id), sort_keys=True))
            store.db.execute("DELETE FROM marks WHERE task_id=? AND key='auth_route_attempt'", (task,))
            store.db.execute("RELEASE auth_restore")
        except Exception:
            store.db.execute("ROLLBACK TO auth_restore")
            store.db.execute("RELEASE auth_restore")
            raise
        for attempt in store.by_state(ar.agent.name, ("ready",)):
            if attempt.note == f"authentication held for profile {profile}":
                ar._resume_hold.pop(attempt.id, None)
        return [ar.agent.name]
    finally:
        store.db.close()



def pending(runner, ar):
    """Validated obligations only. This reads local state; it never probes or grants."""
    if not runner.cfg.extension_enabled or not ar.agent_id:
        return {}
    store = Store(runner.cfg.state_dir / "runner.sqlite")
    try:
        out = {}
        # No routing command or credential access during ordinary idle ticks.
        # Only a recorded obligation for this exact lane can reach environment().
        candidates = []
        for task, raw in store.marked(auth.OWNER, OUTBOX).items():
            if not task.startswith(auth.PREFIX) or not auth.PROFILE.fullmatch(task.removeprefix(auth.PREFIX)):
                continue
            try:
                value = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if (isinstance(value, dict) and value.get("lane") == ar.agent.name
                    and value.get("mesh_actor") == ar.agent_id
                    and value.get("workspace_id") == runner.cfg.extension_workspace_id):
                candidates.append((task, raw, value))
        if not candidates:
            return out
        env = environment(ar)
        if env is None:
            return out
        profile = auth.profile_id(env, ar.agent.profile_dir)
        for task, raw, value in candidates:
            if task != auth.PREFIX + profile:
                continue
            scope = store.mark(auth.OWNER, task, "auth_hold_scope")
            if (scope and value.get("generation") == scope[0] and not auth.held(store, profile)):
                out[task] = raw
        return out
    finally:
        store.db.close()


def deliver(runner, only=None):
    """Schedule each durable obligation once in this process; restart can redeliver.

    Acknowledgement belongs AFTER an ordinary admission tick. Death before that
    point may redeliver a scheduling hint, never a new attempt or provider proof.
    """
    with _DELIVERY_LOCK:
        seen = getattr(runner, "_auth_wake_seen", None)
        if seen is None:
            seen = runner._auth_wake_seen = {}
        names = []
        for ar in ([only] if only is not None else runner.agents):
            for task, value in pending(runner, ar).items():
                key = (ar.agent.name, task)
                if seen.get(key) == value:
                    continue
                runner.wake([ar.agent.name])
                seen[key] = value
                names.append(ar.agent.name)
        return names


def acknowledge(runner, ar, before):
    """Ack only obligations present before the completed ordinary tick."""
    current = pending(runner, ar)
    store = Store(runner.cfg.state_dir / "runner.sqlite")
    try:
        for task, value in before.items():
            if current.get(task) == value:
                store.compare_mark(auth.OWNER, task, OUTBOX, value, "")
    finally:
        store.db.close()


def notify_command(argv):
    """Explicit owner repair-completion notifier; exact route, no credential mutation."""
    from .config import load
    from .runner import Runner
    p = argparse.ArgumentParser(prog="mesh-runner notify-auth-change")
    p.add_argument("--config", required=True)
    p.add_argument("--agent", required=True)
    args = p.parse_args(argv)
    runner = Runner(load(args.config), lock=False)
    try:
        ar = next((ar for ar in runner.agents if ar.agent.name == args.agent), None)
        env = environment(ar) if ar else None
        if env is None:
            print('{"status":"held","reason":"unsupported-auth-route"}')
            return 1
        profile = auth.profile_id(env, ar.agent.profile_dir)
    finally:
        for ar in runner.agents:
            ar.store.db.close()
        runner.store.db.close()
    # Producer supplies an event, never proof: the signed consumer checks the
    # configured own actor and the current generation, then performs fresh HTTPS.
    return command(["--config", args.config, "--agent", args.agent, "--profile", profile])


def command(argv):
    """Owned explicit auth-change notification; consumer supplies all proof."""
    import ipaddress
    from .config import load
    from .runner import Runner
    from .mesh import MeshError
    from .extension_service import SecretStore, sign
    p = argparse.ArgumentParser(prog="mesh-runner recheck-auth")
    p.add_argument("--config", required=True)
    p.add_argument("--agent", required=True)
    p.add_argument("--profile", required=True)
    args = p.parse_args(argv)
    cfg = load(args.config)
    host = cfg.extension_host or "127.0.0.1"
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1"
    try:
        available = cfg.extension_enabled and ipaddress.ip_address(host).is_loopback
    except ValueError:
        available = False
    record = SecretStore(cfg.state_dir).load()
    if not available or not record or record.get("workspace_id") != cfg.extension_workspace_id:
        print('{"status":"held","reason":"recovery-endpoint-unavailable"}')
        return 2
    runner = Runner(cfg, lock=False)
    try:
        ar = next((ar for ar in runner.agents if ar.agent.name == args.agent), None)
        row = runner.store.mark(auth.OWNER, auth.PREFIX + args.profile, auth.KEY)
        scope = runner.store.mark(auth.OWNER, auth.PREFIX + args.profile, "auth_hold_scope")
        if not ar or not row or not row[0] or not scope or not auth.PROFILE.fullmatch(args.profile):
            print('{"status":"held","reason":"profile-not-held"}')
            return 1
        ar.refresh_me()  # Same authenticated Mesh actor, no initialization/tick.
        body = json.dumps(dict(event="auth.recheck", version=1, event_id=str(uuid.uuid4()),
            workspace_id=cfg.extension_workspace_id, lane=args.agent, mesh_actor=ar.agent_id,
            profile=args.profile, generation=scope[0], observed_at=time.time())).encode()
        stamp = str(int(time.time()))
        target = "[" + host + "]" if ":" in host else host
        request = urllib.request.Request(f"http://{target}:{cfg.extension_port}/mesh/events", data=body,
            headers={"X-Mesh-Timestamp": stamp, "X-Mesh-Signature": sign(record["signing_secret"], stamp, body)})
        with urllib.request.urlopen(request, timeout=30) as response:
            accepted = response.status == 202
        current = runner.store.mark(auth.OWNER, auth.PREFIX + args.profile, auth.KEY)
        released = accepted and current is not None and current[0] == ""
        print(json.dumps(dict(status="rechecked" if released else "held", released=released,
                              model_capability="unknown")))
        return 0 if released else 1
    except (OSError, MeshError):
        print('{"status":"held","reason":"recovery-unavailable"}')
        return 1
    finally:
        for ar in runner.agents:
            ar.store.db.close()
        runner.store.db.close()
