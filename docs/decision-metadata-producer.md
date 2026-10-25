# Decision request metadata producer

The Runner owns this producer: `AgentRunner` passes its configuration to
`Effects.on_wait` / `_file_decision`, which creates the decision card.
Board-services and queue-feeder rule flags do not configure this path.

```toml
[runner]
# Existing fields remain unchanged. Use the intended recipient's verified Mesh
# agent UUID, not its name or the waiting/source agent UUID.
decision_request_metadata_only_recipients = [] # default OFF
```

For a scoped coordinator canary, replace the empty array with only the intended
recipient's freshly verified canonical agent UUID. Entries must be lowercase canonical UUID strings;
malformed configuration is rejected. Scope matches the identity resolved from
`WAIT decision:@handle`, not prose, source labels or a source lane name.
The existing `mesh_effects = false` parent switch still disables decision creation.

Only a new POST receives `runner:metadata-only`, alongside `decision-request`,
`pin-assignee`, and `owner-locked`. No source, existing, legacy referenced or
ambiguous/timeout-created request is relabelled; no alert is opted in. An ambiguous
create retains its durable identity and is settled by the runner: the card is adopted
if a search finds it, or the create is repeated once after the create window with the
same labels. It is never retried blindly. Resolved self/person/unknown recipients still create no decision card.

Install an accepted Runner with metadata admission and this producer before
opting in. Preserve unrelated TOML fields, use a private backup and exclude the
normal config writers. This static setting needs the normal controlled Runner
restart, with the old process gone before its replacement; restarting
board-services alone cannot activate it. Do not revive the legacy queue feeder.

Acceptance must inspect a genuinely new request addressed to the scoped UUID:
four labels, a metadata-only attempt with no Git leases, and normal ownership,
human, auth, quota and disk policy still enforced. A new request to an unlisted
recipient must retain the original three labels. Do not relabel old cards to
manufacture acceptance. Metadata work retains normal tools and permissions; it
is not a sandbox. Code work requires the normal durable workspace promotion path.

Rollback future creation by restoring this array to its previous value (`[]` for
OFF) under writer exclusion and performing the same controlled Runner restart.
Keep a compatible Runner while labelled cards/metadata attempts remain; disabling
future labels is not a safe downgrade of existing durable state. No queue WAIT
flags, generations, receipts, leases or old request labels should be changed.
