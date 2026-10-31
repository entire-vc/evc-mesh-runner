#!/usr/bin/env python3
"""Router adapter for `mesh-runner reviewer-merge`: the stock review router's choice, read-only.

    select-reviewer.py <project_id> <author-lane> [--router-home DIR] [--state-dir DIR]

Prints one JSON object ``{lane, reason, author_family}`` (``lane`` null when the router will not
assign). Depends on an installed fleet review router: ``--router-home`` (or env
``MESH_ROUTER_HOME``) is the directory whose ``scripts/`` holds ``review_router``,
``runtime_switch`` and ``fleet-balancer.py``; ``--state-dir`` (or env ``MESH_ROUTER_STATE_DIR``)
holds ``runtime-catalog/desired.json`` and ``runtime-controller/pools.json``. Neither has a
default: a missing one is ``unknown: inputs``. The adapter calls the router's own
``review_router.pick_reviewer`` with the inputs the dispatcher gives it: the project's reviewer
group (PROJECT_LEAD_MAP), the runtime catalog and fresh pool observations, the author's current
family (active runtime profile). Unreadable input -> ``unknown: ...``. Writes nothing, prints no
env or secrets.
"""
import importlib.util
import json
import os
import sys
import time
from pathlib import Path


def main(argv):
    args, opts, it = [], {"router-home": os.environ.get("MESH_ROUTER_HOME", ""),
                          "state-dir": os.environ.get("MESH_ROUTER_STATE_DIR", "")}, iter(argv)
    for a in it:
        name, eq, val = a.partition("=")
        if name in ("--router-home", "--state-dir"):
            opts[name[2:]] = val if eq else next(it, "")
        else:
            args.append(a)
    if len(args) != 2:
        print(json.dumps({"lane": None, "reason": "unknown: usage"}))
        return 2
    project_id, author = args
    try:
        if not opts["router-home"] or not opts["state-dir"]:
            raise ValueError("router home or state dir not set")
        bob = Path(os.path.expanduser(opts["router-home"]))
        sys.path.insert(0, str(bob / "scripts"))
        import review_router as rr
        spec = importlib.util.spec_from_file_location("fleet_balancer_rr", str(bob / "scripts" / "fleet-balancer.py"))
        fb = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fb)
        base = Path(os.path.expanduser(opts["state-dir"]))
        cat = json.loads((base / "runtime-catalog" / "desired.json").read_text())["catalog"]
        obs = json.loads((base / "runtime-controller" / "pools.json").read_text())["pools"]
        from runtime_switch import load_active
        active = {k.lower(): v for k, v in (load_active() or {}).items()}
        groups = {pid: dict(v["reviewers"]) for pid, v in fb.PROJECT_LEAD_MAP.items() if v.get("reviewers")}
    except Exception as e:                                    # noqa: BLE001
        print(json.dumps({"lane": None, "reason": f"unknown: inputs ({type(e).__name__})"}))
        return 1
    wall = time.time()
    g = groups.get(project_id) or {}

    def harness_of(lane):                                     # the active profile decides; no guess without it
        return None

    def available(fam):
        chain = ((cat.get("bindings") or {}).get(str(g.get(fam)).lower()) or {}).get("permitted_profiles") or []
        return rr.family_availability(fam, chain, cat, obs, wall)

    ctx = {"groups": groups, "available": available,
           "family_of": lambda lane: rr.lane_family(lane, harness_of, active)}
    lane, reason = rr.pick_reviewer({"project_id": project_id, "has_mr": True}, author, ctx)
    print(json.dumps({"lane": lane, "reason": reason, "author_family": ctx["family_of"](author)}))
    return 0 if lane else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
