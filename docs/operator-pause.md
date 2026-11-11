# Operator pause

A lane is paused (starts and resumes nothing; waits are still tracked) while ANY of these holds:

1. config `paused = true`;
2. at least one token exists in `<state_dir>/pause.d/<name>/` or the host-wide
   `<state_dir>/pause.d/_all/`.

The same predicate is used by start, resume, peer availability and status.

To pause a lane by hand, create a token (any name of the form below); to resume it, remove that token:

```sh
mkdir -p <state_dir>/pause.d/builder && echo "why" > <state_dir>/pause.d/builder/manual
rm <state_dir>/pause.d/builder/manual
```

## The old marker `<state_dir>/pause/<name>`

Up to 0.2.5 a file `<state_dir>/pause/<name>` paused the lane too. Since 0.2.8 the runner turns each such
regular file into the token `<state_dir>/pause.d/<name>/legacy-pause-file` once at start, before the first
tick: the token is written first (with the file's text), then the file is removed, one log line per file.
The lane shows `<name>/legacy-pause-file` in `status.json` `pause_tokens`; removing that token resumes it.

- A name that is not a configured agent is migrated as well (logged); a symlink or a directory under
  `pause/` is left alone (logged) and does not pause anything.
- A file whose token cannot be written (an unwritable `pause.d`, a name that is not a valid token name) stays,
  is logged, and still pauses its lane for that run; the next start tries again.
- After the migration the runner no longer reads `<state_dir>/pause/`. **A marker written while the runner
  runs does not pause the lane** until the next start migrates it: scripts and people that wrote the marker
  must write a token instead. Read-mostly commands that run beside the service (`plan`, `slots`) migrate
  nothing and still read a marker they find.

## Operation tokens

An operation (drain before a config change, a pool migration) must never create or remove the shared
marker: that name has uncoordinated writers and no syscall makes its removal conditional on who wrote it.
It owns one uniquely named file instead:

```python
from mesh_runner import op_pause
op_pause.take(state_dir, "builder", "drain-<card8>-<pid>")      # or "_all" for every lane
...
op_pause.release(state_dir, "builder", "drain-<card8>-<pid>")   # removes only that file
```

Token names match `[A-Za-z0-9][A-Za-z0-9._-]{0,127}`. `take` is a single `O_CREAT|O_EXCL` of the final
name (no temp file), so a crash leaves nothing or a complete token. A token left by a crashed operation
keeps the lane paused (fail closed) until its owner removes it; an unreadable `pause.d` namespace
also counts as paused. Tokens are listed in `status.json` (`pause_tokens`) and counted in
`mesh_runner_agent_pause_tokens{agent}`; `mesh_runner_agent_paused` is 1 while any is present.
