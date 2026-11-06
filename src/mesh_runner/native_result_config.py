"""Narrow, default-off native path adoption INSIDE the existing Runner.

The startup config pins a separate operator policy. Caller/task/UID/review prose
never supplies authority. This module only publishes an immutable path pair;
it never rewrites TOML, initializes a ledger, enrolls a source or changes leases.
All native consumers hold the same lock for their entire operation. Rollback
is possible only before either destination exists: unknown history is a refusal.
The OS account and the independently admitted startup config are trusted; this
does not protect a process from an attacker controlling that account's memory.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import stat
import threading
import time
import tomllib

from .extension_service import signature_ok, sign
from .native_result_ledger import canonical, identity, receiver, uuid

PREFIX = "/operator/native-result-config/"
FIELDS = ("native_result_dir", "native_result_channels_dir")
PURPOSE = "native-result-config-v1"
MAX_BODY = 8192
_INIT = threading.Lock()


class Refused(ValueError):
    pass


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def unrelated(raw):
    """Type-preserving digest of all TOML content except the two allowed keys."""
    value = tomllib.loads(raw.decode())
    for key in FIELDS:
        value.get("runner", {}).pop(key, None)

    def typed(v):
        if isinstance(v, dict):
            return ["table", [[k, typed(v[k])] for k in sorted(v)]]
        if isinstance(v, list):
            return ["array", [typed(x) for x in v]]
        return [type(v).__name__, str(v)]
    return identity(typed(value))


def private(path, limit=65536):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise Refused("private_owned_file_required")
        raw = f.read(limit + 1)
    if len(raw) > limit:
        raise Refused("file_too_large")
    return raw


@dataclass(frozen=True)
class Pair:
    journal: Path | None
    channels: Path | None

    def json(self):
        return [str(x) if x is not None else None for x in (self.journal, self.channels)]


class Paths:
    def __init__(self, cfg):
        self.lock = threading.RLock()
        self.value = Pair(cfg.native_result_dir, cfg.native_result_channels_dir)


def state(cfg):
    with _INIT:
        if not hasattr(cfg, "_native_paths"):
            cfg._native_paths = Paths(cfg)
        return cfg._native_paths


@contextmanager
def paths(cfg):
    current = state(cfg)
    with current.lock:
        yield current.value


class Adoption:
    """Owned by Runner; no listener/launcher/config-manager of its own."""
    def __init__(self, runner):
        self.runner, self.cfg = runner, runner.cfg
        self.paths = state(self.cfg)
        self.pid, self.started = os.getpid(), time.time_ns()
        self.epoch = secrets.token_hex(32)
        self.generation = 0
        self.operation = ""
        self.previous = None
        self.candidate = ""
        # This pin came from config.load's exact startup bytes, not today's disk.
        self.pin = self.cfg.native_result_adoption

    def readback(self):
        return {"pid": self.pid, "started_ns": self.started, "epoch": self.epoch,
                "generation": self.generation, "paths": self.paths.value.json(),
                "startup_config_sha256": self.cfg.native_config_preimage,
                "loaded_digest": identity({"startup": self.cfg.native_config_preimage,
                    "paths": self.paths.value.json(), "generation": self.generation}),
                "candidate_config_sha256": self.candidate, "operation": self.operation,
                "receipt_scope": "CONTROLLER_LOADED_NATIVE_PATH_PAIR_NOT_ENROLLMENT_OR_ACK"}

    def _authority(self, path, headers, body):
        if not self.pin:
            raise Refused("native_control_disabled")
        st = self.cfg.state_dir.lstat()
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise Refused("private_control_state_required")
        policy_raw = private(self.pin["policy"])
        if sha(policy_raw) != self.pin["policy_sha256"]:
            raise Refused("untrusted_policy")
        p = json.loads(policy_raw)
        if (set(p) != {"version", "purpose", "operator", "agent", "receiver", "paths",
                       "key_file", "key_sha256", "not_before", "expires", "min_revision"}
                or p["version"] != 1 or p["purpose"] != PURPOSE):
            raise Refused("invalid_policy")
        uuid(p["operator"])
        receiver(p["receiver"])
        if not isinstance(p["agent"], str) or not p["agent"]:
            raise Refused("invalid_agent")
        now = time.time()
        if (any(type(p[k]) is not int for k in ("not_before", "expires", "min_revision"))
                or not p["not_before"] <= now < p["expires"] or p["min_revision"] < 1):
            raise Refused("policy_expired_or_unknown")
        key = private(p["key_file"]).strip()
        if len(key) < 32 or sha(key) != p["key_sha256"]:
            raise Refused("untrusted_key")
        # These credentials belong to different authority domains.
        foreign = [self.cfg.extension_secret.encode()]
        if self.cfg.tgpeek_editor_control_secret_file:
            foreign.append(private(self.cfg.tgpeek_editor_control_secret_file).strip())
        if any(v and hmac.compare_digest(v, key) for v in foreign):
            raise Refused("foreign_authority_key")
        # Reject unsigned loopback traffic before any durable control-state write.
        if not signature_ok(key.decode("ascii"), headers.get("X-Native-Timestamp", ""),
                headers.get("X-Native-Signature", ""), path.encode() + b"\n" + body,
                time.time(), tolerance=60):
            raise Refused("untrusted_operator")
        rev = json.loads(private(self.pin["revocations"]))
        if set(rev) != {"version", "policy_sha256", "revision", "expires", "revoked", "signature"}:
            raise Refused("revocations_unknown")
        signature = rev.pop("signature")
        if (rev["version"] != 1 or rev["policy_sha256"] != self.pin["policy_sha256"]
                or type(rev["revision"]) is not int or rev["revision"] < p["min_revision"]
                or type(rev["expires"]) is not int or not now < rev["expires"] <= p["expires"]
                or type(rev["revoked"]) is not bool or not isinstance(signature, str)
                or not hmac.compare_digest(signature, hmac.new(key, canonical(rev), hashlib.sha256).hexdigest())):
            raise Refused("revocations_untrusted")
        record = self.cfg.state_dir / ("native-config-" + self.pin["policy_sha256"] + ".json")
        present = record.exists() or record.is_symlink()
        if present:
            history = json.loads(private(record))
            if (set(history) != {"revision", "revoked", "seen"}
                    or type(history["revision"]) is not int or type(history["revoked"]) is not bool
                    or not isinstance(history["seen"], list)):
                raise Refused("control_history_unknown")
        else:
            history = {"revision": p["min_revision"], "revoked": False, "seen": []}
        if rev["revision"] < history["revision"]:
            raise Refused("stale_revocations")
        changed = (not present or rev["revision"] != history["revision"]
                   or (rev["revoked"] and not history["revoked"]))
        history.update(revision=rev["revision"], revoked=history["revoked"] or rev["revoked"])
        if changed:
            self._save(record, history)  # persist trusted revocation even when it refuses this signed call
        if history["revoked"]:
            raise Refused("authority_revoked")
        return p, key, rev["revision"], record, history

    @staticmethod
    def _save(path, value):
        # Private state directory is created by Runner. Never overwrite authority,
        # revocation, TOML, ledger, registration or any other state file.
        tmp = path.with_name(path.name + "." + secrets.token_hex(12))
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(canonical(value))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
            fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            tmp.unlink(missing_ok=True)

    def _fresh(self, pair, policy):
        if pair.json() != policy["paths"] or len(policy["paths"]) != 2:
            raise Refused("foreign_paths")
        roots = (pair.journal, pair.channels)
        if any(x is None for x in roots) or roots[0] == roots[1]:
            raise Refused("invalid_paths")
        protected = [Path(self.cfg.config_path), self.cfg.state_dir,
                     Path(self.pin["policy"]), Path(self.pin["revocations"]), Path(policy["key_file"])]
        for x in roots:
            if (not x.is_absolute() or x.resolve() != x or x.exists() or x.is_symlink()
                    or any(p.resolve().is_relative_to(x) for p in protected)
                    or any(y != x and y.is_relative_to(x) for y in roots)):
                raise Refused("existing_invalid_or_unknown_history")
            st = x.parent.lstat()
            if (not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077
                    or x.parent.resolve() != x.parent):
                raise Refused("private_canonical_parent_required")

    def handle(self, method, path, headers, body, peer):
        try:
            if method != "POST" or peer not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
                raise Refused("loopback_post_required")
            action = path.removeprefix(PREFIX)
            if path != PREFIX + action or action not in ("status", "adopt", "rollback") or len(body) > MAX_BODY:
                raise Refused("invalid_request")
            if headers.get("JOB-TOKEN") is not None:
                raise Refused("untrusted_operator")
            with self.paths.lock:
                policy, key, revision, record, history = self._authority(path, headers, body)
                req = json.loads(body)
                if not isinstance(req, dict):
                    raise Refused("invalid_request")
                if action == "status":
                    if req:
                        raise Refused("invalid_status_request")
                    return 200, {**self.readback(), "authority_revision": revision}
                expected = {"request_id", "policy_sha256", "authority_revision", "receiver", "expected",
                            "candidate_config_sha256", "paths", "rollback_of"}
                if set(req) != expected:
                    raise Refused("invalid_request_fields")
                uuid(req["request_id"])
                if req["request_id"] in history["seen"] or len(history["seen"]) >= 1024:
                    raise Refused("request_reused_or_capacity")
                if (req["policy_sha256"] != self.pin["policy_sha256"] or req["authority_revision"] != revision
                        or type(req["authority_revision"]) is not int):
                    raise Refused("stale_authority")
                actual = self.readback()
                binding = {k: actual[k] for k in ("pid", "started_ns", "epoch", "generation",
                                                 "loaded_digest", "startup_config_sha256")}
                if (not isinstance(req["expected"], dict) or req["expected"] != binding
                        or any(type(req["expected"].get(k)) is not int for k in ("pid", "started_ns", "generation"))):
                    raise Refused("stale_process_or_config")
                if receiver(req["receiver"]) != policy["receiver"]:
                    raise Refused("wrong_receiver_scope")
                # An authenticated task pin is scope, never the operator authenticator.
                self.runner._native_config_receiver(policy)
                raw = private(self.cfg.config_path, 1 << 20)
                if sha(raw) != req["candidate_config_sha256"]:
                    raise Refused("stale_disk_preimage")
                r = tomllib.loads(raw.decode()).get("runner", {})
                values = [r.get(k) for k in FIELDS]
                if values != req["paths"]:
                    raise Refused("candidate_paths_mismatch")
                if action == "adopt":
                    if (self.paths.value != Pair(None, None) or self.operation or req["rollback_of"]
                            or unrelated(raw) != self.cfg.native_config_unrelated):
                        raise Refused("already_configured_or_unrelated_drift")
                    if any(not isinstance(x, str) or not x for x in values):
                        raise Refused("invalid_paths")
                    candidate = Pair(*(Path(x) for x in values))
                    self._fresh(candidate, policy)
                else:
                    if (not self.operation or req["rollback_of"] != self.operation
                            or self.previous != Pair(None, None) or values != [None, None]):
                        raise Refused("invalid_rollback_anchor")
                    # Rollback never reads, resets or detaches an enrolled/unknown store.
                    self._fresh(self.paths.value, policy)
                    candidate = self.previous
                # Revalidate current receiver, revocations and disk after all preparation.
                self.runner._native_config_receiver(policy)
                _, _, final_revision, _, _ = self._authority(path, headers, body)
                if final_revision != revision or private(self.cfg.config_path, 1 << 20) != raw:
                    raise Refused("concurrent_change")
                history["seen"].append(req["request_id"])
                self._save(record, history)  # crash before publish => consumed request, never a false receipt
                self.previous = self.paths.value
                self.paths.value = candidate  # single immutable publication, same lock as every native consumer
                self.generation += 1
                self.operation, self.candidate = req["request_id"], sha(raw)
                return 200, {**self.readback(), "authority_revision": revision,
                             "operator": policy["operator"], "request_id": req["request_id"]}
        except (Refused, OSError, ValueError, KeyError, TypeError, UnicodeError):
            # No caller-controlled exception text/secret paths are returned or logged.
            return 409, {"error": "native_config_refused"}


def command(argv):
    """Send an operator-authored request; never edits config or manufactures bindings."""
    import argparse
    import urllib.error
    import urllib.request
    from .config import load
    p = argparse.ArgumentParser(prog="mesh-runner native-result-config")
    p.add_argument("--config", required=True)
    p.add_argument("--action", required=True, choices=("status", "adopt", "rollback"))
    p.add_argument("--request-file", required=True)
    args = p.parse_args(argv)
    try:
        cfg = load(args.config)
        if not cfg.native_result_adoption or not cfg.extension_enabled or cfg.extension_host not in ("127.0.0.1", "::1"):
            raise Refused("disabled_or_nonloopback")
        pin = cfg.native_result_adoption
        raw = private(pin["policy"])
        if sha(raw) != pin["policy_sha256"]:
            raise Refused("untrusted_policy")
        policy = json.loads(raw)
        key = private(policy["key_file"]).strip()
        if sha(key) != policy["key_sha256"]:
            raise Refused("untrusted_key")
        body = private(args.request_file, MAX_BODY)
        path = PREFIX + args.action
        timestamp = str(int(time.time()))
        headers = {"Content-Type": "application/json", "X-Native-Timestamp": timestamp,
                   "X-Native-Signature": sign(key.decode("ascii"), timestamp, path.encode() + b"\n" + body)}
        host = "[::1]" if cfg.extension_host == "::1" else cfg.extension_host
        req = urllib.request.Request(f"http://{host}:{cfg.extension_port}" + path, body, headers, method="POST")
        # Loopback control must never follow redirects or proxy a signed request elsewhere.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **kw):
                return None
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(req, timeout=15) as response:
            result = json.loads(response.read(MAX_BODY + 1))
        print(json.dumps(result, sort_keys=True))
        return 0
    except (OSError, ValueError, KeyError, TypeError):
        print('{"error":"native_config_refused"}')
        return 1
