#!/usr/bin/env python3
"""Read-only canary: auto-selected reviewer -> the GitLab /user its fresh env resolves to.

    reviewer-canary.py --config runner.toml [--adapter select-reviewer.py] <project_id> <author-lane>

Runs the router adapter from the config, builds the selected lane's env exactly as the runner
does for the gate (env_file, then env_command), asks GitLab /user, prints
``{selected, reason, lane_gitlab_username, actual_user, match}``. Never prints the env, a token
or the /user body; never merges, creates or writes anything.
"""
import json
import subprocess
import sys

from mesh_runner import config as config_mod
from mesh_runner import reviewer_merge as rm
from mesh_runner.config import read_env_file


def lane_env(agent) -> dict:
    env = {}
    if agent.env_file:
        env.update(read_env_file(agent.env_file))
    if agent.env_command:
        res = subprocess.run(agent.env_command, capture_output=True, text=True, timeout=60)
        if res.returncode != 0:
            raise RuntimeError(f"env command failed (rc {res.returncode})")
        for line in res.stdout.splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.removeprefix("export ").split("=", 1)
                env[k.strip()] = v.strip().strip("'\"")
    env.update(agent.inline_env or {})
    return env


def main(argv) -> int:
    adapter = None
    if len(argv) == 6 and argv[2] == "--adapter":
        adapter, argv = argv[3], argv[:2] + argv[4:]
    if len(argv) != 4 or argv[0] != "--config":
        print(__doc__)
        return 2
    cfg = config_mod.load(argv[1])
    command = ["python3", adapter, "{project_id}", "{author}"] if adapter else list(cfg.reviewer_select_command)
    sel = rm.run_selector(command, project_id=argv[2], author=argv[3])
    agent = next((a for a in cfg.agents if a.name.casefold() == str(sel.get("lane")).casefold()), None)
    out = {"selected": sel.get("lane"), "reason": sel.get("reason"), "actual_user": None, "match": False}
    if agent is None:
        out["error"] = "selected lane is not in this runner config"
    else:
        out["lane_gitlab_username"] = agent.gitlab_username
        try:
            who = rm.check_identity(lane_env(agent), {"reviewer_username": agent.gitlab_username}, cfg.gitlab_url)
            out.update(match=True, actual_user=who)
        except (rm.Refused, RuntimeError, OSError) as e:
            out["error"] = str(e)[:120]
    print(json.dumps(out, indent=2))
    return 0 if out["match"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
