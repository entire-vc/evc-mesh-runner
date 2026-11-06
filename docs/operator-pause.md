# Operator pause

A lane is paused (starts and resumes nothing; waits are still tracked) while ANY of these holds:

1. config `paused = true`;
2. the shared marker `<state_dir>/pause/<name>` exists (people, fleet scripts; unchanged);
3. at least one operation token exists in `<state_dir>/pause.d/<name>/` or the host-wide
   `<state_dir>/pause.d/_all/`.

The same predicate is used by start, resume, peer availability and status.

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
