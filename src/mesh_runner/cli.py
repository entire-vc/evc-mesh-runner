"""mesh-runner command line.

    mesh-runner run    --config runner.toml     # service loop
    mesh-runner once   --config runner.toml     # one tick, print status
    mesh-runner status --config runner.toml     # attempts from the local registry, no Mesh calls
    mesh-runner plan   --config runner.toml     # dry run: what would start, and why not
    mesh-runner slots  --config runner.toml     # worktree pool, with reasons for held slots
    mesh-runner free-slot --config runner.toml --path <slot>   # return a held slot if it is safe now
    mesh-runner free-slot --config runner.toml --path <slot> --archive   # save uncommitted work on a ref, then return it
    mesh-runner quarantine-list --config runner.toml --path <slot> [--verify]   # archived-source quarantine runs of a slot
    mesh-runner quarantine-restore --config runner.toml --path <slot> --run <run>   # put a run's entries back
    mesh-runner asks classify --config runner.toml [--json]   # read-only: open human gates, hard or soft
    mesh-runner reviewer-merge --config runner.toml --agent <lane> --task <uuid> --mr group/project!iid --sha <full sha> [--apply]   # opt-in reviewer merge-only handoff; dry-run unless --apply
    mesh-runner pool   --config runner.toml [--prune-ignored [--ignored-idle-hours H]] [--prune-checkpoints [--checkpoint-grace-hours H]] [--compact-checkpoints] [--evict [--apply] --idle-hours H --keep N --max-evictions M]
"""
from __future__ import annotations

import argparse
from pathlib import Path
import json
import logging
import math
import os
import signal
import sys
import threading

from . import config as config_mod
from . import maintenance
from . import repo_admission
from . import workspace_requests
from . import git_index_admission
from .native_result_ledger import identity as native_wait_identity
from . import auth
from .runner import AlreadyRunning, Runner
from .store import OPEN, Store
from .workspace import WorkspaceError


def _idle_hours_arg(text: str) -> float:
    try:
        hours = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}") from None
    if not math.isfinite(hours) or hours < 0:
        raise argparse.ArgumentTypeError(f"must be >= 0, got {text!r}")     # never a cutoff in the future
    return hours


def _asks(argv: list[str]) -> int:
    """`asks classify`: GET requests only, never raises a gate or writes a comment."""
    from . import ask_audit
    p = argparse.ArgumentParser(prog="mesh-runner asks")
    p.add_argument("action", choices=["classify"])
    p.add_argument("--config", required=True)
    p.add_argument("--json", action="store_true", help="machine-readable output")
    args = p.parse_args(argv)
    cfg = config_mod.load(args.config)
    rows = ask_audit.open_gates(cfg)
    totals = ask_audit.summary(rows)
    if args.json:
        json.dump({"summary": totals, "gates": rows}, sys.stdout, ensure_ascii=False, indent=2)
        print()
        return 0
    for r in rows:
        if "verdict" not in r:
            print("errors: " + "; ".join(r["errors"]))
            continue
        print(f"{r['verdict'].upper():4}  #{r['task']}  @{r['asked'] or '?':<14} {r['rule']:<28} {r['title']}")
    print(f"total {totals['gates']}: hard {totals['hard']}, soft {totals['soft']}")
    return 0


def _reviewer_merge(argv: list[str]) -> int:
    """`reviewer-merge`: dry-run by default (reads only); --apply creates the one merge-only card."""
    from . import reviewer_merge
    from .mesh import MeshClient
    from .wake import GitLab
    p = argparse.ArgumentParser(prog="mesh-runner reviewer-merge")
    p.add_argument("--config", required=True)
    p.add_argument("--agent", required=True, help="lane whose Mesh key is used to read and create")
    p.add_argument("--task", required=True, help="product card, full UUID")
    p.add_argument("--mr", required=True, help="group/project!iid")
    p.add_argument("--sha", required=True, help="full expected head sha")
    p.add_argument("--apply", action="store_true")
    args = p.parse_args(argv)
    cfg = config_mod.load(args.config)
    agent = next((a for a in cfg.agents if a.name == args.agent), None)
    if agent is None:
        p.error("--agent must name a configured lane")
    mesh = MeshClient(cfg.mesh_url, agent.key, basic_auth=cfg.mesh_basic_auth)
    try:
        ws = (mesh.me() or {}).get("workspace_id", "")
        ids = {str(r.get("name")): r["id"] for r in mesh.workspace_agents(ws) if r.get("id") and r.get("name")}
        out = reviewer_merge.request(
            mesh=mesh, gitlab=GitLab(cfg.gitlab_url, cfg.gitlab_token), source_id=args.task, target=args.mr,
            sha=args.sha, selector_argv=list(cfg.reviewer_select_command), agent_ids=ids, apply=args.apply, lock_dir=str(cfg.state_dir / "reviewer-merge"),
            usernames={a.name: a.gitlab_username for a in cfg.agents if a.gitlab_username})
    except reviewer_merge.Refused as e:
        out = {"mode": "apply" if args.apply else "dry-run", "ready": False, "refusals": [str(e)],
               "request": None, "created": False, "card": None}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out["ready"] else 1


def _editor_control(argv: list[str]) -> int:
    """`editor-control`: the operator's signed call to the running runner's editor lifecycle (loopback only)."""
    import secrets
    import time
    import urllib.error
    import urllib.request
    from . import editor_control as ec
    p = argparse.ArgumentParser(prog="mesh-runner editor-control")
    p.add_argument("action", choices=ec.ACTIONS)
    p.add_argument("--config", required=True)
    p.add_argument("--expected-generation", type=int, help="generation from `status`; required for every change")
    p.add_argument("--expected-pid", type=int, help="pid from `status`; required for every change")
    p.add_argument("--expected-started-at", type=int, help="started_at from `status`; required for every change")
    p.add_argument("--expected-instance", help="instance from `status`; required for every change")
    p.add_argument("--expected-digest", help="applied_digest from `status`; required while an editor is applied")
    args = p.parse_args(argv)
    cfg = config_mod.load(args.config)
    if not cfg.tgpeek_editor_control_secret_file or not cfg.extension_enabled:
        print(json.dumps({"error": "operator_control_disabled"}))
        return 2
    try:
        secret = Path(cfg.tgpeek_editor_control_secret_file).read_text().strip()
    except OSError:
        print(json.dumps({"error": "secret_unreadable"}))
        return 2
    req = {"request_id": secrets.token_hex(16)}
    if args.expected_generation is not None:
        req["expected_generation"] = args.expected_generation
    for key, value in (("expected_pid", args.expected_pid), ("expected_started_at", args.expected_started_at)):
        if value is not None:
            req[key] = value
    if args.expected_instance:
        req["expected_instance"] = args.expected_instance
    if args.expected_digest:
        req["expected_digest"] = args.expected_digest
    body = json.dumps(req).encode()
    path, ts = ec.PREFIX + args.action, str(int(time.time()))
    host = "127.0.0.1" if cfg.extension_host in ("", "0.0.0.0", "::") else cfg.extension_host
    http_req = urllib.request.Request(f"http://{host}:{cfg.extension_port}{path}", data=body, method="POST", headers={
        "X-Mesh-Timestamp": ts, "X-Operator-Signature": ec.sign_request(secret, ts, path, body), "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(http_req, timeout=60) as resp:
            status, out = resp.status, resp.read()
    except urllib.error.HTTPError as e:
        status, out = e.code, e.read()
    except OSError:
        print(json.dumps({"error": "runner_unreachable"}))
        return 2
    print(out.decode(errors="replace"))
    return 0 if 200 <= status < 300 else 1


def _receipt_wait(argv: list[str]) -> int:
    """`receipt-wait`: the operator's arm / inspect / disarm / rebaseline of one card's receipt
    registration, written through Runner's own store API (never by hand into its database)."""
    import time
    from . import receipt_wait as rw
    p = argparse.ArgumentParser(prog="mesh-runner receipt-wait")
    p.add_argument("action", choices=["arm", "inspect", "disarm", "rebaseline"])
    p.add_argument("--config", required=True)
    p.add_argument("--agent", required=True, help="the lane that owns the source card")
    p.add_argument("--task", required=True, help="full UUID of the source card (the card that will wait)")
    p.add_argument("--owner-id", help="arm: Mesh agent id of that lane; must match at declaration and wake")
    p.add_argument("--journal", action="append", default=[], metavar="NAME=/abs/path/telegram-receipts.jsonl",
                   help="arm: repeatable; a journal that does not exist yet is registered as absent")
    p.add_argument("--name", help="rebaseline: the registered journal name")
    args = p.parse_args(argv)
    cfg = config_mod.load(args.config)
    if not cfg.receipt_wait_v1:
        print(json.dumps({"error": "receipt_wait_v1 is off in this config"}))
        return 2
    if args.agent not in {a.name for a in cfg.agents}:
        p.error("--agent must name an exact configured lane")
    store = Store(cfg.state_dir / "runner.sqlite")
    try:
        if args.action == "arm":
            journals = dict(j.split("=", 1) for j in args.journal if "=" in j)
            if not args.owner_id or not journals or len(journals) != len(args.journal):
                p.error("arm needs --owner-id and at least one NAME=PATH --journal")
            rw.arm(store, args.agent, args.task, args.owner_id, journals, time.time())
        elif args.action == "disarm":
            rw.disarm(store, args.agent, args.task)
        elif args.action == "rebaseline":
            if not args.name:
                p.error("rebaseline needs --name")
            rw.rebaseline(store, args.agent, args.task, args.name)
        out = rw.inspect(store, args.agent, args.task)
    except (ValueError, OSError) as exc:
        print(json.dumps({"error": str(exc)[:200]}))
        return 1
    print(json.dumps(out, sort_keys=True))
    return 0


def _stall_wait(argv: list[str]) -> int:
    """`stall-wait`: the operator's arm / inspect / disarm of one card's mesh-stall-control
    registration, written through Runner's own store API. Arm reads the card's current comments
    through the owner lane's own Mesh key, so events already there become the baseline."""
    import time
    from . import stall_wait as sw
    from .mesh import MeshClient
    p = argparse.ArgumentParser(prog="mesh-runner stall-wait")
    p.add_argument("action", choices=["arm", "inspect", "disarm"])
    p.add_argument("--config", required=True)
    p.add_argument("--agent", required=True, help="the lane that owns the source card")
    p.add_argument("--task", required=True, help="full UUID of the source card (the card that will wait)")
    p.add_argument("--owner-id", help="arm: Mesh agent id of that lane (the notifier's comment author)")
    args = p.parse_args(argv)
    cfg = config_mod.load(args.config)
    if not cfg.stall_wait_v1:
        print(json.dumps({"error": "stall_wait_v1 is off in this config"}))
        return 2
    lane = next((a for a in cfg.agents if a.name == args.agent), None)
    if lane is None:
        p.error("--agent must name an exact configured lane")
    store = Store(cfg.state_dir / "runner.sqlite")
    try:
        if args.action == "arm":
            if not args.owner_id:
                p.error("arm needs --owner-id")
            mesh = MeshClient(cfg.mesh_url, lane.key, basic_auth=cfg.mesh_basic_auth,
                              bearer=cfg.extension_token)
            sw.arm(store, args.agent, args.task, args.owner_id, mesh.comments_all(args.task), time.time())
        elif args.action == "disarm":
            sw.disarm(store, args.agent, args.task)
        out = sw.inspect(store, args.agent, args.task)
    except Exception as exc:    # noqa: BLE001 - a Mesh read failure refuses the arm (fail closed)
        print(json.dumps({"error": str(exc)[:200]}))
        return 1
    print(json.dumps(out, sort_keys=True))
    return 0


def _recovery_wait(argv: list[str]) -> int:
    """`recovery-wait`: the operator's arm / inspect / disarm of one card's future shared recovery
    card registration, written through Runner's own store API (never by hand into its database)."""
    import time
    from . import recovery_wait as rw
    p = argparse.ArgumentParser(prog="mesh-runner recovery-wait")
    p.add_argument("action", choices=["arm", "inspect", "disarm"])
    p.add_argument("--config", required=True)
    p.add_argument("--agent", required=True, help="the lane that owns the source card")
    p.add_argument("--task", required=True, help="full UUID of the source card (the card that will wait)")
    p.add_argument("--owner-id", help="arm: Mesh agent id of that lane; must match at declaration and wake")
    p.add_argument("--project-id", help="arm: UUID of the project the recovery card is created in")
    p.add_argument("--marker", help="arm: exact text the recovery card's title/description carries")
    p.add_argument("--recovery-owner-id", help="arm: Mesh agent id the recovery card is assigned to")
    p.add_argument("--receipt-author-id", help="arm: Mesh agent id whose `carrier-receipt: <ref>` comment completes it")
    args = p.parse_args(argv)
    cfg = config_mod.load(args.config)
    if not cfg.recovery_wait_v1:
        print(json.dumps({"error": "recovery_wait_v1 is off in this config"}))
        return 2
    if args.agent not in {a.name for a in cfg.agents}:
        p.error("--agent must name an exact configured lane")
    store = Store(cfg.state_dir / "runner.sqlite")
    try:
        if args.action == "arm":
            need = (args.owner_id, args.project_id, args.marker, args.recovery_owner_id, args.receipt_author_id)
            if not all(need):
                p.error("arm needs --owner-id --project-id --marker --recovery-owner-id --receipt-author-id")
            rw.arm(store, args.agent, args.task, *need, time.time())
        elif args.action == "disarm":
            rw.disarm(store, args.agent, args.task)
        out = rw.inspect(store, args.agent, args.task)
    except (ValueError, OSError) as exc:
        print(json.dumps({"error": str(exc)[:200]}))
        return 1
    print(json.dumps(out, sort_keys=True))
    return 0


def _lifecycle_wait(argv: list[str]) -> int:
    """`lifecycle-wait`: the operator's arm / inspect / disarm / rebaseline of one card's native
    SessionStart registration, written through Runner's own store API."""
    import time
    from . import lifecycle_wait as lw
    p = argparse.ArgumentParser(prog="mesh-runner lifecycle-wait")
    p.add_argument("action", choices=["arm", "inspect", "disarm", "rebaseline"])
    p.add_argument("--config", required=True)
    p.add_argument("--agent", required=True, help="the lane that owns the source card")
    p.add_argument("--task", required=True, help="full UUID of the source card (the card that will wait)")
    p.add_argument("--owner-id", help="arm: Mesh agent id of that lane")
    p.add_argument("--profile", help="arm: Codex profile of the observed lane, e.g. khan")
    p.add_argument("--manifest", help="arm: absolute path of the lane's lifecycle manifest.json")
    p.add_argument("--codex-home", help="arm: absolute CODEX_HOME holding sessions/")
    p.add_argument("--journal", help="arm: absolute path of khan-lifecycle.jsonl (may not exist yet)")
    args = p.parse_args(argv)
    cfg = config_mod.load(args.config)
    if not cfg.lifecycle_wait_v1:
        print(json.dumps({"error": "lifecycle_wait_v1 is off in this config"}))
        return 2
    if args.agent not in {a.name for a in cfg.agents}:
        p.error("--agent must name an exact configured lane")
    store = Store(cfg.state_dir / "runner.sqlite")
    try:
        if args.action == "arm":
            if not (args.owner_id and args.profile and args.manifest and args.codex_home and args.journal):
                p.error("arm needs --owner-id --profile --manifest --codex-home --journal")
            lw.arm(store, args.agent, args.task, args.owner_id, args.profile, args.manifest,
                   args.codex_home, args.journal, time.time())
        elif args.action == "disarm":
            lw.disarm(store, args.agent, args.task)
        elif args.action == "rebaseline":
            lw.rebaseline(store, args.agent, args.task)
        out = lw.inspect(store, args.agent, args.task)
    except (ValueError, OSError) as exc:
        print(json.dumps({"error": str(exc)[:200]}))
        return 1
    print(json.dumps(out, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    args_in = sys.argv[1:] if argv is None else argv
    if args_in and args_in[0] == "recheck-delivery":
        from .delivery_requests import command
        return command(args_in[1:])
    if args_in and args_in[0] == "reviewer-merge":
        return _reviewer_merge(args_in[1:])
    if args_in and args_in[0] == "session-receiver":
        from .session_receiver import command
        return command(args_in[1:])
    if args_in and args_in[0] == "notify-access-change":
        from .access_recovery import notify_command
        return notify_command(args_in[1:])
    if args_in and args_in[0] == "notify-auth-change":
        from .auth_recovery import notify_command
        return notify_command(args_in[1:])
    if args_in and args_in[0] == "recheck-auth":
        from .auth_recovery import command
        return command(args_in[1:])
    if args_in and args_in[0] == "recheck-access":
        from .access_recovery import command
        return command(args_in[1:])
    if args_in and args_in[0] == "editor-control":
        return _editor_control(args_in[1:])
    if args_in and args_in[0] == "native-rebind":
        from .native_result_rebind import command
        return command(args_in[1:])
    if args_in and args_in[0] == "receipt-wait":
        return _receipt_wait(args_in[1:])
    if args_in and args_in[0] == "stall-wait":
        return _stall_wait(args_in[1:])
    if args_in and args_in[0] == "recovery-wait":
        return _recovery_wait(args_in[1:])
    if args_in and args_in[0] == "lifecycle-wait":
        return _lifecycle_wait(args_in[1:])
    if args_in and args_in[0] == "asks":
        return _asks(args_in[1:])
    p = argparse.ArgumentParser(prog="mesh-runner")
    p.add_argument("command", choices=["run", "once", "status", "plan", "slots", "free-slot", "quarantine-list", "quarantine-restore", "pool", "suspend-wait-workspace", "admit-workspace-repo", "admit-git-index", "workspace-maintenance", "clear-auth-hold", "rearm-cap"])
    p.add_argument("--config", required=True)
    p.add_argument("--path", help="slot path for free-slot / quarantine-list / quarantine-restore")
    p.add_argument("--run", help="quarantine-restore: quarantine run name (see quarantine-list)")
    p.add_argument("--verify", action="store_true", help="quarantine-list: recompute entry checksums")
    p.add_argument("--archive", action="store_true",
                   help="free-slot: commit a held slot's uncommitted work onto refs/mesh-runner/archive/ "
                        "(never pushed) and then return the slot")
    p.add_argument("--profile", help="exact held profile fingerprint from status/plan")
    p.add_argument("--agent")
    p.add_argument("--task")
    p.add_argument("--attempt")
    p.add_argument("--reason")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--wait-generation")
    p.add_argument("--repo", help="admit-workspace-repo: name of the newly configured repository")
    p.add_argument("--request-id")
    p.add_argument("--action", choices=["inspect", "acquire", "finish"],
                   help="workspace-maintenance: inspect (read-only identity + windows + receipt), acquire, finish")
    p.add_argument("--pool", help="workspace-maintenance: <agent>/<repo> pool of the leased slot")
    p.add_argument("--slot", help="workspace-maintenance: canonical slot path from inspect")
    p.add_argument("--lease", help="workspace-maintenance: lease token from inspect")
    p.add_argument("--window", help="workspace-maintenance finish: request id of the acquire that opened the window")
    p.add_argument("--evict", action="store_true", help="pool: preview (or with --apply, perform) eviction")
    p.add_argument("--apply", action="store_true", help="pool --evict: remove slots; needs the exclusive runner lock")
    p.add_argument("--prune-ignored", action="store_true",
                   help="pool: preview (or with --apply, perform) removal of node_modules/target/.next/dist/.venv in idle free slots")
    p.add_argument("--ignored-idle-hours", type=_idle_hours_arg, default=None, help="pool --prune-ignored: override config idle threshold")
    p.add_argument("--compact-checkpoints", action="store_true",
                   help="pool: preview (or with --apply, perform) rewriting waiting old-format checkpoints without regenerable caches")
    p.add_argument("--prune-checkpoints", action="store_true",
                   help="pool: preview (or with --apply, perform) removal of checkpoints of ended attempts and superseded generations")
    p.add_argument("--checkpoint-grace-hours", type=_idle_hours_arg, default=None,
                   help="pool --prune-checkpoints: override config grace period")
    p.add_argument("--idle-hours", type=float, default=6)
    p.add_argument("--keep", type=int, default=2)
    p.add_argument("--max-evictions", type=int, default=4)
    p.add_argument("--ttl-seconds", type=int, default=300)
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(message)s")
    if args.command in ("run", "once"):
        from . import launch_guard
        if launch_guard.HANDOFF not in os.environ:
            # Both the installed console and `python -m mesh_runner.cli` use the
            # supported boundary. The exec child validates the inherited lock.
            guarded = ["--target-release", str(Path(__file__).resolve().parents[2]),
                       "--config", args.config, "--log-level", args.log_level]
            if args.command == "once":
                guarded.append("--once")
            return launch_guard.main(guarded)

    if args.command == "rearm-cap":
        from . import cap_rearm
        from uuid import UUID
        if not args.agent or not args.task or not args.reason or not args.reason.strip():
            p.error("rearm-cap requires --agent, --task and --reason")
        if args.apply and args.dry_run:
            p.error("choose --apply or --dry-run")
        try:
            if str(UUID(args.task)) != args.task:
                raise ValueError()
        except ValueError:
            p.error("--task must be a full canonical UUID")
        cfg = config_mod.load(args.config)
        if not any(a.name == args.agent for a in cfg.agents):
            p.error("--agent must name an exact configured lane")
        store = Store(cfg.state_dir / "runner.sqlite")
        try:
            result = cap_rearm.request(store, args.agent, args.task, args.reason, apply=args.apply)
        except ValueError as exc:
            print(json.dumps({"eligible": False, "reason": str(exc)}))
            return 1
        print(json.dumps(result))
        return 0

    if args.command == "clear-auth-hold":
        if not args.profile or not auth.PROFILE.fullmatch(args.profile):
            p.error("--profile must be the exact fingerprint from status/plan")
        cfg = config_mod.load(args.config)
        store = Store(cfg.state_dir / "runner.sqlite")
        if args.dry_run:
            would = auth.held(store, args.profile)
            print(json.dumps({"profile": args.profile, "dry_run": True, "would_clear": would,
                              "note": "dry run: nothing was cleared; rerun without --dry-run to clear"}))
            return 0 if would else 1
        cleared = auth.clear(store, args.profile)
        print(json.dumps({"profile": args.profile, "cleared": cleared,
                          "note": "normal admission still applies; this command does not repair authentication"}))
        return 0 if cleared else 1

    if args.command == "suspend-wait-workspace":
        cfg = config_mod.load(args.config)
        if args.agent not in {a.name for a in cfg.agents}:
            p.error("--agent must name a configured agent")
        if not all((args.task, args.attempt, args.wait_generation, args.request_id)):
            p.error("exact --task, --attempt, --wait-generation and --request-id required")
        try:
            result = workspace_requests.submit(cfg.state_dir, args.agent, args.task,
                args.attempt, args.wait_generation, args.request_id, args.ttl_seconds)
        except (ValueError, OSError):
            print("request rejected (identity, deadline or private spool)", file=sys.stderr)
            return 2
        print(json.dumps(result, sort_keys=True))
        return 0

    if args.command == "workspace-maintenance":
        cfg = config_mod.load(args.config)
        if args.agent not in {a.name for a in cfg.agents}:
            p.error("--agent must name a configured agent")
        if not args.action:
            p.error("--action required")
        store = Store(cfg.state_dir / "runner.sqlite")
        try:
            if args.action == "inspect":
                if not args.attempt:
                    p.error("exact --attempt required")
                a = store.get(args.attempt)
                if a is None or a.agent != args.agent:
                    print("unknown attempt for this agent", file=sys.stderr)
                    return 2
                slots = [dict(r) for r in store.db.execute("SELECT * FROM slots WHERE attempt=? AND state='leased'", (a.id,))]
                out = {"task": a.task_id, "attempt": a.id, "state": a.state,
                       "wait_generation": workspace_requests.generation(a),
                       "slots": [{"pool": x["pool"], "slot": x["path"], "lease": maintenance.lease_token(x)} for x in slots],
                       **maintenance.inspect(cfg.state_dir, store, args.agent, args.request_id or "")}
                print(json.dumps(out, sort_keys=True))
                return 0
            if not all((args.task, args.attempt, args.wait_generation, args.request_id, args.pool, args.slot, args.lease)):
                p.error("exact --task, --attempt, --wait-generation, --request-id, --pool, --slot and --lease required")
            if args.action == "finish" and not args.window:
                p.error("finish requires --window")
            result = maintenance.submit(cfg.state_dir, args.agent, args.action, args.task, args.attempt,
                args.wait_generation, args.request_id, args.pool, args.slot, args.lease, args.ttl_seconds,
                args.window or "")
        except (ValueError, OSError):
            print("request rejected (identity, deadline or private spool)", file=sys.stderr)
            return 2
        print(json.dumps(result, sort_keys=True))
        return 0

    if args.command == "admit-git-index":
        # Request only: the owning Runner tick verifies the pinned policy and is the sole issuer.
        cfg = config_mod.load(args.config)
        if args.agent not in {a.name for a in cfg.agents}:
            p.error("--agent must name a configured agent")
        if not all((args.task, args.attempt, args.wait_generation, args.request_id)):
            p.error("exact --task, --attempt, --wait-generation and --request-id required")
        try:
            result = git_index_admission.submit(cfg.state_dir, cfg.git_index_admission, args.agent, args.task,
                                                args.attempt, args.wait_generation, args.request_id, args.ttl_seconds)
        except git_index_admission.Unverifiable:
            print("request rejected (reviewed policy is not verifiable)", file=sys.stderr)
            return 2
        except (ValueError, OSError):
            print("request rejected (identity, deadline or private spool)", file=sys.stderr)
            return 2
        print(json.dumps(result, sort_keys=True))
        return 0

    if args.command == "admit-workspace-repo":
        cfg = config_mod.load(args.config)       # the fresh config is the source of the pinned spec
        agent = next((x for x in cfg.agents if x.name == args.agent), None)
        if agent is None:
            p.error("--agent must name a configured agent")
        if not all((args.task, args.attempt, args.wait_generation, args.request_id, args.repo)):
            p.error("exact --task, --attempt, --wait-generation, --request-id and --repo required")
        spec = next((r for r in agent.repos if r.name == args.repo), None)
        if spec is None:
            p.error("--repo must be configured for --agent in the fresh config")
        try:
            result = repo_admission.submit(cfg.state_dir, args.agent, args.task, args.attempt,
                                           args.wait_generation, args.request_id, spec, args.ttl_seconds)
        except (ValueError, OSError):
            print("request rejected (identity, deadline or private spool)", file=sys.stderr)
            return 2
        print(json.dumps(result, sort_keys=True))
        return 0

    if args.command == "status":
        cfg = config_mod.load(args.config)
        store = Store(cfg.state_dir / "runner.sqlite")
        out = []
        for agent in cfg.agents:
            for a in store.by_state(agent.name, OPEN):
                out.append({"agent": agent.name, "task": a.task8, "state": a.state, "title": a.title[:70],
                            "task_id": a.task_id, "attempt": a.id, "wait_generation": workspace_requests.generation(a),
                            "wait": a.wait.get("kind", ""), "turns": a.turns, "session": a.session_id,
                            "native_rebind_generation": (native_wait_identity(a.wait)
                                if a.wait.get("kind") == "native" else None),
                            "native_registration": (a.wait.get("target")
                                if a.wait.get("kind") == "native" else None),
                            "workspaces": a.workspaces, "updated": a.updated_at,
                            **({"prompt_delivery": {"pending": True,
                                   "turn": a.wait["interactive_prompt_intent"].get("turn"),
                                   "resolution_required": a.state == "unknown",
                                   "reason": a.wait["interactive_prompt_intent"].get("resolution", "") }}
                               if isinstance(a.wait.get("interactive_prompt_intent"), dict) else {})})
        # Keep the existing top-level list and expose durable holds even when no
        # attempt is currently open on the profile.
        out.extend({"auth_hold": row} for row in auth.status(store, args.config))
        json.dump(out, sys.stdout, ensure_ascii=False, indent=2)
        print()
        return 0

    cfg = config_mod.load(args.config)
    if args.command == "pool" and args.apply and not (args.evict or args.prune_ignored or args.prune_checkpoints or args.compact_checkpoints):
        p.error("--apply needs --evict, --prune-ignored, --prune-checkpoints or --compact-checkpoints")
    if args.archive and args.command != "free-slot":
        p.error("--archive is only for free-slot")
    if args.command == "pool" and (args.idle_hours < 0 or args.keep < 0 or args.max_evictions < 1):
        p.error("--idle-hours/--keep must be >= 0 and --max-evictions >= 1")
    if args.command == "pool" and args.apply:
        # Exclusive: refuse while the service (or another maintenance tool) owns the state dir.
        try:
            runner = Runner(cfg)
        except AlreadyRunning as e:
            print(e, file=sys.stderr)
            return 2
    elif args.command in ("plan", "slots", "free-slot", "quarantine-list", "quarantine-restore", "pool"):
        runner = Runner(cfg, lock=False)          # read-mostly: may run beside the service
        if args.command == "slots":
            json.dump([{"agent": ar.agent.name, "slots": ar.pool.rows()} for ar in runner.agents],
                      sys.stdout, ensure_ascii=False, indent=2)
            print()
            return 0
        if args.command == "pool":
            return _pool(runner, cfg, args)
        if args.command in ("quarantine-list", "quarantine-restore"):
            for ar in runner.agents:
                if args.path and Path(args.path).is_relative_to(ar.pool.root):
                    if args.command == "quarantine-restore" and not args.run:
                        p.error("quarantine-restore needs --run")
                    try:
                        if args.command == "quarantine-list":
                            out = ar.pool.quarantine_inventory(args.path, verify=args.verify)
                        else:
                            out = {"restored": ar.pool.quarantine_restore(args.path, args.run)}
                    except WorkspaceError as e:
                        print(f"{args.command}: {e}", file=sys.stderr)
                        return 1
                    json.dump(out, sys.stdout, ensure_ascii=False, indent=2)
                    print()
                    if args.command == "quarantine-list" and any(r["state"] == "unreadable" for r in out):
                        print(f"{args.command}: a quarantine manifest is unreadable or malformed", file=sys.stderr)
                        return 1                  # the inventory above is still printed; corruption is not success
                    return 0
            print("no agent pool contains that path")
            return 1
        if args.command == "free-slot":
            for ar in runner.agents:
                if args.path and args.path.startswith(str(ar.pool.root)):
                    print(ar.pool.free_path(args.path, archive=args.archive))
                    return 0
            print("no agent pool contains that path")
            return 1
    else:
        try:
            runner = Runner(cfg)
        except AlreadyRunning as e:
            print(e, file=sys.stderr)
            return 2
    if args.command == "pool":
        return _pool(runner, cfg, args)
    if args.command == "plan":
        plans = [ar.plan() for ar in runner.agents]
        for plan in plans:
            plan["auth_holds"] = auth.status(runner.store, args.config)
        json.dump(plans, sys.stdout, ensure_ascii=False, indent=2)
        print()
        return 0
    if args.command == "once":
        json.dump(runner.tick(), sys.stdout, ensure_ascii=False, indent=2)
        print()
        return 0
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):     # launchd stop: finish the ticks, then exit
        # set from a helper thread: Event.set() inside a handler can deadlock on the
        # Event's own lock if the signal lands while the main thread waits on it
        signal.signal(sig, lambda *_: threading.Thread(target=stop.set, daemon=True).start())
    runner.run_forever(stop)
    return 0


def _pool(runner, cfg, args) -> int:
    """Pool metrics; with --evict a bounded batch (dry run unless --apply, which holds the runner lock)."""
    from . import pool_ops
    out = {"pool": pool_ops.summary(runner.store, cfg.pool_root)}
    if args.evict:
        removed = []
        for ar in runner.agents:
            removed += pool_ops.evict(ar.pool, idle_hours=args.idle_hours, keep=args.keep,
                                      limit=args.max_evictions - len(removed), apply=args.apply)
            if len(removed) >= args.max_evictions:
                break
        out["evict"] = {"applied": args.apply, "slots": removed}
        if args.apply:
            out["pool"] = pool_ops.summary(runner.store, cfg.pool_root)
    if args.prune_ignored:
        out["prune_ignored"] = [pool_ops.prune_ignored(ar.pool, idle_hours=args.ignored_idle_hours
                                                       if args.ignored_idle_hours is not None
                                                       else cfg.ignored_cleanup_idle_hours, apply=args.apply)
                                for ar in runner.agents]
    if args.prune_checkpoints:
        grace = args.checkpoint_grace_hours if args.checkpoint_grace_hours is not None else cfg.checkpoint_gc_grace_hours
        out["prune_checkpoints"] = [pool_ops.prune_checkpoints(ar.pool, grace_hours=grace, apply=args.apply)
                                    for ar in runner.agents]
    if args.compact_checkpoints:
        out["compact_checkpoints"] = [pool_ops.compact_checkpoints(ar.pool, apply=args.apply)
                                      for ar in runner.agents]
    json.dump(out, sys.stdout, ensure_ascii=False, indent=2)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
