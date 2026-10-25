# Capped event observations

A new turn that ends at its daily turn budget with a structured card, pipeline
or merge-request WAIT retains an observation. While that observation remains
unresolved, a new UTC date or an unrelated comment does not grant another turn.
The actual terminal event still grants one ordinary admission attempt. Static
holds, start-after, routing, daily start caps and admission gates remain effective.
NEXT and failures without an observation retain their existing budget behavior.
This change does not reconstruct historical failed turns.

## Explicitly changed instructions

The configured project lead may publish a comment on the same task using the
existing Mesh add-comment interface, with these metadata (not a text marker):

```json
{"runner_cap_override":{"attempt_id":"EXACT_CAPPED_ATTEMPT_UUID","action":"instructions_changed"}}
```

The comment body must contain the replacement instructions. The attempt UUID is
available in the Runner's read-only local attempts inventory. This is not the
task UUID. Use the project's existing authenticated lead identity and normal
comment permissions; do not impersonate another author. Runner itself does not
publish an override or add a new HTTP endpoint.

Runner checks the comment's server UUID, author ID and timestamp strictly after
the immutable cap-observation mark timestamp and no later than now. Polls that
update a pipeline's seen state do not move this generation anchor. Missing or
malformed observation timestamps fail closed; no old turn is reconstructed. Only the project's
configured lead, freshly resolved in the current workspace directory, is
authorized. Exact agent IDs take precedence; a configured name must resolve to
exactly one workspace agent. Ambiguous names fail closed. The executing agent cannot authorize itself, including when it is
also configured as project lead. A different lead can take over through normal
Mesh reassignment; the old agent then does not execute the task. When the lead
and executor are the same, use existing human assignment rather than a self
override.

Missing/unavailable metadata, unknown authors or leads, wrong attempt IDs,
malformed identities/times and system comments do not override an observation.
Only the recent comment window returned by the existing Mesh client is examined
(default 40, possibly supplemented with the previous page).
The full instruction body must be nonempty and at most 16,384 characters;
oversized instructions are rejected rather than silently truncated. The API
must preserve comment metadata; a compact response that omits it fails closed.
No deployment API check is claimed by the local tests.

The observation retains the exact comment ID, author, attempt and instruction
body. After environment and argument preparation, immediately before process
creation, it records the new attempt ID as the instruction's
consumer. The launch prompt includes the full body and provenance independently
of the ordinary recent-comment summary. Changed instructions do not assert that
the original awaited target completed. A restart does not replay that override
into another attempt. A known failure during environment/argument preparation
does not consume the instruction: ordinary restart cleanup of the unstarted
attempt allows the same instruction to be used. A crash at the process-creation
boundary is ambiguous and can conservatively consume the override without a
confirmed launch: inspect the local attempts and possible writers first, then publish a
new addressed instruction if necessary; do not edit Runner's database.
