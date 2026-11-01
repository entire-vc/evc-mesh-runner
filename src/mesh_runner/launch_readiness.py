"""No-child executable readiness for the exact launch environment.

This observes static executable/interpreter availability, not model entitlement,
provider authentication, tool permissions or MCP initialization. It never runs a
harness or reads credentials. Native exec-required is not a loader-validity proof;
the single intended Popen provides that boundary. Unsupported shebangs stay held.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat as statmod
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Observation:
    state: str
    reason: str
    fingerprint: str


def check(binary: str, cwd: Path, env: dict[str, str], *,
          additional_binaries: tuple[str, ...] = (),
          required_files: tuple[Path, ...] = ()) -> Observation:
    binaries = (binary, *additional_binaries)
    facts: list = [list(binaries), str(cwd), env.get("PATH", os.defpath),
                   [str(path) for path in required_files]]

    def result(state: str, reason: str) -> Observation:
        digest = hashlib.sha256(json.dumps(facts).encode()).hexdigest()
        return Observation(state, reason, digest)

    search_errors: dict[str, str] = {}

    def resolve(name: str) -> Path | None:
        denied = False
        unavailable = False
        if "/" in name:
            candidates = [Path(name) if Path(name).is_absolute() else cwd / name]
        else:
            candidates = []
            for entry in env.get("PATH", os.defpath).split(os.pathsep):
                directory = Path(entry) if entry else cwd
                candidates.append((directory if directory.is_absolute() else cwd / directory) / name)
        for path in candidates:
            try:
                item_stat = path.stat()
                facts.append([str(path), item_stat.st_dev, item_stat.st_ino, item_stat.st_mode,
                              item_stat.st_size, item_stat.st_mtime_ns, item_stat.st_ctime_ns])
                if statmod.S_ISREG(item_stat.st_mode) and os.access(path, os.X_OK):
                    return path
                denied = True
            except FileNotFoundError:
                facts.append([str(path), "absent"])
            except NotADirectoryError:
                facts.append([str(path), "not-a-directory"])
            except PermissionError:
                facts.append([str(path), "permission-denied"])
                denied = True
            except OSError as exc:
                facts.append([str(path), type(exc).__name__])
                unavailable = True
        search_errors[name] = "permission" if denied else "unavailable" if unavailable else "missing"
        return None

    def missing_reason(name: str, default: str) -> tuple[str, str]:
        failure = search_errors.get(name)
        if failure == "permission":
            return "deny", "executable-search-permission-denied"
        if failure == "unavailable":
            return "unknown", "executable-observation-unavailable"
        return "deny", default

    try:
        try:
            cwd_stat = cwd.stat()
            facts.append(["cwd", cwd_stat.st_dev, cwd_stat.st_ino, cwd_stat.st_mode,
                          cwd_stat.st_uid, cwd_stat.st_gid])
        except FileNotFoundError:
            facts.append(["cwd", "absent"])
            return result("deny", "missing-working-directory")
        except OSError as exc:
            facts.append(["cwd", type(exc).__name__])
            if isinstance(exc, PermissionError):
                return result("deny", "unsearchable-working-directory")
            return result("unknown", "working-directory-observation-unavailable")
        if not statmod.S_ISDIR(cwd_stat.st_mode):
            return result("deny", "not-directory-working-directory")
        if not os.access(cwd, os.X_OK):
            return result("deny", "unsearchable-working-directory")

        for file_path in required_files:
            try:
                file_stat = file_path.stat()
                facts.append(["file", str(file_path), file_stat.st_dev, file_stat.st_ino,
                              file_stat.st_mode, file_stat.st_size, file_stat.st_mtime_ns])
            except FileNotFoundError:
                facts.append(["file", str(file_path), "absent"])
                return result("deny", "missing-launch-file")
            if not statmod.S_ISREG(file_stat.st_mode) or not os.access(file_path, os.R_OK):
                return result("deny", "unreadable-launch-file")

        native = (b"\x7fELF", b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe",
                  b"\xfe\xed\xfa\xcf", b"\xcf\xfa\xed\xfe",
                  b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca",
                  b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca")
        exec_required = False
        for binary_index, candidate in enumerate(binaries):
            via_env = False
            name = candidate
            seen = set()
            for depth in range(5):
                path = resolve(name)
                if path is None:
                    if binary_index == 0:
                        reason = "missing-executable" if depth == 0 else "missing-interpreter"
                    else:
                        reason = "missing-wrapper" if depth == 0 else "missing-wrapper-interpreter"
                    state, reason = missing_reason(name, reason)
                    return result(state, reason)
                identity = path.resolve()
                if identity in seen:
                    return result("deny", "interpreter-cycle")
                seen.add(identity)
                try:
                    source = path.open("rb")
                except PermissionError:
                    # Only the intended OS exec may decide an unreadable primary
                    # executable/interpreter. env/downstream wrappers exec later.
                    if binary_index == 0 and not via_env:
                        exec_required = True
                        break
                    raise
                with source:
                    prefix_bytes = source.read(4)
                    if prefix_bytes[:2] != b"#!":
                        if prefix_bytes not in native:
                            return result("unknown", "unsupported-executable-format")
                        if binary_index == 0 and not via_env:
                            exec_required = True
                        break
                    source.seek(2)
                    line = source.readline(4096)
                if len(line) >= 4096 or not line.endswith(b"\n") or b"\r" in line or b"\x00" in line:
                    return result("unknown", "unsupported-shebang")
                facts.append(hashlib.sha256(line).hexdigest())
                try:
                    text = line[:-1].decode("utf-8")
                    # The kernel separates interpreter and argument with ASCII
                    # space/tab only; Unicode split() would invent an executable.
                    if any(c.isspace() and c not in " \t" for c in text):
                        return result("unknown", "unsupported-shebang")
                    text = text.strip(" \t")
                    boundary = next((i for i, c in enumerate(text) if c in " \t"), len(text))
                    words = [text[:boundary]] if text else []
                    argument = text[boundary:].strip(" \t")
                    if argument:
                        words.append(argument)
                except UnicodeDecodeError:
                    return result("unknown", "unsupported-shebang")
                if not words or not words[0].startswith("/"):
                    return result("unknown", "unsupported-shebang")
                name = words[0]
                if name == "/usr/bin/env":
                    via_env = True
                    # env receives ONE kernel shebang argument, not shell words.
                    # -S, assignments and options require platform-specific parsing.
                    if len(words) != 2 or len(words[1].split()) != 1 or words[1].startswith("-") or "=" in words[1]:
                        return result("unknown", "unsupported-env-shebang")
                    if resolve(name) is None:
                        state, reason = missing_reason(name, "missing-interpreter")
                        return result(state, reason)
                    name = words[1]
            else:
                return result("unknown", "interpreter-depth")
        if exec_required:
            return result("exec-required", "native-exec-required")
        return result("ready", "executable-available")
    except (OSError, ValueError):
        return result("unknown", "executable-observation-unavailable")
