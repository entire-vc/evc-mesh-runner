"""API mode: a lane turn under a provider API key, within the budget of its spend pool.

A profile ``<account>/api`` of the host manifest runs a turn on an Anthropic API key instead of
the lane's subscription login. It is admitted only when all of these hold:

* route: the profile is the lane's explicit primary (``api_primary_profile``) or arrives from the
  lane's route (env command, ``MESH_RUNNER_API_PROFILE``) as a reserve whose account provider the
  lane lists in ``api_reserve_providers``;
* consent: the lane has ``api_optin = true``. Without consent, or for a profile the lane does not
  permit, the API route is ignored and the turn runs on the subscription, as before
  (``Refused.fallback``);
* launch: the turn is headless (``claude -p``). The interactive program asks before it uses an
  environment key, so a key is never handed to it;
* key: read by ``credential_ref`` from the operator's carrier file, in-process. The value goes
  only into the environment of the turn's process: never argv, never a log, a mark, a comment;
* budget: the spend pool has a configured cap. Each turn reserves the per-run amount (also passed
  as ``--max-budget-usd``) before the process starts and settles with the turn's
  ``total_cost_usd``; an unknown cost is charged at the whole reservation. Committed spend plus a
  new reservation over the cap refuses the start with reason ``budget_exhausted``.

Every refusal carries a short reason code and never a credential.
"""
from __future__ import annotations

import contextlib
import hashlib
import os
import re
import sqlite3
import stat
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

PROFILE_VAR = "MESH_RUNNER_API_PROFILE"   # route marker an env command may print: a profile ref, no secret
KEY_VAR = "ANTHROPIC_API_KEY"
# credentials of another login that must not travel with an API turn: the key is the only one
DROPPED_ENV = ("ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY")
MODE = "api"
_PROFILE = re.compile(r"([A-Za-z0-9][A-Za-z0-9._:-]{0,99}/[A-Za-z0-9][A-Za-z0-9._:-]{0,99})/api\Z")
_CRED = re.compile(r"cred:[a-zA-Z0-9][a-zA-Z0-9._/-]{0,194}\Z")
_ASSIGN = re.compile(r"(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)\Z")
_API_KEY = re.compile(r"sk-ant-api[A-Za-z0-9_-]{16,500}\Z")
_CARRIER_MAX = 4096
_MICRO = 1_000_000


class Refused(Exception):
    """An API turn is not admitted. ``reason`` is a short code; ``fallback`` = the route itself is not
    permitted, so the lane runs on its subscription instead of holding the start."""

    def __init__(self, reason: str, fallback: bool = False):
        super().__init__(reason)
        self.reason, self.fallback = reason, fallback


def usd_micros(value) -> int | None:
    """A USD amount -> integer micro-dollars; None for anything that is not a sane amount."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not amount.is_finite() or amount < 0 or amount > 100000:
        return None
    return int((amount * _MICRO).to_integral_value())


def fmt(micros: int) -> str:
    return f"{micros / _MICRO:.2f}"


@dataclass(frozen=True)
class Carrier:
    """Where the key of one ``credential_ref`` lives: a private file with exactly one assignment."""
    path: Path
    field: str = KEY_VAR
    sha256_8: str = ""                      # pins the exact key; a rotation updates it in the same change


@dataclass(frozen=True)
class PoolBudget:
    cap: int                                # micro-dollars the pool may commit in total
    run: int                                # micro-dollars one turn reserves (and --max-budget-usd)


@dataclass
class ApiConfig:
    """``[api]`` of runner.toml. Empty = no lane can run under a key."""
    manifest: Path | None = None
    ledger: Path | None = None
    pools: dict = field(default_factory=dict)          # pool ref -> PoolBudget
    credentials: dict = field(default_factory=dict)    # cred ref -> Carrier

    @property
    def enabled(self) -> bool:
        return bool(self.pools and self.credentials and self.manifest)


def parse_config(raw, state_dir: Path, catalog_manifest: Path | None, error) -> ApiConfig:
    """``[api]`` -> ApiConfig; ``error`` is the config error class. Amounts are strings or numbers in USD."""
    if raw is None:
        return ApiConfig()
    if not isinstance(raw, dict):
        raise error("[api] must be a table")
    expand = lambda v: Path(os.path.expanduser(str(v))) if v else None
    manifest = expand(raw.get("manifest")) or catalog_manifest
    ledger = expand(raw.get("ledger")) or state_dir / "api-spend.sqlite"
    pools = {}
    for ref, p in (raw.get("pools") or {}).items():
        if not isinstance(p, dict):
            raise error(f"[api.pools] {ref!r} must be a table")
        cap, run = usd_micros(p.get("cap_usd")), usd_micros(p.get("run_usd"))
        if cap is None or run is None or run == 0 or run > cap:
            raise error(f"[api.pools] {ref!r}: cap_usd and run_usd must be amounts with 0 < run_usd <= cap_usd")
        pools[str(ref)] = PoolBudget(cap, run)
    creds = {}
    for ref, c in (raw.get("credentials") or {}).items():
        if not _CRED.fullmatch(str(ref)) or not isinstance(c, dict) or not c.get("path"):
            raise error(f"[api.credentials] {ref!r}: a 'cred:' ref with a path is required")
        path = expand(c["path"])
        if not path.is_absolute():
            raise error(f"[api.credentials] {ref!r}: path must be absolute")
        pin = str(c.get("sha256_8") or "")
        if pin and not re.fullmatch(r"[0-9a-f]{8}", pin):
            raise error(f"[api.credentials] {ref!r}: sha256_8 must be 8 lowercase hex digits")
        creds[str(ref)] = Carrier(path, str(c.get("field") or KEY_VAR), pin)
    if (pools or creds) and not manifest:
        raise error("[api] needs a manifest (or [catalog] manifest): the accounts and their api pools")
    return ApiConfig(manifest, ledger, pools, creds)


# ----------------------------------------------------------------------- route

@dataclass(frozen=True)
class Route:
    profile: str                           # <account>/api
    account: str
    credential_ref: str
    pools: tuple                           # spend pools of the account's api mode
    role: str                              # "primary" | "reserve"


def _manifest(path: Path) -> dict:
    import json
    try:
        m = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        raise Refused("api_manifest_unreadable") from None
    if not isinstance(m, dict) or not isinstance(m.get("accounts"), dict):
        raise Refused("api_manifest_unreadable")
    return m


def select(agent, routed: str, cfg: ApiConfig) -> Route | None:
    """The API route of this turn, or None (no API route: the subscription, unchanged).

    ``routed`` is the profile the lane's route named (``MESH_RUNNER_API_PROFILE``); the lane's explicit
    primary wins over it. Raises Refused: ``fallback`` when the route is not permitted for this lane
    (no consent, a profile it does not list), plain when a permitted route cannot be executed here."""
    primary = str(getattr(agent, "api_primary_profile", "") or "")
    profile = primary or str(routed or "").strip()
    if not profile:
        return None
    if getattr(agent, "api_optin", False) is not True:
        raise Refused("api_profile_not_opted_in", fallback=True)
    m = _PROFILE.fullmatch(profile)
    if not m:
        raise Refused("api_profile_invalid", fallback=True)
    if not cfg.enabled:
        raise Refused("api_not_configured")
    account_ref = m.group(1)
    account = _manifest(cfg.manifest)["accounts"].get(account_ref)
    modes = account.get("modes") if isinstance(account, dict) else None
    if (not isinstance(account, dict) or account.get("enabled") is not True or not isinstance(modes, dict)
            or MODE not in modes):
        raise Refused("api_profile_not_in_manifest", fallback=True)
    if primary:
        role = "primary"
    elif account.get("provider") in tuple(getattr(agent, "api_reserve_providers", ()) or ()):
        role = "reserve"
    else:
        raise Refused("api_profile_not_permitted", fallback=True)
    if account.get("harness") != "claude_code" or account.get("provider") != "anthropic":
        raise Refused("api_harness_unsupported")
    pools = tuple((modes[MODE] or {}).get("pools") or ())
    if not pools:
        raise Refused("api_pool_missing")
    if any(p not in cfg.pools for p in pools):
        raise Refused("api_budget_unconfigured")      # money never runs on a pool without a cap
    cred = account.get("credential_ref")
    if not isinstance(cred, str) or cred not in cfg.credentials:
        raise Refused("credential_carrier_unknown")
    return Route(profile, account_ref, cred, pools, role)


# ----------------------------------------------------------------------- key

def read_key(carrier: Carrier) -> str:
    """Exactly one assignment of ``carrier.field`` from a private regular file of this user, holding an
    Anthropic API key (an OAuth token is refused) whose fingerprint matches the pin. The value never
    enters an exception, a log or a record."""
    path = carrier.path
    try:
        if path.is_symlink() or path.parent.is_symlink():
            raise Refused("credential_carrier_symlink")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except Refused:
        raise
    except OSError:
        raise Refused("credential_carrier_unreadable") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise Refused("credential_carrier_not_regular")
        if info.st_uid != os.getuid():
            raise Refused("credential_carrier_owner")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise Refused("credential_carrier_permissions")
        if info.st_size > _CARRIER_MAX:
            raise Refused("credential_carrier_oversize")
        raw = os.read(fd, _CARRIER_MAX + 1)
    except OSError:
        raise Refused("credential_carrier_unreadable") from None
    finally:
        os.close(fd)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise Refused("credential_carrier_malformed") from None
    found = None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _ASSIGN.fullmatch(line)
        if not m:
            raise Refused("credential_carrier_malformed")
        name, value = m.group(1), m.group(2).strip()
        if name != carrier.field:
            raise Refused("credential_carrier_extra_field")
        if found is not None:
            raise Refused("credential_carrier_duplicate_field")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        found = value
    if found is None:
        raise Refused("credential_carrier_field_missing")
    if not _API_KEY.fullmatch(found):
        raise Refused("credential_not_api_key")
    if carrier.sha256_8 and hashlib.sha256(found.encode()).hexdigest()[:8] != carrier.sha256_8:
        raise Refused("credential_fingerprint_mismatch")
    return found


def turn_env(env: dict[str, str], key: str) -> dict[str, str]:
    """The turn's process environment under the key: other logins' tokens and endpoints dropped."""
    out = {k: v for k, v in env.items() if k not in DROPPED_ENV and k != PROFILE_VAR}
    out[KEY_VAR] = key
    return out


def fingerprint_env(env: dict[str, str]) -> dict[str, str]:
    """An env with the shape of an API turn for profile fingerprints (auth.profile_id reads only the
    presence of the key, never its value): no secret is placed in it."""
    out = {k: v for k, v in env.items() if k not in DROPPED_ENV}
    out[KEY_VAR] = "present"
    return out


def with_budget(argv: list[str], run_micros: int) -> list[str]:
    """Bound the CLI's own spend to the reservation."""
    out, i = [], 0
    while i < len(argv):
        if argv[i] == "--max-budget-usd":
            i += 2
            continue
        if not argv[i].startswith("--max-budget-usd="):
            out.append(argv[i])
        i += 1
    return out + ["--max-budget-usd", fmt(run_micros)]


# ----------------------------------------------------------------------- ledger

class Ledger:
    """Durable, idempotent per-turn spend ledger with a hard cap per pool (sqlite, 0600).

    ``reserve(attempt, pool)`` holds the pool's per-run amount; the same attempt key returns its open
    reservation. It refuses when settled spend plus open reservations plus the new one would pass the
    cap. ``settle`` closes it once with the turn's cost. Any storage doubt refuses (fail-closed): an
    unreadable ledger is never "nothing spent"."""

    def __init__(self, path: Path, budgets: dict, clock=time.time):
        self.path, self.budgets, self._clock = Path(path), dict(budgets), clock

    def __repr__(self):
        return f"Ledger(path={str(self.path)!r})"

    def _connect(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if not self.path.exists():
                os.close(os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600))
            info = os.lstat(self.path)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) & 0o077):
                raise Refused("budget_ledger_unavailable")
            db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
            db.execute("PRAGMA busy_timeout=15000")
            db.execute("CREATE TABLE IF NOT EXISTS spend (attempt TEXT NOT NULL, pool TEXT NOT NULL, "
                       "reserved INTEGER NOT NULL, spent INTEGER, state TEXT NOT NULL, source TEXT, "
                       "created REAL NOT NULL, updated REAL NOT NULL, PRIMARY KEY (attempt, pool))")
            return db
        except Refused:
            raise
        except Exception:  # noqa: BLE001
            raise Refused("budget_ledger_unavailable") from None

    @contextlib.contextmanager
    def _tx(self):
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.execute("COMMIT")
        except BaseException:
            with contextlib.suppress(Exception):
                db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    @staticmethod
    def _committed(db, pool) -> int:
        row = db.execute("SELECT COALESCE(SUM(CASE WHEN state='settled' THEN spent ELSE reserved END), 0) "
                         "FROM spend WHERE pool=?", (pool,)).fetchone()
        return int(row[0] or 0)

    def _budget(self, pool) -> PoolBudget:
        b = self.budgets.get(pool)
        if b is None:
            raise Refused("api_budget_unconfigured")
        return b

    def reserve(self, attempt: str, pools) -> int:
        """Reserve the per-run amount on every pool; returns the smallest run amount (the CLI bound)."""
        pools = tuple(pools)
        if not attempt or not pools:
            raise Refused("budget_attempt_invalid")
        budgets = {p: self._budget(p) for p in pools}
        try:
            with self._tx() as db:
                for pool, b in budgets.items():
                    row = db.execute("SELECT state FROM spend WHERE attempt=? AND pool=?", (attempt, pool)).fetchone()
                    if row is not None:
                        if row[0] != "reserved":
                            raise Refused("budget_attempt_settled")
                        continue
                    if self._committed(db, pool) + b.run > b.cap:
                        raise Refused("budget_exhausted")
                    now = self._clock()
                    db.execute("INSERT INTO spend VALUES (?,?,?,?,?,?,?,?)",
                               (attempt, pool, b.run, None, "reserved", None, now, now))
        except Refused:
            raise
        except Exception:  # noqa: BLE001
            raise Refused("budget_ledger_unavailable") from None
        return min(b.run for b in budgets.values())

    def settle(self, attempt: str, spent_usd, source: str) -> int | None:
        """Close ``attempt`` once on each of its pools: ``spent_usd`` None = unknown -> the reservation is
        charged. A settled attempt is left as it is. Returns the micro-dollars charged now, None if the
        attempt has no open reservation."""
        try:
            with self._tx() as db:
                rows = db.execute("SELECT pool, reserved FROM spend WHERE attempt=? AND state='reserved'",
                                  (attempt,)).fetchall()
                if not rows:
                    return None
                micros = usd_micros(spent_usd) if spent_usd is not None else None
                charged = None
                for pool, reserved in rows:
                    amount, src = (micros, source) if micros is not None else (reserved, "unknown_charged_reservation")
                    db.execute("UPDATE spend SET spent=?, state='settled', source=?, updated=? WHERE attempt=? AND pool=?",
                               (amount, re.sub(r"[^a-z0-9_]", "", src.lower())[:40], self._clock(), attempt, pool))
                    charged = amount
                return charged
        except Exception:  # noqa: BLE001
            raise Refused("budget_ledger_unavailable") from None

    def release(self, attempt: str) -> bool:
        """No process ran for ``attempt``: its open reservation is removed, so the same turn may reserve
        again. A settled attempt (a turn that ran) is left as it is and still refuses a second charge."""
        try:
            with self._tx() as db:
                return db.execute("DELETE FROM spend WHERE attempt=? AND state='reserved'", (attempt,)).rowcount > 0
        except Exception:  # noqa: BLE001
            raise Refused("budget_ledger_unavailable") from None

    def status(self, pool: str) -> dict:
        b = self._budget(pool)
        with self._tx() as db:
            committed = self._committed(db, pool)
            spent = int(db.execute("SELECT COALESCE(SUM(spent), 0) FROM spend WHERE pool=? AND state='settled'",
                                   (pool,)).fetchone()[0] or 0)
            open_ = int(db.execute("SELECT COUNT(*) FROM spend WHERE pool=? AND state='reserved'",
                                   (pool,)).fetchone()[0] or 0)
        return {"pool": pool, "cap_usd": fmt(b.cap), "run_usd": fmt(b.run), "spent_usd": fmt(spent),
                "committed_usd": fmt(committed), "remaining_usd": fmt(max(b.cap - committed, 0)),
                "open_reservations": open_, "spent_micros": spent}
