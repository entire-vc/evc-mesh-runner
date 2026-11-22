"""The turn fields of a spend report (``POST /agents/me/sessions/report``).

With ``[spend_report] turn_fields = true`` the report of a turn carries, beside its tokens and cost:

- ``turn_id``: the turn's report id (``<attempt>:<turn>``), the same on every resend of the report;
- ``started_at``: when the runner launched the turn; ``ended_at``: the last write of the turn's log;
- ``provider``: who served the turn (``anthropic``, ``openai``, ``zai``, ``moonshot``), read from the model
  the harness recorded, else from a harness that talks to one provider only;
- ``account_ref``: the limit-pool account of the turn's login, ``<harness>/<account>``, the name quota
  observations give the account's pools (``claude/ops-team/5h``), so turns and limits join on one key;
- ``billing_mode``: ``api`` for a turn on an API key, ``subscription`` for a turn on a login;
- ``cache_read`` / ``cache_write``: the parts of ``tokens_in`` read from and written to the cache;
  ``cache_write_1h``: the part of ``cache_write`` written with the 1 h lifetime (priced apart).

``tokens_in`` keeps its meaning: the whole input, cache included. A cache figure the harness did not give
is listed in ``unknown`` (``cache_read``, ``cache_write``), never sent as 0. A label or time the runner
cannot establish is left out; Mesh stores it as null. Every value is checked here against what Mesh
accepts: Mesh refuses a whole report over one bad field, and a refused report loses the turn's tokens and
cost with it.

The launch facts (start time, account, billing mode) are recorded when the turn is launched, from the
environment that turn actually ran with, never read later from the lane's current route.
"""
from __future__ import annotations

import datetime as dt
import math
import re

from . import auth

MAX_TURN_ID = 200
MAX_PROVIDER = 64
BILLING_MODES = ("subscription", "api")
# Same families as Mesh's own derivation from the model name; a GLM model is served by Z.ai.
_MODEL_PROVIDERS = ((re.compile(r"claude|anthropic"), "anthropic"),
                    (re.compile(r"(^|/)(gpt|o[0-9]|codex|chatgpt|openai)"), "openai"),
                    (re.compile(r"kimi|moonshot"), "moonshot"),
                    (re.compile(r"glm|zhipu|z-ai|zai/"), "zai"),
                    (re.compile(r"gemini|google/"), "google"))
# Harnesses that talk to one provider only. The Claude harness is not one: its endpoint may serve
# another provider's model, so a Claude turn without a recognisable model has no provider.
_HARNESS_PROVIDERS = {"codex": "openai", "kimi": "moonshot", "zcode": "zai", "agy": "google"}
# Anthropic API keys are the only API route of the runner (api_mode.py).
API_ROUTE_PROVIDER = "anthropic"
# A file's mtime comes from the kernel's coarse clock, a tick behind the wall clock the launch record
# reads, so a log written within the tick of the launch can look older than it. Up to this lag the turn
# ended when it started; beyond it the two times contradict each other.
CLOCK_SKEW = dt.timedelta(seconds=1)
_API_KEY_VARS = {"claude": ("ANTHROPIC_API_KEY",), "codex": ("OPENAI_API_KEY", "CODEX_API_KEY")}
# A Claude spawn served by another endpoint or cloud: its login directory names no account of the
# turn and the runner does not know how that endpoint bills, so neither is reported.
_CLAUDE_FOREIGN_VARS = ("ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
                        "CLAUDE_CODE_USE_FOUNDRY")
_ANTHROPIC_URLS = ("", "https://api.anthropic.com", "https://api.anthropic.com/")


def launch_facts(harness: str, env: dict[str, str], api_account: str = "", aliases: dict | None = None,
                 cwd=None) -> dict:
    """What the launch of a turn says about its spend: ``account_ref`` and ``billing_mode`` when known.

    ``api_account`` is the account of the turn's API route (api_mode), if it runs on one."""
    if api_account:
        out = {"billing_mode": "api", "provider": API_ROUTE_PROVIDER}
        if auth.ACCOUNT_REF.fullmatch(api_account):
            out["account_ref"] = api_account
        return out
    if harness not in _API_KEY_VARS:
        return {}                               # its own login, which the runner does not read
    if harness == "claude" and ((env.get("ANTHROPIC_BASE_URL") or "").strip() not in _ANTHROPIC_URLS
                                or any(env.get(var) for var in _CLAUDE_FOREIGN_VARS)):
        return {}                               # another endpoint: whose account, billed how, unknown
    if any(env.get(var) for var in _API_KEY_VARS[harness]):
        return {"billing_mode": "api"}          # a key of unknown account
    if harness == "claude" and env.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return {"billing_mode": "subscription"}  # a login token, not the directory's login
    out = {"billing_mode": "subscription"}      # the harness's login directory
    ref = auth.account_ref(harness, env, cwd, aliases)
    if ref:
        out["account_ref"] = ref
    return out


def provider(model: str, harness: str, launch: dict | None = None) -> str:
    """Who served the turn: the API route's provider, else the model's family, else a single-provider
    harness's provider, else "" (unknown)."""
    launched = (launch or {}).get("provider")
    if isinstance(launched, str) and launched:
        return launched
    name = (model or "").strip().lower()
    for pattern, family in _MODEL_PROVIDERS:
        if name and pattern.search(name):
            return family
    return _HARNESS_PROVIDERS.get(harness, "")


def _count(usage: dict, key: str):
    """A token counter: a non-negative integer, else None (missing, null, malformed)."""
    v = usage.get(key) if isinstance(usage, dict) else None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0 or v != int(v):
        return None
    return int(v)


def cache_fields(usage: dict | None) -> tuple[dict, list[str]]:
    """({cache_read, cache_write[, cache_write_1h]} known, [names unknown]) of a turn's usage.

    Codex usage (``cached_input_tokens``) counts its cache hits inside ``input_tokens`` and has no cache
    write: OpenAI bills every input token that is not a hit as plain input, so the part of the input
    written to the cache at a price of its own is 0. Anthropic-style usage (Claude, Kimi, ZCode) counts
    its cache buckets apart; a bucket the turn did not report is unknown, not 0."""
    u = usage if isinstance(usage, dict) else {}
    if "cached_input_tokens" in u:
        cached, inp = _count(u, "cached_input_tokens"), _count(u, "input_tokens")
        if cached is None or inp is None or cached > inp:
            return {}, ["cache_read", "cache_write"]
        return {"cache_read": cached, "cache_write": 0}, []
    out, unknown = {}, []
    read, written = _count(u, "cache_read_input_tokens"), _count(u, "cache_creation_input_tokens")
    if read is None:
        unknown.append("cache_read")
    else:
        out["cache_read"] = read
    if written is None:
        unknown.append("cache_write")
        return out, unknown
    out["cache_write"] = written
    split = u.get("cache_creation")
    if isinstance(split, dict):
        # the same rule as the list price: the split counts only when its lifetimes add up to the total
        five = _count(split, "ephemeral_5m_input_tokens") if "ephemeral_5m_input_tokens" in split else 0
        hour = _count(split, "ephemeral_1h_input_tokens") if "ephemeral_1h_input_tokens" in split else 0
        if five is not None and hour is not None and five + hour == written:
            out["cache_write_1h"] = hour
    return out, unknown


def _time(value) -> dt.datetime | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return dt.datetime.fromtimestamp(value, dt.timezone.utc)
    if isinstance(value, str) and value:
        try:
            t = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return t if t.tzinfo else None
    return None


def _rfc3339(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def turn_fields(turn_id: str, usage: dict | None, model: str, harness: str, launch: dict | None,
                ended) -> dict:
    """The turn fields of one report, each one Mesh accepts; ``unknown`` lists the cache figures the turn
    has none for. ``launch`` is the turn's launch record ({} when it is not this turn's); ``ended`` is the
    turn log's last write (epoch seconds) or None."""
    launch = launch if isinstance(launch, dict) else {}
    out: dict = {}
    if isinstance(turn_id, str) and 0 < len(turn_id.strip()) <= MAX_TURN_ID:
        out["turn_id"] = turn_id.strip()
    started, ended = _time(launch.get("started_at")), _time(ended)
    if started is not None and ended is not None and ended < started:
        if started - ended <= CLOCK_SKEW:
            ended = started
        else:
            started = ended = None              # no duration is better than a negative one
    if started is not None:
        out["started_at"] = _rfc3339(started)
    if ended is not None:
        out["ended_at"] = _rfc3339(ended)
    p = provider(model, harness, launch)
    if p and len(p) <= MAX_PROVIDER:
        out["provider"] = p
    ref = launch.get("account_ref")
    if isinstance(ref, str) and auth.ACCOUNT_REF.fullmatch(ref):
        out["account_ref"] = ref
    if launch.get("billing_mode") in BILLING_MODES:
        out["billing_mode"] = launch["billing_mode"]
    cache, unknown = cache_fields(usage)
    out.update(cache)
    if unknown:
        out["unknown"] = unknown
    return out
