"""Bounded editor-job capability: ``POST /v1/tgpeek/editor-jobs`` and ``DELETE /v1/tgpeek/editor-jobs/{id}``.

One fixed role (``tgpeek-editor``), one synchronous terminal answer inside a 45 s deadline that includes
the queue, no GET/polling, no transport retries. The caller never picks a model, agent, path or command:
those come from the operator configuration only.

What this module guarantees by itself, and what it only gates:

* strict ``tgpeek-editor-bridge-v1`` envelope (exact keys, duplicate-key rejection, body <= 8 MiB), immutable hash
  bindings, wire-compatible with the reviewed product client (venturecrew/tgpeek docs/codex-editor-bridge.md);
* caller auth is a GitLab ``CI_JOB_TOKEN`` checked server-side through ``GET /job`` against a fixed
  project/pipeline/job/SHA allowlist; the caller's claims are never trusted;
* a durable ledger (SQLite, safe hashes and state only) keyed by an idempotency key, with an attempt
  budget of initial + 2 corrections per publication that a CI retry cannot reset; an attempt is spent when
  the process is launched, so a crash or an unknown outcome has already consumed it;
* cancel and deadline kill the whole process group, delete the private input/output, and a late result is
  discarded;
* the success path is fail-closed on provenance: the executor must return an observed receipt (authenticated
  principal, family, model, *resolved* version from an accepted provenance source, ``tools_used == []``, an
  isolation attestation bound to the launch digest). No accepted source is configured by default, so no
  response can be ``completed`` until the owner of the contract names one. A configured model, the CLI
  version or the model's own words are never an observed version.

Evidence and results are never logged, written to the ledger, or kept past delivery. The terminal envelope
is signed by an injected signer (trusted supervisor); the signature does not prove provenance.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import collections
import json
import logging
import os
import re
import shutil
import signal
import sqlite3
import stat
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from .host_admission import AdmissionCancelled, AdmissionClosed, AdmissionTimeout, HostAdmission

log = logging.getLogger("mesh_runner")

POST_PATH = "/v1/tgpeek/editor-jobs"
DELETE_PREFIX = POST_PATH + "/"
PRINCIPAL = "tgpeek-editor"
FAMILY = "openai"
MAX_BODY = 8 << 20
MAX_TEXT = 128000
MAX_ENVELOPE = 256000
TOTAL_DEADLINE = 45.0
DRAIN_TIMEOUT = 10.0                  # upper bound of EditorBridge.close(); the kill itself is prompt, the rest is slack
MAX_ATTEMPTS = 3                       # initial + 2 corrections; the wire attempt is 1..3, the ledger index 0..2
PROTOCOL = "tgpeek-editor-bridge-v1"
ISOLATION = "tools-disabled-v1"
EXECUTOR_ID = re.compile(r"[A-Za-z0-9_-]{1,96}")
HEX64 = re.compile(r"[0-9a-f]{64}")
OPAQUE = re.compile(r"[0-9a-f]{64}")
RESPONSE_KEYS = ("bindings", "job_id", "identity", "status", "text", "result_hash", "executor_receipt", "signature")
TERMINAL = ("completed", "rejected", "failed", "cancelled", "expired")
REQUEST_KEYS = ("protocol_version", "contract_digest", "project_id", "pipeline_id", "ci_job_id", "commit_sha",
                "publication_id", "publication_version", "attempt", "input_hash", "system_hash", "schema_hash",
                "deadline_at", "payload_hash", "payload")
UNSPENT = frozenset({"busy", "queue_deadline", "attempt_out_of_order", "shutting_down"})   # refused before any attempt was spent
RESULT_KEYS = frozenset({"claims", "limitations"})
SAFE_ATTESTATION = {"tools": [], "mcp": False, "shell": False, "web": False, "hostfiles": False,
                    "other_profiles": False, "env_secrets": False}


class BadRequest(Exception):
    """A request refused before any work; ``status`` is the HTTP code, ``code`` a fixed token."""

    def __init__(self, status: int, code: str):
        super().__init__(code)
        self.status, self.code = status, code


# ---------------------------------------------------------------------------------------------- JSON and hashes
def _no_dupes(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise ValueError("duplicate key")
        out[k] = v
    return out


def _no_const(name):
    raise ValueError("non-finite number")


def strict_loads(raw: bytes | str):
    """JSON with duplicate keys and NaN/Infinity refused. Raises ValueError."""
    text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    return json.loads(text, object_pairs_hook=_no_dupes, parse_constant=_no_const)


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest(obj) -> str:
    """The wire ``digest(x)``: SHA256 of the canonical JSON encoding, lowercase hex."""
    return sha256(canonical(obj))


def request_hash_of(env: dict) -> str:
    """The request without its payload, deadline included: a reused key with another deadline is a conflict."""
    return digest({k: v for k, v in env.items() if k != "payload"})


def job_id_of(env: dict) -> str:
    """Idempotency key == job id: the request without ``payload`` and ``deadline_at``. A new deadline never makes a new job."""
    return digest({k: v for k, v in env.items() if k not in ("payload", "deadline_at")})


def binding_hash(project_id, pipeline_id, ci_job_id, commit_sha) -> str:
    """The ledger keeps only this digest of the CI job binding, never the raw identifiers."""
    return sha256(canonical([str(project_id), str(pipeline_id), str(ci_job_id), commit_sha]))


def parse_deadline(value) -> float:
    """``deadline_at`` is integer Unix milliseconds; returns seconds."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("deadline_at")
    return value / 1000.0


# ---------------------------------------------------------------------------------------------------- config
@dataclass
class EditorConfig:
    state_dir: Path
    protocol_version: str
    contract_digest: str
    system_hashes: frozenset
    schema_hashes: frozenset
    gitlab_url: str
    project_id: int
    job_names: frozenset
    allowed_shas: frozenset                            # exact commits the genuine job may run on (operator-pinned)
    default_branch: str = "main"
    pipeline_source: str = "web"
    argv: tuple = ()                                   # fixed command of the isolated harness; never from a caller
    launch_digest: str = ""                            # digest of argv + sandbox profile, set by the operator
    sandbox: bool = False                              # wrap argv in the deny-by-default OS sandbox (editor_sandbox)
    sandbox_read_roots: tuple = ()
    sandbox_network: bool = False
    sandbox_exec_extra: tuple = ()                     # further exact executables of the composition (e.g. node, native codex)
    sandbox_pinned_files: tuple = ()                   # ((absolute path, sha256), ...): extra executables, harness script, bundle
    provenance_sources: frozenset = frozenset()        # accepted observed-version sources; empty = fail-closed
    max_queue: int = 1
    skew: float = 2.0
    project_path: str = ""                             # exact path_with_namespace of the private project the control plane must report
    control_token_file: str = ""                       # server-held read-only GitLab identity (Reporter + read_api); path only
    signing_key_file: str = ""                         # supervisor-held Ed25519 seed file (mode 600); the path is configuration only
    signing_public_key: str = ""                       # pinned Ed25519 public key (standard base64, or legacy 64 hex); the key file must derive exactly this
    executor_id: str = "tgpeek-editor"                 # receipt ``executor_id`` (1..96 of A-Za-z0-9_-)


# ---------------------------------------------------------------------------------------------------- ledger
class Ledger:
    """Durable idempotency + attempt budget. Only hashes, ids and state: never evidence or results."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        os.close(fd)
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.lock = threading.RLock()
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS jobs(idem TEXT PRIMARY KEY, opaque TEXT UNIQUE NOT NULL, request_hash TEXT NOT NULL,
                budget TEXT NOT NULL, attempt INTEGER NOT NULL, binding TEXT NOT NULL,
                state TEXT NOT NULL, result_hash TEXT, created REAL, finished REAL);
            CREATE TABLE IF NOT EXISTS budget(key TEXT PRIMARY KEY, used INTEGER NOT NULL);""")

    def get(self, idem: str):
        with self.lock:
            return self.db.execute("SELECT idem,opaque,request_hash,budget,attempt,binding,state,result_hash "
                                   "FROM jobs WHERE idem=?", (idem,)).fetchone()

    def by_opaque(self, opaque: str):
        with self.lock:
            return self.db.execute("SELECT idem,opaque,request_hash,budget,attempt,binding,state,result_hash "
                                   "FROM jobs WHERE opaque=?", (opaque,)).fetchone()

    def count(self) -> int:
        with self.lock:
            return self.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]

    def used(self, budget: str) -> int:
        with self.lock:
            row = self.db.execute("SELECT used FROM budget WHERE key=?", (budget,)).fetchone()
        return row[0] if row else 0

    def create(self, idem, opaque, request_hash, budget, attempt, env, now) -> None:
        with self.lock:
            self.db.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (idem, opaque, request_hash, budget, attempt, binding_hash(env["project_id"], env["pipeline_id"],
                             env["ci_job_id"], env["commit_sha"]), "queued", None, now, None))

    def start(self, idem: str, budget: str) -> bool:
        """Spend the attempt and mark the job running in one durable step, before the process is launched.

        False (nothing spent) when the job is no longer queued, e.g. a DELETE cancelled it in the meantime.
        """
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                used = self.db.execute("SELECT used FROM budget WHERE key=?", (budget,)).fetchone()
                used = used[0] if used else 0
                row = self.db.execute("SELECT attempt FROM jobs WHERE idem=? AND state='queued'", (idem,)).fetchone()
                moved = 0
                if row is not None and row[0] == used and used < MAX_ATTEMPTS:    # re-checked where the attempt is spent
                    moved = self.db.execute("UPDATE jobs SET state='running' WHERE idem=? AND state='queued'", (idem,)).rowcount
                if moved:
                    self.db.execute("INSERT INTO budget VALUES(?,1) ON CONFLICT(key) DO UPDATE SET used=used+1", (budget,))
                self.db.execute("COMMIT")
                return bool(moved)
            except Exception:
                self.db.execute("ROLLBACK")
                raise

    def finish(self, idem: str, state: str, result_hash: str | None, now: float) -> None:
        with self.lock:
            self.db.execute("UPDATE jobs SET state=?, result_hash=?, finished=? WHERE idem=? AND state NOT IN "
                            "('completed','rejected','failed','cancelled','expired')", (state, result_hash, now, idem))

    def drop(self, idem: str, cancelled: bool = False) -> None:
        """Forget a job that never started: its key may be submitted again and nothing was spent. ``cancelled`` is only
        for a job the supervisor itself stopped before launch (shutdown); a cancelled running job stays on record."""
        states = "'queued','rejected','expired','cancelled'" if cancelled else "'queued','rejected','expired'"
        with self.lock:
            self.db.execute(f"DELETE FROM jobs WHERE idem=? AND state IN ({states})", (idem,))

    def recover(self, now: float) -> int:
        """After a crash a 'running' job has an unknown outcome: it stays spent and ends as failed."""
        with self.lock:
            cur = self.db.execute("UPDATE jobs SET state='failed', finished=? WHERE state='running'", (now,))
            self.db.execute("UPDATE jobs SET state='expired', finished=? WHERE state='queued'", (now,))
            return cur.rowcount


# ------------------------------------------------------------------------------------------------- GitLab auth
def gitlab_fetch_job(base_url: str, timeout: float = 5.0) -> Callable[[str], tuple[int, dict | None]]:
    """``GET {base}/api/v4/job`` with the job token. Fixed URL, no redirects, no proxy; the token is a header only."""

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)

    def fetch(token: str):
        req = urllib.request.Request(base_url.rstrip("/") + "/api/v4/job", headers={"JOB-TOKEN": token})
        try:
            with opener.open(req, timeout=timeout) as resp:
                body = resp.read(1 << 20)
                return resp.status, strict_loads(body)
        except urllib.error.HTTPError as e:
            return e.code, None
        except (OSError, ValueError):
            return 0, None

    return fetch


class GitlabControlPlane:
    """Read-only trusted metadata from the fixed GitLab origin, with a server-held identity.

    The token is read from a mode-600 file on every call and travels only in a header; it is never logged, returned
    or passed to a child. Fixed origin, no redirects, no proxy, TLS verification on, response <= 1 MiB. Paths and the
    GraphQL document are built here from integers, never from a caller string.
    """

    def __init__(self, base_url: str, token_file: str, timeout: float = 5.0):
        self.base, self.token_file, self.timeout = base_url.rstrip("/"), token_file, timeout

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None

        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)

    def _token(self) -> str | None:
        try:
            path = Path(self.token_file)
            st = path.stat()
            if not stat.S_ISREG(st.st_mode) or stat.S_IMODE(st.st_mode) != 0o600 or st.st_uid != os.getuid():
                return None
            token = path.read_text().strip()
        except OSError:
            return None
        return token if token and len(token) <= 512 else None

    def _call(self, url: str, data: bytes | None):
        token = self._token()
        if token is None:
            return 0, None
        headers = {"PRIVATE-TOKEN": token}
        if data is not None:
            headers["Content-Type"] = "application/json"
        try:
            with self.opener.open(urllib.request.Request(url, data=data, headers=headers), timeout=self.timeout) as resp:
                return resp.status, strict_loads(resp.read(1 << 20))
        except urllib.error.HTTPError as e:
            return e.code, None
        except (OSError, ValueError):
            return 0, None

    def get(self, path: str) -> tuple[int, dict | None]:
        return self._call(self.base + "/api/v4" + path, None)

    def graphql(self, project_path: str, ci_job_id: int) -> tuple[int, dict | None]:
        query = ('query { project(fullPath:%s) { id fullPath job(id:"gid://gitlab/Ci::Build/%d") '
                 '{ id name manualJob allowFailure status pipeline { id } } } }' % (json.dumps(project_path), ci_job_id))
        return self._call(self.base + "/api/graphql", json.dumps({"query": query}).encode())


@dataclass(frozen=True)
class JobBinding:
    project_id: str
    pipeline_id: str
    ci_job_id: str
    commit_sha: str


class JobAuthenticator:
    """Turns a CI job token into a verified job binding or refuses; callers' claims are checked against it."""

    def __init__(self, cfg: EditorConfig, fetch: Callable[[str], tuple[int, dict | None]], control=None):
        self.cfg, self.fetch, self.control = cfg, fetch, control

    def verify(self, token: str) -> JobBinding:
        if not token or len(token) > 512:
            raise BadRequest(401, "unauthorized")
        status, job = self.fetch(token)
        if status != 200 or not isinstance(job, dict):
            raise BadRequest(401, "unauthorized")
        pipe = job.get("pipeline") if isinstance(job.get("pipeline"), dict) else {}
        ok = (job.get("status") == "running"
              and job.get("name") in self.cfg.job_names
              and job.get("ref") == self.cfg.default_branch and job.get("tag") is False
              and str(pipe.get("project_id")) == str(self.cfg.project_id)
              and pipe.get("ref") == self.cfg.default_branch
              and pipe.get("source") == self.cfg.pipeline_source
              and isinstance(pipe.get("sha"), str) and pipe["sha"] in self.cfg.allowed_shas
              and isinstance(job.get("id"), int) and isinstance(pipe.get("id"), int))
        if not ok:
            raise BadRequest(403, "forbidden")
        self._confirm_control_plane(job["id"], pipe["id"], pipe["sha"], job["name"])
        return JobBinding(str(pipe["project_id"]), str(pipe["id"]), str(job["id"]), pipe["sha"])

    def _confirm_control_plane(self, job_id: int, pipeline_id: int, sha: str, name: str) -> None:
        """``GET /job`` only authenticates the token. private / protected / manual come from the GitLab origin itself.

        Current server-side properties, not a historical protected-at-creation attestation. Anything missing, null,
        unsupported, an error or a mismatch refuses before the editor runs. Without the approved identity: NOT_SUPPORTED.
        """
        cfg, cp = self.cfg, self.control
        if cp is None or not cfg.project_path:
            raise BadRequest(503, "not_supported_trusted_metadata_credential_unavailable")
        pid = cfg.project_id

        def rest(path: str) -> dict:
            status, body = cp.get(path)
            if status != 200 or not isinstance(body, dict):
                raise BadRequest(503, "control_plane_unavailable")
            return body

        def need(ok: bool) -> None:
            if not ok:
                raise BadRequest(403, "forbidden")

        proj = rest(f"/projects/{pid}")
        need(proj.get("id") == pid and proj.get("path_with_namespace") == cfg.project_path
             and proj.get("visibility") == "private" and proj.get("default_branch") == cfg.default_branch)
        branch = rest(f"/projects/{pid}/repository/branches/{cfg.default_branch}")
        need(branch.get("name") == cfg.default_branch and branch.get("default") is True and branch.get("protected") is True)
        rjob = rest(f"/projects/{pid}/jobs/{job_id}")
        rpipe_of_job = rjob.get("pipeline") if isinstance(rjob.get("pipeline"), dict) else {}
        commit = rjob.get("commit") if isinstance(rjob.get("commit"), dict) else {}
        need(rjob.get("id") == job_id and rjob.get("name") == name and rjob.get("status") == "running"
             and rjob.get("ref") == cfg.default_branch and rjob.get("tag") is False
             and rpipe_of_job.get("id") == pipeline_id and commit.get("id") == sha)
        pipe = rest(f"/projects/{pid}/pipelines/{pipeline_id}")
        need(pipe.get("id") == pipeline_id and pipe.get("project_id") == pid and pipe.get("ref") == cfg.default_branch
             and pipe.get("sha") == sha and sha in cfg.allowed_shas and pipe.get("source") == cfg.pipeline_source
             and pipe.get("status") == "running")
        status, gql = cp.graphql(cfg.project_path, job_id)
        if status != 200 or not isinstance(gql, dict) or gql.get("errors"):
            raise BadRequest(503, "control_plane_unavailable")
        data = gql.get("data") if isinstance(gql.get("data"), dict) else {}
        gproj = data.get("project") if isinstance(data.get("project"), dict) else {}
        gjob = gproj.get("job") if isinstance(gproj.get("job"), dict) else {}
        gpipe = gjob.get("pipeline") if isinstance(gjob.get("pipeline"), dict) else {}
        need(gproj.get("fullPath") == cfg.project_path and gproj.get("id") == f"gid://gitlab/Project/{pid}"
             and gjob.get("id") == f"gid://gitlab/Ci::Build/{job_id}" and gjob.get("name") == name
             and gjob.get("manualJob") is True and gjob.get("allowFailure") is False and gjob.get("status") == "RUNNING"
             and gpipe.get("id") == f"gid://gitlab/Ci::Pipeline/{pipeline_id}")


HEX40 = re.compile(r"[0-9a-f]{40}")


# ---------------------------------------------------------------------------------------------------- executor
@dataclass
class Observation:
    """What the harness reports it observed. It is validated, never trusted, by the bridge."""
    text: str
    principal: str = ""
    family: str = ""
    model: str = ""
    resolved_version: str = ""
    provenance_source: str = ""
    tools_used: list | None = None
    attestation: dict | None = None


class Executor(Protocol):
    def run(self, job_input: dict, deadline: float, cancel: threading.Event) -> Observation: ...


class ExecutorFailed(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class UnavailableExecutor:
    """Default until the isolated harness is provisioned: every job fails closed, nothing is launched."""

    def run(self, job_input, deadline, cancel):
        raise ExecutorFailed("executor_not_provisioned")


class ProcessExecutor:
    """Runs the fixed harness command in its own session with a scrubbed environment and a private directory.

    The command is configuration only. The private input is the file ``input.json`` in the harness's working directory (mode 600, canonical JSON of
    the job input); stdin is closed. stdout must be one JSON object
    ``{"text", "observation": {...}}``. Deadline, cancel and any failure kill the whole process group and
    delete the private directory. This is the process boundary; the OS/harness isolation (no tools, MCP,
    shell, web, host files, other profiles, env secrets) is attested by the harness and bound to the
    launch digest, and is proven separately by the activation owner.
    """

    def __init__(self, argv: tuple, tmp_root: Path, max_output: int = MAX_ENVELOPE):
        self.argv, self.tmp_root, self.max_output = tuple(argv), Path(tmp_root), max_output
        self.groups: set[int] = set()                         # process groups of launched harnesses, until reaped
        self.sweep()

    def command(self, work: Path) -> list:
        return list(self.argv)

    def supervise(self, ob: "Observation") -> "Observation":
        """Hook for a launcher that issues its own attestation; the plain process boundary keeps the harness's."""
        return ob

    def sweep(self) -> None:
        """Remove job directories a crashed runner left behind (they hold submitted input). Concurrency is 1 and this
        runs at construction, before any job, so every ``job-*`` here is stale."""
        if self.tmp_root.is_dir():
            for d in self.tmp_root.glob("job-*"):
                shutil.rmtree(d, ignore_errors=True)

    def residue(self) -> list:
        """What a drained executor must not hold: private job directories and live harness process groups."""
        left = []
        if self.tmp_root.is_dir() and any(self.tmp_root.glob("job-*")):
            left.append("scratch")
        for pid in list(self.groups):
            try:
                os.killpg(pid, 0)
            except ProcessLookupError:
                self.groups.discard(pid)
            except PermissionError:
                pass
            if pid in self.groups:
                left.append("child_group")
                break
        return left

    def run(self, job_input, deadline, cancel):
        if not self.argv:
            raise ExecutorFailed("executor_not_provisioned")
        self.tmp_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        work = Path(tempfile.mkdtemp(prefix="job-", dir=self.tmp_root))
        os.chmod(work, 0o700)
        proc = None
        try:
            (work / "input.json").write_bytes(canonical(job_input))
            os.chmod(work / "input.json", 0o600)
            if cancel.is_set():
                raise ExecutorFailed("cancelled")                 # last look before anything is launched
            proc = subprocess.Popen(self.command(work), cwd=work, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, env={},
                                    start_new_session=True, close_fds=True)
            self.groups.add(proc.pid)
            out: list[bytes] = []
            reader = threading.Thread(target=lambda: out.append(proc.stdout.read(self.max_output + 1)), daemon=True)
            reader.start()
            while proc.poll() is None:
                if cancel.is_set():
                    raise ExecutorFailed("cancelled")
                if time.monotonic() >= deadline:
                    raise ExecutorFailed("deadline")
                time.sleep(0.01)
            reader.join(2)
            data = out[0] if out else b""
            if proc.returncode != 0:
                raise ExecutorFailed("harness_failed")
            if len(data) > self.max_output:
                raise ExecutorFailed("output_too_large")
            try:
                res = strict_loads(data)
                ob = res["observation"]
                return self.supervise(Observation(text=res["text"], principal=ob.get("principal", ""), family=ob.get("family", ""),
                                   model=ob.get("model", ""), resolved_version=ob.get("resolved_version", ""),
                                   provenance_source=ob.get("provenance_source", ""), tools_used=ob.get("tools_used"),
                                   attestation=ob.get("attestation")))
            except (ValueError, KeyError, TypeError, AttributeError):
                raise ExecutorFailed("bad_output")
        finally:
            if proc is not None and proc.poll() is None:
                self._kill(proc)
            elif proc is not None:
                self._kill(proc)            # stragglers of an already exited leader
            if proc is not None and proc.stdout is not None:
                proc.stdout.close()
            shutil.rmtree(work, ignore_errors=True)

    def _kill(self, proc) -> None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            proc.wait(2)
        except subprocess.TimeoutExpired:
            pass
        self.residue()                                        # forgets the group once it is really gone


# ----------------------------------------------------------------------------------------------------- bridge
class Signer(Protocol):
    def sign(self, data: bytes) -> str: ...


class NoSigner:
    def sign(self, data: bytes) -> str:
        raise RuntimeError("signer not provisioned")


def public_key_bytes(value: str) -> bytes | None:
    """The pinned public key: standard base64 of 32 bytes (the client's form) or, for older configs, 64 hex."""
    if HEX64.fullmatch(value or ""):
        return bytes.fromhex(value)
    try:
        raw = base64.b64decode(value or "", validate=True)
    except ValueError:
        return None
    return raw if len(raw) == 32 else None


class Ed25519Signer:
    """Signs terminal envelopes with the supervisor-held Ed25519 key; the signature is standard base64 (wire v1).

    The key file holds the 32-byte seed as 64 hex characters, mode 0600 and owned by the runner user. The derived
    public key must equal the pinned one, so a swapped or stale key refuses to start instead of signing quietly.
    Needs the optional ``cryptography`` package; without it construction fails and the bridge stays on NoSigner.
    """

    def __init__(self, key_file: str, public_key_hex: str):
        try:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        except ImportError as exc:
            raise RuntimeError("ed25519 signer needs the cryptography package") from exc
        path = Path(key_file)
        st = path.stat()
        if not stat.S_ISREG(st.st_mode) or stat.S_IMODE(st.st_mode) != 0o600 or st.st_uid != os.getuid():
            raise RuntimeError("signing key file must be a regular file, mode 600, owned by the runner user")
        seed = path.read_text().strip()
        if not HEX64.fullmatch(seed):
            raise RuntimeError("signing key file must hold a 32-byte seed as 64 lowercase hex characters")
        self._key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(seed))
        derived = self._key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        if public_key_bytes(public_key_hex) != derived:
            raise RuntimeError("signing key does not match the pinned public key")

    def sign(self, data: bytes) -> str:
        return base64.b64encode(self._key.sign(data)).decode("ascii")


@dataclass
class _Live:
    cancel: threading.Event = field(default_factory=threading.Event)
    gate: threading.Lock = field(default_factory=threading.Lock)     # cancel-set and queued->running are one step
    binding: JobBinding | None = None
    decided: bool = False        # the result is being delivered; a later cancel is refused, not accepted


class EditorBridge:
    def __init__(self, cfg: EditorConfig, auth: JobAuthenticator, executor: Executor, signer: Signer,
                 clock: Callable[[], float] = time.time, mono: Callable[[], float] = time.monotonic,
                 admission: HostAdmission | None = None):
        self.cfg, self.auth, self.executor, self.signer, self.clock, self.mono = cfg, auth, executor, signer, clock, mono
        self.admission = admission                       # the host cap shared with card/chat windows; None = local concurrency only
        self.ledger = Ledger(Path(cfg.state_dir) / "editor-jobs.sqlite")
        self.ledger.recover(clock())
        self.slot = threading.Semaphore(1)               # concurrency 1 inside the host cap
        self.mu = threading.Lock()
        self.drained = threading.Condition(self.mu)      # notified whenever a job leaves ``live``
        self.closed = False                              # set by close(): nothing new is admitted
        self.waiting = 0
        self.live: dict[str, _Live] = {}
        self.cache: dict[str, tuple[float, dict]] = {}   # delivered terminals, memory only, short-lived
        self.reasons: collections.deque = collections.deque(maxlen=64)   # fixed reason codes, newest last; never on the wire
        self.cache_ttl = 120.0
        self.cache_max = 8
        self.max_rows = 10000              # durable ledger ceiling: full means refuse, never forget a budget

    # ------------------------------------------------------------------------------------------ validation
    def _parse(self, body: bytes) -> dict:
        if len(body) > MAX_BODY:
            raise BadRequest(413, "body_too_large")
        try:
            env = strict_loads(body)
        except (ValueError, UnicodeDecodeError, RecursionError):
            raise BadRequest(400, "invalid_json")
        if not isinstance(env, dict) or set(env) != set(REQUEST_KEYS):
            raise BadRequest(400, "bad_envelope")
        if not isinstance(env["payload"], dict):
            raise BadRequest(400, "bad_envelope")
        for k in ("project_id", "pipeline_id", "ci_job_id", "publication_version", "attempt", "deadline_at"):
            if isinstance(env[k], bool) or not isinstance(env[k], int):      # one type, so one budget key per publication
                raise BadRequest(400, "bad_envelope")
        for k in ("protocol_version", "contract_digest", "commit_sha", "publication_id", "input_hash",
                  "system_hash", "schema_hash", "payload_hash"):
            if not isinstance(env[k], str) or not env[k]:
                raise BadRequest(400, "bad_envelope")
        for k in ("contract_digest", "input_hash", "system_hash", "schema_hash", "payload_hash"):
            if not HEX64.fullmatch(env[k]):
                raise BadRequest(400, "bad_envelope")
        if not HEX40.fullmatch(env["commit_sha"]) or env["attempt"] not in range(1, MAX_ATTEMPTS + 1):
            raise BadRequest(400, "bad_envelope")
        return env

    def _check_bindings(self, env: dict, bound: JobBinding) -> None:
        cfg = self.cfg
        if (env["protocol_version"] != cfg.protocol_version
                or not hmac.compare_digest(env["contract_digest"], cfg.contract_digest)
                or env["system_hash"] not in cfg.system_hashes or env["schema_hash"] not in cfg.schema_hashes):
            raise BadRequest(400, "binding_mismatch")
        if (str(env["project_id"]), str(env["pipeline_id"]), str(env["ci_job_id"]), env["commit_sha"]) != (
                bound.project_id, bound.pipeline_id, bound.ci_job_id, bound.commit_sha):
            raise BadRequest(403, "forbidden")
        if digest(env["payload"]) != env["payload_hash"]:
            raise BadRequest(400, "hash_mismatch")
        try:
            deadline = parse_deadline(env["deadline_at"])
        except ValueError:
            raise BadRequest(400, "bad_envelope")
        now = self.clock()
        if deadline <= now:
            raise BadRequest(400, "deadline_passed")
        if deadline > now + TOTAL_DEADLINE + cfg.skew:
            raise BadRequest(400, "deadline_too_far")

    # --------------------------------------------------------------------------------------------- submit
    def submit(self, headers, body: bytes) -> tuple[int, dict]:
        arrived = self.mono()
        if self.closed:
            return 503, {"error": "shutting_down"}
        try:
            env = self._parse(body)
            idem = job_id_of(env)
            if not hmac.compare_digest(str(headers.get("Idempotency-Key") or ""), idem):
                raise BadRequest(400, "idempotency_key_mismatch")
            bound = self.auth.verify(headers.get("JOB-TOKEN", ""))
            self._check_bindings(env, bound)
        except BadRequest as e:
            return e.status, {"error": e.code}
        deadline = arrived + min(TOTAL_DEADLINE, parse_deadline(env["deadline_at"]) - self.clock())
        req_hash, attempt = request_hash_of(env), env["attempt"] - 1      # the ledger counts attempts from 0
        budget = sha256(canonical([env["project_id"], env["publication_id"], env["publication_version"]]))
        with self.ledger.lock:
            row = self.ledger.get(idem)
            if row is not None:
                if row[2] != req_hash:
                    return 409, {"error": "idempotency_conflict"}
                return self._replay(row, env)
            if self.ledger.count() >= self.max_rows:
                return 503, {"error": "ledger_full"}           # fail closed; rows are the idempotency/budget record
            used = self.ledger.used(budget)
            opaque = idem                                     # the job id is the deterministic idempotency key
            live = _Live(binding=bound)
            with self.mu:                                  # visible to DELETE before the row is, so no cancel is lost
                if self.closed:
                    return 503, {"error": "shutting_down"}
                self.live[opaque] = live
            try:
                self.ledger.create(idem, opaque, req_hash, budget, attempt, env, self.clock())
                reason = ("attempts_exhausted" if used >= MAX_ATTEMPTS else
                          "attempt_out_of_order" if attempt != used else None)
                if reason:
                    return self._finish(idem, opaque, env, req_hash, "rejected", reason)
            except BaseException:
                self._forget_live(opaque)
                raise
        try:
            return self._execute(idem, opaque, env, req_hash, budget, deadline, live)
        finally:
            self._forget_live(opaque)

    def _forget_live(self, opaque: str) -> None:
        with self.drained:
            self.live.pop(opaque, None)
            self.drained.notify_all()

    def _replay(self, row, env) -> tuple[int, dict]:
        opaque, state = row[1], row[6]
        if state in ("queued", "running"):
            return 409, {"error": "inflight"}
        hit = self.cache.get(row[0])
        if hit and self.clock() - hit[0] < self.cache_ttl:
            return 200, hit[1]
        # the terminal was delivered or lost with a restart; the result is not kept anywhere, so say so
        self.reasons.append("result_unavailable" if state == "completed" else "replayed")
        return self._terminal(opaque, env, state if state != "completed" else "failed")

    def _execute(self, idem, opaque, env, req_hash, budget, deadline, live) -> tuple[int, dict]:
        got = self.slot.acquire(blocking=False)
        if not got:
            with self.mu:
                busy = self.waiting >= self.cfg.max_queue
                if not busy:
                    self.waiting += 1
            if busy:                                             # outside ``mu``: the ledger lock is taken before it elsewhere
                return self._finish(idem, opaque, env, req_hash, "rejected", "busy")
            try:
                while not got:                                    # short slices: a cancel or close() ends the wait at once
                    left = deadline - self.mono()
                    if left <= 0 or live.cancel.is_set() or self.closed:
                        break
                    got = self.slot.acquire(timeout=min(0.05, left))
            finally:
                with self.mu:
                    self.waiting -= 1
        if not got:
            return self._finish(idem, opaque, env, req_hash, *self._not_admitted(live))
        lease = None
        try:
            if self.admission is not None:                        # the common host cap; nothing is spent until this is held
                try:
                    lease = self.admission.acquire(deadline, live.cancel)
                except AdmissionTimeout:
                    return self._finish(idem, opaque, env, req_hash, "expired", "queue_deadline")
                except (AdmissionCancelled, AdmissionClosed):
                    return self._finish(idem, opaque, env, req_hash, *self._not_admitted(live))
                except Exception as e:                            # an unreadable host state is a refusal, not a launch
                    log.warning("editor admission failed: %s", type(e).__name__)
                    return self._finish(idem, opaque, env, req_hash, "rejected", "busy")
            with live.gate:                                       # a cancel is either before this block or sees a running job
                if live.cancel.is_set():
                    return self._finish(idem, opaque, env, req_hash, "cancelled", "cancelled_in_queue")
                if self.mono() >= deadline:
                    return self._finish(idem, opaque, env, req_hash, "expired", "queue_deadline")
                if not self.ledger.start(idem, budget):           # attempt spent before anything runs; not if cancelled
                    if live.cancel.is_set() or (self.ledger.get(idem) or ("",) * 7)[6] != "queued":
                        return self._finish(idem, opaque, env, req_hash, "cancelled", "cancelled_in_queue")
                    used = self.ledger.used(budget)           # a concurrent job spent the attempt this one was promised
                    return self._finish(idem, opaque, env, req_hash, "rejected",
                                        "attempts_exhausted" if used >= MAX_ATTEMPTS else "attempt_out_of_order")
            job_input = {"system_hash": env["system_hash"], "schema_hash": env["schema_hash"],
                         "payload": env["payload"], "job": opaque}
            started_ms = int(self.clock() * 1000)
            try:
                ob = self.executor.run(job_input, deadline, live.cancel)
            except ExecutorFailed as e:
                state = "cancelled" if e.code == "cancelled" else "expired" if e.code == "deadline" else "failed"
                return self._finish(idem, opaque, env, req_hash, state, e.code)
            except Exception as e:                                # an unknown crash is an unknown outcome
                log.warning("editor executor crashed: %s", type(e).__name__)
                return self._finish(idem, opaque, env, req_hash, "failed", "executor_crashed")
            with live.gate:                                       # cancel accepted before this point wins, after it loses
                if live.cancel.is_set():
                    return self._finish(idem, opaque, env, req_hash, "cancelled", "late_result_discarded")
                if self.mono() >= deadline:
                    return self._finish(idem, opaque, env, req_hash, "expired", "late_result_discarded")
                live.decided = True
            return self._complete(idem, opaque, env, req_hash, ob, started_ms, int(self.clock() * 1000))
        finally:
            if lease is not None:
                lease.release()
            self.slot.release()

    def _not_admitted(self, live: _Live) -> tuple[str, str]:
        """Terminal state and reason of a job that never reached the executor."""
        if self.closed:
            return "cancelled", "shutting_down"
        if live.cancel.is_set():
            return "cancelled", "cancelled_in_queue"
        return "expired", "queue_deadline"

    def close(self, timeout: float = DRAIN_TIMEOUT) -> bool:
        """Stop admitting, wake queued work, cancel running jobs and wait (at most ``timeout`` seconds) for them to leave.

        Idempotent. A queued job answers ``cancelled``; a running one has its process group killed by the executor and
        never delivers a late success; a job whose terminal answer is already being delivered is left to finish. The
        durable attempt budget is untouched: an attempt spent at launch stays spent. True when nothing is left running."""
        with self.mu:
            self.closed = True
            pending = list(self.live.values())
        if self.admission is not None:
            self.admission.close()
        for live in pending:
            with live.gate:
                if not live.decided:
                    live.cancel.set()
        end = self.mono() + max(0.0, timeout)
        with self.drained:
            while self.live:
                left = end - self.mono()
                if left <= 0:
                    return False
                self.drained.wait(left)
        return True

    # ------------------------------------------------------------------------------------ result gating
    def residue(self) -> list:
        """Reasons this bridge is not clean: a job still live, or the executor still holds a child group or scratch."""
        with self.mu:
            left = ["live_job"] if self.live else []
        probe = getattr(self.executor, "residue", None)
        return left + (list(probe()) if callable(probe) else [])

    def provenance_problem(self, ob: Observation) -> str | None:
        cfg = self.cfg
        if ob.principal != PRINCIPAL:
            return "wrong_principal"
        if ob.family != FAMILY:
            return "wrong_family"
        if ob.model != "gpt-6.1-sol":
            return "wrong_model"
        if not isinstance(ob.resolved_version, str) or not ob.resolved_version.strip():
            return "no_observed_version"
        if ob.provenance_source not in cfg.provenance_sources:      # empty by default: nothing is observed yet
            return "provenance_source_not_accepted"
        if ob.tools_used != []:
            return "tool_use"
        att = ob.attestation
        if (not isinstance(att, dict) or not cfg.launch_digest
                or not hmac.compare_digest(str(att.get("launch_digest", "")), cfg.launch_digest)
                or any(att.get(k) != v for k, v in SAFE_ATTESTATION.items())):
            return "isolation_not_attested"
        return None

    def _complete(self, idem, opaque, env, req_hash, ob: Observation, started_ms: int, finished_ms: int) -> tuple[int, dict]:
        problem = self.provenance_problem(ob)
        if problem:
            return self._finish(idem, opaque, env, req_hash, "failed", problem)
        if not isinstance(ob.text, str) or len(ob.text.encode("utf-8")) > MAX_TEXT:
            return self._finish(idem, opaque, env, req_hash, "failed", "result_too_large")
        try:
            result = strict_loads(ob.text)
        except ValueError:
            return self._finish(idem, opaque, env, req_hash, "failed", "bad_result")
        if not isinstance(result, dict) or set(result) != RESULT_KEYS:
            return self._finish(idem, opaque, env, req_hash, "failed", "bad_result")
        # Observed identity and the supervisor's own execution metadata; the isolation string is only emitted
        # because the attestation was validated above (a failed attestation never reaches this point).
        identity = {"principal": ob.principal, "family": ob.family, "model": ob.model, "version": ob.resolved_version}
        receipt = {"executor_id": self.cfg.executor_id, "config_digest": self.cfg.launch_digest, "started_at": started_ms,
                   "finished_at": finished_ms, "isolation": ISOLATION, "tools_used": []}
        return self._finish(idem, opaque, env, req_hash, "completed", "", result=result, identity=identity, receipt=receipt,
                            text=ob.text)

    # ------------------------------------------------------------------------------------ terminal envelope
    def _finish(self, idem, opaque, env, req_hash, state, reason, **done) -> tuple[int, dict]:
        out = self._terminal(opaque, env, state, **done)
        self.reasons.append(reason)
        h = digest(done["result"]) if done and out[0] == 200 and out[1].get("status") == "completed" else None
        self.ledger.finish(idem, out[1].get("status", "failed"), h, self.clock())
        if reason in UNSPENT:
            self.ledger.drop(idem, cancelled=reason == "shutting_down")             # transient: a retry of the same key must be able to run, not replay this
        elif out[0] == 200:
            now = self.clock()
            with self.mu:                      # bounded: expired entries go first, then the oldest beyond cache_max
                for k in [k for k, v in self.cache.items() if now - v[0] >= self.cache_ttl]:
                    del self.cache[k]
                self.cache[idem] = (now, out[1])
                while len(self.cache) > self.cache_max:
                    del self.cache[min(self.cache, key=lambda k: self.cache[k][0])]
        return out

    def _terminal(self, opaque, env, state, result=None, identity=None, receipt=None, text=None) -> tuple[int, dict]:
        """The signed wire v1 envelope: exactly RESPONSE_KEYS. Only ``completed`` carries identity/text/receipt."""
        body = {"bindings": {k: v for k, v in env.items() if k != "payload"}, "job_id": opaque, "identity": identity,
                "status": state, "text": text, "result_hash": digest(result) if result is not None else None,
                "executor_receipt": receipt}
        raw = canonical(body)
        try:
            sig = self.signer.sign(raw)
        except Exception:
            return 500, {"error": "signer_unavailable"}
        if len(raw) + len(sig) + 32 > MAX_ENVELOPE:
            return self._terminal(opaque, env, "failed")
        return 200, dict(body, signature=sig)

    # ---------------------------------------------------------------------------------------------- cancel
    def cancel(self, headers, opaque: str) -> tuple[int, dict]:
        if not OPAQUE.fullmatch(opaque or ""):
            return 404, {"error": "not_found"}
        try:
            bound = self.auth.verify(headers.get("JOB-TOKEN", ""))
        except BadRequest as e:
            return e.status, {"error": e.code}
        row = self.ledger.by_opaque(opaque)
        if row is None or row[5] != binding_hash(bound.project_id, bound.pipeline_id, bound.ci_job_id, bound.commit_sha):
            return 404, {"error": "not_found"}        # another job's id looks exactly like an unknown one
        with self.mu:
            live = self.live.get(opaque)
        if live is not None:
            with live.gate:
                if live.decided:
                    return 200, {"state": "too_late"}         # the terminal answer is already being delivered
                live.cancel.set()
        elif row[6] == "queued":
            self.ledger.finish(row[0], "cancelled", None, self.clock())
        return 200, {"state": "cancelling" if live is not None else row[6] if row[6] in TERMINAL else "cancelled"}

    # ------------------------------------------------------------------------------------------------ http
    def handle(self, method: str, path: str, headers, body: bytes = b"") -> tuple[int, dict] | None:
        """None when the path is not an editor-job route."""
        if method == "POST" and path == POST_PATH:
            return self.submit(headers, body)
        if method == "DELETE" and path.startswith(DELETE_PREFIX):
            return self.cancel(headers, path[len(DELETE_PREFIX):])
        return None


def parse_config(section: dict, state_dir: Path, error=ValueError) -> EditorConfig | None:
    """``[tgpeek_editor]`` -> EditorConfig; None when the section is absent or not enabled."""
    if not section or section.get("enabled", False) is not True:
        return None

    def hexes(key: str) -> frozenset:
        v = section.get(key)
        if not isinstance(v, list) or not v or any(not isinstance(x, str) or not HEX64.fullmatch(x) for x in v):
            raise error(f"[tgpeek_editor] {key} must be a nonempty list of sha256 hex digests")
        return frozenset(v)

    digest = str(section.get("contract_digest") or "")
    names = section.get("job_names")
    argv = section.get("argv", [])
    sources = section.get("provenance_sources", [])
    if not HEX64.fullmatch(digest):
        raise error("[tgpeek_editor] contract_digest must be a sha256 hex digest")
    if not isinstance(names, list) or not names or any(not isinstance(x, str) or not x for x in names):
        raise error("[tgpeek_editor] job_names must be a nonempty list of exact CI job names")
    if not str(section.get("gitlab_url") or "").startswith("https://"):
        raise error("[tgpeek_editor] gitlab_url must be an https URL")
    if isinstance(section.get("project_id"), bool) or not isinstance(section.get("project_id"), int):
        raise error("[tgpeek_editor] project_id must be an integer")
    if not isinstance(argv, list) or any(not isinstance(x, str) for x in argv):
        raise error("[tgpeek_editor] argv must be a list of strings")
    if not isinstance(sources, list) or any(not isinstance(x, str) or not x for x in sources):
        raise error("[tgpeek_editor] provenance_sources must be a list of names")
    shas = section.get("allowed_shas")
    if not isinstance(shas, list) or not shas or any(not isinstance(x, str) or not HEX40.fullmatch(x) for x in shas):
        raise error("[tgpeek_editor] allowed_shas must be a nonempty list of full lowercase commit SHAs")
    launch = str(section.get("launch_digest") or "")
    if (argv or sources) and not HEX64.fullmatch(launch):
        raise error("[tgpeek_editor] launch_digest (sha256 of argv + sandbox profile) is required with argv")
    roots = section.get("sandbox_read_roots", [])
    if (not isinstance(roots, list) or any(not isinstance(x, str) or not os.path.isabs(x) for x in roots)
            or not isinstance(section.get("sandbox", False), bool) or not isinstance(section.get("sandbox_network", False), bool)):
        raise error("[tgpeek_editor] sandbox/sandbox_network are booleans, sandbox_read_roots absolute paths")
    if section.get("sandbox", False) and not argv:
        raise error("[tgpeek_editor] sandbox needs argv")
    extra = section.get("sandbox_exec_extra", [])
    pinned = section.get("sandbox_pinned_files", {})
    if (not isinstance(extra, list) or any(not isinstance(x, str) or not os.path.isabs(x) for x in extra)
            or not isinstance(pinned, dict)
            or any(not isinstance(k, str) or not os.path.isabs(k) or not isinstance(v, str) or not HEX64.fullmatch(v)
                   for k, v in pinned.items())):
        raise error("[tgpeek_editor] sandbox_exec_extra is a list of absolute paths, sandbox_pinned_files a table of path = sha256")
    if (extra or pinned) and not section.get("sandbox", False):
        raise error("[tgpeek_editor] sandbox_exec_extra / sandbox_pinned_files need sandbox = true")
    project_path = str(section.get("project_path") or "")
    control_file = str(section.get("control_token_file") or "")
    if bool(control_file) and not project_path:
        raise error("[tgpeek_editor] control_token_file needs project_path (path_with_namespace)")
    key_file = str(section.get("signing_key_file") or "")
    pub = str(section.get("signing_public_key") or "")
    if bool(key_file) != bool(pub) or (pub and public_key_bytes(pub) is None):
        raise error("[tgpeek_editor] signing_key_file and signing_public_key (base64 of 32 bytes) must be set together")
    executor_id = str(section.get("executor_id") or "tgpeek-editor")
    if not EXECUTOR_ID.fullmatch(executor_id):
        raise error("[tgpeek_editor] executor_id must be 1..96 of A-Za-z0-9_-")
    return EditorConfig(
        state_dir=Path(state_dir), protocol_version=str(section.get("protocol_version") or PROTOCOL),
        contract_digest=digest, system_hashes=hexes("system_hashes"), schema_hashes=hexes("schema_hashes"),
        gitlab_url=str(section["gitlab_url"]), project_id=section["project_id"], job_names=frozenset(names),
        allowed_shas=frozenset(shas), default_branch=str(section.get("default_branch", "main")), argv=tuple(argv), launch_digest=launch,
        sandbox=bool(section.get("sandbox", False)), sandbox_read_roots=tuple(roots), sandbox_network=bool(section.get("sandbox_network", False)),
        sandbox_exec_extra=tuple(extra), sandbox_pinned_files=tuple(sorted(pinned.items())),
        provenance_sources=frozenset(sources), signing_key_file=key_file, signing_public_key=pub,
        project_path=project_path, control_token_file=control_file, executor_id=executor_id)


def build_bridge(cfg: EditorConfig, signer: Signer | None = None, admission: HostAdmission | None = None) -> EditorBridge:
    """The production composition: GitLab job-token auth, the configured harness (or none), the given signer."""
    tmp = Path(cfg.state_dir) / "editor-tmp"
    if cfg.argv and not cfg.sandbox:
        raise ValueError("[tgpeek_editor] a configured harness (argv) runs only under the OS sandbox: set sandbox = true")
    if cfg.argv and cfg.sandbox:
        from mesh_runner.editor_sandbox import SandboxExecutor
        executor = SandboxExecutor(cfg.argv, tmp, cfg.sandbox_read_roots, cfg.sandbox_network,
                                   extra_exec=cfg.sandbox_exec_extra, pinned_files=dict(cfg.sandbox_pinned_files), require_pins=True,
                                   immutable_pins=True)   # production: every launch input is pinned and immutable for the runner user
        if not hmac.compare_digest(executor.launch_digest, cfg.launch_digest):
            raise ValueError("[tgpeek_editor] launch_digest does not match argv + sandbox profile "
                             f"(expected {executor.launch_digest})")
    else:
        executor = ProcessExecutor(cfg.argv, tmp) if cfg.argv else UnavailableExecutor()
    if signer is None and cfg.signing_key_file:
        signer = Ed25519Signer(cfg.signing_key_file, cfg.signing_public_key)
    control = GitlabControlPlane(cfg.gitlab_url, cfg.control_token_file) if cfg.control_token_file else None
    return EditorBridge(cfg, JobAuthenticator(cfg, gitlab_fetch_job(cfg.gitlab_url), control), executor, signer or NoSigner(),
                        admission=admission)
