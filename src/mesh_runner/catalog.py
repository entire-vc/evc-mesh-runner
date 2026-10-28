"""Runtime catalog controller: the runner applies its slice of the Mesh catalog and reports on it.

One pass per configured workspace (``[catalog]`` in runner.toml, off by default):

  GET  /workspaces/:ws/runtime/controllers/:ref/desired   (If-None-Match; 304 = unchanged)
  -> check the slice locally against the host manifest (what this host can really execute)
  -> applied.json = that revision + digest
  -> POST /workspaces/:ws/runtime/controllers/:ref/report (schema v2: capabilities, pools, profiles)

``ref`` is the runner's own name (``mesh-runner``), the same name its agent leases carry, so the
catalog, the leases and the report describe one controller. A slice that fails the local check is
reported ``rejected`` for exactly its revision + digest and ``applied.json`` is left as it was; the
state file then says ``status: error``. A report is evidence only: the server never authorizes a
launch from it, and neither does this module.

Pool states come from files other host jobs write, never from a live probe here:
``quota-observations.json`` (schema quota-observations/v1, provider numbers per window) and
``harness-limits.json`` (harness-limits/v1, a usage limit a lane's own session printed). No data is
``unknown``, never 0 %; a harness limit only ever makes a pool ``exhausted``.

The reporter key is read from a file or an environment variable and is never logged.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from . import RUNNER_NAME, __version__

log = logging.getLogger("mesh_runner")

TIMEOUT = 10.0
MAX_BODY = 4 << 20
REPORT_BACKOFF = (0.5, 2.0)        # sleeps between the (at most 3) report attempts; 4xx is never retried
OBSERVATION_SKEW = 5               # the server refuses observed_at in its future
FUTURE_SKEW = 30
EVIDENCE_MAX_AGE = 1800
POOL_STATES = ("available", "exhausted", "threshold", "unknown", "auth_error", "network_error")
BLOCKED = ("threshold", "transfer", "exhausted")
R1_STATE = {"transfer": "threshold"}   # local states the server does not know -> nearest allowed
FALLBACK_THRESHOLDS = {"new_launch_block": 0.90, "transfer": 0.95, "readmit_below": 0.80, "min_pause_sec": 600}
THRESHOLD_REF = "thr-default-v1"
MODES = ("subscription", "api")
QUOTA_SCHEMA = "quota-observations/v1"
LIMITS_SCHEMA = "harness-limits/v1"
QUOTA_FIELDS = ("provider", "account_ref", "pool_id", "catalog_pool", "unit", "window", "status",
                "used_fraction", "remaining", "reset_at", "observed_at", "source", "confidence",
                "max_age", "error_kind")

_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}")
_CRED = re.compile(r"cred:[a-zA-Z0-9][a-zA-Z0-9._/-]{0,194}")
_ETAG = re.compile(r'(W/)?"[0-9A-Za-z_-]{8,100}"')


class CatalogError(ValueError):
    """A local input (manifest, config) is unusable. The message never carries a secret."""


def safe_ref(value) -> bool:
    return isinstance(value, str) and _REF.fullmatch(value) is not None


def hex_digest(value) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def is_uuid(value) -> bool:
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(float(epoch), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value):
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except Exception:
        return None


# --------------------------------------------------------------------------- files

def read_json(path):
    try:
        v = json.loads(Path(path).read_text())
        return v if isinstance(v, dict) else None
    except Exception:
        return None


def write_json(path, value) -> None:
    """Atomic write, mode 600."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(value, fh, indent=1, sort_keys=True)
            fh.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_applied(catalog_dir):
    v = read_json(Path(catalog_dir) / "applied.json") or {}
    rev, digest = v.get("revision"), v.get("digest")
    if type(rev) is not int or rev < 1 or not hex_digest(digest):
        return None
    return {"revision": rev, "digest": digest}


def write_applied(catalog_dir, revision: int, digest: str, now: float) -> dict:
    if type(revision) is not int or revision < 1 or not hex_digest(digest):
        raise CatalogError("applied: revision must be >= 1 and digest 64 lowercase hex")
    record = {"revision": revision, "digest": digest, "recorded_at": iso(now)}
    write_json(Path(catalog_dir) / "applied.json", record)
    return record


# ------------------------------------------------------------------------ manifest

def _strict(obj, allowed, where):
    if not isinstance(obj, dict):
        raise CatalogError(f"{where}: object expected")
    missing = [k for k in allowed if k not in obj]
    if missing:
        raise CatalogError(f"{where}: missing {', '.join(missing)}")
    return obj


def _ref(value, where):
    if not safe_ref(value):
        raise CatalogError(f"{where}: invalid ref")


def load_manifest(path) -> dict:
    """The host manifest: the controllers, accounts, modes and pools this host can execute.

    Only the fields the controller reads are checked; other keys are left to their own readers.
    Credentials are opaque ``cred:`` references, never values."""
    m = read_json(path)
    if m is None:
        raise CatalogError("manifest: unreadable")
    _strict(m, ("schema_version", "controllers", "profile_controller", "defaults", "accounts"), "manifest")
    if m["schema_version"] != 1:
        raise CatalogError("manifest: schema_version 1 expected")
    controllers = m["controllers"]
    if not isinstance(controllers, dict) or not controllers:
        raise CatalogError("manifest: controllers required")
    for cref, c in controllers.items():
        _ref(cref, "manifest controller")
        _strict(c, ("capabilities",), f"controller {cref}")
        if not isinstance(c["capabilities"], list) or not all(safe_ref(x) for x in c["capabilities"]):
            raise CatalogError(f"controller {cref}: capabilities must be a list of refs")
    if m["profile_controller"] not in controllers:
        raise CatalogError("manifest: profile_controller is not a listed controller")
    age = (m["defaults"] or {}).get("evidence_max_age_seconds", EVIDENCE_MAX_AGE) \
        if isinstance(m["defaults"], dict) else None
    if not (isinstance(age, int) and not isinstance(age, bool) and 1 <= age <= 86400):
        raise CatalogError("manifest: defaults.evidence_max_age_seconds must be 1..86400")
    accounts = m["accounts"]
    if not isinstance(accounts, dict) or not accounts:
        raise CatalogError("manifest: accounts required")
    for aref, a in accounts.items():
        where = f"account {aref}"
        _ref(aref, where)
        _strict(a, ("credential_ref", "pools", "modes", "harness", "model", "model_developer", "model_family",
                    "capabilities"), where)
        if not isinstance(a["capabilities"], list) or not all(safe_ref(x) for x in a["capabilities"]):
            raise CatalogError(f"{where}: capabilities must be a list of refs")
        for key in ("harness", "model", "model_developer", "model_family"):
            _ref(a[key], f"{where}.{key}")
        if not isinstance(a["credential_ref"], str) or not _CRED.fullmatch(a["credential_ref"]):
            raise CatalogError(f"{where}: credential_ref must be an opaque 'cred:' ref")
        if not isinstance(a["pools"], dict) or not a["pools"] or not all(safe_ref(p) for p in a["pools"]):
            raise CatalogError(f"{where}: pools required")
        modes = a["modes"]
        if not isinstance(modes, dict) or not modes or set(modes) - set(MODES):
            raise CatalogError(f"{where}: modes must be a non-empty subset of {list(MODES)}")
        for mode, md in modes.items():
            pools = md.get("pools") if isinstance(md, dict) else None
            if (not isinstance(pools, list) or not pools or len(set(pools)) != len(pools)
                    or any(p not in a["pools"] for p in pools)):
                raise CatalogError(f"{where} mode {mode}: pools must be unique known pool refs")
    return m


def reference_errors(catalog) -> list[str]:
    """Mirror of evc-mesh internal/domain/runtime.go ValidateReferences."""
    errors: list[str] = []
    pools = catalog["pools"]
    physical, aliases = set(), set()
    for ref, p in pools.items():
        key = (p["provider"], p["resource_ref"])
        if key in physical:
            errors.append(f"pools.{ref}: duplicate physical pool")
        physical.add(key)
        for a in p["aliases"]:
            if a == ref or a in pools or a in aliases:
                errors.append(f"pools.{ref}: alias clash")
            aliases.add(a)

    def resolve(ref):
        if ref in pools:
            return ref
        for name, p in pools.items():
            if ref in p["aliases"]:
                return name
        return None

    for aref, a in catalog["accounts"].items():
        for refs in a["quota_pools_by_mode"].values():
            for ref in refs:
                name = resolve(ref)
                if name is None or pools[name]["provider"] != a["provider"]:
                    errors.append(f"accounts.{aref}: pool ref unknown or wrong provider")
    for pref, p in catalog["profiles"].items():
        c = catalog["controllers"].get(p["controller_ref"])
        a = catalog["accounts"].get(p["account_ref"])
        if c is None or a is None or not a["quota_pools_by_mode"].get(p["execution_mode"]):
            errors.append(f"profiles.{pref}: controller/account/mode pools missing")
            continue
        if any(cap not in c["capabilities"] for cap in p["capabilities"]):
            errors.append(f"profiles.{pref}: capability not offered by controller")
    seen = set()
    for bref, b in catalog["bindings"].items():
        ident = tuple(sorted(b["binding"].items()))
        if ident in seen:
            errors.append(f"bindings.{bref}: duplicate identity")
        seen.add(ident)
        permitted = b["permitted_profiles"]
        if any(r not in catalog["profiles"] for r in permitted):
            errors.append(f"bindings.{bref}: unknown permitted profile")
        pol = b["policy"]
        if any(r not in permitted for r in pol["primary_profiles"]):
            errors.append(f"bindings.{bref}: primary not permitted")
        for src, targets in pol["quota_edges"].items():
            if src not in permitted or any(t not in permitted for t in targets):
                errors.append(f"bindings.{bref}: edge outside permitted profiles")
        for provider, acct in pol["preferred_accounts"].items():
            a = catalog["accounts"].get(acct)
            if a is None or a["provider"] != provider or not any(
                    catalog["profiles"].get(p, {}).get("account_ref") == acct for p in permitted):
                errors.append(f"bindings.{bref}: preferred account invalid")
    return errors


def validate_desired(body, controller_ref: str, manifest: dict):
    """Local check of a desired slice before it may be called applied.

    Returns (revision, digest, errors); errors are short class labels only. The slice is not a whole
    catalog, so only the reference check applies, plus the check that this host can execute what is
    asked: every profile belongs to this controller and names an account, mode and harness the manifest
    knows, with capabilities the host offers; every account carries the manifest's own credential_ref
    and asks only for pools the manifest gives that mode."""
    errors = []
    if not isinstance(body, dict):
        return None, None, ["body"]
    rev, digest = body.get("revision"), body.get("digest")
    if type(rev) is not int or rev < 1:
        errors.append("revision")
    if not hex_digest(digest):
        errors.append("digest")
    if body.get("controller_ref") != controller_ref:
        errors.append("controller_ref")
    for flag in ("enabled", "drain_requested"):
        if type(body.get(flag)) is not bool:
            errors.append(flag)
    controller, cat = body.get("controller"), body.get("catalog")
    if not isinstance(controller, dict) or not isinstance(cat, dict):
        return rev, digest, sorted(set(errors + ["shape"]))
    parts = ("accounts", "pools", "profiles", "bindings")
    if any(not isinstance(cat.get(k), dict) for k in parts):
        return rev, digest, sorted(set(errors + ["shape"]))
    try:
        if reference_errors({"controllers": {controller_ref: controller}, **{k: cat[k] for k in parts}}):
            errors.append("reference")
        known = manifest["accounts"]
        offered = set(manifest["controllers"].get(controller_ref, {}).get("capabilities", []))
        for prof in cat["profiles"].values():
            acct = known.get(prof.get("account_ref"))
            if prof.get("controller_ref") != controller_ref:
                errors.append("foreign_profile")
            elif (acct is None or prof.get("execution_mode") not in acct["modes"]
                    or acct["harness"] != prof.get("harness")):
                errors.append("unexecutable_profile")
            elif not set(prof["capabilities"]) <= offered & set(acct["capabilities"]):
                errors.append("capability")
        for aref, acct in cat["accounts"].items():
            local = known.get(aref)
            if local is None or acct.get("credential_ref") != local["credential_ref"]:
                errors.append("account")
                continue
            for mode, refs in acct["quota_pools_by_mode"].items():
                if mode not in local["modes"] or not set(refs) <= set(local["modes"][mode]["pools"]):
                    errors.append("pools")
    except (KeyError, TypeError, AttributeError, ValueError):
        errors.append("malformed")
    return rev, digest, sorted(set(errors))


# ---------------------------------------------------------------- pool observations

def load_thresholds(path) -> dict:
    """{'defaults', 'pools', 'threshold_ref'}; a missing or unreadable file falls back to defaults."""
    out = {"defaults": dict(FALLBACK_THRESHOLDS), "pools": {}, "threshold_ref": THRESHOLD_REF}
    raw = read_json(path) if path else None
    if raw is None:
        return out
    for key, val in (raw.get("defaults") or {}).items():
        if key in FALLBACK_THRESHOLDS and _num(val):
            out["defaults"][key] = val
    for ref, over in (raw.get("pools") or {}).items():
        if isinstance(over, dict):
            out["pools"][ref] = {k: v for k, v in over.items() if k in FALLBACK_THRESHOLDS and _num(v)}
    if safe_ref(raw.get("threshold_ref")):
        out["threshold_ref"] = raw["threshold_ref"]
    return out


def thresholds_for(cfg: dict, ref: str) -> dict:
    t = dict(cfg["defaults"])
    t.update(cfg["pools"].get(ref, {}))
    return t


def map_state(reading, t, now, prior=None, max_age=EVIDENCE_MAX_AGE):
    """reading: None or {'fraction', 'exhausted', 'observed', 'reset_at', 'source'}.
    Returns (state, blocked_since). Hysteresis: after a blocked state the pool stays "threshold"
    until the fraction is below readmit_below AND min_pause_sec passed since the block began."""
    if not reading:
        return "unknown", None
    observed = reading.get("observed")
    if not _num(observed) or now - observed > max_age or observed > now + FUTURE_SKEW:
        return "unknown", None
    frac = reading.get("fraction")
    if frac is not None and (not _num(frac) or frac < 0):
        frac = None
    if reading.get("exhausted") is True or (frac is not None and frac >= 1.0):
        raw = "exhausted"
    elif frac is None:
        return "unknown", None
    elif frac >= t["transfer"]:
        raw = "transfer"
    elif frac >= t["new_launch_block"]:
        raw = "threshold"
    else:
        raw = "available"
    prior_state = (prior or {}).get("state")
    since = (prior or {}).get("blocked_since")
    if raw in BLOCKED:
        if not (prior_state in BLOCKED and _num(since)):
            since = now
        return raw, since
    if prior_state in BLOCKED and _num(since):
        if now - since < t["min_pause_sec"] or (frac is not None and frac >= t["readmit_below"]):
            return "threshold", since
    return "available", None


def effective(record: dict, now: float) -> dict:
    """A quota record as it may be used at ``now``: stale, undated or future-dated is unknown."""
    rec = {k: record.get(k) for k in QUOTA_FIELDS}
    observed = parse_iso(rec.get("observed_at"))
    max_age = rec.get("max_age") if _num(rec.get("max_age")) else EVIDENCE_MAX_AGE
    if observed is None or observed > now + FUTURE_SKEW or now - observed > max_age:
        rec.update(status="unknown", used_fraction=None, remaining=None, confidence="none", error_kind="stale")
    return rec


def _reading(record: dict, now: float):
    rec = effective(record, now)
    if rec["status"] not in ("available", "exhausted"):
        return None
    return {"fraction": rec["used_fraction"], "exhausted": rec["status"] == "exhausted",
            "observed": parse_iso(rec["observed_at"]),
            "reset_at": parse_iso(rec["reset_at"]) if rec["reset_at"] else None,
            "source": rec["source"], "window": rec.get("window")}


def pool_reading(records, ref: str, now: float):
    """One reading for catalog pool ``ref``: the worst fresh window wins. ``partial`` marks a pool
    with a window that has no usable data (that window may be the more used one)."""
    mine = [r for r in records if isinstance(r, dict)
            and (r.get("catalog_pool") == ref or (r.get("catalog_pool") is None and r.get("pool_id") == ref))]
    known, partial = [], False
    for rec in mine:
        rd = _reading(rec, now)
        if rd is None or (rd["fraction"] is None and not rd["exhausted"]) or not _num(rd["observed"]):
            partial = True
            continue
        known.append(rd)
    if not known:
        return None
    worst = max(known, key=lambda x: (x["exhausted"], x["fraction"] if x["fraction"] is not None else -1))
    worst["partial"] = partial
    return worst


def _schema_records(path, schema: str) -> list:
    doc = read_json(path) if path else None
    if not doc or doc.get("schema") != schema:
        return []
    return [r for r in doc.get("records") or [] if isinstance(r, dict)]


def active_limits(path, now: float) -> dict:
    """{pool_ref: record} of the harness usage limits in effect at ``now``; the longer one wins."""
    out = {}
    for rec in _schema_records(path, LIMITS_SCHEMA):
        exp, obs = parse_iso(rec.get("expires_at")), parse_iso(rec.get("observed_at"))
        if (exp is None or obs is None or obs > now + FUTURE_SKEW or now >= exp
                or not isinstance(rec.get("pools"), list)):
            continue
        for pool in rec["pools"]:
            cur = out.get(pool)
            if isinstance(pool, str) and (cur is None or exp > parse_iso(cur["expires_at"])):
                out[pool] = rec
    return out


class PoolObserver:
    """Pool observations from the host's quota and harness-limit files. Never raises: any failure of
    one pool makes that pool unknown. Without an observations file every pool is unknown."""

    def __init__(self, observations=None, harness_limits=None, thresholds=None, max_age=EVIDENCE_MAX_AGE):
        self.observations = observations
        self.harness_limits = harness_limits
        self.cfg = thresholds if thresholds is not None else load_thresholds(None)
        self.max_age = max_age

    def observe(self, pool_refs, now: float, prior: dict | None = None) -> dict:
        prior = prior or {}
        floor = now - OBSERVATION_SKEW
        records = _schema_records(self.observations, QUOTA_SCHEMA)
        limits = active_limits(self.harness_limits, now) if self.harness_limits else {}
        out = {}
        for ref in sorted(pool_refs):
            old = prior.get(ref) if isinstance(prior.get(ref), dict) else None
            try:
                reading = pool_reading(records, ref, now)
                state, since = map_state(reading, thresholds_for(self.cfg, ref), now, old, self.max_age)
                if reading and reading.get("partial") and state == "available":
                    state, since = "unknown", None
                limit = limits.get(ref)
                if limit:
                    # the provider's verdict on the account, re-confirmed by the clock each pass
                    kept = old and old.get("state") in BLOCKED and _num(old.get("blocked_since"))
                    state, since = "exhausted", (old["blocked_since"] if kept else now)
                    reading = {"observed": now, "fraction": None, "exhausted": True, "source": "harness_limit",
                               "reset_at": parse_iso(limit["expires_at"]), "window": None}
            except Exception:
                reading, state, since = None, "unknown", None
            known = state != "unknown"
            at = min(reading["observed"], floor) if known else floor
            entry = {"state": state, "verified": known, "observed_at": iso(at),
                     "source": reading["source"] if reading else "none"}
            if reading and reading.get("fraction") is not None:
                entry["used_fraction"] = round(float(reading["fraction"]), 4)
            if reading and reading.get("reset_at"):
                entry["reset_at"] = iso(reading["reset_at"])
            if reading and reading.get("window"):
                entry["window"] = reading["window"]
            if since is not None:
                entry["blocked_since"] = since
            out[ref] = entry
        return out

    def for_report(self, observations: dict) -> dict:
        """What the server accepts: state/verified/observed_at, a threshold state with its threshold_ref."""
        out = {}
        for ref, o in observations.items():
            st = R1_STATE.get(o["state"], o["state"])
            e = {"state": st, "verified": o["verified"], "observed_at": o["observed_at"]}
            if st == "threshold":
                e["threshold_ref"] = self.cfg["threshold_ref"]
                e["evidence_kind"] = "configured_threshold"
            out[ref] = e
        return out


# -------------------------------------------------------------------------- client

@dataclass(frozen=True)
class DesiredResult:
    outcome: str            # ok|not_modified|forbidden|not_found|server_error|transport|bad_response
    http_status: int | None
    etag: str | None = None
    body: dict | None = field(default=None, repr=False)


@dataclass(frozen=True)
class ReportResult:
    outcome: str            # recorded|forbidden|stale_revision|invalid|not_found|server_error|transport|bad_response
    http_status: int | None
    recorded: bool = False
    attempts: int = 1


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _classify(code):
    return ("forbidden" if code in (401, 403) else "stale_revision" if code == 409 else
            "not_found" if code == 404 else "invalid" if code in (400, 422) else
            "server_error" if code >= 500 else "bad_response")


class ControllerClient:
    """Controller side of the runtime catalog API, authenticated with the reporter agent key."""

    def __init__(self, base_url: str, workspace_id: str, agent_key: str, *, basic_auth: str = "",
                 timeout: float = TIMEOUT, opener=None, sleep=None):
        if not is_uuid(workspace_id):
            raise CatalogError("invalid workspace id")
        if not agent_key or any(ord(c) < 33 or ord(c) > 126 for c in agent_key):
            raise CatalogError("invalid reporter key")
        self.base = base_url.rstrip("/") + "/api/v1/workspaces/" + workspace_id + "/runtime/controllers/"
        self._key = agent_key
        self._basic = basic_auth
        self.timeout = timeout
        self._open = (opener or urllib.request.build_opener(_NoRedirect())).open
        self._sleep = sleep or threading.Event().wait

    def __repr__(self) -> str:      # never show the key
        return f"ControllerClient({self.base!r})"

    def _exchange(self, method: str, url: str, body=None, etag=None):
        """(status, etag, parsed dict or None); status None on a transport failure."""
        headers = {"Accept": "application/json", "X-Agent-Key": self._key}
        if self._basic:
            import base64
            headers["Authorization"] = "Basic " + base64.b64encode(self._basic.encode()).decode()
        data = None
        if body is not None:
            data = json.dumps(body, separators=(",", ":")).encode()
            headers["Content-Type"] = "application/json"
        if etag:
            headers["If-None-Match"] = etag
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with self._open(req, timeout=self.timeout) as resp:
                status, tag, raw = resp.status, resp.headers.get("ETag"), resp.read(MAX_BODY + 1)
        except urllib.error.HTTPError as e:
            return e.code, None, None
        except (OSError, ValueError):
            return None, None, None
        if len(raw) > MAX_BODY:
            return status, tag, None
        try:
            value = json.loads(raw) if raw else None
        except ValueError:
            value = None
        return status, tag, value if isinstance(value, dict) else None

    def desired(self, controller_ref: str, etag: str | None = None) -> DesiredResult:
        if not safe_ref(controller_ref):
            raise CatalogError("invalid controller ref")
        if etag is not None and not _ETAG.fullmatch(etag):
            etag = None
        code, tag, value = self._exchange("GET", self.base + quote(controller_ref, safe="") + "/desired", etag=etag)
        if code is None:
            return DesiredResult("transport", None)
        if code == 304:
            return DesiredResult("not_modified", 304, etag)
        if code == 200:
            if value is None or not isinstance(tag, str) or not tag:
                return DesiredResult("bad_response", 200)
            return DesiredResult("ok", 200, tag, value)
        return DesiredResult(_classify(code), code)

    def report(self, controller_ref: str, revision: int, digest: str, status: str, capabilities,
               emergency_paused: bool, pools: dict, profiles: dict | None = None) -> ReportResult:
        if not safe_ref(controller_ref):
            raise CatalogError("invalid controller ref")
        if type(revision) is not int or revision < 1 or not hex_digest(digest):
            raise CatalogError("invalid revision or digest")
        if status not in ("applied", "rejected"):
            raise CatalogError("invalid status")
        for ref, obs in pools.items():
            if (not safe_ref(ref) or obs.get("state") not in POOL_STATES or type(obs.get("verified")) is not bool
                    or not isinstance(obs.get("observed_at"), str)
                    or set(obs) - {"state", "verified", "observed_at", "threshold_ref", "evidence_kind"}):
                raise CatalogError("invalid pool observation")
        # Exactly the server's report fields (it rejects unknown ones): the controller is named by the
        # URL path, and the version the server expects is matched against desired.controller.version.
        body = {"schema_version": 2, "revision": revision, "digest": digest, "status": status,
                "capabilities": sorted(capabilities), "emergency_paused": emergency_paused, "pools": pools}
        if profiles:
            body["profiles"] = profiles
        url = self.base + quote(controller_ref, safe="") + "/report"
        result, attempts = None, 0
        for delay in (0.0,) + REPORT_BACKOFF:
            if delay:
                self._sleep(delay)
            attempts += 1
            code, _tag, value = self._exchange("POST", url, body)
            if code is None:
                result = ReportResult("transport", None, attempts=attempts)
            elif code == 200:
                ok = value is not None and value.get("recorded") is True and value.get("launch_authorized") is False
                result = ReportResult("recorded" if ok else "bad_response", 200, ok, attempts)
            else:
                result = ReportResult(_classify(code), code, attempts=attempts)
            if result.outcome not in ("transport", "server_error"):
                break
        return result


# --------------------------------------------------------------------------- pass

@dataclass
class WorkspaceController:
    """The catalog controller of one workspace."""
    workspace_id: str
    client: ControllerClient
    manifest_path: Path
    home: Path                       # state.json, pools.json, desired-etag.json, emergency-pause
    catalog_dir: Path                # applied.json, desired.json
    observer: PoolObserver
    controller_ref: str = RUNNER_NAME
    version: str = __version__

    def _sync_desired(self, manifest: dict, now: float):
        """(info for state.json, reject) where reject is {'revision','digest'} of a slice that failed
        the local check, or None."""
        tag = read_json(self.home / "desired-etag.json") or {}
        res = self.client.desired(self.controller_ref, tag.get("etag") if isinstance(tag.get("etag"), str) else None)
        info = {"desired_outcome": res.outcome, "desired_http": res.http_status}
        if res.outcome != "ok":
            return info, None
        rev, digest, errors = validate_desired(res.body, self.controller_ref, manifest)
        if errors:
            info.update(desired_outcome="invalid", desired_errors=errors)
            if type(rev) is int and rev >= 1 and hex_digest(digest):
                return info, {"revision": rev, "digest": digest}
            return info, None        # nothing exact to reject; the previous applied heartbeat goes on
        current = read_applied(self.catalog_dir)
        if current and rev < current["revision"]:
            info.update(desired_outcome="stale")       # never walk the applied revision backwards
            return info, None
        if not current or (current["revision"], current["digest"]) != (rev, digest):
            write_applied(self.catalog_dir, rev, digest, now)
            info["desired_applied"] = True
        write_json(self.home / "desired-etag.json", {"etag": res.etag, "revision": rev, "digest": digest})
        write_json(self.catalog_dir / "desired.json", res.body)
        info.update(desired_revision=rev, desired_enabled=res.body["enabled"],
                    desired_drain_requested=res.body["drain_requested"])
        wanted = (res.body.get("controller") or {}).get("version")
        if wanted is not None and wanted != self.version:
            info["version_mismatch"] = {"catalog": wanted, "host": self.version}
        return info, None

    def _scope(self):
        """(pool refs, profile refs) of the applied slice, or None: the server refuses a whole report
        that names a pool or profile its current catalog lacks."""
        applied = read_applied(self.catalog_dir)
        body = read_json(self.catalog_dir / "desired.json")
        if applied is None or body is None:
            return None
        if (body.get("revision"), body.get("digest")) != (applied["revision"], applied["digest"]):
            return None
        try:
            pools, profiles = body["catalog"]["pools"], body["catalog"]["profiles"]
            if isinstance(pools, dict) and isinstance(profiles, dict):
                return frozenset(pools), frozenset(profiles)
        except (KeyError, TypeError):
            pass
        return None

    def _gather(self, manifest: dict, now: float):
        controllers = manifest["controllers"]
        if self.controller_ref not in controllers:
            raise CatalogError("controller ref is not in the manifest")
        capabilities = sorted(controllers[self.controller_ref]["capabilities"])
        local, profiles = {}, {}
        if manifest["profile_controller"] == self.controller_ref:
            scope = self._scope()
            wanted = set()
            stamp = iso(now - OBSERVATION_SKEW)
            for aref, acct in manifest["accounts"].items():
                for mode, md in acct["modes"].items():
                    profile_ref = f"{aref}/{mode}"
                    if scope is not None and profile_ref not in scope[1]:
                        continue
                    wanted.update(p for p in md["pools"] if scope is None or p in scope[0])
                    profiles[profile_ref] = {"model": acct["model"], "model_developer": acct["model_developer"],
                                             "model_family": acct["model_family"], "observed_at": stamp}
            prior = (read_json(self.home / "pools.json") or {}).get("pools") or {}
            local = self.observer.observe(wanted, now, prior)
        return capabilities, local, profiles

    def tick(self, now: float) -> dict:
        """One pass. Returns the state written to ``home/state.json``; ``status`` is ok or error."""
        state = {"controller_ref": self.controller_ref, "version": self.version, "workspace_id": self.workspace_id,
                 "tick_at": iso(now)}

        def done(status, **kw):
            state.update(kw, status=status)
            write_json(self.home / "state.json", state)
            return state

        paused = (self.home / "emergency-pause").exists()
        try:
            manifest = load_manifest(self.manifest_path)
        except CatalogError as e:
            return done("error", outcome="local_error", recorded=False, error=str(e))
        try:
            info, reject = self._sync_desired(manifest, now)
        except (OSError, CatalogError):
            info, reject = {"desired_outcome": "local_error"}, None
        state.update(info)
        if reject is not None:
            try:
                res = self.client.report(self.controller_ref, reject["revision"], reject["digest"], "rejected",
                                         manifest["controllers"].get(self.controller_ref, {}).get("capabilities", []),
                                         paused, {}, None)
                rejected = res.recorded
            except CatalogError:
                rejected = False
            return done("error", outcome="desired_rejected", recorded=False, rejected_reported=rejected,
                        revision=reject["revision"], digest=reject["digest"])
        applied = read_applied(self.catalog_dir)
        if applied is None:
            return done("error" if info.get("desired_outcome") == "invalid" else "ok",
                        outcome="no_applied_revision", recorded=False)
        try:
            capabilities, local, profiles = self._gather(manifest, now)
            result = self.client.report(self.controller_ref, applied["revision"], applied["digest"], "applied",
                                        capabilities, paused, self.observer.for_report(local), profiles)
        except CatalogError as e:
            return done("error", outcome="local_error", recorded=False, error=str(e))
        write_json(self.home / "pools.json", {"observed_at": iso(now), "controller_ref": self.controller_ref,
                                              "pools": local})
        if result.recorded:
            state["last_report_at"] = iso(now)
        bad = result.recorded is False or info.get("desired_outcome") == "invalid"
        return done("error" if bad else "ok", outcome=result.outcome, http_status=result.http_status,
                    recorded=result.recorded, attempts=result.attempts, revision=applied["revision"],
                    digest=applied["digest"], pools_reported=len(local), profiles_reported=len(profiles))


def build_controllers(cfg) -> list[WorkspaceController]:
    """One controller per ``[[catalog.workspace]]`` of a loaded RunnerConfig."""
    c = cfg.catalog
    thresholds = load_thresholds(c.thresholds)
    out = []
    for ws in c.workspaces:
        manifest = ws.manifest or c.manifest
        try:
            age = load_manifest(manifest)["defaults"].get("evidence_max_age_seconds", EVIDENCE_MAX_AGE)
        except CatalogError:
            age = EVIDENCE_MAX_AGE           # the pass itself reports the manifest error
        out.append(WorkspaceController(
            workspace_id=ws.workspace_id,
            client=ControllerClient(cfg.mesh_url, ws.workspace_id, ws.reporter_key, basic_auth=cfg.mesh_basic_auth),
            manifest_path=manifest, home=ws.home, catalog_dir=ws.catalog_dir,
            observer=PoolObserver(c.observations, c.harness_limits, thresholds, age),
            controller_ref=c.controller_ref))
    return out


def catalog_loop(controllers, stop: threading.Event, every: float, clock=None) -> None:
    """Runs every controller once per ``every`` seconds until ``stop``; one failing workspace never
    stops the others."""
    import time
    clock = clock or time.time
    while not stop.is_set():
        for ctl in controllers:
            try:
                st = ctl.tick(clock())
                if st["status"] != "ok":
                    log.warning("catalog %s: %s (%s)", ctl.workspace_id[:8], st.get("outcome"),
                                st.get("desired_outcome"))
                if st.get("version_mismatch"):
                    log.warning("catalog %s: catalog wants version %s, host runs %s", ctl.workspace_id[:8],
                                st["version_mismatch"]["catalog"], st["version_mismatch"]["host"])
            except Exception as e:       # a bug in one pass must not end the loop
                log.warning("catalog %s: pass failed: %s", ctl.workspace_id[:8], type(e).__name__)
        stop.wait(every)
