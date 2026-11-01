"""Explicit own-capability recovery. Signed events trigger a probe, never grant access.

Only trusted start hooks emitting the v1 binding opt in. SQLite marks fence a
single hold epoch across restarts; ordinary task events/time cannot release it.
"""
from __future__ import annotations

import argparse
import json
import re
import time
import uuid
from pathlib import Path

KEY = "access_hold_v1"
REASON = "capability held; waits for authenticated own-access recheck"
FIELDS = {"version", "task_id", "lane", "mesh_actor", "gitlab_actor", "project",
          "capability", "head", "route_sha256"}


def packed(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def binding(value, lane, task):
    if (not isinstance(value, dict) or set(value) != FIELDS
            or type(value.get("version")) is not int or value["version"] != 1
            or value.get("lane") != lane or value.get("task_id") != task
            or type(value.get("gitlab_actor")) is not int or value["gitlab_actor"] <= 0
            or any(not isinstance(value.get(k), str) or not value[k] or len(value[k]) > 512
                   for k in ("mesh_actor", "project", "capability"))
            or not re.fullmatch(r"[0-9a-f]{40}", str(value.get("head", "")))
            or not re.fullmatch(r"[0-9a-f]{64}", str(value.get("route_sha256", "")))):
        return None
    return value


def held(store, lane, task):
    row = store.mark(lane, task, KEY)
    return row[0] if row and row[0] else ""


def remember(ar, task, mode, raw):
    try:
        value = binding(json.loads(raw), ar.agent.name, task)
    except (ValueError, TypeError):
        return
    if value is None:
        return
    hold = packed(dict(binding=value, epoch=str(uuid.uuid4()), created_at=time.time(), mode=mode))
    # Never overwrite a current epoch (even if routing changes underneath it).
    from .store import iso
    ar.store.db.execute(
        "INSERT INTO marks VALUES (?,?,?,?,?) ON CONFLICT(agent,task_id,key) "
        "DO UPDATE SET value=excluded.value,at=excluded.at WHERE marks.value=''",
        (ar.agent.name, task, KEY, hold, iso()))


def probe(ar, task, raw):
    """Invoke only the own read-only hook phase; no _gate, capacity or spawn."""
    try:
        hold = json.loads(raw)
        expected = binding(hold["binding"], ar.agent.name, task)
        if expected is None or hold["mode"] not in ("work", "review") or not ar.agent.start_gate_command:
            return False
        rc, out, _ = ar._run_hook(ar.agent.start_gate_command,
                                 ar._hook_env(task, hold["mode"], "access-recheck"), timeout=120)
        observed = json.loads(ar._kv(out).get("ACCESS_ALLOW", "null"))
        return rc == 0 and observed == expected
    except (ValueError, TypeError, KeyError):
        return False


def consume(runner, payload):
    """Authenticated ingress calls this. CAS releases one exact epoch only after a fresh probe."""
    if not isinstance(payload, dict) or payload.get("version") != 1 or type(payload.get("version")) is not int:
        return []
    if not runner.cfg.extension_enabled or payload.get("workspace_id") != runner.cfg.extension_workspace_id:
        return []
    observed = payload.get("observed_at")
    if type(observed) not in (int, float) or not time.time() - 300 <= observed <= time.time() + 5:
        return []
    event_id = payload.get("event_id")
    try:
        if str(uuid.UUID(event_id)) != event_id:
            return []
    except (ValueError, TypeError, AttributeError):
        return []
    task, lane = payload.get("task_id"), payload.get("lane")
    if not isinstance(task, str) or not isinstance(lane, str):
        return []
    ar = next((ar for ar in runner.agents if ar.agent.name == lane), None)
    if ar is None:
        return []
    # Use this callback's dedicated connection, not an agent's concurrently ticking connection.
    from .store import Store
    store = Store(runner.cfg.state_dir / "runner.sqlite")
    try:
        raw = held(store, lane, task)
        try:
            hold = json.loads(raw)
            if (payload.get("hold") != hold or type(hold["created_at"]) not in (int, float)
                    or observed < hold["created_at"]):
                return []
        except (ValueError, TypeError, KeyError):
            return []
        # Persist replay rejection even if this probe remains UNKNOWN. A later
        # confirmed permission event must carry a fresh ID, never replay a grant.
        if not store.claim(lane, task, "access_event_v1:" + event_id, "observed"):
            return []
        if not probe(ar, task, raw):
            return []
        if not store.compare_mark(lane, task, KEY, raw, ""):
            return []
        ar._gate_hold.pop(task, None)
        return [lane]
    finally:
        store.db.close()


def command(argv):
    """Owned explicit recheck entrypoint, intended for a credential/permission event handler.

    No timer, no alternate account, no CLI-provided proof or arbitrary destination.
    First probe here; the running consumer probes again before clearing the hold.
    """
    import ipaddress
    import urllib.error
    import urllib.request
    from .config import load
    from .runner import Runner
    from .mesh import MeshError
    from .extension_service import SecretStore, sign
    p = argparse.ArgumentParser(prog="mesh-runner recheck-access")
    p.add_argument("--config", required=True)
    p.add_argument("--agent", required=True)
    p.add_argument("--task", required=True)
    args = p.parse_args(argv)
    cfg = load(args.config)
    host = cfg.extension_host or "127.0.0.1"
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1"
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if not cfg.extension_enabled or not loopback:
        print('{"status":"held","reason":"recovery-endpoint-unavailable"}')
        return 2
    rec = SecretStore(cfg.state_dir).load()
    if not rec or rec.get("workspace_id") != cfg.extension_workspace_id:
        print('{"status":"held","reason":"recovery-install-unavailable"}')
        return 2
    runner = Runner(cfg, lock=False)
    ar = next((a for a in runner.agents if a.agent.name == args.agent), None)
    raw = held(runner.store, args.agent, args.task)
    if ar and raw:
        try:
            # Runner construction does not initialize agents. Use the normal
            # authenticated read only; no tick, checkout or session adoption.
            ar.refresh_me()
            expected = binding(json.loads(raw)['binding'], args.agent, args.task)
            if expected is None or not ar.agent_id or ar.agent_id != expected['mesh_actor']:
                ar = None
        except (MeshError, ValueError, TypeError, KeyError):
            ar = None
    if not ar or not raw or not probe(ar, args.task, raw):
        print('{"status":"held","reason":"own-capability-unconfirmed"}')
        return 1
    body = packed(dict(event="access.restored", version=1, event_id=str(uuid.uuid4()), workspace_id=cfg.extension_workspace_id,
                       lane=args.agent, task_id=args.task, hold=json.loads(raw), observed_at=time.time())).encode()
    timestamp = str(int(time.time()))
    url_host = "[" + host + "]" if ":" in host else host
    request = urllib.request.Request(f"http://{url_host}:{cfg.extension_port}/mesh/events", data=body,
                                    headers={"X-Mesh-Timestamp":timestamp,
                                             "X-Mesh-Signature":sign(rec["signing_secret"], timestamp, body),
                                             "Content-Type":"application/json"})
    try:
        with urllib.request.urlopen(request, timeout=130) as response:
            accepted = response.status == 202
    except (OSError, urllib.error.HTTPError):
        accepted = False
    # HTTP acceptance isn't proof of release; report the persisted readback only.
    released = accepted and not held(runner.store, args.agent, args.task)
    print(packed(dict(status="rechecked" if released else "held", released=released)))
    return 0 if released else 1


def notify_command(argv):
    """Owner-host grant ACK trigger. Select existing exact holds; never grant access."""
    import contextlib
    import io
    from .config import load
    from .store import Store
    p = argparse.ArgumentParser(prog="mesh-runner notify-access-change")
    p.add_argument("--config", required=True)
    p.add_argument("--gitlab-actor", required=True, type=int)
    p.add_argument("--project", required=True)
    args = p.parse_args(argv)
    cfg = load(args.config)
    db = cfg.state_dir / "runner.sqlite"
    if args.gitlab_actor <= 0 or not db.is_file():
        print(packed(dict(status="notification_pending", reason="owner-state-unavailable")))
        return 1
    store = Store(db)
    selected = []
    try:
        for lane in cfg.agents:
            for task, raw in store.marked(lane.name, KEY).items():
                try:
                    value = binding(json.loads(raw)["binding"], lane.name, task)
                except (ValueError, TypeError, KeyError):
                    continue
                if (value and value["gitlab_actor"] == args.gitlab_actor
                        and value["project"] == args.project
                        and value["capability"] == "independent-account-note-label/v1"):
                    selected.append((lane.name, task))
    finally:
        store.db.close()
    if len(selected) > 16:
        print(packed(dict(status="notification_pending", reason="matching-hold-limit", selected=len(selected))))
        return 1
    released = 0
    for lane, task in selected:
        # Reuse the real explicit producer + signed ingress + running consumer.
        # Suppress per-task output; only bounded delivery facts leave this CLI.
        with contextlib.redirect_stdout(io.StringIO()):
            rc = command(["--config", args.config, "--agent", lane, "--task", task])
        released += rc == 0
    complete = released == len(selected)
    print(packed(dict(status="rechecked" if complete else "notification_pending",
                      selected=len(selected), released=released)))
    return 0 if complete else 1
