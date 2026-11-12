#!/usr/bin/env python3
"""Release commit preparation: one version, one CHANGELOG section, fragments consumed.

Every change records its changelog entry in its own file, ``changelog.d/<card>-<slug>.md``, so two
merge requests never edit the same line. Only the release commit on a ``release/*`` branch touches
``CHANGELOG.md`` and the version; this script makes that commit's tree:

    python3 ops/release.py 0.2.27                  # today's date (UTC)
    python3 ops/release.py 0.2.27 --date 2026-11-12
    python3 ops/release.py 0.2.27 --dry-run        # print the new section, change nothing

It sets the version in ``pyproject.toml``, ``src/mesh_runner/__init__.py`` and
``mesh-extension.json``, moves the fragments (and any entries still written under
``## Unreleased``) into a new ``## <version> — <date>`` section, updates the ``updated:`` field of
the front matter and deletes the consumed fragment files. Nothing is written when any check fails.
Standard library only. Procedure: ``docs/release-process.md``.
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRAGMENT_DIR = "changelog.d"
FRAGMENT_NAME = re.compile(r"^[0-9a-f]{8}(?:-[a-z0-9]+)*\.md$")    # <card>.md or <card>-<slug>.md
FRAGMENT_SKIP = {"README.md"}
UNRELEASED_NOTE = ("Entries wait in `changelog.d/` until the release commit "
                   "(`docs/release-process.md`).")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")

# path -> pattern whose group 2 is the version (exactly one match per file)
VERSION_FILES = {
    "pyproject.toml": re.compile(r'^(version = ")([^"]+)(")', re.M),
    "src/mesh_runner/__init__.py": re.compile(r'^(__version__ = ")([^"]+)(")', re.M),
    "mesh-extension.json": re.compile(r'^(  "version": ")([^"]+)(",?)$', re.M),
}


class ReleaseError(Exception):
    pass


def version_tuple(v: str) -> tuple[int, ...]:
    if not VERSION_RE.match(v):
        raise ReleaseError(f"version {v!r} is not MAJOR.MINOR.PATCH")
    return tuple(int(p) for p in v.split("."))


def read_version(path: str, text: str) -> str:
    found = VERSION_FILES[path].findall(text)
    if len(found) != 1:
        raise ReleaseError(f"{path}: expected one version line, found {len(found)}")
    return found[0][1]


def fragment_problem(name: str, text: str) -> str | None:
    """Why a changelog.d file is not a valid fragment, or None."""
    if not FRAGMENT_NAME.match(name):
        return f"{FRAGMENT_DIR}/{name}: name must be <8-hex card>.md or <8-hex card>-<slug>.md (lowercase, digits, hyphens)"
    body = text.strip()
    if not body:
        return f"{FRAGMENT_DIR}/{name}: empty"
    if not body.startswith("- "):
        return f"{FRAGMENT_DIR}/{name}: must start with a list item '- **What changed.** ...'"
    if re.search(r"^#", body, re.M):
        return f"{FRAGMENT_DIR}/{name}: no headings; the release script writes the version heading"
    return None


def collect_fragments(root: Path) -> list[tuple[Path, str]]:
    folder = root / FRAGMENT_DIR
    if not folder.is_dir():
        return []
    out, problems = [], []
    for path in sorted(folder.iterdir()):
        if path.name in FRAGMENT_SKIP or path.name.startswith("."):
            continue
        if not path.is_file():
            problems.append(f"{FRAGMENT_DIR}/{path.name}: not a file")
            continue
        text = path.read_text(encoding="utf-8")
        problem = fragment_problem(path.name, text)
        if problem:
            problems.append(problem)
        else:
            out.append((path, text.strip()))
    if problems:
        raise ReleaseError("\n".join(problems))
    return out


@dataclass
class Plan:
    version: str
    previous: str
    files: dict[str, str]          # relative path -> new text
    consumed: list[Path]
    section: str


def render_changelog(text: str, version: str, date: str, entries: list[str], updated: str) -> tuple[str, str]:
    """New CHANGELOG.md text and the new section. ``entries`` are fragment bodies; entries still
    written under ``## Unreleased`` (before fragments existed) follow them."""
    if re.search(rf"^## {re.escape(version)}(\s|$)", text, re.M):
        raise ReleaseError(f"CHANGELOG.md already has a section for {version}")
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end < 0:
            raise ReleaseError("CHANGELOG.md: unterminated front matter")
        head = re.sub(r"^updated: .*$", f"updated: {updated}", text[:end], count=1, flags=re.M)
        text = head + text[end:]
    m = re.search(r"^## Unreleased[ \t]*\n", text, re.M)
    if not m:
        raise ReleaseError("CHANGELOG.md has no '## Unreleased' heading")
    nxt = re.search(r"^## ", text[m.end():], re.M)
    body_end = m.end() + nxt.start() if nxt else len(text)
    legacy = text[m.end():body_end].replace(UNRELEASED_NOTE, "").strip()
    blocks = [e.strip() for e in entries if e.strip()]
    if legacy:
        blocks.append(legacy)
    if not blocks:
        raise ReleaseError(f"nothing to release: no fragments in {FRAGMENT_DIR}/ and no entries under Unreleased")
    section = f"## {version} — {date}\n\n" + "\n".join(blocks) + "\n"
    rest = text[body_end:]
    new = text[:m.start()] + f"## Unreleased\n\n{UNRELEASED_NOTE}\n\n" + section + ("\n" + rest if rest else "")
    return new, section


def plan_release(root: Path, version: str, date: str, updated: str) -> Plan:
    new_v = version_tuple(version)
    texts = {rel: (root / rel).read_text(encoding="utf-8") for rel in VERSION_FILES}
    current = {rel: read_version(rel, t) for rel, t in texts.items()}
    if len(set(current.values())) != 1:
        raise ReleaseError(f"version files disagree before the release: {current}")
    previous = next(iter(current.values()))
    if new_v <= version_tuple(previous):
        raise ReleaseError(f"version {version} is not newer than the current {previous}")
    files = {}
    for rel, text in texts.items():
        files[rel] = VERSION_FILES[rel].sub(lambda mm: mm.group(1) + version + mm.group(3), text, count=1)
    fragments = collect_fragments(root)
    # sorted by file name: deterministic; the order inside one release carries no meaning
    entries = [body for _, body in fragments]
    changelog, section = render_changelog((root / "CHANGELOG.md").read_text(encoding="utf-8"),
                                          version, date, entries, updated)
    files["CHANGELOG.md"] = changelog
    return Plan(version, previous, files, [p for p, _ in fragments], section)


def apply(root: Path, plan: Plan) -> None:
    for rel, text in plan.files.items():
        tmp = root / (rel + ".release-tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(root / rel)
    for path in plan.consumed:
        path.unlink()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("version", help="the new version, MAJOR.MINOR.PATCH")
    ap.add_argument("--date", help="release date YYYY-MM-DD (default: today, UTC)")
    ap.add_argument("--root", type=Path, default=ROOT, help="repository root (default: this checkout)")
    ap.add_argument("--dry-run", action="store_true", help="print the new section and change nothing")
    args = ap.parse_args(argv)
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    date = args.date or now.date().isoformat()
    try:
        dt.date.fromisoformat(date)
        plan = plan_release(args.root, args.version, date, now.isoformat())
    except (ReleaseError, ValueError) as exc:
        print(f"release refused: {exc}", file=sys.stderr)
        return 2
    print(plan.section, end="")
    if args.dry_run:
        print(f"\n(dry run: {plan.previous} -> {plan.version}, {len(plan.consumed)} fragment(s) would be consumed)",
              file=sys.stderr)
        return 0
    apply(args.root, plan)
    print(f"\n{plan.previous} -> {plan.version}: {', '.join(plan.files)} written, "
          f"{len(plan.consumed)} fragment(s) removed from {FRAGMENT_DIR}/. "
          f"Commit with: git add -A {' '.join(plan.files)} {FRAGMENT_DIR} && git commit -m 'release: {plan.version}'",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
