"""The deploy inventory: which repositories have a live-commit source a deploy marker can read.

Mesh releases a card wait ``until=deployed`` only when every open or merged merge request linked to the
target carries a deploy record. Those records are written by a deploy marker that reads the running
commit of each component listed in the inventory (``deploy_inventory`` in the runner config)::

    {"repos": {"<group>/<project>": {"components": {"<name>": {"source": {"type": "http_text", ...}}}}}}

A component reads only through sources none of which is ``type: none`` (``sources`` may list several);
a component without a source does not read. A repository the inventory lists with no readable
component never gets a deploy record, so a wait for its deploy never ends by itself. A repository the
inventory does not list is left alone: the runner knows of no rule for it.
"""
from __future__ import annotations

import json
from pathlib import Path


def load(path: Path | str | None) -> dict | None:
    """``repos`` of the inventory; None when there is no path or the file is unreadable or malformed."""
    if not path:
        return None
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    repos = data.get("repos") if isinstance(data, dict) else None
    return repos if isinstance(repos, dict) else None


def _sources(component) -> list[dict]:
    if not isinstance(component, dict):
        return []
    if isinstance(component.get("sources"), list):
        return [s for s in component["sources"] if isinstance(s, dict)]
    if isinstance(component.get("source"), dict):
        return [component["source"]]
    return []


def never_deployed(repo: str, repos: dict) -> str:
    """Why a deploy of ``repo`` is never recorded ('' = it may be: listed with a readable component, or
    not listed at all)."""
    conf = repos.get(repo)
    if not isinstance(conf, dict):
        return ""
    comps = conf.get("components") if isinstance(conf.get("components"), dict) else {}
    if any(_sources(c) and all(s.get("type") != "none" for s in _sources(c)) for c in comps.values()):
        return ""
    note = next((" ".join(str(s["note"]).split()) for c in comps.values() for s in _sources(c) if s.get("note")), "")
    return f"{repo} has no live-commit source (type: none" + (f": {note[:160]}" if note else "") + ")"
