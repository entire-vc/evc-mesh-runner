"""How long a lane's turn waits after a provider limit that account routing could not route around.

The on-limit command exits non-zero when it finds no other account or profile: the very next resume would
meet the same exhausted account. The turn then waits for the window's reset when the provider named it
(the typed ``resets_at`` of a headless turn, or the reset the limit notice states), otherwise a pause that
grows with each limit in a row on the same task. Runner-internal: never a task wait or ``start_after``.
"""
from __future__ import annotations

import datetime as dt
import re

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:                  # pragma: no cover - Python < 3.9
    ZoneInfo = None
    ZoneInfoNotFoundError = Exception

# A reset more than this far ahead is not believed: the hold falls back to the growing pause.
MAX_RESET_AHEAD = dt.timedelta(days=8)
# A notice that names only a time of day speaks of the current window, at most this far ahead
# (the shortest provider window is five hours). The next occurrence of a time further away is
# the occurrence already passed: a notice left on screen after its stated time, not a reset.
TIME_ONLY_AHEAD = dt.timedelta(hours=5, minutes=5)
# A notice that names a weekly (or daily) window may state a time of day up to a day ahead:
# "You've hit your weekly limit · resets 12pm" read at 02:00 is ten hours away and not passed.
LONG_WINDOW = re.compile(r"\b(?:weekly|week|daily)\b", re.I)
RESET_SLACK = dt.timedelta(seconds=30)          # the provider's clock and ours: start just after the reset
BACKOFF_SECONDS = 1800                           # first pause when no reset is known; doubles per limit in a row:
BACKOFF_CAP_SECONDS = 7200                       # 30+60+120+120 min > a 5-hour window, so a window costs at most 4 tries

MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
# "resets 12am (Europe/Moscow)", "resets 3:30pm", "continuing automatically at 12am",
# "resets Nov 22, 3am (UTC)", "resets at 14:10 on 15 Nov"
NOTICE_RESET = re.compile(
    r"(?:\bresets?|continuing automatically)\s+(?:at\s+)?"
    r"(?:(?P<mon1>[A-Za-z]{3})[a-z]*\s+(?P<day1>\d{1,2}),?\s+(?:at\s+)?)?"
    r"(?P<h>\d{1,2})(?:(?::(?P<m>\d{2}))(?:\s*(?P<ap>[ap]m))?|\s*(?P<ap2>[ap]m))\b"
    r"(?:\s+on\s+(?P<day2>\d{1,2})\s+(?P<mon2>[A-Za-z]{3})[a-z]*)?"
    r"(?:\s*\((?P<tz>[A-Za-z_]+(?:/[A-Za-z_+-]+)*)\))?", re.I)


def _zone(name: str | None, local: dt.tzinfo) -> dt.tzinfo | None:
    if not name:
        return local
    if name.upper() in ("UTC", "GMT"):
        return dt.timezone.utc
    if ZoneInfo is None:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def notice_reset(text: str, now: dt.datetime) -> tuple[dt.datetime | None, str]:
    """(reset, how) from a limit notice's words. ``now`` is aware; a notice without a zone speaks the
    host's local time (``now``'s zone). how: 'notice', 'notice_passed' (a time of day whose window is
    over: the notice outlived it), 'notice_far', or '' (the text names no reset). A time of day more
    than five hours ahead is passed unless the notice names a weekly or daily window."""
    m = NOTICE_RESET.search(text or "")
    if not m:
        return None, ""
    hour, minute, ap = int(m["h"]), int(m["m"] or 0), (m["ap"] or m["ap2"] or "").lower()
    if ap:
        if not 1 <= hour <= 12:
            return None, ""
        hour = hour % 12 + (12 if ap == "pm" else 0)
    if hour > 23 or minute > 59:
        return None, ""
    zone = _zone(m["tz"], now.tzinfo)
    if zone is None:
        return None, ""
    here = now.astimezone(zone)
    mon, day = (m["mon1"] or m["mon2"] or "").lower()[:3], m["day1"] or m["day2"]
    if mon or day:
        if mon not in MONTHS or not day:
            return None, ""
        try:
            at = here.replace(month=MONTHS[mon], day=int(day), hour=hour, minute=minute, second=0, microsecond=0)
        except ValueError:
            return None, ""
        if at < here - dt.timedelta(days=180):          # "Jan 2" read on 30 Dec is next year
            try:
                at = at.replace(year=at.year + 1)
            except ValueError:
                return None, ""
        if at <= here:
            return None, "notice_passed"
        return (at, "notice") if at - here <= MAX_RESET_AHEAD else (None, "notice_far")
    at = here.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if at <= here:
        at += dt.timedelta(days=1)
    if at - here > TIME_ONLY_AHEAD and not LONG_WINDOW.search(text):
        return None, "notice_passed"
    return at, "notice"


def reset_time(limit: dict | None, text: str, now: dt.datetime) -> tuple[dt.datetime | None, str]:
    """The window's reset: the provider's typed ``resets_at`` (epoch seconds) first, then the notice."""
    resets = (limit or {}).get("resets_at")
    if isinstance(resets, (int, float)) and not isinstance(resets, bool) and resets > 0:
        at = dt.datetime.fromtimestamp(resets, dt.timezone.utc)
        if now < at <= now + MAX_RESET_AHEAD:
            return at, "rate_limit_event"
    return notice_reset(text, now)


def hold_seconds(streak: int, reset: dt.datetime | None, now: dt.datetime) -> int:
    """Seconds the turn waits: until just after a known reset, else the growing pause for the
    ``streak``-th limit in a row (1 = the first)."""
    if reset is not None and reset > now:
        return int((reset - now + RESET_SLACK).total_seconds())
    return int(min(BACKOFF_CAP_SECONDS, BACKOFF_SECONDS * 2 ** max(0, min(streak, 16) - 1)))
