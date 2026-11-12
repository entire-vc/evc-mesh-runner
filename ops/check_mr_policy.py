#!/usr/bin/env python3
"""Merge request gate: changelog fragments and release-only version changes.

Run by the ``mr-policy`` CI job on every merge request pipeline; locally:

    python3 ops/check_mr_policy.py --base origin/main --branch "$(git branch --show-current)"

Rules (``docs/release-process.md``):

1. Outside a ``release/*`` branch a merge request must not change the version in
   ``pyproject.toml``, ``src/mesh_runner/__init__.py`` or ``mesh-extension.json``, nor
   ``CHANGELOG.md``: the release commit does that.
2. A merge request that changes ``src/`` adds or edits its entry in
   ``changelog.d/<card>-<slug>.md``. A change that needs no entry says so with the label
   ``no-changelog`` or a description line ``Changelog: none``.
3. Every fragment the merge request adds or edits is well formed.
4. A ``release/*`` branch raises the version in all three files to one new value, has a
   non-empty ``CHANGELOG.md`` section for it and leaves no fragment behind. Its ``src/`` changes
   arrive only through merged merge requests (each carried its fragment through rule 2): a commit
   made on the release branch itself may change ``src/`` only in the version line.

Inputs default to the GitLab merge request variables (``CI_MERGE_REQUEST_DIFF_BASE_SHA``,
``CI_MERGE_REQUEST_SOURCE_BRANCH_NAME``, ``CI_MERGE_REQUEST_LABELS``,
``CI_MERGE_REQUEST_DESCRIPTION``). Exit 0 = pass, 1 = refused, 2 = cannot evaluate.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("ops_release", HERE / "release.py")
release = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("ops_release", release)
_spec.loader.exec_module(release)

SKIP_LABEL = "no-changelog"
SKIP_LINE = re.compile(r"^\s*changelog:\s*none\b", re.I | re.M)
DOC = "docs/release-process.md"


class CannotEvaluate(Exception):
    pass


def git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    res = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    if check and res.returncode != 0:
        raise CannotEvaluate(f"git {' '.join(args)}: {res.stderr.strip()}")
    return res


def show(root: Path, rev: str, path: str) -> str | None:
    res = git(root, "show", f"{rev}:{path}", check=False)
    return res.stdout if res.returncode == 0 else None


def changed_files(root: Path, base: str, head: str) -> list[tuple[str, str]]:
    out = git(root, "diff", "--no-renames", "--name-status", f"{base}...{head}").stdout
    rows = []
    for line in out.splitlines():
        status, _, path = line.partition("\t")
        rows.append((status[:1], path))
    return rows


def version_at(root: Path, rev: str, path: str) -> str | None:
    text = show(root, rev, path)
    if text is None:
        return None
    try:
        return release.read_version(path, text)
    except release.ReleaseError:
        return None


def check(root: Path, base: str, head: str, branch: str, labels: list[str], description: str) -> list[str]:
    """Problems that refuse the merge request; empty = pass."""
    git(root, "rev-parse", "--verify", f"{base}^{{commit}}")
    git(root, "rev-parse", "--verify", f"{head}^{{commit}}")
    rows = changed_files(root, base, head)
    paths = {p for _, p in rows}
    is_release = branch.startswith("release/")
    problems: list[str] = []

    version_moves = {}
    for rel in release.VERSION_FILES:
        before, after = version_at(root, base, rel), version_at(root, head, rel)
        if before != after:
            version_moves[rel] = (before, after)

    fragments = [(s, p) for s, p in rows
                 if p.startswith(release.FRAGMENT_DIR + "/") and p.count("/") == 1
                 and Path(p).name not in release.FRAGMENT_SKIP]
    for status, path in fragments:
        if status == "D":
            continue
        problem = release.fragment_problem(Path(path).name, show(root, head, path) or "")
        if problem:
            problems.append(problem)

    if not is_release:
        for rel, (before, after) in sorted(version_moves.items()):
            problems.append(
                f"{rel}: version {before} -> {after} outside a release/* branch. The version changes only in "
                f"the release commit of the release branch; leave it at {before} and describe the change in "
                f"{release.FRAGMENT_DIR}/<card>-<slug>.md ({DOC}).")
        if "CHANGELOG.md" in paths:
            problems.append(
                f"CHANGELOG.md is edited outside a release/* branch. Move the entry into "
                f"{release.FRAGMENT_DIR}/<card>-<slug>.md (one file per change, a list item "
                f"'- **What changed.** ...'); the release commit collects it ({DOC}).")
        touches_src = any(p.startswith("src/") for p in paths)
        has_entry = any(s != "D" for s, _ in fragments)
        skipped = SKIP_LABEL in labels or bool(SKIP_LINE.search(description or ""))
        if touches_src and not has_entry and not skipped:
            problems.append(
                f"src/ changes without a changelog entry. Add {release.FRAGMENT_DIR}/<card>-<slug>.md with a list "
                f"item '- **What changed.** ...'. A change that needs no entry: label '{SKIP_LABEL}' or a "
                f"description line 'Changelog: none', then re-run this job ({DOC}).")
    else:
        heads = {rel: version_at(root, head, rel) for rel in release.VERSION_FILES}
        values = set(heads.values())
        new = next(iter(values)) if len(values) == 1 else None
        if new is None:
            problems.append(f"release branch: version files disagree at the head: {heads}")
        else:
            old = version_at(root, base, "pyproject.toml")
            try:
                newer = old is None or release.version_tuple(new) > release.version_tuple(old)
            except release.ReleaseError as exc:
                problems.append(f"release branch: {exc}")
                newer = True                     # reported above
            if not newer:
                problems.append(f"release branch: version {new} is not newer than main's {old}; "
                                f"run python3 ops/release.py <version> ({DOC})")
            changelog = show(root, head, "CHANGELOG.md") or ""
            sec = re.search(rf"^## {re.escape(new)}(?:[ \t][^\n]*)?\n(.*?)(?=^## |\Z)", changelog, re.M | re.S)
            if not sec:
                problems.append(f"release branch: CHANGELOG.md has no '## {new}' section; "
                                f"run python3 ops/release.py {new} ({DOC})")
            elif not re.search(r"^- ", sec.group(1), re.M):
                problems.append(f"release branch: the '## {new}' section of CHANGELOG.md has no entry ({DOC})")
        # Code reaches a release branch only by merging reviewed merge requests (each passed this gate
        # with its fragment). Commits made on the branch itself (first-parent, non-merge) may touch
        # src/ only for the version line written by ops/release.py.
        own = git(root, "rev-list", "--first-parent", "--no-merges", f"{base}..{head}").stdout.split()
        for sha in own:
            touched = git(root, "diff-tree", "--no-commit-id", "--name-only", "-r", sha).stdout.split()
            direct = [p for p in touched if p.startswith("src/") and p != "src/mesh_runner/__init__.py"]
            if direct:
                problems.append(
                    f"release branch: commit {sha[:12]} changes {', '.join(direct)} directly. Code enters a "
                    f"release branch only by merging a reviewed merge request with its fragment "
                    f"(git merge --no-ff <reviewed-head-sha>); ship the fix through its own merge request ({DOC}).")
            elif "src/mesh_runner/__init__.py" in touched:
                diff = git(root, "show", "--format=", "-U0", sha, "--", "src/mesh_runner/__init__.py").stdout
                changed = [l for l in diff.splitlines()
                           if l[:1] in "+-" and not l.startswith(("+++", "---"))]
                if any(not re.match(r'^[+-]__version__ = "[^"]*"$', l) for l in changed):
                    problems.append(
                        f"release branch: commit {sha[:12]} changes src/mesh_runner/__init__.py beyond the "
                        f"version line; code enters a release branch only through a reviewed merge request ({DOC}).")
        left = git(root, "ls-tree", "--name-only", head, release.FRAGMENT_DIR + "/").stdout.split()
        left = [p for p in left if Path(p).name not in release.FRAGMENT_SKIP]
        if left:
            problems.append(f"release branch: fragments not collected into CHANGELOG.md: {', '.join(left)}; "
                            f"run python3 ops/release.py <version> on the branch head ({DOC})")
    return problems


def main(argv: list[str] | None = None) -> int:
    env = os.environ
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", type=Path, default=HERE.parent)
    ap.add_argument("--base", default=env.get("CI_MERGE_REQUEST_DIFF_BASE_SHA"),
                    help="merge base or target ref (default: $CI_MERGE_REQUEST_DIFF_BASE_SHA)")
    ap.add_argument("--head", default=env.get("CI_COMMIT_SHA") or "HEAD")
    ap.add_argument("--branch", default=env.get("CI_MERGE_REQUEST_SOURCE_BRANCH_NAME"),
                    help="source branch (default: $CI_MERGE_REQUEST_SOURCE_BRANCH_NAME)")
    ap.add_argument("--labels", default=env.get("CI_MERGE_REQUEST_LABELS", ""), help="comma-separated")
    ap.add_argument("--description", default=env.get("CI_MERGE_REQUEST_DESCRIPTION", ""))
    args = ap.parse_args(argv)
    if not args.base or not args.branch:
        print("mr-policy: cannot evaluate: --base and --branch are required outside a merge request pipeline",
              file=sys.stderr)
        return 2
    labels = [x.strip() for x in args.labels.split(",") if x.strip()]
    try:
        problems = check(args.root, args.base, args.head, args.branch, labels, args.description)
    except CannotEvaluate as exc:
        print(f"mr-policy: cannot evaluate: {exc}", file=sys.stderr)
        return 2
    if problems:
        print(f"mr-policy: refused ({args.branch}):", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    print(f"mr-policy: pass ({args.branch}, base {args.base[:12]})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
