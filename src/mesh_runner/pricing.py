"""Notional list cost of a turn whose harness reported none.

Headless Claude (``claude -p``) ends a turn with ``total_cost_usd``. Interactive Claude, Codex, Kimi and
ZCode report only token counters, so their spend reached Mesh as ``unknown: ["estimated_cost"]`` and a
card's cost read «—». When the provider gave no price, the runner prices the turn's tokens at the
model's list tariff: the same notional figure the Claude CLI reports itself, never what the fleet pays
under a plan. A model without a tariff stays unknown: an honest «—», never a guess.

Tariffs are $ per million tokens:

- ``input``: input that was neither read from nor written to the cache;
- ``output``: output tokens (reasoning included, as providers bill it);
- ``cache_read``: cache hits;
- ``cache_write_5m`` / ``cache_write_1h``: Anthropic cache writes by lifetime;
- ``cache_write``: a cache write of a provider with one lifetime (or a total whose split is unknown).

``[prices."<model>"]`` in the runner config adds a model or replaces a built-in tariff whole; each entry
names its ``source``. ``[prices] builtin = false`` drops the built-in table.
"""
from __future__ import annotations

import math
import re

RATE_KEYS = ("input", "output", "cache_read", "cache_write", "cache_write_5m", "cache_write_1h")

# Provider list prices, short context, global routing. Each tariff names where it was read.
BUILTIN: dict[str, dict] = {
    # The Claude CLI's own total_cost_usd fits these exactly over 525 headless turns (2026-11-12..18).
    "claude-opus-5-5": {"input": 4.0, "output": 20.0, "cache_read": 0.20, "cache_write_5m": 5.0,
                        "cache_write_1h": 8.0,
                        "source": "https://platform.claude.com/docs/en/about-claude/pricing (2026-11-18)"},
    # The CLI's own total_cost_usd fits these exactly over 95 headless turns (2026-11-12..18).
    "claude-sonnet-5-5": {"input": 2.0, "output": 10.0, "cache_read": 0.10, "cache_write_5m": 2.5,
                          "cache_write_1h": 4.0,
                          "source": "https://platform.claude.com/docs/en/about-claude/pricing (2026-11-18)"},
    # Short-context rates (<= 272K input per request); a longer request is billed higher.
    "gpt-6.1-sol": {"input": 2.0, "output": 10.0, "cache_read": 0.10,
                    "source": "https://developers.openai.com/api/docs/pricing (2026-11-18)"},
    "gpt-6-luna": {"input": 0.10, "output": 0.50, "cache_read": 0.01,
                   "source": "https://developers.openai.com/api/docs/pricing (2026-11-18)"},
    "gpt-6-astra": {"input": 10.0, "output": 50.0, "cache_read": 1.0,
                    "source": "https://developers.openai.com/api/docs/pricing (2026-11-18)"},
}


class PriceError(ValueError):
    pass


def parse_config(section) -> dict[str, dict]:
    """``[prices]`` of the runner config over the built-in table. Absent = the built-in table."""
    if section is None:
        section = {}
    if not isinstance(section, dict):
        raise PriceError("[prices] must be a table")
    builtin = section.get("builtin", True)
    if not isinstance(builtin, bool):
        raise PriceError("[prices] builtin must be true or false")
    table = {k: dict(v) for k, v in BUILTIN.items()} if builtin else {}
    for model, entry in section.items():
        if model == "builtin":
            continue
        if not isinstance(entry, dict):
            raise PriceError(f'[prices."{model}"] must be a table')
        source = entry.get("source")
        if not isinstance(source, str) or not source.strip():
            raise PriceError(f'[prices."{model}"] must name its source')
        unknown = set(entry) - set(RATE_KEYS) - {"source"}
        if unknown:
            raise PriceError(f'[prices."{model}"] unknown keys: {", ".join(sorted(unknown))}')
        rates = {}
        for key in RATE_KEYS:
            if key in entry:
                v = entry[key]
                if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
                    raise PriceError(f'[prices."{model}"] {key} must be a non-negative number ($ per MTok)')
                rates[key] = float(v)
        if "input" not in rates or "output" not in rates:
            raise PriceError(f'[prices."{model}"] needs input and output')
        table[model] = {**rates, "source": source}
    return table


def tariff(model: str, table: dict[str, dict]) -> dict | None:
    """The model's tariff: the exact id, else the id without a ``[...]`` variant suffix (``[1m]``)."""
    if not model:
        return None
    if model in table:
        return table[model]
    base = re.sub(r"\[[^\]]*\]$", "", model).strip()
    return table.get(base) if base != model else None


def _count(usage: dict, key: str, default=None):
    v = usage.get(key, default)
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
        return None
    return v


def estimate(usage: dict | None, model: str, table: dict[str, dict]) -> float | None:
    """The turn's notional list cost in $, or None when the model has no tariff or a counter the price
    needs is missing. Codex usage (``cached_input_tokens``) counts the cache hits inside
    ``input_tokens``; Anthropic-style usage (Claude, Kimi, ZCode) counts its cache buckets apart."""
    rates = tariff(model, table)
    if not rates or not isinstance(usage, dict):
        return None
    inp, out = _count(usage, "input_tokens"), _count(usage, "output_tokens")
    if inp is None or out is None:
        return None
    if "cached_input_tokens" in usage:
        cached = _count(usage, "cached_input_tokens")
        if cached is None or cached > inp:
            return None
        buckets = [(inp - cached, "input"), (cached, "cache_read"), (out, "output")]
    else:
        read = _count(usage, "cache_read_input_tokens", 0)
        written = _count(usage, "cache_creation_input_tokens", 0)
        if read is None or written is None:
            return None
        buckets = [(inp, "input"), (read, "cache_read"), (out, "output")]
        split = usage.get("cache_creation")
        w5 = _count(split, "ephemeral_5m_input_tokens", 0) if isinstance(split, dict) else None
        w1 = _count(split, "ephemeral_1h_input_tokens", 0) if isinstance(split, dict) else None
        if w5 is not None and w1 is not None and w5 + w1 == written:
            buckets += [(w5, "cache_write_5m"), (w1, "cache_write_1h")]
        else:
            buckets.append((written, "cache_write"))
    cost = 0.0
    for tokens, key in buckets:
        if not tokens:
            continue
        rate = rates.get(key)
        if rate is None and key in ("cache_write_5m", "cache_write_1h"):
            rate = rates.get("cache_write")
        if rate is None and key == "cache_write":
            # a total whose lifetime is unknown is priced only when both lifetimes cost the same
            five, hour = rates.get("cache_write_5m"), rates.get("cache_write_1h")
            rate = five if five is not None and five == hour else None
        if rate is None:
            return None
        cost += tokens * rate / 1e6
    return cost
