# Bare base and linked worktree configuration

When `extensions.worktreeConfig` is enabled, shared `core.bare=true` also
applies to linked worktrees. Before first enabling the extension, the pool moves
the main-only `core.bare=true` and `core.worktree` settings into the base's
`config.worktree`, following Git's [configuration migration](https://git-scm.com/docs/git-worktree#_configuration_file).
The shared config replacement holds both the repository lock and Git's config
lock. The base remains bare, and pre-existing linked worktrees keep their
meaning. Each owned linked slot gets `core.bare=false` in its own config.
Free slots are configured before their cleanliness check, so a legacy broken
slot can be reused without deleting its contents. For an already-enabled legacy base, the existing pool repair moves shared
`core.bare=true` into the base config before leasing, under the base lock;
per-slot identity/config files are untouched. The owned slot also gets an explicit
`core.bare=false`. A leased slot is handled separately through maintenance below.

## Recovery of an existing leased slot

Do not repair a leased slot through the free-slot path or while its writer is
active. Use the installed runner's `workspace-maintenance` inspect/acquire/finish
protocol, pinning the task, attempt, wait generation, slot and lease returned by
inspect. Wait for the runner's ready maintenance receipt, then revalidate the
receipt, process state and lease before any write. The barrier belongs to the
whole attempt and must remain held during backup and repair.

Back up the full working directory, including untracked files, and the slot's
Git admin directory/config before changing anything. Record the backup location,
file modes and hashes. A broken `git status` is not evidence of a clean tree;
do not reset, clean, recreate or release it. For a legacy base where the
extension is already enabled, the repair is exactly `git config --worktree
core.bare false` in that slot. Do not change shared `core.bare` or global config.
Rollback restores the previous slot config (or removes the newly created config
if absent before repair), under the same validated barrier.

Verify the base is still bare, the linked slot is a worktree, and `ls-files`
and `status` work. Compare the backed-up content, modes and Git index with the
repaired tree. Finish maintenance through the installed runner, using its
receipt and unchanged identity tokens. Let the ordinary runner checkpoint and
resume the existing native session; do not inject a replacement prompt.
Acceptance requires the verified checkpoint/journal, slot lease transitions,
native acceptance and ordinary output for that same attempt. A successful
`git status` alone is insufficient. Install the reviewed catalog release through
the normal activation path before calling the producer fixed in production.

## Checkpoint diagnostics

Failed checkpoint list operations report the Git subcommand, exit code and a
finite cause, including `git-not-a-work-tree`. Unknown stderr maps to
`git-other`; raw stderr, paths and credentials are never included in these
list-operation exceptions. The structured slim failure retains the capture
stage and Git operation rather than replacing the cause with a volume mismatch.
