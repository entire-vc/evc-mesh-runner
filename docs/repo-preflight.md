# Repository preflight before a code/review start

Before a work/review attempt leases anything, the runner compares the repository named by the
card's typed MR links (`group/project` on the configured GitLab host) with this lane's actual
config. Task prose and arbitrary URLs never name or grant a repository; metadata-only cards and
cards without an MR link are not blocked.

Reasons (`repo_requirements.py`): `undeclared_repo`, `no_membership`, `missing_approved_clone`,
`no_assigned_checkout`, `checkpoint_conflict`, `no_free_slot`. Each durable reason names
`next_actor` and the supported action (reviewed config/admission path, the existing
`admit-workspace-repo` request); access reasons address the access granter with `@<Agent>`
(`docs/repository-access.md`). One handoff comment per distinct fact set (stored fingerprint); a
refused start aborts the attempt with no lease and no model turn. `no_free_slot` stays with the
existing pool hold. The check never edits config, clones, uses another actor key, or touches
foreign leases. Once the approved config carries the repo the same card starts normally.

A waiting attempt needs no `admit-workspace-repo` request for a repository the lane gained after it was
admitted: on its resume, after a checkpoint restore too, the runner leases each missing `lane_repos`
entry through the same journal (intent mark, lease, one commit of workspaces, done mark and notice),
and the turn's prompt says `[runner] Repository <name> was added to this task`. The restore itself
still brings back exactly the journaled set; an ambiguous inventory (a lease outside the attempt's
workspaces without that intent, a journal not in `active`, a snapshot disagreeing with the journal)
adds nothing, and the restore's own refusal stays `InventoryError`. A repository without a free slot
is skipped for that turn; an unsent-prompt retry keeps its exact workspaces.

When Mesh `repository_access` is explicit for the lane, Mesh alone decides membership and the
host inventory supplies the clone: see `docs/repository-access.md`.
