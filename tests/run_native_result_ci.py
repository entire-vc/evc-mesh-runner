#!/usr/bin/env python3
"""Run native-result filesystem/process controls as an unprivileged CI user.

The Python job image starts as root, which bypasses the chmod-based write
failures these controls must exercise. Drop real/effective/saved credentials
before pytest creates its fixtures; never skip or mock that boundary.
"""
from __future__ import annotations

import os
import pwd
import sys
import tempfile


def main():
    if os.geteuid() == 0:
        account = pwd.getpwnam("nobody")
        if account.pw_uid == 0 or account.pw_gid == 0:
            raise RuntimeError("native-result CI requires an unprivileged account")
        os.setgroups([])
        os.setgid(account.pw_gid)
        os.setuid(account.pw_uid)
    if os.getuid() == 0 or os.geteuid() == 0:
        raise RuntimeError("native-result permission controls must not run as root")
    # Neither this process nor its real child processes need checkout writes.
    sys.dont_write_bytecode = True
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    print(f"native-result CI uid={os.getuid()} gid={os.getgid()}", flush=True)
    import pytest
    # A preceding root-run check may already own pytest's default temp root.
    with tempfile.TemporaryDirectory(prefix="native-result-ci-") as scratch:
        return pytest.main(["-q", "-p", "no:cacheprovider", "--basetemp", scratch,
                            *(sys.argv[1:] or ["tests/native_result"])])


if __name__ == "__main__":
    raise SystemExit(main())
