#!/usr/bin/env python3
"""Install or roll back a Runner release on this host. One command, no fleet drain.

    install.py --source <exported tree> [--version X]     copy the tree into the catalog and switch to it
    install.py --activate <version>                       switch to a version already in the catalog (rollback)
    install.py --status                                   what is installed and what is running

The tree is exported from the release commit, the head of ``release/<version>``:
``git archive <release-sha> | tar -x -C <dir>``. ``git archive`` fills ``SOURCE_COMMIT`` in (export-subst)
with that commit and its parents; the file is copied into the catalog, and ``--status`` and the install
report name the commit a release was exported from. A merge commit (the release merged into ``main``
after main moved on) also carries the commits merged after the cut-off, which the version's CHANGELOG
does not name: ``--source`` refuses it unless ``--allow-merge-commit``.

Why this needs no drain: agent turns run in their own sessions (tmux, or a child in its own
process session) and write their event stream to a file; the attempt registry is SQLite. A
restarted Runner re-attaches to live turns and reconciles pending delivery rows from the journal
(`launch_guard.journal_compatible`). Stopping the Runner therefore stops scheduling, not work.

What it does, in order:
  1. copy the source tree to <catalog>/<version> (refused when that version exists with other content);
  2. load the live config with the NEW code (a config the new schema rejects never reaches launchd);
  3. point the launchd plist at the new release, stop the job, start it;
  4. wait for the new process to publish its receipt in status.json;
  5. if it does not, put the previous plist back, start it, and exit non-zero.

The activation marker of the release being left is kept (`activated-release.<version>.json`). A release
from before the journal rule refuses to start on another release's marker while a delivery row is
pending; its own marker is put back before it starts, on a rollback and on `--activate`.

Nothing here reads or writes the attempt registry, leases, pause tokens or the config.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import sys
import time
from pathlib import Path

LABEL = "vc.entire.mesh-runner"
HOME = Path.home()
CATALOG = HOME / ".mesh-runner-catalog" / "agent-runner"
PLIST = HOME / "Library" / "LaunchAgents" / f"{LABEL}.plist"
CONFIG = HOME / ".mesh-runner" / "runner.toml"
SOURCE_COMMIT = "SOURCE_COMMIT"
COPIED = ("src", "pyproject.toml", "mesh-extension.json", "CHANGELOG.md", "README.md", "LICENSE", "requirements.lock",
          SOURCE_COMMIT)


class Failed(RuntimeError):
    pass


def tree_digest(release: Path) -> str:
    """Content identity of the Python sources of a release (paths relative to the release)."""
    source = release / "src" / "mesh_runner"
    entries = [[str(p.relative_to(release)), hashlib.sha256(p.read_bytes()).hexdigest()]
               for p in sorted(source.rglob("*.py"))]
    if not entries or not (source / "runner.py").is_file():
        raise Failed(f"{release} is not a Runner release (no src/mesh_runner/runner.py)")
    return hashlib.sha256(json.dumps(entries, separators=(",", ":")).encode()).hexdigest()


def source_version(release: Path) -> str:
    for line in (release / "src" / "mesh_runner" / "__init__.py").read_text().splitlines():
        if line.startswith("__version__"):
            return line.split("=", 1)[1].strip().strip("\"'")
    raise Failed("no __version__ in src/mesh_runner/__init__.py")


def source_commit(release: Path) -> dict:
    """The commit ``release`` was exported from, read from ``SOURCE_COMMIT`` (``$Format:%H %P$`` filled in by
    ``git archive <commit>``): ``{"sha", "parents"}``, or ``{"sha": None, "why"}`` when the tree does not say
    (no file: a release from before it; the placeholder: a copy of a checkout, or ``git archive`` of a tree)."""
    try:
        words = (release / SOURCE_COMMIT).read_text().split()
    except OSError:
        return {"sha": None, "why": f"no {SOURCE_COMMIT} in the tree"}
    if not words or not all(len(w) in (40, 64) and all(c in "0123456789abcdef" for c in w) for w in words):
        return {"sha": None, "why": f"{SOURCE_COMMIT} is not filled in: export the tree with git archive <commit>"}
    return {"sha": words[0], "parents": words[1:]}


def refuse_merge_commit(source: Path) -> None:
    commit = source_commit(source)
    if len(commit.get("parents") or []) > 1:
        raise Failed(f"{source} is exported from merge commit {commit['sha']} (parents {', '.join(commit['parents'])}): "
                     "it also carries what reached main after the release cut-off. Export the release commit, the "
                     "head of release/<version> (the second parent of the release merge), see docs/release-process.md; "
                     "--allow-merge-commit installs it anyway")


def stage(source: Path, catalog: Path, version: str) -> Path:
    """Copy ``source`` to ``catalog/version``; an existing identical tree is reused.

    Identical means the same Python sources and the same source commit: an entry staged before
    ``SOURCE_COMMIT`` existed gets the source's file; an entry exported from another commit is refused,
    so ``--status`` never names a commit the installed tree was not exported from."""
    target = catalog / version
    if target.exists():
        if tree_digest(target) != tree_digest(source):
            raise Failed(f"{target} already holds different content; choose another --version")
        held, given = source_commit(target).get("sha"), source_commit(source).get("sha")
        if held is None and (source / SOURCE_COMMIT).is_file():
            shutil.copy2(source / SOURCE_COMMIT, target / SOURCE_COMMIT)
        elif held != given:
            raise Failed(f"{target} holds the same code exported from commit {held}, not {given or 'an unknown commit'}; "
                         "export the commit that version was installed from, or choose another --version")
        return target
    catalog.mkdir(parents=True, exist_ok=True)
    partial = catalog / f".{version}.partial"
    if partial.exists():
        shutil.rmtree(partial)
    partial.mkdir()
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache")
    for name in COPIED:
        item = source / name
        if item.is_dir():
            shutil.copytree(item, partial / name, ignore=ignore)
        elif item.is_file():
            shutil.copy2(item, partial / name)
    if tree_digest(partial) != tree_digest(source):
        shutil.rmtree(partial)
        raise Failed("the copy differs from the source")
    os.replace(partial, target)
    return target


def preflight(release: Path, config: Path, python: str) -> None:
    """The new code must import and accept the live config before launchd is touched."""
    code = ("import sys; from mesh_runner.config import load; from mesh_runner import launch_guard; "
            "cfg = load(sys.argv[1]); launch_guard.target_identity(__import__('pathlib').Path(sys.argv[2])); "
            "print(len(cfg.agents))")
    env = {**os.environ, "PYTHONPATH": str(release / "src")}
    done = subprocess.run([python, "-c", code, str(config), str(release)], env=env,
                          capture_output=True, text=True, timeout=120)
    if done.returncode != 0:
        raise Failed("the new release does not load the live config:\n" + (done.stderr or done.stdout)[-1500:])


def plist_for(current: dict, release: Path) -> dict:
    """The same job, pointed at ``release``: guard path, target and PYTHONPATH; nothing else changes."""
    new = json.loads(json.dumps(current))            # plain deep copy (the plist holds only JSON types)
    args = list(new["ProgramArguments"])
    guard = next((i for i, a in enumerate(args) if a.endswith("/src/mesh_runner/launch_guard.py")), None)
    target = args.index("--target-release") + 1 if "--target-release" in args else None
    if guard is None and target is None and args[1:4] == ["-m", "mesh_runner.cli", "run"] and "--config" in args[4:]:
        # A job definition from before the guard (`python -m mesh_runner.cli run --config X`): a deploy script
        # that copies such a file over the installed one puts the service on whatever release its PYTHONPATH
        # names. It is rebuilt in the guarded form; the rest of it is kept.
        config = args[args.index("--config") + 1]
        args = [args[0], "launch_guard.py", "--target-release", "", "--config", config]
        guard, target = 1, 3
    if guard is None or target is None:
        raise Failed("the plist does not launch through launch_guard.py --target-release; not rewriting it")
    args[guard] = str(release / "src" / "mesh_runner" / "launch_guard.py")
    args[target] = str(release)
    if "--previous-config" in args:                   # a leftover of an old operation
        i = args.index("--previous-config")
        del args[i:i + 2]
    new["ProgramArguments"] = args
    new.setdefault("EnvironmentVariables", {})["PYTHONPATH"] = str(release / "src")
    return new


def running_release(plist: dict) -> Path | None:
    """The release a job definition launches: the guard's ``--target-release``; for a definition from before
    the guard, the release its PYTHONPATH points into; None when neither says."""
    args = plist.get("ProgramArguments") or []
    if "--target-release" in args:
        return Path(args[args.index("--target-release") + 1])
    for part in str((plist.get("EnvironmentVariables") or {}).get("PYTHONPATH", "")).split(":"):
        if part.endswith("/src"):
            return Path(part[:-len("/src")])
    return None


def runs_another_tree(state: Path, release: Path, fresh_seconds: int = 120) -> bool:
    """A live process is publishing status and it is not ``release``: status.json is fresh and its receipt is
    missing (a release from before receipts) or names another source tree. An absent or stale status file says
    nothing: the job may simply not be running."""
    try:
        status = json.loads((state / "status.json").read_text())
    except (OSError, ValueError):
        return False
    if time.time() - float(status.get("unix") or 0) > fresh_seconds:
        return False
    source = str((status.get("receipt") or {}).get("source") or "")
    try:
        return not source or not Path(source).resolve().is_relative_to(release.resolve())
    except OSError:
        return True


def job_pid(label: str) -> int:
    """The pid of the loaded launchd job, 0 when it is not loaded (or the host has no launchd)."""
    try:
        out = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{label}"],
                             capture_output=True, text=True).stdout
    except FileNotFoundError:
        return 0
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("pid ="):
            return int(line.split("=", 1)[1])
    return 0


def stop(label: str, stop_seconds: int) -> None:
    """Unload the job and wait for its process to exit (it finishes its ticks first)."""
    before = job_pid(label)
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{label}"], capture_output=True)
    deadline = time.monotonic() + stop_seconds
    while before and time.monotonic() < deadline:
        try:
            os.kill(before, 0)
        except ProcessLookupError:
            return
        time.sleep(0.5)


def start(label: str, plist_path: Path) -> None:
    last = ""
    for _ in range(20):                               # bootstrap answers EIO while the old job unloads
        done = subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist_path)],
                              capture_output=True, text=True)
        if done.returncode == 0 or job_pid(label):
            return
        last = (done.stderr or done.stdout).strip()
        time.sleep(1.5)
    raise Failed(f"launchd did not load {plist_path}: {last}")


def healthy(state: Path, release: Path, since: float, wait_seconds: int, label: str) -> dict | None:
    """The receipt the new process published: its own pid, its source inside ``release``, written after ``since``."""
    source = str((release / "src" / "mesh_runner").resolve())
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        try:
            receipt = json.loads((state / "status.json").read_text()).get("receipt") or {}
        except (OSError, ValueError):
            receipt = {}
        pid = job_pid(label)
        if (pid and receipt.get("pid") == pid and str(receipt.get("source") or "") == source
                and float(receipt.get("status_unix") or 0) >= since):
            return receipt
        time.sleep(2)
    return None


MARKER = "activated-release.json"


def saved_marker(state: Path, release: Path) -> Path:
    return state / f"activated-release.{release.name}.json"


def keep_marker(state: Path, previous: Path) -> None:
    """Keep the activation marker of the release being left, next to the live one.

    A release older than the journal rule refuses to start on a marker bound to another release while a
    delivery row is pending. Its own marker, put back before it starts, is the binding it already accepted:
    that is what makes going back to such a release possible at all."""
    marker = state / MARKER
    try:
        value = json.loads(marker.read_text())
    except (OSError, ValueError):
        return
    if Path(str(value.get("release") or "")).resolve() == previous.resolve():
        shutil.copy2(marker, saved_marker(state, previous))


def restore_marker(state: Path, release: Path) -> bool:
    """Put back the marker ``release`` was activated with, if this release needs it and one was kept."""
    guard = release / "src" / "mesh_runner" / "launch_guard.py"
    try:
        if "journal_compatible" in guard.read_text():
            return False                              # this release takes pending rows over by itself
    except OSError:
        return False
    kept = saved_marker(state, release)
    if not kept.is_file():
        return False
    partial = state / (MARKER + ".partial")
    shutil.copy2(kept, partial)
    os.replace(partial, state / MARKER)
    return True


def write_plist(path: Path, value: dict) -> None:
    partial = path.with_suffix(".plist.partial")
    with open(partial, "wb") as stream:
        plistlib.dump(value, stream)
    os.replace(partial, path)


def switch(release: Path, args) -> dict:
    with open(args.plist, "rb") as stream:
        current = plistlib.load(stream)
    previous = running_release(current)
    state = Path(args.state)
    report = {"previous": str(previous) if previous else "unknown", "target": str(release), "tree": tree_digest(release),
              "commit": source_commit(release)}
    same = previous is not None and previous.resolve() == release.resolve()
    if same and not args.restart and not runs_another_tree(state, release):
        return {**report, "result": "already-active", "pid": job_pid(args.label)}
    preflight(release, Path(args.config), args.python)
    keep = Path(args.plist).with_suffix(f".plist.before-{release.name}")
    shutil.copy2(args.plist, keep)
    if previous is not None:
        keep_marker(state, previous)
    began = time.time()
    write_plist(Path(args.plist), plist_for(current, release))
    try:
        stop(args.label, args.stop_seconds)
        report["marker_restored"] = restore_marker(state, release)
        start(args.label, Path(args.plist))
        receipt = healthy(state, release, began, args.wait_seconds, args.label)
    except Failed as exc:
        receipt, report["error"] = None, str(exc)
    if receipt is not None:
        return {**report, "result": "active", "pid": receipt["pid"], "release": receipt.get("release"),
                "seconds": round(time.time() - began, 1), "previous_plist": str(keep)}
    # not healthy: the previous job definition goes back, whatever the new process is doing
    write_plist(Path(args.plist), current)
    stop(args.label, args.stop_seconds)
    if previous is not None:
        restore_marker(state, previous)
    start(args.label, Path(args.plist))
    back = previous is not None and healthy(state, previous, time.time() - 1, args.wait_seconds, args.label)
    report.update(result="rolled-back" if back else "rollback-unconfirmed", pid=job_pid(args.label),
                  seconds=round(time.time() - began, 1))
    return report


def status(args) -> dict:
    with open(args.plist, "rb") as stream:
        configured = running_release(plistlib.load(stream))
    try:
        receipt = json.loads((Path(args.state) / "status.json").read_text()).get("receipt") or {}
    except (OSError, ValueError):
        receipt = {}
    catalog = Path(args.catalog)
    return {"configured": str(configured) if configured else "unknown",
            "commit": source_commit(configured) if configured else {"sha": None, "why": "no configured release"},
            "pid": job_pid(args.label),
            "receipt": {k: receipt.get(k) for k in ("pid", "release", "source", "status_unix")},
            "receipt_age_seconds": round(time.time() - float(receipt.get("status_unix") or 0), 1),
            "catalog": sorted(p.name for p in catalog.iterdir() if p.is_dir() and not p.name.startswith("."))
            if catalog.is_dir() else []}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    what = ap.add_mutually_exclusive_group(required=True)
    what.add_argument("--source", type=Path, help="exported release tree to install")
    what.add_argument("--activate", metavar="VERSION", help="switch to a version already in the catalog")
    what.add_argument("--status", action="store_true")
    ap.add_argument("--version", help="catalog name for --source (default: __version__ of the tree)")
    ap.add_argument("--allow-merge-commit", action="store_true",
                    help="install a tree exported from a merge commit (normally the release commit is installed)")
    ap.add_argument("--restart", action="store_true", help="restart even when the target is already active")
    ap.add_argument("--catalog", default=str(CATALOG))
    ap.add_argument("--plist", default=str(PLIST))
    ap.add_argument("--config", default=str(CONFIG))
    ap.add_argument("--state", default=str(CONFIG.parent))
    ap.add_argument("--label", default=LABEL)
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--stop-seconds", type=int, default=120, help="how long the old process may take to exit")
    ap.add_argument("--wait-seconds", type=int, default=180, help="how long the new process may take to report")
    args = ap.parse_args(argv)
    try:
        if args.status:
            out = status(args)
        elif args.activate:
            release = Path(args.catalog) / args.activate
            if not release.is_dir():
                raise Failed(f"{release} is not in the catalog")
            out = switch(release, args)
        else:
            source = args.source.resolve()
            if not args.allow_merge_commit:
                refuse_merge_commit(source)
            out = switch(stage(source, Path(args.catalog), args.version or source_version(source)), args)
    except Failed as exc:
        print(json.dumps({"result": "refused", "reason": str(exc)}, ensure_ascii=False, indent=1))
        return 2
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0 if out.get("result") in (None, "active", "already-active") or args.status else 1


if __name__ == "__main__":
    raise SystemExit(main())
