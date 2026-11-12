#!/usr/bin/env python3
"""Fail if a built wheel or sdist of the Runner carries the execution canary tools, or if either
lacks the execution publisher (mesh_runner/execution_observations.py ships in the package).

Usage: check-dist-isolation.py <dist-dir>
"""
import sys
import tarfile
import zipfile
from pathlib import Path

FORBIDDEN = ("execution_canary", "execution_actual_canary", "tools/execution_canary")
REQUIRED = "mesh_runner/execution_observations.py"


def names(path: Path) -> list[str]:
    if path.suffix == ".whl":
        return zipfile.ZipFile(path).namelist()
    return tarfile.open(path).getnames()


def main() -> int:
    dists = [p for p in Path(sys.argv[1]).iterdir() if p.suffix == ".whl" or p.name.endswith(".tar.gz")]
    if len(dists) < 2:
        print(f"FAIL expected a wheel and an sdist in {sys.argv[1]}, found {[p.name for p in dists]}")
        return 1
    bad = [(p.name, n) for p in dists for n in names(p) if any(f in n for f in FORBIDDEN)]
    missing = [p.name for p in dists if not any(n.endswith(REQUIRED) for n in names(p))]
    for artifact, name in bad:
        print(f"FAIL {artifact} carries {name}")
    for artifact in missing:
        print(f"FAIL {artifact} lacks {REQUIRED}")
    if not bad and not missing:
        print("OK " + ", ".join(f"{p.name} ({len(names(p))} files, {REQUIRED})" for p in dists))
    return 1 if bad or missing else 0


if __name__ == "__main__":
    sys.exit(main())
