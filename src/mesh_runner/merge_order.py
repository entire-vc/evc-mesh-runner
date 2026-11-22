"""Merge order: a card that may merge only after other code says so in its description, one line per
predecessor (another card, or one merge request):

    merge-after: #0123abcd
    merge-after: group/project!12

The line may sit in a list item or in backticks; several references may share one line. A card
reference is the short id after ``#`` or the full id; a merge request is ``group/project!iid`` or its
GitLab URL. Prose is not read: "merges after X" in a sentence holds nothing, only the ``merge-after:``
line does. A ``merge-after:`` line that names nothing readable (a bare ``!12``, a typo, nothing at all) is
an order that can never be met: the author is told which line, nothing is merged.

The merge gate does not start while any predecessor is unmerged; the author's merge wait stands until
then (whoever declared it: the author, or the runner on a SHIP verdict). The order is also met by the
author's own wait on another merge request (``wait_for on=mr until=merged``) that a runner merge wait
replaced: that target is carried into the merge wait (``after``).

A predecessor card is merged when every merge request linked to it is settled (merged or closed) and at
least one is merged, each confirmed by this GitLab (a request on another forge or host holds until the
card is done), or when the card is done. A card cancelled before that, or a named merge request closed
without merge, can no longer meet the order: the author is told, nothing is merged. Anything unreadable
holds: an order is never assumed met. A predecessor card Mesh does not find (404: a typo, another
workspace) is ``MISSING``: the caller holds a few checks, then tells the author.
"""
from __future__ import annotations

import re

from .mesh import MeshError
from .wake import GITLAB_MR_URL, gitlab_mr_targets

MET, HELD, BROKEN, MISSING = "met", "held", "broken", "missing"

LINE = re.compile(r"^[ \t>*\-+]*`?merge-after:[ \t]*(.*)$", re.I | re.M)   # empty: named nothing
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_MR = re.compile(r"(?<![\w./-])([A-Za-z0-9_.][\w.-]*(?:/[\w.-]+)+)!([1-9][0-9]{0,11})\b")
_CARD = re.compile(rf"(?<![\w-])(?:#({_UUID}|[0-9a-f]{{8}})|({_UUID}))(?![\w-])", re.I)
_URL = re.compile(r"https?://[^\s`,;)>]+")
_LEFT = re.compile(r"[!#]\w|\w+/[\w./-]*!|https?://")     # a reference-like remnant nothing above read
PR_TYPES = ("pr", "pull_request", "merge_request")


def refs(description: str | None, carried=()) -> list[str]:
    """The predecessors of a card: ``card:<id>`` and ``mr:<group/project!iid>``, in order, once each.
    ``carried``: references kept in the merge wait itself (see the module text)."""
    out: list[str] = []
    for ref in carried or ():
        if isinstance(ref, str) and ref.startswith(("card:", "mr:")) and ref not in out:
            out.append(ref)
    for line in LINE.finditer(description or ""):
        text, found = line.group(1), []
        for m in _URL.finditer(text):
            url = GITLAB_MR_URL.match(m.group(0))
            found.append(f"url:{url.group(0)}" if url else f"bad:{m.group(0)}")
        rest = _URL.sub(" ", text)
        found += [f"mr:{m.group(1)}!{m.group(2)}" for m in _MR.finditer(rest)]
        rest = _MR.sub(" ", rest)
        found += [f"card:{(m.group(1) or m.group(2)).lower()}" for m in _CARD.finditer(rest)]
        if not found or _LEFT.search(_CARD.sub(" ", rest)):
            found.append(f"bad:{text.strip().strip('`').strip()[:80]}")
        for ref in found:
            if ref not in out:
                out.append(ref)
    return out


def description(task: dict) -> str:
    """The description of a card read on its own (``GET /tasks/{id}``). Mesh leaves an empty one out
    (``omitempty``) and always says whether there is one (``has_description``): no text is an empty
    description only when that flag says false. Any other answer without the text (a list row, a partial
    answer) is unreadable (ValueError), never an empty order."""
    text = task.get("description")
    if isinstance(text, str):
        return text
    if "description" not in task and task.get("has_description") is False:
        return ""
    raise ValueError("card description not sent")


def _name(ref: str) -> str:
    kind, _, value = ref.partition(":")
    return f"#{value[:8]}" if kind == "card" else value


def _mr_state(gitlab, target: str) -> str:
    row = gitlab.get_mr(target)
    state = row.get("state") if isinstance(row, dict) else None
    if state not in ("opened", "locked", "merged", "closed"):
        raise ValueError("merge request state unavailable")
    return state


def _card(mesh, gitlab, cats, ident: str) -> tuple[str, str]:
    name = f"#{ident[:8]}"
    try:
        t = mesh.task(ident) if len(ident) == 36 else mesh.task_by_short(ident)
        t = t.get("task", t) if isinstance(t, dict) else None
        if not isinstance(t, dict) or not t.get("id"):
            raise ValueError("card unavailable")
        links = [link for link in mesh.vcs_links(t["id"]) if link.get("link_type") in PR_TYPES]
        category = cats.category(t)
    except MeshError as e:
        if e.status == 404:
            return MISSING, f"{name} is not found (Mesh 404)"
        return HELD, f"{name} cannot be read (Mesh {e.status})"
    except Exception as e:                     # noqa: BLE001 - unreadable: the order holds
        return HELD, f"{name} cannot be read ({type(e).__name__})"
    if category == "done":
        return MET, f"{name} is done"
    states, shown = [], []
    try:
        for link in links:
            ref = gitlab_mr_targets([link], getattr(gitlab, "base_url", "")) if gitlab is not None else []
            if ref:
                state = _mr_state(gitlab, ref[0])
                shown.append(f"{ref[0]} {state}")
            else:                              # another forge or host: nothing here confirms its state
                state = "unconfirmed"
                shown.append(f"{link.get('url') or link.get('external_id') or '?'} not on this GitLab")
            states.append(state)
    except Exception as e:                     # noqa: BLE001
        return HELD, f"{name}: its merge request cannot be read ({type(e).__name__})"
    if states and "merged" in states and all(s in ("merged", "closed") for s in states):
        return MET, f"{name} merged ({', '.join(shown)})"
    if category == "cancelled":
        return BROKEN, f"{name} was cancelled before its code was merged"
    if not states:
        return HELD, f"{name} has no merge request yet"
    return HELD, f"{name} is not merged yet ({', '.join(shown)})"


def _mr(gitlab, target: str) -> tuple[str, str]:
    if gitlab is None:
        return HELD, f"{target} cannot be read (no GitLab observer)"
    try:
        state = _mr_state(gitlab, target)
    except Exception as e:                     # noqa: BLE001
        return HELD, f"{target} cannot be read ({type(e).__name__})"
    if state == "merged":
        return MET, f"{target} merged"
    if state == "closed":
        return BROKEN, f"{target} was closed without merge"
    return HELD, f"{target} is not merged yet"


def _url(gitlab, url: str) -> tuple[str, str]:
    ref = gitlab_mr_targets([{"url": url}], getattr(gitlab, "base_url", "")) if gitlab is not None else []
    if not ref:
        return BROKEN, f"{url} is not a merge request on this GitLab: name its card instead"
    return _mr(gitlab, ref[0])


def _one(ref: str, mesh, gitlab, cats) -> tuple[str, str]:
    kind, _, value = ref.partition(":")
    if kind == "card":
        return _card(mesh, gitlab, cats, value)
    if kind == "mr":
        return _mr(gitlab, value)
    if kind == "url":
        return _url(gitlab, value)
    return BROKEN, (f"the line `merge-after: {value}` names no card (#<8 hex>) or merge request "
                    "(group/project!iid) that can be read")


def evaluate(order: list[str], mesh, gitlab, cats) -> tuple[str, str]:
    """(MET | HELD | MISSING | BROKEN, text) over every predecessor: one that can no longer be met
    decides, then a card Mesh does not find, then one still unmerged; the text names each not met."""
    found: dict[str, list[str]] = {BROKEN: [], MISSING: [], HELD: []}
    for ref in order:
        state, text = _one(ref, mesh, gitlab, cats)
        if state in found:
            found[state].append(text)
    for state in (BROKEN, MISSING, HELD):
        if found[state]:
            return state, "; ".join(found[BROKEN] if state == BROKEN else found[MISSING] + found[HELD])
    return MET, ", ".join(_name(r) for r in order)
