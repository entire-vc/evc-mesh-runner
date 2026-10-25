# WAIT workspace suspension

`runner.workspace_wait_release_minutes` defaults to zero (disabled).
`[[agents]].workspace_wait_release_minutes` overrides it for a scoped canary;
for example global zero and one agent 30 enables only that agent after 30 minutes.
Zero or negative values disable new suspensions. Configuration is read once
when the command starts; this requires a controlled runner restart, not SIGHUP.
Disabling capture does not disable safe restoration of existing checkpoints. Long-standing WAITs return workspace leases, after their entire
writer process group has disappeared. A PID start-time mismatch is not proof
that a group is stopped. Manual/human wait conditions are unchanged.

Which waits give the lease back: `card`, `decision`, `merge` (the runner runs the
merge gate itself from `merge_command_cwd`, never in the task tree), `mr` on the
`merged` contract (only the MR state is watched), and `time` waits whose deadline
is at least the threshold away. `pipeline` waits, `mr` waits that watch CI
(`until=change|success`) and `mr` waits with no readable contract keep the lease.
A released `merge`/`mr` wait keeps polling and runs the gate unchanged; on wake
the workspace is restored from the checkpoint like any other released wait.

The same opt-in threshold applies to a stopped `ready` attempt while it still
owns Git leases. A maintenance pass observes it at most once per
`wake_poll_seconds`, independently of admission: operator/quota pause, memory,
full concurrency, authentication, checkout and standing review holds do not
prevent lease return. Its hold clock starts at the first ready maintenance
observation or refused admission, is bound to the exact attempt and survives
restart. Ownership reads begin only after that threshold. Only a successful
harness start clears that clock; a passing start gate followed by a fresh human
refusal does not restart it. After the threshold, a fresh
ownership check and the existing writer-group, inventory and disk-budget checks
must all succeed before checkpoint/release. The attempt stays `ready`, with its
original WAIT and human/start gates intact; no harness is started by release.
Ordinary admission must later succeed before the existing checkpoint restore
path can resume work. Disabled policy, uncertain ownership, live writers or an
unverifiable checkpoint retain the source leases. The addressed emergency CLI
remains restricted to `waiting` attempts.

Checkpoints live beside the pool in `checkpoints/<owner>/<random-id>` inside a
0700 directory. The manifest, index and bundle are 0600. Snapshot contents are
never printed. All tracked, staged, unstaged, untracked and ignored filesystem
data is included; symlinks are preserved without following them, as are file
and directory permissions. There are no guessed cache exclusions. A stash
commit created without modifying the checkout carries staged-only git blobs
into the bundle. Snapshot refs and checkpoint files remain for recovery until
checkpoint garbage collection (below) removes them.

The manifest records HEAD, configured upstream base SHA, original branch,
workspaces, launch cwd, native session, harness and full WAIT condition. The
filesystem inventory hashes contents, modes and symlink targets. A scratch git
repository must reconstruct matching HEAD, staged diff, unstaged diff, status
and filesystem inventory before a lease may be released. The source is checked
again after this replay. Historical stage-0 gitlink placeholders are supported
only for UTF-8 paths (including newlines), when absent or empty real directories
with no symlink ancestors, no
.gitmodules in HEAD/index/worktree, no effective submodule configuration and no
per-worktree/shared modules metadata. Their external repository OIDs survive
in the parent bundle/index; no external repository data is claimed saved.
Non-UTF8 gitlink paths, staged deletion/replacement of HEAD gitlinks,
populated or configured submodules (including dirty/untracked work), nested
repositories, split indexes,
special files and unverifiable states retain their leases.

Capture reserves enough available disk for the complete filesystem copies and
git object store, plus 512 MiB. Scratch replay and wake replay check their own
budgets too. Available space uses `shutil.disk_usage` (the unprivileged
`f_bavail` value). APFS file clones reduce actual allocations; the disk estimate
stays conservative and also covers portable copy fallback. Low disk fails
closed before release, rather than allocating unbounded copies or increasing
the pool. Incomplete temporary captures are removed; published snapshots are
deleted only by the opt-in garbage collection below.

The durable journal phases are `verified -> releasing -> released -> restoring
-> restored -> active`. Source cleanup occurs only after verified publication.
A restart finishes an interrupted release without touching any newer occupant
of its old slot. Wake obtains fresh leases and verifies every expected
repository before admitting a writer. Failed restoration retains its lease for
retry. Re-entry into `restored` never overwrites subsequent changes. Multi-repo
partial capture cannot omit a repository from the resumed attempt.

Wake starts a fresh native session even if the same slot pathname was reused:
cwd migration cannot be assumed. Its prompt includes previous session id,
workspace paths, HEAD and WAIT target; current leased paths come from the
ordinary first prompt. Once active, ordinary resumes retain that new session.

## Slim snapshots and the size budget

A snapshot stores only what the repository cannot give back:

- `tree/` holds the delta against HEAD: changed and untracked files (manifest `delta.files`),
  the paths deleted from the worktree (`delta.deleted`) and empty untracked directories
  (`delta.dirs`). Restore checks HEAD out, applies the delta, puts every recorded file mode back and
  compares the full fingerprint, so the result is byte-for-byte the captured state.
- git-ignored files and directories (`ignored` in the manifest) and untracked files above 16 MiB are
  left out: logs, caches and build output regenerate. A restored workspace does not have them.
- `commits.bundle` carries `base_sha..HEAD` (plus the staged-index stash), with `base_sha` as its
  prerequisite (`bundle.mode = incremental`), or no bundle at all when HEAD is already in the base
  (`bundle.mode = none`). `base_sha` is pinned by `refs/runner-checkpoint-bases/<id>`; a restore into
  a clone that lacks it fetches that ref from the capture source.
- Capture replays the result into scratch before publishing. If the compact form does not reproduce
  the state (clean/smudge filters, `skip-worktree`, a missing base) the full form is kept and the
  manifest says `slim_failed`. Submodule repositories always use the full form.

Measured on the reference workspace: 135 MB and 48 s before, 1 MB and 4 s after.

Snapshots written by an earlier runner (except those of repositories with submodules, which keep
the full form and are skipped by the migration) are migrated by the gc pass (`compact_checkpoints`, one at a
time, each replay-verified before an atomic swap of the directory; an old one is untouched on any
failure).

`runner.checkpoint_budget_gb` (default 4) is the budget for all snapshots together. The gc pass
measures them, publishes `mesh_runner_checkpoint_bytes` and `status.json` `checkpoints.bytes`, and
logs an overrun; the monitoring alert `MeshRunnerCheckpointsOverBudget` fires above 6 GiB. Nothing is
evicted to meet the budget: a snapshot is the only copy of a waiting card's uncommitted work.

## Checkpoint garbage collection

Without it, checkpoints accumulate: every later WAIT of a resumed attempt
publishes a new generation and keeps the previous one, and an attempt that
ends keeps all of its generations. `runner.checkpoint_gc = true` (default
false) removes, from the same background pass as the ignored-directory
cleanup and within its shared time budget:

- every checkpoint of an attempt that ended (any state other than `starting`,
  `running`, `unknown`, `waiting`, `ready`) more than
  `runner.checkpoint_gc_grace_hours` ago (default 6), with its journal row;
- a checkpoint directory no journal row names (a replaced generation) whose
  modification time is older than the grace period;
- `.pending-` capture directories while no capture runs in the process;
- `refs/runner-checkpoints/<id>` and `refs/runner-checkpoint-bases/<id>` in a
  pooled repository once no `checkpoints/*/<id>` directory exists.

A checkpoint of a live attempt is never removed, nor one whose attempt the
store does not know. Decisions and the rename into `.trash-<id>` happen under
the repository lock that capture and journaling hold, so a capture in flight
is never treated as garbage; removal of `.trash-*` is bounded by the pass
budget and continues on the next pass. Refs inside submodule git directories
are not collected. Preview with
`mesh-runner pool --config runner.toml --prune-checkpoints [--checkpoint-grace-hours H]`;
`--apply` performs it and needs the exclusive runner lock. Two runners must
not share one pool root.

## Recovery and rollback

Stop admission before manual recovery. Do not reuse a `restoring` lease or
remove its checkpoint. Repair disk/access/corruption first, then retry using
this runner version; a corrupt snapshot cannot admit a writer.

**Do not roll back the binary while attempts have suspended checkpoints.** An
older runner does not understand the journal and its remembered paths may now
belong to another attempt. Set release minutes to zero, stop admission, restore
all `verified/releasing/released/restoring/restored` attempts with their fresh
leases, persist their new workspaces and reset native sessions, then verify no
such journal phases remain before an older binary can run. If capacity is
insufficient, retain the new binary and snapshots until a safe drain completes.
No deployment or live configuration changes are part of this change.

## Addressed emergency request (not a release policy)

When every workspace is leased to WAIT attempts, an operator can request one
specific WAIT checkpoint without increasing concurrency or changing the timed
release policy. This command only publishes a private local request. The same
owning Runner consumes at most one request per agent tick, after settle/wake
checks and before ordinary admission. It does not launch a second Runner,
write SQLite from the CLI, kill a process, clear a human/manual hold, acquire a
Mesh checkout, or wake the selected attempt. Installation of this code is a
separate owner deployment; an older Runner will leave the request pending.

1. Use `mesh-runner status --config runner.toml` to capture the full attempt ID,
   exact configured agent, full `task_id`, and
   `wait_generation`. Confirm this is the intended stopped WAIT and that
   checkpoint disk reserve is available. Status now includes the attempt ID
   and a generation digest; it does not print checkpoint contents.
2. Generate a new UUID request ID and retain it. Submit, replacing every
   placeholder with that captured identity:

   ```sh
   mesh-runner suspend-wait-workspace --config runner.toml \
     --agent AGENT --task TASK_UUID --attempt ATTEMPT_UUID \
     --wait-generation GENERATION_SHA256 --request-id REQUEST_UUID --ttl-seconds 300
   ```

3. Repeat exactly the same command to read its receipt. Repeating the ID does
   not extend the original TTL. `suspended` proves lease return through the
   checkpoint journal; it does not prove a repair task has started. Check normal
   Runner status and pool ownership for that separately. A pool of 18 WAIT
   leases can return one lease and admit one eligible repair through the
   ordinary scheduler, still subject to all its admission guards.

The deadline is 1–900 seconds, checked before capture and again after remote
ownership lookup. A fresh agent/task/attempt/state and WAIT generation must
match. Generation binds WAIT start (updated timestamp fallback), turn count and
native session; wake-to-another-WAIT cannot reuse an old request. Writer-group,
checkpoint inventory, source-mutation and disk guards are unchanged. Failed or
partial capture returns `failed_preserved`: some successfully checkpointed
repositories may already be released, while every uncaptured repository stays
leased. The existing journal reconciles this on ordinary wake/restore; do not
manually free those leases. A crash after release but before receipt is
reconciled idempotently from released journal rows, including after expiry.

Requests/receipts are immutable 0600 JSON files in private 0700
`state_dir/workspace-requests/<sha256(agent)>/`; contents contain identities,
deadlines and allowlisted status only. The spool is limited to 32 requests per
agent and 4 KiB per file; it fails closed above its scan bound. Keep terminal
receipts for evidence. Archive a terminal request and its receipt together only
after owner review; never remove pending requests or checkpoint inventory to
make room. Malformed, symlinked, expired or stale requests do not authorize
release. An expired request which never ran needs a new reviewed identity and
new request ID, not a force option.

This is an emergency entry path, not prevention of recurrent 18/18 saturation.
Prevention requires the runtime owner to canary and separately enable the
existing scoped WAIT-release policy described above. A 120-minute threshold
can still leave admission blocked until that threshold is reached. Claiming
recurrence prevention requires owner-enabled observation of the ready backlog,
`pool_full` and absence of active work, followed by separate threshold tuning
(or a separately reviewed pressure-release design) if needed. No automatic
pressure-release mode is added here. This command never enables that policy. Rollback is to stop issuing requests; pending requests expire.
Code rollback/deployment remains owner-controlled, retaining checkpoints and
receipts for existing restore obligations.
