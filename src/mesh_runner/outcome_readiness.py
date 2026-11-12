"""Read-only evidence for retiring text outcomes and checking the installation.

This reports the counter criteria only; it does not authorize a release.
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
from pathlib import Path
import sqlite3

UTC = dt.timezone.utc
DAY = dt.timedelta(days=1)
SOURCES = {"typed", "comment", "final_text", "none"}


def timestamp(value: str) -> dt.datetime:
    result = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timestamps need an explicit timezone")
    return result.astimezone(UTC)


def report(events: list[tuple], observed_at: dt.datetime,
           installed_at: dt.datetime | None = None) -> dict:
    """Compare half-open UTC windows using settled-turn and retry counters.

    Source counting began later than the events table: old unrelated events do
    not prove that outcome instrumentation covered a window. Keep retry events
    separate, since a turn can emit no_outcome more than once.
    """
    rows = [(timestamp(at), kind, json.loads(detail) if kind == "outcome_source" else {})
            for at, kind, detail in events if timestamp(at) <= observed_at]
    sources = [(at, detail["source"]) for at, kind, detail in rows if kind == "outcome_source"]
    first = min((at for at, _ in sources), default=None)

    def window(start, end):
        counts = collections.Counter(source for at, source in sources if start <= at < end)
        unknown = set(counts) - SOURCES
        if unknown:
            raise ValueError("unknown outcome sources: " + ", ".join(sorted(unknown)))
        return {"start": start.isoformat(), "end": end.isoformat(),
                "elapsed": end <= observed_at,
                "counter_coverage": first is not None and first <= start,
                "sources": {source: counts[source] for source in sorted(SOURCES)},
                "no_outcome_events": sum(start <= at < end and kind == "no_outcome"
                                         for at, kind, _ in rows)}

    week = window(observed_at - 7 * DAY, observed_at)
    week["ready"] = (week["counter_coverage"] and
                     week["sources"]["comment"] == week["sources"]["final_text"] == 0)
    result = {"observed_at": observed_at.isoformat(),
              "first_outcome_source_at": first.isoformat() if first else None,
              "last_day": window(observed_at - DAY, observed_at), "zero_week": week}
    if installed_at is not None:
        if installed_at > observed_at:
            raise ValueError("installation timestamp is in the future")
        before, after = window(installed_at - DAY, installed_at), window(installed_at, installed_at + DAY)
        result["post_install"] = {
            "installed_at": installed_at.isoformat(), "before": before, "after": after,
            "ready": (before["counter_coverage"] and after["elapsed"] and
                      after["sources"]["none"] <= before["sources"]["none"] and
                      after["no_outcome_events"] <= before["no_outcome_events"])}
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True, help="Runner SQLite database (read only)")
    parser.add_argument("--as-of", type=timestamp, help="UTC/offset observation time; defaults to now")
    parser.add_argument("--installed-at", type=timestamp, help="actual installation receipt timestamp")
    parser.add_argument("--check", action="store_true", help="exit 1 until the requested counter criterion passes")
    args = parser.parse_args(argv)
    try:
        # mode=ro neither creates a missing database nor mutates the live one.
        with sqlite3.connect(args.db.expanduser().resolve().as_uri() + "?mode=ro", uri=True) as conn:
            events = conn.execute("SELECT at,kind,detail FROM events "
                                  "WHERE kind IN ('outcome_source','no_outcome')").fetchall()
        data = report(events, args.as_of or dt.datetime.now(UTC), args.installed_at)
    except (ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        parser.exit(2, f"outcome readiness: {exc}\n")
    print(json.dumps(data, indent=2, sort_keys=True))
    criterion = data["post_install"] if args.installed_at else data["zero_week"]
    return int(args.check and not criterion["ready"])


if __name__ == "__main__":
    raise SystemExit(main())
