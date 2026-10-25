# Per-card metadata workspace scope

The first opt-in scope is a work-mode decision request with both exact labels:
`decision-request` and `runner:metadata-only`. Task titles, descriptions, and
comments never establish this scope. A separate work-mode diagnostics opt-in
requires `runner:alert-diagnostics`, `runner:metadata-only`, and at least one of
`alert` / `auto-incident`. The alert labels alone never bypass Git capacity.
Diagnostics with `decision-request` are ambiguous and require normal repository
admission, even with both opt-ins. Decision opt-in plus an alert also remains
normal work. Unknown/missing opt-ins, malformed labels, non-work modes and review
cards keep normal admission. The `runner:workspace-required` label always wins.
A ready metadata attempt whose fresh labels no longer satisfy one unambiguous
scope must obtain normal leases before its next turn.

This scope means no Git worktree allocation; it is not a security sandbox. The
same agent, profile, instructions, tools, and permissions continue to apply.
Mesh task checkout, human gates, dependencies, authentication, quota, memory,
disk floor, and parallel limits continue to apply. Only the Git slot-capacity
hold may be bypassed, for the explicitly opted-in metadata task itself. A full
pool must not hide that task behind an earlier ordinary code task. Alert type,
urgency or diagnostic opt-in grants no checkout, author/reviewer or human-gate
exception.

Alert diagnostics are limited to task metadata and existing operational evidence
(for example, reading service status or an existing diagnostic receipt). The
turn must not modify source, configuration or running services. Before any such
repair, add `runner:workspace-required`, end with `↻ NEXT`, and wait for the next
authorized turn with normal admission and leased checkouts. This is an admission
and instruction guarantee, not filesystem or tool enforcement: the same harness
can still access its existing tools and permissions. Labels do not make arbitrary
shell commands read-only. Use this opt-in only for an assigned agent trusted to
honor the diagnostic scope; if enforcement is required, do not opt in.

Existing attempts with repository leases or checkpoint journals keep their
normal workspace semantics even if a metadata-only label is later added.
An attempt's chosen scope must be persisted with its attempt identity and read
on restart; a mark from another attempt cannot establish metadata-only scope.

If an opted-in metadata task requires code work, add `runner:workspace-required` and
end the turn with `↻ NEXT`. Before the next harness turn, the runner must obtain
normal repository admission and leases. A full pool or disk/auth/gate hold keeps
the same attempt ready without starting a harness. A partially interrupted
transition must resume from durable lease state; it must never reclassify an
existing lease or checkpoint as disposable metadata work. Repository edits in a
shared canonical clone are not an alternative to obtaining the task's leases.
The transition records workspace scope before its first lease and reuses owned
leases after restart, including a crash before the complete paths list was saved.
The profile cwd stays the same, so the native session can resume with the newly
admitted directories and an explicit leased-path instruction; checkpoint restore
continues to use the existing fresh-session rule.

Rollout is per-card, beginning with one typed decision or explicitly opted-in
alert diagnosis that needs only existing evidence. Review never opts in.
Turning off opt-in restores normal admission for subsequent
turns; it does not delete an attempt, discard a checkpoint, release another
task's lease, or bypass a human decision.

For rollback, first stop adding the opt-in labels and add
`runner:workspace-required` to unfinished opted-in metadata tasks while this version
is still running. Let their next authorized turn obtain normal leases, or let
the existing task lifecycle finish them. Do not wake a human-held or waiting
task merely to drain the rollout. Rolling back only the alert extension to the
decision-scope version is compatible with the same attempt-bound receipt: that
version requires normal admission for every alert and durably promotes before
its next turn. A full pool or other gate may keep the attempt parked.

Reverting to a binary without per-card scope support must wait until there are
no unfinished metadata-only attempts without workspaces: that older runner does
not interpret the scope receipt or perform this transition. Keep waiting
attempts parked on a compatible version until that condition holds. No receipt,
lease or checkpoint deletion is part of rollback; queue generations and terminal
acknowledgements remain intact.
