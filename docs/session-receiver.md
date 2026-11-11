# Session receiver: the existing-session metadata receiver as its own process

`[session_metadata]` starts its receiver only inside `Runner.run_forever`, once, at startup: an already running controller
cannot take it up without a restart (there is no reload, signal or plug-in path). `mesh-runner session-receiver` runs the
same receiver (`meta_delivery`, wire and security contract unchanged) as a separate opt-in process under its own launchd job.
It is not a controller and not a producer launcher.

## What it never does

Start, resume, adopt or restore a native session, a controller or a tmux server/window; signal or kill anything; register a
runner identity (the registered runner token is only read); fall back to a new session. Its tmux commands are `list-panes`
(read) and the paste into the pinned window (`load-buffer`, `paste-buffer`, `send-keys`). The launchd job supervises only it.

## Fail closed

Nothing is registered and nothing is pasted unless every gate passes at start; the producer is re-checked before each paste:

| Gate | Refusal |
|---|---|
| config absent, not mode 600, `enabled` not true, unknown section/key, missing pin | `config_*`, `not_enabled` |
| tmux window gone / ambiguous / pane dead | `target_missing`, `target_ambiguous`, `pane_dead` |
| tmux server pid or window id differs from the pin | `tmux_identity_changed` |
| pane pid or its process start time differs from the pin | `producer_changed` |
| native transcript not on this machine | `session_unknown` |
| runner credential absent, not 600, runner id differs from the identity | `runner_credential_*`, `runner_id_mismatch` |
| ledger pinned to another identity, or holding records without a pin | `ledger_identity_mismatch`, `ledger_unbound` |
| another consumer holds the lock | exit 4 |

Refusal exit codes: 3 (gate), 4 (another consumer), 5 (`revoke` rejected by Mesh). A paste is journalled before it happens; after a possible paste there is
no automatic repeat, a restart settles it as `uncertain` unless the producer's own ACK file exists.

## One consumer, one ledger

`<ledger_root>` holds `journal/`, `acks/`, `ledger-identity.json` (sha256 of the six identity fields) and `consumer.lock`.
The controller receiver and this process take the same `flock`, so at most one injects for the identity, including parallel
start; a crash frees the lock and keeps the ledger. Point `ledger_root` at the existing `<runner state_dir>/session_metadata`
when a controller ledger exists. A ledger with records and no pin is refused until the operator adopts it once with
`run --bind-existing-ledger`. A new empty ledger is never created next to an existing one to bypass `uncertain`/replay.

## Config (`session-receiver.toml`, mode 600, off by default)

`[session_metadata]` is the existing section (agent, credential_file, allowed_sender_agent_ids, the six identity fields,
interval_seconds) filled from the facts of the actual host. `[session_receiver]`:

```toml
[session_receiver]
enabled = false                    # the opt-in switch
mesh_url = "https://..."
ledger_root = "~/.mesh-runner/session_metadata"
harness = "claude"                 # or "codex"
runner_credential_file = "~/.mesh-runner/chat-runner-<agent>.json"   # the ALREADY registered runner; read only
tmux_session = "..."               # the producer's tmux session and window, as running now
tmux_window = "..."
tmux_window_id = "@N"              # tmux display -p -t <target> '#{window_id}'
tmux_server_pid = 0                # '#{pid}'
producer_pid = 0                   # '#{pane_pid}'
producer_started = "..."           # ps -o lstart= -p <producer_pid>
```

### Transport `iterm` (local iTerm, default off; the default stays `tmux`)

For a native producer that already runs in an iTerm2 session on this Mac. Set `transport = "iterm"`, drop the four `tmux_*`
keys (mixing them is refused) and pin the terminal instead:

```toml
transport = "iterm"
iterm_session_id = "7A3C1F52-..."  # iTerm `unique id` of the exact session (stable for the session's life)
iterm_tty = "/dev/ttys007"         # `tty` of that session
producer_pid = 0                   # the native producer reading that tty
producer_started = "..."           # ps -o lstart= -p <producer_pid>
```

Interface: iTerm2's AppleScript dictionary through `/usr/bin/osascript`, with fixed scripts and every value passed as argv.
It needs the Automation access the receiver's process ALREADY has for iTerm; nothing here requests or installs a grant, and
no grant (-1743), iTerm not running, an unknown or duplicated id all end in zero injection. Not used: SSH/CDP, System
Events keystrokes, frontmost window, create/adopt/restart/relocate, killing anything.

Binding proved before every injection: (1) the id exists exactly once and its tty equals `iterm_tty` (the write script
re-reads the tty in the same call, so a session replaced after the check is not hit); (2) the pinned PID has the pinned
start time, is on that tty and is its foreground process group (`ps pgid == tpgid`); (3) the admitted `hostname` is this
machine (`host_binding` in preflight). The digest of the whole pin plus `host_id`/`runner_id`/`session_id` is stored in the
ledger pin and the admission record: another terminal, tty, producer, host or runner (or a tmux config on this ledger, and
the reverse) is refused for that ledger and its revoke, so it needs a new revision/ledger. A ledger pinned by 0.1.25 means
tmux and stays valid for tmux only.

Limits (not worked around): no cross-process atomicity exists, so the write script re-proves tty, producer start and foreground group
itself before the paste and again right before the Enter (an interrupted write leaves inert, unsubmitted text and is `uncertain`);
text goes in as a bracketed paste
(`ESC[200~ … ESC[201~`, then Enter) like `tmux paste-buffer -p` — a producer without bracketed paste would show the markers;
a paste that is not confirmed is `uncertain` and never replayed. Preflight probes iTerm and, if the Automation grant is
missing, macOS may show its prompt: run the first preflight from the process context that already holds the grant.

The pin values are a baseline captured from the running producer (metadata only, no transcript). They are never guessed
from a hostname or taken from another host.

### Transport `cmux` (local cmux, default off)

For a native producer that already runs in a cmux terminal surface on this Mac. Set `transport = "cmux"`, drop the `tmux_*`
and `iterm_*` keys (mixing them is refused) and pin the surface:

```toml
transport = "cmux"
cmux_workspace_id = "11111111-..."   # cmux workspace UUID of the exact surface
cmux_surface_id = "66666666-..."     # cmux terminal surface UUID
cmux_tty = "/dev/ttys002"            # tty the surface's CURRENT runtime reported
producer_pid = 0                     # the native producer reading that tty
producer_started = "..."             # ps -o lstart= -p <producer_pid>
# cmux_binary = "/Applications/cmux.app/Contents/Resources/bin/cmux"   # optional, the bundled CLI (default)
# cmux_socket = ""                   # optional --socket path; empty = the CLI's default
```

Interface: one method `surface.exact_input` (contract `cmux.exact_input.v1`, modes probe/stage/submit) called through the
bundled `cmux rpc` CLI with the request as one JSON argv element; the caller's `CMUX_*` routing variables are dropped. The
socket access mode stays as it is (`cmuxOnly` admits exactly that CLI); nothing here changes cmux settings or asks for a
grant. **cmux 0.65.2 does not have this method**: it needs a cmux built with `contrib/cmux/0001-surface-exact-input.patch`
and, inside its `ghostty/` submodule, `contrib/cmux/0002-ghostty-text-bracketed-only.patch` (see `contrib/cmux/README.md`).
A cmux without it answers `method_not_found`, which preflight reports as `terminal_binding: cmux_exact_input_unsupported`
and the receiver refuses with zero input. The stock input verbs are never used: they resume a hibernated agent, queue on a
surface without a live runtime, or queue per recipient surface without the native process.

Binding proved before every injection, in one cmux main-actor call and again locally: literal workspace and surface (no
focus, index or caller fallback; the panel and the live registry must own the same surface object); not hibernated (never
resumed); a live runtime (never queued or started); the current runtime reported `cmux_tty`; the pinned PID has the pinned
birth time, that controlling tty and is its foreground group; bracketed paste is on. Typed refusals: `exact_target_missing`,
`exact_target_replacing`, `exact_target_hibernated`, `exact_surface_unavailable`, `exact_tty_unreported`,
`exact_tty_mismatch`, `exact_producer_gone`, `exact_producer_changed`, `exact_producer_not_on_tty`,
`exact_producer_not_foreground`, `exact_paste_unbracketed`, `exact_generation_changed`, `exact_queued_not_received`,
`exact_invalid_params`. The digest of the pin plus `host_id`/`runner_id`/`session_id` is stored in the ledger pin and the
admission record exactly as for `iterm`; `cmux_binary`/`cmux_socket` are client location, not binding.

Delivery: the stage is written ONLY as a bracketed paste (`ESC[200~ … ESC[201~`; refused, not written raw, when the terminal
has bracketed paste off) and the Enter is a separate submit bound to the stage's runtime generation. Between them the
producer is re-proved; any change sends no Enter and the delivery is `uncertain` (inert unsubmitted text at most), never
replayed. `received` needs `delivery: "received"` with `queued: false` and `resumed: false` echoing the exact pin for both
calls; a queue acknowledgement is never `received`. `acknowledged` comes only from the producer's own ack file, as before.
The QA assignment section (`qa_assignment`) stays `iterm`-only.

## Commands (run on the actual host with `PYTHONPATH=<release>/src`; no `-I`, which would ignore it)

```
python3 -s -m mesh_runner.cli session-receiver preflight --config C    # read-only; exit 0 when every gate except the opt-in switch is ok (`enabled` is only reported)
python3 -s -m mesh_runner.cli session-receiver status    --config C    # checks and ledger state counts
python3 -s -m mesh_runner.cli session-receiver plist --config C --pythonpath <release>/src --log-dir L > P
```

Enable: set `enabled = true`, `plutil -lint P`, `cp P ~/Library/LaunchAgents/vc.entire.mesh-session-receiver.plist`,
`launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/vc.entire.mesh-session-receiver.plist`.

Disable and rollback, in this order (touches only the receiver and its own admission; the ledger, the producer's pid/start,
native session and tmux window are not touched):

```
launchctl bootout gui/$(id -u)/vc.entire.mesh-session-receiver          # 1. stop the receiver (releases the consumer lock)
python3 -s -m mesh_runner.cli session-receiver revoke --config C        # 2. revoke only this receiver's admission
rm ~/Library/LaunchAgents/vc.entire.mesh-session-receiver.plist         # 3. remove the plist, set enabled = false in C
```

`revoke` sends the frozen `DELETE /runner/existing-session/receivers/:receiver_id` (204) with the host's own agent key and
its already registered runner credential. Every start registers again, so `<ledger_root>/admission.json` (mode 600: identity
sha256, runner id, and the list of receiver_id + revision this receiver recorded; no secret) holds all of its admissions and
`revoke` removes them newest first; an admission that was not revoked stays in the record for the retry. It registers nothing,
grants nothing and revokes nothing it did not record: no record, a record for another identity or runner, a ledger pinned to
another identity or a runner credential of another runner is `refused` (exit 3) with zero requests; the receiver still running
(lock held) is exit 4. Any server answer other than 204 is exit 5 `revoke_failed`: the record stays, nothing is reported as
disabled, and the same command is safe to repeat. On success the record is renamed to `admission-revoked.json`; the journal,
the ledger pin and the tmux/producer are untouched. Re-enabling registers a fresh admission over the same ledger.
Keep `ledger_root`. Do not run step 3 before step 2 succeeded: without the plist the stop-state is fine, but an unrevoked
admission stays active Mesh-side.

Before enabling: baseline of the actual installed artifact/config/pointer, the host's own approved scoped identity and a
released backend. Install by the catalog route of `docs/release-package.md`; do not install from a checkout.
