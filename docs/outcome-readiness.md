# Text outcome retirement evidence

Before removing text outcome parsing, collect seven consecutive days with no
settled turn using the `comment` or `final_text` source. A database containing
older launch events does not prove seven days of outcome counter coverage.
The first `outcome_source` event must predate the window.

Run with Python 3.11 or newer against the actual fleet Runner database:

```sh
python -m mesh_runner.outcome_readiness --db /path/to/runner.sqlite --check
```

The command opens SQLite in read-only mode, prints UTC window boundaries,
the last day's sources, the first counter event, and `zero_week.ready`.
Exit 1 means the counter criterion is not met; exit 2 means evidence could not
be read or validated. Omit `--check` for an informational report. `--as-of`
accepts an explicit timestamp with timezone for reproducible historical reports.

After installation, use the timestamp from the actual installation receipt:

```sh
python -m mesh_runner.outcome_readiness --db /path/to/runner.sqlite \
  --installed-at 2026-12-01T12:00:00Z --check
```

This compares the complete 24 hours immediately before installation with the
complete 24 hours immediately after it. `post_install.ready` requires neither
settled turns with source `none` nor `no_outcome` retry events to increase.
These are separate counters: one turn may emit several retry events. It fails
until the observation day has elapsed or if baseline counter coverage is absent.
Windows include their start and exclude their end; offsets normalize to UTC.

This report covers the counter criteria only. Review, CI, compatible outcome
commands on every consuming instance, and ordinary live behaviour still need
their own evidence. It never changes a task, a runtime, or its database.
