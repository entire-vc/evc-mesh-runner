#!/usr/bin/env python3
"""Fail if a built wheel or sdist of the Runner carries the execution publisher/canary tools or a tools/ tree.

Usage: check-dist-isolation.py <dist-dir>
"""
import sys
import tarfile
import zipfile
from pathlib import Path

FORBIDDEN = ("execution_observations", "execution_canary", "execution_actual_canary", "tools/execution_canary")


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
    for artifact, name in bad:
        print(f"FAIL {artifact} carries {name}")
    if not bad:
        print("OK " + ", ".join(f"{p.name} ({len(names(p))} files)" for p in dists))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
