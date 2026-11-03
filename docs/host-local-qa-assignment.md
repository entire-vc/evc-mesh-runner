# Host-local QA assignment v1 (`host_local_qa_assignment_v1`)

Default OFF. A separate capability of the existing-session receiver (`docs/session-receiver.md`): one fixed brief text goes
into ONE already running native producer in its local iTerm session. It is not a command channel and not install authority:
nothing here executes the text, and a transport or producer ACK is never an install/loaded/matrix/field receipt.

Frozen bytes are PRIVATE and are not part of this package or its public export: `brief.txt` (sha256 5422e262…), `policy.json`
(3d3f06d5…) and `schema.json` (eb3dc20f…) live with the operator. The receiver loads them only from the explicit local
opt-in `bundle_dir` and verifies all three against the pins in `qa_assignment.py` before it registers, reads the QA
credential or touches any state: missing directory/file or one changed byte is `pinned_bytes_missing` / `pinned_bytes_changed`,
never a silent accept. Absent, empty or `enabled = false` never reads the bundle or the QA credential. The public package
carries only `conformance.json`; the suite runs on a synthetic bundle of the same shape.

## Opt-in (session-receiver config, mode 600)

```toml
[host_local_qa_assignment_v1]
enabled = false                    # default; only the boolean true opts in. Unknown keys are refused even when off
bundle_dir = "/abs/path"           # required when enabled: local dir holding the frozen brief.txt/policy.json/schema.json
credential_file = "..."            # its OWN scoped credential; equal to the metadata credential = refused
allowed_sender_agent_ids = ["..."] # must equal policy.json
expires = 0                        # absolute epoch, > now and <= now+8h; no renewal here
interval_seconds = 5
[host_local_qa_assignment_v1.target]    # the six identity fields, EXACTLY the [session_metadata] identity
[host_local_qa_assignment_v1.producer]  # transport="iterm", terminal_session_id, tty, producer_pid, producer_started: EXACTLY the iTerm pin
```

Requires `transport = "iterm"`. Absent or disabled: nothing is registered, no QA route is called, behaviour is unchanged.

## Routes (under `/api/v1`, own scope; Authorization bearer = the QA credential, `X-Runner-Token` = the registered runner)

Receiver: `POST /runner/host-local-qa/v1/receivers` (body exactly `{schema_version, capability, policy_sha256, target,
producer, expires, allowed_sender_agent_ids}`; the answer's receiver_id, admission_revision and authenticated ids are
verified), `DELETE .../receivers/:id` (204 only), `GET .../deliveries?host_id=&session_id=`, `POST .../deliveries/:id/ack`
(binding ids/hashes/identity + `observed_at`; `received` always precedes `acknowledged`). Check:
`POST https://mesh.entire.host/api/v1/runner/host-local-qa/v1/deliveries/:id/check` — fixed origin, no redirect, no
environment proxy, no fallback; body exactly the ten contract fields, no text, no token; the answer is verified field by
field (`active` is `true`, `checked_at` within 120 s of now, nothing extra). Timeout, malformed, mismatching, denied, revoked
or expired = refusal; no success is cached.

## What the receiver does per delivery

Strict schema (extra property at any depth, bool-as-int, wrong const = refused before any effect) → exact target, producer,
receiver and revision → journal replay/409 (request id is immutable across capabilities and across ANY envelope change; the
change is decided BEFORE validation, the original record is untouched, a different delivery of that request is reported
`failed/request_conflict` to the backend, a changed re-send of the same delivery is answered locally only) →
live local binding (host, iTerm id+tty, producer PID+start, foreground group) → **journal intent `injecting`** → `/check`
+ local binding → bracketed paste WITHOUT Enter → `/check` + local binding again → Enter. Refused before the paste:
`failed`, zero bytes. Anything unclear after the first byte: `uncertain`, never replayed or re-keyed. The producer ACK is a
file `acks-qa-host-local-v1/<delivery_id>.json` naming every identity, pin, hash and `observed_at`.

One `ConsumerLock`, one journal/ledger and one injector (same thread) with the metadata receiver; legacy readers skip QA
records and the reverse, so a request id cannot cross capabilities. Admission lives in its own
`admission-qa-host-local-v1.json` under the same ledger root. An old ledger is preserved; unbound history is refused unless
adopted once with `run --bind-existing-ledger`.

## Supported read-only proof of the actual binding

```
session-receiver qa-observe --config C            # one JSON line, or exit 3 `refused: <code>`
qa_assignment.observe_local(ident, pin, runner_credential, known, api, info, argv_of, local_host)   # Python API
sr.qa_observer(cfg) -> () -> observation          # the same, built from the config
```
Observed NOW from the machine, never echoed from config: host name; iTerm session id and tty; producer pid, start, tty and
foreground group; the native session id **named by the producer's own structured argv** (kernel `KERN_PROCARGS2`, argument boundaries kept, environment never read; a rendered command line is not used, a prompt argument that merely contains `--resume <id>` is one token and proves nothing; no such source = unproven) (`--resume <id>`, `--session-id <id>`,
`resume <id>`, `=` forms; a transcript that merely exists is not the relation); the runner id of the registered runner
credential (read only, the token is never returned). Result: `{target, producer, provenance, observed_at}`; `provenance`
says per field `observed` (hostname, session_id, runner_id) or `backend` (workspace_id, agent_id, host_id have no local
source: the authenticated `/check` binds them). Refusals: `host_mismatch`, iTerm/producer codes, `producer_mismatch`,
`native_relation_unproven`, `session_unknown`, `runner_id_mismatch`. It injects nothing, writes no ledger, takes no lock and
never creates, adopts or restarts a producer. The receiver runs the same proof at the injection boundaries (after the journal
intent, before the paste, before the Enter) around each `/check`.

## Guard for the operation owner

```
session-receiver qa-check --config C --delivery-id <id> --boundary <b>
```
Boundaries: `qa_stop qa_start asset_replace_main_js asset_replace_manifest_json asset_replace_styles_css channel_start
controller_lifecycle`. Run it before EACH normal mutation. Exit 0 plus one JSON line (including the observed `target`, `producer` and `provenance`) only if the observation made BEFORE the
live backend `/check` and the one made AFTER it are identical, equal to the delivery's binding, and `/check` says yes; anything else is exit 3 `refused: <code>` and the mutation must not happen. It
is read-only (no lock, no ledger write) and executes nothing. Existing operations do not automatically call this guard. An operation adapter and
any new operation/package pins require separate implementation and review by their
source owner. Actual installed and loaded-state readback remains a source-owned
runtime gate; this receiver cannot attest it.

## Disable / rollback (QA only)

```
session-receiver qa-revoke --config C     # DELETE the recorded QA admission(s), 204 only; exit 5 keeps the record, safe to repeat
```
then set `enabled = false`. `qa-revoke`, `revoke` and `status` load the config even after its `expires` has passed (the disable path never
gets stuck behind a lapsed expiry); `run`, `qa-check` and `qa-observe` refuse an expired section. Producer PID/start, the ledger and its pin, the journal and the legacy admission are not
touched; the metadata receiver keeps running. The running QA receiver notices the local revoke at its next boundary.
