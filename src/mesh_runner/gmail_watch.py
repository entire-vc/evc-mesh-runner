"""Opt-in Gmail WAIT: one durable, read-only observation of a NEW inbound message.

``⏳ WAIT gmail:<registration-id>`` sleeps until the registration fires, i.e. until one new
inbound message matches the registered predicate. Nothing here is an agent turn and nothing is
PubSub/push: Runner's own scheduler pulls message HEADERS through an injected reader (a restricted
wrapper around a Gmail CLI), evaluates the exact predicate itself and commits the result
in one SQLite transaction. See docs/gmail-watch.md for the contract.

Two registration forms:
* thread: ``mailbox + from address + thread id``
* search: ``mailbox + sender domains + markers`` (proves "a matching mail arrived", nothing more)

Rules that make it safe to leave running:
* the capture boundary is the registration's own ``high_water_internal_ms`` + ``gmail_seen``,
  never the wall clock and never a mailbox/thread historyId;
* a read is a candidate source only: the exact predicate (From, SENT, internalDate, seen) runs here;
* fire = CAS ``active -> fired`` + receipt + outbox rows in ONE ``BEGIN IMMEDIATE`` transaction,
  no provider I/O inside; a lost CAS leaves nothing behind;
* delivery comes only from the outbox with an idempotency key; the receiver dedups by key;
* every provider failure keeps baseline and seen untouched and only backs off the NEXT tick.
Bodies and subjects are never read, stored or logged.
"""
from __future__ import annotations

import datetime as dt
import email.policy
import hashlib
import json
import random
import re
import subprocess
import unicodedata
import uuid
from dataclasses import dataclass

ID_RE = re.compile(r"gmr_[0-9a-f]{12}")
LOOKBACK_S = 600                 # absorbs Gmail internalDate / indexing skew
BACKOFF_BASE_S = 30
BACKOFF_CAP_S = 1800
ALERT_AFTER = 5
ERROR_CLASSES = ("auth", "transport", "parse", "rate")
MAX_MARKER = 80
_MAILBOX = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,190}")
_MSG_ID = re.compile(r"[A-Za-z0-9_-]{4,64}")
_FORBIDDEN_MARKER = set("\"'\\{}()[]:")

WAIT_GUIDANCE = (
    "invalid gmail wait: `⏳ WAIT gmail:<registration-id>` is accepted only when the Gmail observer "
    "is enabled on this runner and an operator registered it for this card and this lane "
    "(`mesh-runner watch gmail register`). It wakes once on the first new inbound message that "
    "matches the registered predicate, or once with `cancelled`. Time never completes it. "
    "Declare another condition (card, mr, pipeline, decision) or ask the operator to register it."
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS gmail_registration (
    id TEXT PRIMARY KEY, mailbox TEXT NOT NULL, predicate_json TEXT NOT NULL, predicate_hash TEXT NOT NULL,
    observer_task TEXT NOT NULL, resume_agent TEXT NOT NULL, source_task TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL, baseline_internal_ms INTEGER NOT NULL, high_water_internal_ms INTEGER NOT NULL,
    created_at INTEGER NOT NULL, fired_at INTEGER, cancelled_at INTEGER, cancel_reason TEXT NOT NULL DEFAULT '',
    last_ok_at INTEGER, last_error_class TEXT NOT NULL DEFAULT '', consec_errors INTEGER NOT NULL DEFAULT 0,
    alert_class TEXT NOT NULL DEFAULT '', next_check_at INTEGER NOT NULL DEFAULT 0,
    UNIQUE (mailbox, predicate_hash, observer_task));
CREATE TABLE IF NOT EXISTS gmail_seen (
    registration_id TEXT NOT NULL, message_id TEXT NOT NULL, internal_ms INTEGER NOT NULL,
    PRIMARY KEY (registration_id, message_id));
CREATE TABLE IF NOT EXISTS gmail_receipt (
    registration_id TEXT PRIMARY KEY, message_id TEXT NOT NULL, thread_id TEXT NOT NULL, from_norm TEXT NOT NULL,
    internal_ms INTEGER NOT NULL, observed_at INTEGER NOT NULL, extra_count INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS gmail_outbox (
    outbox_id INTEGER PRIMARY KEY AUTOINCREMENT, registration_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, target TEXT NOT NULL, payload_json TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0, acked_at INTEGER);
"""


class GmailError(Exception):
    """A provider failure. ``cls`` is one of ERROR_CLASSES; the message never carries mail content."""

    def __init__(self, cls: str, detail: str = ""):
        super().__init__(f"{cls}: {detail}"[:200])
        self.cls = cls if cls in ERROR_CLASSES else "transport"


@dataclass(frozen=True)
class Read:
    records: list
    provider_now_ms: int | None = None


@dataclass
class Tick:
    """What one scan of one registration did."""
    outcome: str                      # fired | advanced | error | skipped
    error_class: str = ""
    alert: str = ""                   # class to alert on, once per class until recovery
    message_id: str = ""


# ------------------------------------------------------------------ schema
def ensure_schema(db) -> None:
    for stmt in SCHEMA.split(";"):
        if stmt.strip():
            db.execute(stmt)


def _tx(db):
    """Open an immediate transaction unless the caller already holds one; returns True when owned."""
    if db.in_transaction:
        return False
    db.execute("BEGIN IMMEDIATE")
    return True


# ------------------------------------------------------------- normalizing
def norm_domain(value: str) -> str | None:
    """Lowercase + IDNA; None when it is not a plain DNS name."""
    if not isinstance(value, str) or not value or len(value) > 253:
        return None
    try:
        out = value.strip().lower().rstrip(".").encode("idna").decode("ascii")
    except UnicodeError:
        return None
    return out if re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?)+", out) else None


def parse_from(header: str) -> tuple[str, str] | None:
    """(address, domain) of a From header with exactly one RFC 5322 mailbox, else None.
    The address, not the display name, is what is matched."""
    if not isinstance(header, str) or len(header) > 998 or any(c in header for c in "\r\n\0"):
        return None
    try:
        parsed = email.policy.default.header_factory("From", header)
        addrs = list(parsed.addresses)
    except Exception:                                  # noqa: BLE001 - malformed header is just "no match"
        return None
    if len(addrs) != 1 or not addrs[0].username or not addrs[0].domain:
        return None
    domain = norm_domain(addrs[0].domain)
    if domain is None:
        return None
    return f"{addrs[0].username.lower()}@{domain}", domain


def domain_matches(domain: str, wanted: str) -> bool:
    """Exact, or a subdomain separated by a dot: never contains, never a dotless suffix."""
    return domain == wanted or domain.endswith("." + wanted)


def validate_marker(marker: str) -> str:
    """A marker is a candidate hint for the provider search; it is never escaped into a query,
    it is refused when it could act as one."""
    if (not isinstance(marker, str) or not 1 <= len(marker) <= MAX_MARKER or marker != marker.strip()
            or marker.startswith("-") or any(c in _FORBIDDEN_MARKER for c in marker)
            or any(unicodedata.category(c) in ("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp") for c in marker)):
        raise ValueError("marker: 1..80 chars, no control chars, quotes, backslash, braces, brackets, "
                         "parentheses, ':' or a leading '-'")
    return marker


def _uuid(value) -> str | None:
    try:
        text = str(uuid.UUID(value)) if isinstance(value, str) else None
    except ValueError:
        return None
    return text if text == value else None


def build_predicate(*, from_addr: str | None = None, thread: str | None = None,
                    domains: list[str] | None = None, markers: list[str] | None = None) -> dict:
    thread_form = bool(from_addr or thread)
    if thread_form == bool(domains or markers):
        raise ValueError("give either --from + --thread or --sender-domain + --marker")
    if thread_form:
        parsed = parse_from(from_addr or "")
        if not parsed or parsed[0] != (from_addr or "").strip().lower() or not thread or not _MSG_ID.fullmatch(thread):
            raise ValueError("--from must be a plain address and --thread a Gmail thread id")
        return {"form": "thread", "from": parsed[0], "thread": thread}
    if not domains or not markers:
        raise ValueError("search form needs at least one --sender-domain and one --marker")
    nd = [norm_domain(d) for d in domains]
    if any(d is None for d in nd):
        raise ValueError("--sender-domain must be a plain DNS name")
    return {"form": "search", "domains": sorted(set(nd)), "markers": sorted({validate_marker(m) for m in markers})}


def predicate_hash(predicate: dict) -> str:
    return hashlib.sha256(json.dumps(predicate, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]


# ------------------------------------------------------------------ records
def clean_records(raw) -> tuple[list[dict], int]:
    """Strictly parsed header records and the count skipped. Unknown fields are ignored; a record
    without a usable From / Message-ID / internalDate is skipped, never guessed."""
    if not isinstance(raw, list):
        raise GmailError("parse", "records is not a list")
    out, skipped = [], 0
    for r in raw:
        try:
            mid, tid, ms = r["message_id"], r["thread_id"], r["internal_ms"]
            labels = r.get("labels") or []
            if (not isinstance(mid, str) or not _MSG_ID.fullmatch(mid) or not isinstance(tid, str)
                    or not _MSG_ID.fullmatch(tid) or type(ms) is not int or ms <= 0
                    or not isinstance(labels, list) or not all(isinstance(x, str) for x in labels)):
                raise ValueError
            frm = parse_from(r["from"])
            if frm is None:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            skipped += 1
            continue
        out.append({"message_id": mid, "thread_id": tid, "internal_ms": ms, "from": frm[0], "domain": frm[1],
                    "sent": "SENT" in labels})
    return out, skipped


def evaluate(predicate: dict, baseline_ms: int, seen: set[str], records: list[dict]) -> list[dict]:
    """Pure: the records that are a NEW inbound match, oldest first. Label-only changes of an old
    message are not an event: it is already seen or at/below the baseline."""
    out = []
    for r in records:
        if r["sent"] or r["internal_ms"] <= baseline_ms or r["message_id"] in seen:
            continue
        if predicate["form"] == "thread":
            if r["from"] != predicate["from"] or r["thread_id"] != predicate["thread"]:
                continue
        elif not any(domain_matches(r["domain"], d) for d in predicate["domains"]):
            continue
        out.append(r)
    return sorted(out, key=lambda r: (r["internal_ms"], r["message_id"]))


def read_request(predicate: dict, mailbox: str, after_s: int) -> dict:
    """The structured request handed to the read-only wrapper: no query string, no shell."""
    base = {"mailbox": mailbox, "after_s": max(0, int(after_s))}
    if predicate["form"] == "thread":
        return {**base, "form": "thread", "thread": predicate["thread"]}
    return {**base, "form": "search", "sender_domains": list(predicate["domains"]), "markers": list(predicate["markers"])}


# --------------------------------------------------------------- the reader
class SubprocessReader:
    """Runs the operator-configured read-only wrapper: JSON request on stdin, JSON on stdout.
    ``argv`` is a fixed list (never a shell string); nothing from mail content reaches it.
    Exit codes: 3 auth, 4 rate, anything else non-zero transport; undecodable output is parse."""

    def __init__(self, argv: list[str], timeout: float = 60.0):
        if not isinstance(argv, (list, tuple)) or not argv or not all(isinstance(a, str) and a for a in argv):
            raise ValueError("gmail reader argv must be a non-empty list of strings")
        self.argv, self.timeout = list(argv), timeout

    def __call__(self, request: dict) -> Read:
        try:
            done = subprocess.run(self.argv, input=json.dumps(request).encode(), capture_output=True,
                                  timeout=self.timeout, check=False)
        except subprocess.TimeoutExpired:
            raise GmailError("transport", "timeout") from None
        except OSError as exc:
            raise GmailError("transport", type(exc).__name__) from None
        if done.returncode != 0:
            raise GmailError({3: "auth", 4: "rate"}.get(done.returncode, "transport"), f"rc={done.returncode}")
        if len(done.stdout) > 4 * 1024 * 1024:
            raise GmailError("parse", "response too large")
        try:
            body = json.loads(done.stdout)
            now_ms = body.get("now_ms")
            return Read(body["records"], now_ms if type(now_ms) is int and now_ms > 0 else None)
        except (ValueError, KeyError, AttributeError, TypeError):
            raise GmailError("parse", "unreadable response") from None


# ------------------------------------------------------------ registration
def _row(db, reg_id: str):
    return db.execute("SELECT * FROM gmail_registration WHERE id=?", (reg_id,)).fetchone()


def register(store, reader, *, mailbox: str, observer_task: str, resume_agent: str, predicate: dict,
             source_task: str = "", since_ms: int | None = None, dry_run: bool = False,
             interval_s: float = 120.0) -> dict:
    """Take the baseline and write the registration atomically. Idempotent on
    (mailbox, predicate, observer_task): a repeat returns the stored registration unchanged."""
    db = store.db
    ensure_schema(db)
    if not _MAILBOX.fullmatch(mailbox or ""):
        raise ValueError("--mailbox must be an e-mail address")
    if _uuid(observer_task) is None or (source_task and _uuid(source_task) is None):
        raise ValueError("--observer-task and --source-task must be canonical UUIDs")
    if not isinstance(resume_agent, str) or not re.fullmatch(r"[\w-]{1,64}", resume_agent):
        raise ValueError("--resume-agent must be a Mesh agent id")
    if since_ms is not None and (type(since_ms) is not int or since_ms <= 0):
        raise ValueError("--since is epoch milliseconds")
    phash = predicate_hash(predicate)
    old = db.execute("SELECT id FROM gmail_registration WHERE mailbox=? AND predicate_hash=? AND observer_task=?",
                     (mailbox, phash, observer_task)).fetchone()
    if old and not dry_run:
        return {**status(store, old["id"]), "existing": True}
    snap = reader(read_request(predicate, mailbox, (since_ms // 1000) if since_ms else 0))
    records, skipped = clean_records(snap.records)
    if predicate["form"] == "thread":
        records = [r for r in records if r["thread_id"] == predicate["thread"]]
    top = max((r["internal_ms"] for r in records), default=0)
    if since_ms:                                        # catch-up: nothing is seeded, matches fire on the first tick
        baseline, seed = since_ms, []
    else:
        baseline = max(top, snap.provider_now_ms or 0)  # provider time, never the local clock
        if baseline <= 0:
            raise ValueError("the provider returned no usable time: refusing to register on the local clock")
        seed = records
    result = {"predicate_hash": phash, "baseline": {"max_internal_ms": baseline, "seen_ids_count": len(seed)},
              "candidates_now": len(evaluate(predicate, baseline, {r["message_id"] for r in seed}, records)),
              "skipped_records": skipped, "dry_run": dry_run}
    if dry_run:
        return result
    reg_id = "gmr_" + uuid.uuid4().hex[:12]
    created = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
    owned = _tx(db)
    try:
        db.execute("INSERT INTO gmail_registration (id, mailbox, predicate_json, predicate_hash, observer_task, "
                   "resume_agent, source_task, state, baseline_internal_ms, high_water_internal_ms, created_at, "
                   "next_check_at) VALUES (?,?,?,?,?,?,?, 'active', ?,?,?,?)",
                   (reg_id, mailbox, json.dumps(predicate, sort_keys=True), phash, observer_task, resume_agent,
                    source_task, baseline, baseline, created, created + int(interval_s * 1000)))
        db.executemany("INSERT OR IGNORE INTO gmail_seen VALUES (?,?,?)",
                       [(reg_id, r["message_id"], r["internal_ms"]) for r in seed])
    except BaseException:
        if owned and db.in_transaction:
            db.execute("ROLLBACK")
        raise
    if owned and db.in_transaction:
        db.execute("COMMIT")
    return {**result, "registration_id": reg_id, "existing": False}


def status(store, reg_id: str | None = None) -> dict | list:
    db = store.db
    ensure_schema(db)
    if reg_id is None:
        return [status(store, r["id"]) for r in db.execute("SELECT id FROM gmail_registration ORDER BY created_at")]
    r = _row(db, reg_id)
    if r is None:
        return {"registration_id": reg_id, "state": "unknown"}
    rc = db.execute("SELECT message_id FROM gmail_receipt WHERE registration_id=?", (reg_id,)).fetchone()
    pending = db.execute("SELECT COUNT(*) FROM gmail_outbox WHERE registration_id=? AND state='pending'",
                         (reg_id,)).fetchone()[0]
    # ``error`` is a display state: the registration stays active and its baseline untouched
    shown = "error" if r["state"] == "active" and r["consec_errors"] >= ALERT_AFTER else r["state"]
    return {"registration_id": reg_id, "state": shown, "mailbox": r["mailbox"], "predicate_hash": r["predicate_hash"],
            "observer_task": r["observer_task"], "resume_agent": r["resume_agent"], "source_task": r["source_task"],
            "cursor": {"baseline_internal_ms": r["baseline_internal_ms"], "high_water_internal_ms": r["high_water_internal_ms"]},
            "last_ok_at": r["last_ok_at"], "last_error_class": r["last_error_class"],
            "consec_errors": r["consec_errors"], "receipt_message_id": rc["message_id"] if rc else None,
            "outbox_pending": pending}


def registrations_of(store, resume_agent: str, states=("active", "fired", "cancelled")) -> list[dict]:
    ensure_schema(store.db)
    q = ",".join("?" * len(states))
    return [dict(r) for r in store.db.execute(
        f"SELECT * FROM gmail_registration WHERE resume_agent=? AND state IN ({q}) ORDER BY created_at",
        (resume_agent, *states))]


def cancel(store, reg_id: str, reason: str = "", now_ms: int | None = None) -> dict:
    """CAS active -> cancelled plus ONE terminal wake in the same transaction. A registration that
    already fired or was cancelled is reported as it is."""
    db = store.db
    ensure_schema(db)
    now_ms = now_ms or int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
    owned = _tx(db)
    try:
        r = _row(db, reg_id)
        if r is not None and db.execute("UPDATE gmail_registration SET state='cancelled', cancelled_at=?, cancel_reason=? "
                                        "WHERE id=? AND state='active'", (now_ms, reason[:200], reg_id)).rowcount == 1:
            _outbox(db, reg_id, f"gmail:{reg_id}:cancelled:wake", "wake", r["observer_task"],
                    {"text": f"gmail:{reg_id} cancelled" + (f" reason={reason[:200]}" if reason else "")})
    except BaseException:
        if owned and db.in_transaction:
            db.execute("ROLLBACK")
        raise
    if owned and db.in_transaction:
        db.execute("COMMIT")
    return status(store, reg_id)


def _outbox(db, reg_id: str, key: str, kind: str, target: str, payload: dict) -> None:
    db.execute("INSERT OR IGNORE INTO gmail_outbox (registration_id, idempotency_key, kind, target, payload_json) "
               "VALUES (?,?,?,?,?)", (reg_id, key, kind, target, json.dumps(payload, sort_keys=True)))


# -------------------------------------------------------------------- tick
def backoff_s(errors: int) -> int:
    return min(BACKOFF_CAP_S, BACKOFF_BASE_S * (2 ** max(0, errors - 1)))


def _iso(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).isoformat(timespec="seconds")


def tick(store, reader, reg_id: str, now_ms: int, interval_s: float = 120.0, jitter: float = 0.1,
         rng=random) -> Tick:
    """One scheduled read of one active registration: read -> pure evaluate -> ONE transaction.
    A failure of any kind changes nothing but the error counters and the next check time."""
    db = store.db
    ensure_schema(db)
    r = _row(db, reg_id)
    if r is None or r["state"] != "active":
        return Tick("skipped")
    predicate = json.loads(r["predicate_json"])
    after_s = r["high_water_internal_ms"] // 1000 - LOOKBACK_S
    try:
        got = reader(read_request(predicate, r["mailbox"], after_s))
        records, _skipped = clean_records(got.records)
    except GmailError as exc:
        return _failed(db, r, exc.cls, now_ms)
    except Exception as exc:                            # noqa: BLE001 - a reader bug is a transport fault, not a crash
        return _failed(db, r, "transport", now_ms, detail=type(exc).__name__)
    if predicate["form"] == "thread":
        records = [x for x in records if x["thread_id"] == predicate["thread"]]
    nxt = now_ms + int(interval_s * 1000 * (1 + rng.uniform(0, jitter)))
    owned = _tx(db)
    try:
        # re-read under the write lock: a cancel or another fire may have won while the provider was read
        cur = _row(db, reg_id)
        if cur is None or cur["state"] != "active":
            if owned and db.in_transaction:
                db.execute("ROLLBACK")
            return Tick("skipped")
        seen = {x[0] for x in db.execute("SELECT message_id FROM gmail_seen WHERE registration_id=?", (reg_id,))}
        found = evaluate(predicate, cur["baseline_internal_ms"], seen, records)
        db.executemany("INSERT OR IGNORE INTO gmail_seen VALUES (?,?,?)",
                       [(reg_id, x["message_id"], x["internal_ms"]) for x in records])
        if found:
            first = found[0]
            won = db.execute("UPDATE gmail_registration SET state='fired', fired_at=?, last_ok_at=?, consec_errors=0, "
                             "last_error_class='', alert_class='' WHERE id=? AND state='active'",
                             (now_ms, now_ms, reg_id)).rowcount == 1
            if not won:
                raise _Lost()
            db.execute("INSERT INTO gmail_receipt VALUES (?,?,?,?,?,?,?)",
                       (reg_id, first["message_id"], first["thread_id"], first["from"], first["internal_ms"],
                        now_ms, len(found) - 1))
            text = (f"gmail:{reg_id} fired message_id={first['message_id']} thread={first['thread_id']} "
                    f"from={first['from']} at={_iso(first['internal_ms'])}")
            key = f"gmail:{reg_id}:{first['message_id']}"
            _outbox(db, reg_id, key + ":wake", "wake", cur["observer_task"], {"text": text})
            if cur["source_task"]:
                _outbox(db, reg_id, key + ":comment", "comment", cur["source_task"], {"text": _comment(
                    cur, first, len(found) - 1, now_ms, predicate["form"])})
            out = Tick("fired", message_id=first["message_id"])
        else:
            top = max((x["internal_ms"] for x in records), default=0)
            db.execute("UPDATE gmail_registration SET high_water_internal_ms=MAX(high_water_internal_ms, ?), last_ok_at=?, "
                       "consec_errors=0, last_error_class='', alert_class='', next_check_at=? WHERE id=?",
                       (top, now_ms, nxt, reg_id))
            out = Tick("advanced")
    except _Lost:
        if owned and db.in_transaction:
            db.execute("ROLLBACK")
        return Tick("skipped")
    except BaseException:
        if owned and db.in_transaction:
            db.execute("ROLLBACK")
        raise
    if owned and db.in_transaction:
        db.execute("COMMIT")
    return out


def scan(store, reader, mesh, resume_agent: str, interval_s: float, busy: set, on_alert, label: str = "") -> None:
    """One pass over the active registrations one lane resumes: each due one is read once (``tick``), then the
    pending comments are delivered. The same pass runs inside the lane's tick (gmail in the runner process) or
    in ``mesh-runner service gmail`` (gmail as a process); the lane then only reads the wake rows this writes.
    ``busy`` holds the registrations being read, so one is never read twice at a time; ``on_alert(reg_id,
    error_class)`` hears a registration that has failed ALERT_AFTER times in a row. Nothing here raises."""
    import logging
    import time
    log = logging.getLogger("mesh_runner")
    now_ms = int(time.time() * 1000)
    try:
        regs = registrations_of(store, resume_agent)
    except Exception as e:                  # noqa: BLE001
        log.warning("%s: gmail scan failed: %s", label, type(e).__name__)
        return
    for r in regs:
        if r["state"] != "active" or r["next_check_at"] > now_ms or r["id"] in busy:
            continue
        busy.add(r["id"])
        try:
            t = tick(store, reader, r["id"], now_ms, interval_s)
            if t.alert:
                log.error("%s: gmail observation %s unavailable (%s) after %d consecutive failures; baseline kept, "
                          "no wake, no turn", label, r["id"], t.alert, ALERT_AFTER)
                on_alert(r["id"], t.alert)
        except Exception as e:             # noqa: BLE001
            log.warning("%s: gmail tick failed: %s", label, type(e).__name__)
        finally:
            busy.discard(r["id"])
    try:
        deliver_comments(store, mesh, resume_agent)
    except Exception as e:                  # noqa: BLE001
        log.warning("%s: gmail comment delivery failed: %s", label, type(e).__name__)


class _Lost(Exception):
    """The CAS was lost; nothing is written."""


def _comment(reg, first: dict, extra: int, now_ms: int, form: str) -> str:
    lines = [f"Gmail observation {reg['id']} fired: new inbound message_id={first['message_id']} "
             f"thread={first['thread_id']} from={first['from']} (message time {_iso(first['internal_ms'])}, "
             f"observed {_iso(now_ms)}). Headers only; the body was not read."]
    if extra:
        lines.append(f"{extra} more matching message(s) were seen in the same read.")
    if form == "search":
        lines.append("Limit: this form proves only that a matching message from the registered domain arrived, "
                     "not that it answers our request; read it before acting.")
    return " ".join(lines)


def _failed(db, r, cls: str, now_ms: int, detail: str = "") -> Tick:
    errors = r["consec_errors"] + 1
    alert = ""
    if errors >= ALERT_AFTER and r["alert_class"] != cls:
        alert = cls
    owned = _tx(db)
    try:
        db.execute("UPDATE gmail_registration SET consec_errors=?, last_error_class=?, next_check_at=?, "
                   "alert_class=CASE WHEN ?!='' THEN ? ELSE alert_class END WHERE id=? AND state='active'",
                   (errors, cls, now_ms + backoff_s(errors) * 1000, alert, alert, r["id"]))
    except BaseException:
        if owned and db.in_transaction:
            db.execute("ROLLBACK")
        raise
    if owned and db.in_transaction:
        db.execute("COMMIT")
    return Tick("error", error_class=cls, alert=alert)


# ------------------------------------------------------------------ outbox
def pending(db, reg_id: str, kind: str) -> list[dict]:
    return [dict(x) for x in db.execute(
        "SELECT * FROM gmail_outbox WHERE registration_id=? AND kind=? AND state='pending' ORDER BY outbox_id",
        (reg_id, kind))]


def ack(db, key: str, now_ms: int | None = None) -> None:
    """Mark one outbox row delivered. Runs inside the caller's transaction when there is one."""
    db.execute("UPDATE gmail_outbox SET state='acked', acked_at=? WHERE idempotency_key=? AND state='pending'",
               (now_ms or int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000), key))


def key_line(key: str) -> str:
    return f"outbox-key: {key}"


def deliver_comments(store, mesh, resume_agent: str) -> int:
    """Post each pending comment once. The receiver is asked first whether a comment carrying the
    key already exists (crash between POST and ACK), then the row is acked after the POST. A Mesh
    error leaves the row pending for the next scan; it never raises into the lane."""
    db = store.db
    sent = 0
    for reg in registrations_of(store, resume_agent, ("fired",)):
        for row in pending(db, reg["id"], "comment"):
            try:
                existing = mesh.comments_all(row["target"])
                already = any(key_line(row["idempotency_key"]) in (c.get("body") or "") for c in existing)
                if not already:
                    body = json.loads(row["payload_json"])["text"] + "\n" + key_line(row["idempotency_key"])
                    mesh.add_comment(row["target"], body, key="gmail|" + str(row["idempotency_key"]))
                    sent += 1
                db.execute("UPDATE gmail_outbox SET attempts=attempts+1 WHERE outbox_id=?", (row["outbox_id"],))
                ack(db, row["idempotency_key"])
            except Exception:                           # noqa: BLE001 - Mesh unavailable: retry on the next scan
                db.execute("UPDATE gmail_outbox SET attempts=attempts+1 WHERE outbox_id=?", (row["outbox_id"],))
    return sent


def wake_text(store, reg_id: str) -> tuple[str, str] | None:
    """(idempotency key, text) of the oldest pending wake of this registration."""
    rows = pending(store.db, reg_id, "wake")
    return (rows[0]["idempotency_key"], json.loads(rows[0]["payload_json"])["text"]) if rows else None
